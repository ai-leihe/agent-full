"""splitter 槽位插件：把干净文本切成适合向量化的片段。"""

from __future__ import annotations

import re
import uuid

from ..core.context import CLEAN_TEXT, CHUNKS, Chunk, PipelineContext
from ..core.skill import Skill, skill


def _make_chunk(index: int, text: str, **meta) -> Chunk:
    return Chunk(id=f"c{index:04d}-{uuid.uuid4().hex[:6]}", index=index, text=text.strip(), meta=meta)


def _merge_overlap(pieces: list[str], chunk_size: int, overlap: int) -> list[str]:
    """把碎片合并为带重叠的窗口，尽量不在句中断开。"""
    chunks: list[str] = []
    buffer = ""
    for piece in pieces:
        if not buffer:
            buffer = piece
        elif len(buffer) + len(piece) + 1 <= chunk_size:
            buffer = f"{buffer}\n{piece}"
        else:
            chunks.append(buffer)
            tail = buffer[-overlap:] if overlap > 0 else ""
            candidate = f"{tail}\n{piece}" if tail else piece
            # 加上重叠后若超出窗口上限，则放弃重叠，保证「每个切片 ≤ chunk_size」
            buffer = candidate if len(candidate) <= chunk_size else piece
    if buffer.strip():
        chunks.append(buffer)
    return chunks


@skill
class RecursiveSplitter(Skill):
    """递归字符切片：按「段落 → 换行 → 句子 → 字符」逐级回退切分（默认推荐）。"""

    name = "recursive_splitter"
    slot = "splitter"
    description = "递归字符切片，按语义优先级逐级回退，保留上下文重叠，适合中长文档。"
    consumes = (CLEAN_TEXT,)
    produces = (CHUNKS,)
    param_schema = {
        "chunk_size": {"type": "int", "default": 500, "label": "切片长度", "min": 50, "max": 4000},
        "chunk_overlap": {"type": "int", "default": 80, "label": "重叠长度", "min": 0, "max": 1000},
    }

    def configure(self, options: dict) -> None:
        self.chunk_size = int(options.get("chunk_size", 500))
        self.chunk_overlap = int(options.get("chunk_overlap", 80))
        if self.chunk_overlap >= self.chunk_size:
            raise ValueError("chunk_overlap 必须小于 chunk_size")
        self.separators = options.get("separators", ["\n\n", "\n", "。", "！", "？", ". ", " ", ""])

    def run(self, ctx: PipelineContext) -> None:
        text: str = ctx.require(CLEAN_TEXT)
        pieces = self._split(text, 0)
        windows = _merge_overlap(pieces, self.chunk_size, self.chunk_overlap)
        chunks = [_make_chunk(i, w, strategy=self.name, chars=len(w.strip())) for i, w in enumerate(windows) if w.strip()]
        if not chunks:
            raise ValueError("切片结果为空")
        ctx.put(CHUNKS, chunks, producer=self.name, strategy=self.name, chunk_size=self.chunk_size)

    def _split(self, text: str, level: int) -> list[str]:
        if len(text) <= self.chunk_size or level >= len(self.separators):
            return [text]
        separator = self.separators[level]
        if separator == "":
            return [text[i : i + self.chunk_size] for i in range(0, len(text), self.chunk_size)]
        parts = text.split(separator)
        out: list[str] = []
        for part in parts:
            if len(part) > self.chunk_size:
                out.extend(self._split(part, level + 1))
            elif part.strip():
                out.append(part)
        return out


@skill
class FixedSplitter(Skill):
    """定长切片：按固定字符数硬切，速度最快，适合日志 / 结构化文本。"""

    name = "fixed_splitter"
    slot = "splitter"
    description = "固定长度硬切并保留重叠，实现简单、吞吐最高。"
    consumes = (CLEAN_TEXT,)
    produces = (CHUNKS,)
    param_schema = {
        "chunk_size": {"type": "int", "default": 400, "label": "切片长度", "min": 20, "max": 4000},
        "chunk_overlap": {"type": "int", "default": 40, "label": "重叠长度", "min": 0, "max": 1000},
    }

    def configure(self, options: dict) -> None:
        self.chunk_size = int(options.get("chunk_size", 400))
        self.chunk_overlap = int(options.get("chunk_overlap", 40))
        if self.chunk_overlap >= self.chunk_size:
            raise ValueError("chunk_overlap 必须小于 chunk_size")

    def run(self, ctx: PipelineContext) -> None:
        text: str = ctx.require(CLEAN_TEXT)
        step = self.chunk_size - self.chunk_overlap
        windows = [text[i : i + self.chunk_size] for i in range(0, len(text), step)]
        chunks = [_make_chunk(i, w, strategy=self.name) for i, w in enumerate(windows) if w.strip()]
        if not chunks:
            raise ValueError("切片结果为空")
        ctx.put(CHUNKS, chunks, producer=self.name, strategy=self.name, chunk_size=self.chunk_size)


@skill
class MarkdownHeaderSplitter(Skill):
    """按 Markdown 标题层级切片，并把标题路径写入 chunk.meta。"""

    name = "markdown_splitter"
    slot = "splitter"
    description = "按 Markdown 标题层级切片，自动把标题路径注入每个 chunk 的元数据。"
    consumes = (CLEAN_TEXT,)
    produces = (CHUNKS,)
    param_schema = {
        "max_level": {"type": "int", "default": 3, "label": "最大标题层级", "min": 1, "max": 6},
    }

    _HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")

    def configure(self, options: dict) -> None:
        self.max_level = int(options.get("max_level", 3))

    def run(self, ctx: PipelineContext) -> None:
        text: str = ctx.require(CLEAN_TEXT)
        track: dict[int, str] = {}
        sections: list[tuple[str, str]] = []
        title, buffer = "", []

        for line in text.split("\n"):
            match = self._HEADING_RE.match(line)
            if match and len(match.group(1)) <= self.max_level:
                if "\n".join(buffer).strip():
                    sections.append((title, "\n".join(buffer).strip()))
                level = len(match.group(1))
                track[level] = match.group(2).strip()
                for deeper in [k for k in track if k > level]:
                    del track[deeper]
                title = " / ".join(track[k] for k in sorted(track))
                buffer = []
            else:
                buffer.append(line)
        if "\n".join(buffer).strip():
            sections.append((title, "\n".join(buffer).strip()))

        chunks = [
            _make_chunk(i, body, strategy=self.name, section=title or None, chars=len(body))
            for i, (title, body) in enumerate(sections)
            if body
        ]
        if not chunks:
            raise ValueError("Markdown 切片结果为空")
        ctx.put(CHUNKS, chunks, producer=self.name, strategy=self.name, sections=len(chunks))

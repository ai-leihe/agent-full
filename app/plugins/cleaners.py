"""cleaner 槽位插件：把原始文本清洗为可用于切片的干净文本。

这里是「插拔式清洗」的主战场——不同文种、不同来源需要不同的清洗策略，
把它们做成同槽位的多个实现，按文件类型自由切换。
"""

from __future__ import annotations

import re
import unicodedata

from ..core.context import PipelineContext, CLEAN_TEXT, TEXT
from ..core.skill import Skill, skill

_URL_RE = re.compile(r"https?://\S+|www\.\S+")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_MULTI_SPACE_RE = re.compile(r"[ \t]{2,}")
_MULTI_NEWLINE_RE = re.compile(r"\n{3,}")


@skill
class BasicCleaner(Skill):
    """通用清洗：去控制符、压缩空白、可选去重 / 去链接 / 转小写。"""

    name = "basic_cleaner"
    slot = "cleaner"
    description = "通用文本清洗：控制符清理、空白压缩、空行合并、可选去重与脱敏。"
    consumes = (TEXT,)
    produces = (CLEAN_TEXT,)
    param_schema = {
        "lowercase": {"type": "bool", "default": False, "label": "转小写"},
        "strip_urls": {"type": "bool", "default": False, "label": "移除链接"},
        "mask_email": {"type": "bool", "default": False, "label": "邮箱脱敏"},
        "dedupe_lines": {"type": "bool", "default": True, "label": "重复行去重"},
        "min_line_length": {"type": "int", "default": 0, "label": "最短保留行", "min": 0, "max": 200},
        "header_footer_lines": {"type": "int", "default": 0, "label": "去页眉页脚行数", "min": 0, "max": 50},
        "unicode_form": {
            "type": "str",
            "default": "NFC",
            "label": "Unicode 归一化",
            "help": "NFC 保留中文全角标点；NFKC 统一全半角",
            "choices": ["NFC", "NFKC", ""],
        },
    }

    def configure(self, options: dict) -> None:
        self.lowercase = bool(options.get("lowercase", False))
        self.strip_urls = bool(options.get("strip_urls", False))
        self.mask_email = bool(options.get("mask_email", False))
        self.dedupe_lines = bool(options.get("dedupe_lines", True))
        self.min_line_length = int(options.get("min_line_length", 0))
        self.header_footer_lines = int(options.get("header_footer_lines", 0))
        # NFC 保留中文全角标点；如需统一全半角可改为 "NFKC"
        self.unicode_form = options.get("unicode_form", "NFC")

    def _drop_header_footer(self, lines: list[str]) -> list[str]:
        n = self.header_footer_lines
        if n <= 0 or len(lines) <= n * 2:
            return lines
        return lines[n:-n]

    def run(self, ctx: PipelineContext) -> None:
        text: str = ctx.require(TEXT)
        before = len(text)

        if self.unicode_form:
            text = unicodedata.normalize(self.unicode_form, text)
        text = _CONTROL_RE.sub("", text)
        text = text.replace("\r\n", "\n").replace("\r", "\n")

        lines = text.split("\n")
        lines = self._drop_header_footer(lines)

        if self.strip_urls:
            lines = [_URL_RE.sub(" ", line) for line in lines]
        if self.mask_email:
            lines = [_EMAIL_RE.sub("[EMAIL]", line) for line in lines]

        cleaned: list[str] = []
        seen: set[str] = set()
        for line in lines:
            line = _MULTI_SPACE_RE.sub(" ", line).strip()
            if len(line) < self.min_line_length:
                continue
            if self.dedupe_lines:
                key = line.lower()
                if key in seen:
                    continue
                seen.add(key)
            cleaned.append(line)

        text = "\n".join(cleaned)
        text = _MULTI_NEWLINE_RE.sub("\n\n", text).strip()
        if self.lowercase:
            text = text.lower()
        if not text:
            raise ValueError("清洗后文本为空，请放宽清洗参数")

        ctx.put(
            CLEAN_TEXT,
            text,
            producer=self.name,
            removed_chars=before - len(text),
            removed_lines=len(lines) - len(cleaned),
        )


@skill
class MarkdownNormalizer(Skill):
    """Markdown 专用清洗：去 front-matter、还原链接、剔除标记符号。"""

    name = "markdown_normalizer"
    slot = "cleaner"
    description = "面向 Markdown 的清洗：剥离 front-matter/代码块标记，链接保留文字，可选保留标题层级。"
    consumes = (TEXT,)
    produces = (CLEAN_TEXT,)
    param_schema = {
        "keep_headings": {"type": "bool", "default": True, "label": "保留标题层级"},
        "keep_bullets": {"type": "bool", "default": True, "label": "保留列表符号"},
        "drop_images": {"type": "bool", "default": True, "label": "丢弃图片"},
    }

    _FRONT_MATTER_RE = re.compile(r"\A---\n.*?\n---\n", re.DOTALL)
    _CODE_FENCE_RE = re.compile(r"^\s*```.*$", re.MULTILINE)
    _IMAGE_RE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
    _LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")
    _HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s*", re.MULTILINE)
    _EMPHASIS_RE = re.compile(r"(\*\*|__|\*|_|`)")
    _BULLET_RE = re.compile(r"^\s{0,3}[-*+]\s+", re.MULTILINE)

    def configure(self, options: dict) -> None:
        self.keep_bullets = bool(options.get("keep_bullets", True))
        self.drop_images = bool(options.get("drop_images", True))
        # 保留 # 层级后，下游 markdown_splitter 仍可按标题切片
        self.keep_headings = bool(options.get("keep_headings", True))

    def run(self, ctx: PipelineContext) -> None:
        text: str = ctx.require(TEXT)
        before = len(text)

        text = self._FRONT_MATTER_RE.sub("", text)
        text = self._CODE_FENCE_RE.sub("", text)
        text = self._IMAGE_RE.sub(r"\1" if not self.drop_images else "", text)
        text = self._LINK_RE.sub(r"\1", text)
        if not self.keep_headings:
            text = self._HEADING_RE.sub("", text)
        text = self._EMPHASIS_RE.sub("", text)
        if not self.keep_bullets:
            text = self._BULLET_RE.sub("", text)
        text = _MULTI_NEWLINE_RE.sub("\n\n", text).strip()

        if not text:
            raise ValueError("Markdown 清洗后为空")
        ctx.put(CLEAN_TEXT, text, producer=self.name, removed_chars=before - len(text))

"""运行上下文：贯穿整条 Agent 流水线的共享数据总线。

设计要点
--------
流水线中的每个 Skill 都只与 ``PipelineContext`` 交互，不直接依赖上下游实现。
上游通过 ``put(kind, value)`` 交付产物，下游通过 ``require(kind)`` 声明依赖，
从而实现「任务联动」——插件的替换不会影响链路中的其它环节。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable

# ---------------------------------------------------------------- 产物类型常量

RAW_FILE = "raw_file"          # 上传的原始二进制文件
TEXT = "text"                  # 解码后的原始文本
CLEAN_TEXT = "clean_text"      # 清洗后的文本
CHUNKS = "chunks"              # 切片结果 list[Chunk]
EMBEDDINGS = "embeddings"      # 向量结果 list[list[float]]（与 chunks 一一对应）
VECTOR_INDEX = "vector_index"  # 可检索的向量索引对象


class ContractError(RuntimeError):
    """流水线契约被破坏（上下游产物不匹配）。"""


class MissingArtifactError(ContractError):
    """当前环节缺少必需的上游产物。"""


# ---------------------------------------------------------------- 数据结构


@dataclass
class Chunk:
    """一个文本切片。"""

    id: str
    index: int
    text: str
    meta: dict[str, Any] = field(default_factory=dict)
    embedding: list[float] | None = None

    def preview(self, limit: int = 200) -> str:
        text = self.text.replace("\n", " ")
        return text if len(text) <= limit else text[:limit] + "…"

    def to_dict(self, with_embedding: bool = False) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "index": self.index,
            "text": self.text,
            "meta": self.meta,
            "chars": len(self.text),
        }
        if with_embedding and self.embedding is not None:
            data["embedding_dim"] = len(self.embedding)
        return data


@dataclass
class Artifact:
    """数据总线上的一个产物槽。"""

    kind: str
    value: Any
    producer: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class TraceRecord:
    """单步执行轨迹，供前端展示与问题排查。"""

    agent: str
    skill: str
    status: str  # ok | error
    duration_ms: float
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "skill": self.skill,
            "status": self.status,
            "duration_ms": round(self.duration_ms, 2),
            "detail": self.detail,
        }


# ---------------------------------------------------------------- 上下文


class PipelineContext:
    """一次流水线运行的全部状态。"""

    def __init__(
        self,
        filename: str,
        data: bytes,
        content_type: str = "",
        run_id: str | None = None,
    ) -> None:
        self.run_id = run_id or uuid.uuid4().hex[:12]
        self.filename = filename
        self.content_type = content_type
        self.data = data
        self.started_at = time.time()
        self.artifacts: dict[str, Artifact] = {}
        self.trace: list[TraceRecord] = []
        self.errors: list[str] = []
        # 上传即写入第一个产物，供 loader 槽位的插件消费
        self.put(RAW_FILE, data, producer="upload", filename=filename, content_type=content_type, size=len(data))

    # -------------------------------------------------- 产物读写

    def put(self, kind: str, value: Any, producer: str = "", **meta: Any) -> Artifact:
        artifact = Artifact(kind=kind, value=value, producer=producer, meta=meta)
        self.artifacts[kind] = artifact
        return artifact

    def get(self, kind: str, default: Any = None) -> Any:
        artifact = self.artifacts.get(kind)
        return artifact.value if artifact is not None else default

    def has(self, kind: str) -> bool:
        return kind in self.artifacts

    def require(self, kind: str) -> Any:
        if kind not in self.artifacts:
            raise MissingArtifactError(
                f"缺少上游产物 '{kind}'，当前环节无法执行。"
                f"（已有产物：{sorted(self.artifacts) or '无'}）"
            )
        return self.artifacts[kind].value

    def artifact_meta(self, kind: str) -> dict[str, Any]:
        artifact = self.artifacts.get(kind)
        return artifact.meta if artifact else {}

    # -------------------------------------------------- 轨迹

    def record(self, agent: str, skill: str, status: str, duration_ms: float, detail: str = "") -> None:
        self.trace.append(TraceRecord(agent, skill, status, duration_ms, detail))

    # -------------------------------------------------- 序列化

    def artifact_summary(self) -> list[dict[str, Any]]:
        """产物的轻量描述（不含大对象本体），用于接口返回。"""
        summary = []
        for kind, artifact in self.artifacts.items():
            if kind == RAW_FILE:
                size = len(artifact.value)
                label = f"{size} bytes"
            elif isinstance(artifact.value, list):
                label = f"{len(artifact.value)} items"
            elif isinstance(artifact.value, str):
                label = f"{len(artifact.value)} chars"
            else:
                label = type(artifact.value).__name__
            summary.append(
                {
                    "kind": kind,
                    "producer": artifact.producer,
                    "size": label,
                    "meta": {k: v for k, v in artifact.meta.items() if k != "content_type"},
                }
            )
        return summary

    def to_dict(self, *, max_chunks: int = 50) -> dict[str, Any]:
        chunks: Iterable[Chunk] = self.get(CHUNKS) or []
        chunk_payload = [c.to_dict() for c in list(chunks)[:max_chunks]]
        return {
            "run_id": self.run_id,
            "filename": self.filename,
            "content_type": self.content_type,
            "elapsed_ms": round((time.time() - self.started_at) * 1000, 2),
            "artifacts": self.artifact_summary(),
            "trace": [t.to_dict() for t in self.trace],
            "errors": self.errors,
            "stats": {
                "chars": len(self.get(TEXT) or ""),
                "clean_chars": len(self.get(CLEAN_TEXT) or ""),
                "chunk_count": len(list(chunks)),
                "embedding_dim": len((self.get(EMBEDDINGS) or [[]])[0]) if self.get(EMBEDDINGS) else 0,
            },
            "chunks": chunk_payload,
        }

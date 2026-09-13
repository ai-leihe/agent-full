"""embedder 槽位插件：把文本切片转为稠密向量。

所有 embedder 实现统一的 ``encode(texts) -> list[list[float]]`` 接口，
因此下游向量库插件无需感知具体用的是哪种向量模型。
"""

from __future__ import annotations

import hashlib
import math
import re
from abc import abstractmethod
from typing import Any

from ..core.context import CHUNKS, EMBEDDINGS, PipelineContext
from ..core.skill import Skill, skill

_ASCII_WORD_RE = re.compile(r"[a-z0-9_]+")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def _tokenize(text: str) -> list[str]:
    """轻量分词：英文词 + 中文单字 + 中文双字组，无需额外依赖。"""
    lowered = text.lower()
    tokens = _ASCII_WORD_RE.findall(lowered)
    cjk = _CJK_RE.findall(lowered)
    tokens.extend(cjk)
    tokens.extend("".join(pair) for pair in zip(cjk, cjk[1:]))
    return tokens


class EmbedderSkill(Skill):
    """向量化插件基类，统一 run() 流程，子类只实现 encode()。"""

    slot = "embedder"
    consumes = (CHUNKS,)
    produces = (EMBEDDINGS,)

    @abstractmethod
    def encode(self, texts: list[str]) -> list[list[float]]:
        """把一批文本编码为向量。"""

    def encode_one(self, text: str) -> list[float]:
        return self.encode([text])[0]

    def run(self, ctx: PipelineContext) -> None:
        chunks = ctx.require(CHUNKS)
        vectors = self.encode([chunk.text for chunk in chunks])
        if len(vectors) != len(chunks):
            raise ValueError(f"{self.name} 返回的向量数({len(vectors)})与切片数({len(chunks)})不一致")
        dimension = len(vectors[0]) if vectors else 0
        ctx.put(EMBEDDINGS, vectors, producer=self.name, embedder=self.name, dimension=dimension)


@skill
class HashEmbedder(EmbedderSkill):
    """零依赖的哈希向量化（Hashing Trick），无需模型与网络，开箱即用。

    适合做平台默认实现与离线演示；生产环境建议替换为 openai_embedder。
    """

    name = "hash_embedder"
    description = "基于 Hashing Trick 的确定性向量化，零依赖、零网络开销，用于默认/离线场景。"
    param_schema = {
        "dim": {"type": "int", "default": 256, "label": "向量维度", "min": 32, "max": 4096},
        "seed": {"type": "int", "default": 42, "label": "哈希种子", "min": 0, "max": 99999},
    }

    def configure(self, options: dict) -> None:
        self.dim = int(options.get("dim", 256))
        self.seed = int(options.get("seed", 42))
        if self.dim <= 0:
            raise ValueError("dim 必须为正整数")

    def _bucket(self, token: str) -> tuple[int, float]:
        digest = hashlib.blake2b(f"{self.seed}:{token}".encode("utf-8"), digest_size=8).digest()
        value = int.from_bytes(digest, "big")
        return value % self.dim, 1.0 if (value >> 63) & 1 else -1.0

    def encode(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            vector = [0.0] * self.dim
            tokens = _tokenize(text)
            if not tokens:
                vectors.append(vector)
                continue
            for token in tokens:
                index, sign = self._bucket(token)
                vector[index] += sign
            norm = math.sqrt(sum(v * v for v in vector)) or 1.0
            vectors.append([v / norm for v in vector])
        return vectors


@skill
class OpenAIEmbedder(EmbedderSkill):
    """OpenAI 向量模型（可选插件，依赖 openai 与 OPENAI_API_KEY）。"""

    name = "openai_embedder"
    optional = True
    description = "调用 OpenAI Embeddings API，需安装 openai 并配置 OPENAI_API_KEY。"
    param_schema = {
        "model": {
            "type": "str",
            "default": "text-embedding-3-small",
            "label": "向量模型",
            "choices": ["text-embedding-3-small", "text-embedding-3-large", "text-embedding-ada-002"],
        },
        "batch_size": {"type": "int", "default": 64, "label": "批大小", "min": 1, "max": 512},
        "base_url": {"type": "str", "default": "", "label": "API Base URL", "help": "留空用官方地址，可填第三方网关"},
    }

    def configure(self, options: dict) -> None:
        self.model = options.get("model", "text-embedding-3-small")
        self.batch_size = int(options.get("batch_size", 64))
        self.api_key = options.get("api_key")
        self.base_url = options.get("base_url")

    def _client(self):
        try:
            from openai import OpenAI  # type: ignore
        except ImportError:  # pragma: no cover
            raise RuntimeError("openai_embedder 需要额外依赖，请执行：pip install openai") from None
        kwargs: dict[str, Any] = {}
        if self.api_key:
            kwargs["api_key"] = self.api_key
        if self.base_url:
            kwargs["base_url"] = self.base_url
        return OpenAI(**kwargs)

    def encode(self, texts: list[str]) -> list[list[float]]:
        client = self._client()
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self.batch_size):
            batch = texts[start : start + self.batch_size]
            response = client.embeddings.create(model=self.model, input=batch)
            vectors.extend(item.embedding for item in response.data)
        return vectors

"""vector_store 槽位插件：把向量落库并对外提供相似度检索。

索引实现（:class:`~app.core.warehouse.MemoryIndex`）住在 ``app.core.warehouse``，
原因见那里的注释：仓库需要在进程重启后按数据库里的切片与向量把它重建出来，
放在插件里会形成「插件 ↔ 仓库」的循环依赖。这里只做转发导出，保持既有导入路径可用。
"""

from __future__ import annotations

from typing import Any

from ..core.context import CHUNKS, EMBEDDINGS, VECTOR_INDEX, Chunk, PipelineContext
from ..core.skill import Skill, skill
from ..core.warehouse import MemoryIndex, warehouse

__all__ = ["MemoryIndex", "MemoryVectorStore"]


@skill
class MemoryVectorStore(Skill):
    """内存向量库：进程内保存索引，并把索引注册到 warehouse 供检索接口使用。

    索引同时会被写入数据库（元数据 + 切片 + 向量），因此进程重启后
    「知识库」列表不会清空，检索也能继续用。
    """

    name = "memory_store"
    slot = "vector_store"
    description = "进程内向量索引，零依赖，支持余弦相似度检索。"
    consumes = (CHUNKS, EMBEDDINGS)
    produces = (VECTOR_INDEX,)
    param_schema = {
        "collection": {"type": "str", "default": "default", "label": "集合名称"},
    }

    def configure(self, options: dict) -> None:
        self.collection = options.get("collection", "default")

    def run(self, ctx: PipelineContext) -> None:
        chunks: list[Chunk] = ctx.require(CHUNKS)
        vectors: list[list[float]] = ctx.require(EMBEDDINGS)
        if len(chunks) != len(vectors):
            raise ValueError("切片数与向量数不一致，无法建索引")

        embedder = ctx.artifact_meta(EMBEDDINGS).get("embedder", "")
        index = MemoryIndex(chunks, vectors, embedder)
        ctx.put(
            VECTOR_INDEX,
            index,
            producer=self.name,
            collection=self.collection,
            size=index.size,
            dimension=index.dimension,
            embedder=embedder,
        )
        warehouse.put(ctx.run_id, ctx.filename, index, collection=self.collection)

"""向量仓库：保存每次运行的索引，供检索接口复用。

存储策略
--------
早期实现把索引整份放在进程内存里，进程一重启「知识库」列表就空了（这正是
「创建时间为空」那条问题的根因之一）。现在改成：

* **元数据落库**：``knowledge_indexes`` 表记录 run_id / 文件名 / 集合 / embedder /
  维度 / 切片数 / 后端 / 归属人 / 创建时间，列表与统计一律读库，重启后依然在；
* **切片与向量落库**：纯内存索引（``memory_store``）额外把切片文本与向量写进
  ``knowledge_chunks``，重启后可以完整重建索引，检索继续可用；
* **外部后端可重建**：Milvus 这类把数据放在外部的索引，只需通过
  :meth:`Warehouse.register_rebuilder` 注册一个「按元数据重建句柄」的工厂，
  重启后同样能继续检索，向量本身不会重复存储。

没有绑定数据库时（例如单元测试直接 ``Warehouse()``）自动退化为纯内存容器。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from .context import Chunk
from .db import Database, from_json, to_json


class SearchableIndex(Protocol):
    """索引对象需要满足的最小协议。

    可选钩子：索引若把数据落在外部后端（如 Milvus / Qdrant），实现 ``drop()``
    即可在 :meth:`Warehouse.delete` 时同步清理后端数据；纯内存索引无需实现。
    """

    embedder: str
    dimension: int

    def search(self, query_vector: list[float], top_k: int = 5) -> list[dict[str, Any]]: ...


def _safe_json(value: Any, fallback: Any) -> str:
    """meta 里混进不可序列化对象时降级，别让一次落库失败拖垮整条流水线。"""
    try:
        return to_json(value)
    except (TypeError, ValueError):
        return to_json(fallback)


class MemoryIndex:
    """纯 Python 实现的向量索引，满足 SearchableIndex 协议。

    放在本模块而不是插件里，是为了让 :class:`Warehouse` 能在重启后直接用
    数据库里的切片与向量把它重建出来（插件与仓库互为依赖会绕成环）。
    """

    backend = "memory"

    def __init__(self, chunks: list[Chunk], vectors: list[list[float]], embedder: str) -> None:
        self.chunks = chunks
        self.vectors = vectors
        self.embedder = embedder
        self.dimension = len(vectors[0]) if vectors else 0

    @property
    def size(self) -> int:
        return len(self.chunks)

    @classmethod
    def from_rows(cls, rows: list[dict[str, Any]], *, embedder: str = "") -> "MemoryIndex | None":
        """按 ``knowledge_chunks`` 的行重建索引；没有可用向量时返回 ``None``。"""
        chunks: list[Chunk] = []
        vectors: list[list[float]] = []
        for row in rows:
            vector = [float(value) for value in (from_json(row.get("embedding"), []) or [])]
            if not vector:
                continue
            chunks.append(
                Chunk(
                    id=str(row.get("chunk_id") or ""),
                    index=int(row.get("chunk_index") or 0),
                    text=row.get("text") or "",
                    meta=from_json(row.get("meta"), {}) or {},
                )
            )
            vectors.append(vector)
        if not chunks:
            return None
        return cls(chunks, vectors, embedder)

    @staticmethod
    def _cosine(a: list[float], b: list[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        na = sum(x * x for x in a) ** 0.5
        nb = sum(y * y for y in b) ** 0.5
        if na == 0 or nb == 0:
            return 0.0
        return dot / (na * nb)

    def search(self, query_vector: list[float], top_k: int = 5) -> list[dict[str, Any]]:
        scored = [
            (self._cosine(query_vector, vector), chunk)
            for chunk, vector in zip(self.chunks, self.vectors)
        ]
        scored.sort(key=lambda item: item[0], reverse=True)
        return [
            {
                "score": round(score, 4),
                "chunk_id": chunk.id,
                "index": chunk.index,
                "text": chunk.text,
                "meta": chunk.meta,
            }
            for score, chunk in scored[:top_k]
        ]


@dataclass
class WarehouseRecord:
    run_id: str
    filename: str
    index: Any
    meta: dict[str, Any] = field(default_factory=dict)


class Warehouse:
    """索引仓库：内存缓存 + 数据库持久化（可替换为 Redis / 分布式实现）。"""

    def __init__(self, db: Database | None = None) -> None:
        self._records: dict[str, WarehouseRecord] = {}
        self._lock = threading.Lock()
        self._db = db
        self._rebuilders: dict[str, Callable[[dict[str, Any]], Any]] = {}

    # -------------------------------------------------- 装配

    def attach(self, db: Database | None) -> None:
        """切换数据源；切换时清掉内存缓存，避免把上一个库的数据带过来。"""
        self._db = db
        with self._lock:
            self._records.clear()

    @property
    def db(self) -> Database | None:
        return self._db

    def register_rebuilder(self, backend: str, factory: Callable[[dict[str, Any]], Any]) -> None:
        """为外部后端登记「按元数据重建索引句柄」的工厂（如 milvus）。"""
        self._rebuilders[str(backend)] = factory

    # -------------------------------------------------- 写入

    def put(self, run_id: str, filename: str, index: Any, **meta: Any) -> None:
        # 记录创建时间（秒级时间戳），供知识库列表展示；已传入则不覆盖。
        meta.setdefault("created_at", time.time())
        record = WarehouseRecord(run_id, filename, index, meta)
        with self._lock:
            self._records[run_id] = record
        self._persist(record)

    def annotate(self, run_id: str, **meta: Any) -> bool:
        """补充元数据（如归属人）；记录不存在时返回 ``False``。"""
        with self._lock:
            record = self._records.get(run_id)
        if record is None:
            return False
        record.meta.update(meta)
        self._persist(record)
        return True

    def _persist(self, record: WarehouseRecord) -> None:
        db = self._db
        if db is None:
            return
        index = record.index
        meta = dict(record.meta)
        created_at = float(meta.get("created_at") or time.time())
        stored_meta = {k: v for k, v in meta.items() if k != "created_at"}
        backend = str(stored_meta.get("backend") or getattr(index, "backend", "") or "memory")
        collection = str(stored_meta.get("collection") or getattr(index, "collection", "") or "")
        owner_id = str(stored_meta.get("owner_id") or "")
        chunks = list(getattr(index, "chunks", None) or [])
        vectors = list(getattr(index, "vectors", None) or [])

        row = {
            "filename": record.filename,
            "collection": collection[:128],
            "embedder": str(getattr(index, "embedder", "") or "")[:128],
            "dimension": int(getattr(index, "dimension", 0) or 0),
            "vec_size": int(getattr(index, "size", 0) or 0),
            "backend": backend[:32],
            "owner_id": owner_id[:64],
            "meta": _safe_json(stored_meta, {}),
            "created_at": created_at,
        }
        with db.transaction() as tx:
            tx.upsert("knowledge_indexes", {"run_id": record.run_id}, row)
            if chunks:
                # 只有自带切片与向量的索引才需要复制一份，外部后端不重复存
                tx.delete("knowledge_chunks", {"run_id": record.run_id})
                for position, chunk in enumerate(chunks):
                    vector = vectors[position] if position < len(vectors) else []
                    tx.insert(
                        "knowledge_chunks",
                        {
                            "run_id": record.run_id,
                            "position": position,
                            "chunk_id": str(getattr(chunk, "id", "") or "")[:64],
                            "chunk_index": int(getattr(chunk, "index", position) or 0),
                            "text": getattr(chunk, "text", "") or "",
                            "meta": _safe_json(getattr(chunk, "meta", {}) or {}, {}),
                            "embedding": _safe_json([float(v) for v in vector], []),
                        },
                    )

    # -------------------------------------------------- 读取

    def get(self, run_id: str) -> WarehouseRecord | None:
        with self._lock:
            record = self._records.get(run_id)
        if record is not None:
            return record
        return self._rebuild(run_id)

    def _rebuild(self, run_id: str) -> WarehouseRecord | None:
        """进程重启后按数据库里的内容重建索引对象（懒加载，只做一次）。"""
        db = self._db
        if db is None:
            return None
        row = db.query_one("SELECT * FROM knowledge_indexes WHERE run_id = ?", [run_id])
        if row is None:
            return None

        index: Any = None
        chunks = db.query(
            "SELECT * FROM knowledge_chunks WHERE run_id = ? ORDER BY position", [run_id]
        )
        if chunks:
            index = MemoryIndex.from_rows(chunks, embedder=str(row.get("embedder") or ""))
        if index is None:
            factory = self._rebuilders.get(str(row.get("backend") or ""))
            if factory is not None:
                try:
                    index = factory(row)
                except Exception:  # noqa: BLE001 - 后端不可用时只是「重建失败」
                    index = None
        if index is None:
            return None

        meta = from_json(row.get("meta"), {}) or {}
        meta.setdefault("created_at", float(row.get("created_at") or 0))
        record = WarehouseRecord(run_id, row.get("filename") or "", index, meta)
        with self._lock:
            self._records[run_id] = record
        return record

    def list(self) -> list[dict[str, Any]]:
        db = self._db
        if db is None:
            records = sorted(
                self._records.values(), key=lambda r: float(r.meta.get("created_at") or 0), reverse=True
            )
            return [
                {
                    "run_id": r.run_id,
                    "filename": r.filename,
                    "size": getattr(r.index, "size", None),
                    "embedder": getattr(r.index, "embedder", None),
                    "dimension": getattr(r.index, "dimension", None),
                    **r.meta,
                }
                for r in records
            ]

        items: list[dict[str, Any]] = []
        for row in db.query("SELECT * FROM knowledge_indexes ORDER BY created_at DESC"):
            extra = from_json(row.get("meta"), {}) or {}
            items.append(
                {
                    **extra,
                    "run_id": row["run_id"],
                    "filename": row.get("filename") or "",
                    "size": row.get("vec_size"),
                    "embedder": row.get("embedder"),
                    "dimension": row.get("dimension"),
                    "collection": row.get("collection") or extra.get("collection"),
                    "backend": row.get("backend") or extra.get("backend") or "memory",
                    "owner_id": row.get("owner_id") or "",
                    "created_at": float(row.get("created_at") or 0),
                }
            )
        return items

    def chunks_of(self, run_id: str) -> list[dict[str, Any]]:
        """读回某次运行的切片（重启后查看历史详情用）。"""
        db = self._db
        if db is None:
            record = self._records.get(run_id)
            chunks = list(getattr(record.index, "chunks", None) or []) if record else []
            return [
                {"chunk_id": c.id, "index": c.index, "text": c.text, "meta": c.meta} for c in chunks
            ]
        rows = db.query(
            "SELECT * FROM knowledge_chunks WHERE run_id = ? ORDER BY position", [run_id]
        )
        return [
            {
                "chunk_id": row.get("chunk_id") or "",
                "index": int(row.get("chunk_index") or 0),
                "text": row.get("text") or "",
                "meta": from_json(row.get("meta"), {}) or {},
            }
            for row in rows
        ]

    # -------------------------------------------------- 删除

    def delete(self, run_id: str) -> bool:
        with self._lock:
            record = self._records.get(run_id)
        db = self._db
        row_exists = db.exists("knowledge_indexes", {"run_id": run_id}) if db is not None else False
        if record is None and not row_exists:
            return False
        if record is None:
            record = self._rebuild(run_id)

        # 索引若落在外部后端（如 Milvus），先尽力清掉后端数据，再删本地记录。
        # 后端不可达（Milvus 未启动 / 连接超时）不能阻塞删除，否则这条索引
        # 既删不掉又用不了，只能永久卡在列表里。
        if record is not None:
            drop = getattr(record.index, "drop", None)
            if callable(drop):
                # 外部后端（Milvus 等）不可达时不让删除失败：记录仍要从库里清掉，
                # 否则索引会变成「删也删不掉」的僵尸条目。
                try:
                    drop()
                except Exception:  # noqa: BLE001
                    pass
        with self._lock:
            self._records.pop(run_id, None)
        if db is not None:
            with db.transaction() as tx:
                tx.delete("knowledge_chunks", {"run_id": run_id})
                tx.delete("knowledge_indexes", {"run_id": run_id})
        return True

    def clear(self) -> None:
        with self._lock:
            records = list(self._records.values())
            self._records.clear()
        for record in records:
            drop = getattr(record.index, "drop", None)
            if callable(drop):
                try:
                    drop()
                except Exception:  # noqa: BLE001 - 清空时单个后端失败不该中断整体
                    pass
        db = self._db
        if db is not None:
            with db.transaction() as tx:
                tx.clear("knowledge_chunks")
                tx.clear("knowledge_indexes")


#: 进程级默认实例；由 ``runtime.bootstrap`` 装配数据库
warehouse = Warehouse()


__all__ = ["MemoryIndex", "SearchableIndex", "Warehouse", "WarehouseRecord", "warehouse"]

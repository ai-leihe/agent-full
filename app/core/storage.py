"""文档对象存储：原始上传文件落盘 + 数据库登记元数据。

为什么单独拎出来
----------------
流水线跑完之后，「重放」需要拿到**原始字节**。早期版本把字节只放在进程内存的
``PipelineContext`` 里，于是进程一重启，历史记录还在（``runs.json``），
重放却直接 410 了。

这里把原始文件按 ``run_id`` 落盘，并往 ``documents`` 表写一行元数据
（文件名、类型、大小、SHA-256、存储键）。列表、统计、删除都走数据库，
字节流走磁盘——也就是常说的「对象存储 + 元数据入库」的最小可用版本。

放磁盘而不是直接塞进 BLOB，是为了避免几十 MB 的文件把数据库撑大、
拖慢备份与查询；后续换成 S3 / COS 只需替换 :meth:`DocumentStore._write_bytes`
等几个方法。
"""

from __future__ import annotations

import hashlib
import re
import time
from pathlib import Path
from typing import Any

from .db import Database, get_database

#: 允许的 run_id 形态（同时充当磁盘文件名，必须严格）
RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class DocumentStore:
    """原始文档的落盘 + 登记。"""

    def __init__(self, db: Database | None = None, root: str | Path | None = None) -> None:
        self._db = db
        self._root = Path(root) if root else None
        if self._root is not None:
            self._root.mkdir(parents=True, exist_ok=True)

    # -------------------------------------------------- 装配

    def attach(self, db: Database, root: str | Path | None = None) -> None:
        """把存储切到指定数据库与目录（bootstrap 每次调用都会重定向）。"""
        self._db = db
        if root is not None:
            self._root = Path(root)
            self._root.mkdir(parents=True, exist_ok=True)

    @property
    def db(self) -> Database | None:
        return self._db

    @property
    def root(self) -> Path | None:
        return self._root

    def _database(self) -> Database | None:
        if self._db is None:
            return None
        return self._db

    def _path(self, run_id: str) -> Path | None:
        if self._root is None or not RUN_ID_PATTERN.match(str(run_id or "")):
            return None
        return self._root / f"{run_id}.bin"

    # -------------------------------------------------- 写入

    def save(self, run_id: str, filename: str, content_type: str, data: bytes) -> dict[str, Any] | None:
        """保存原始文件；``run_id`` 非法或没有可用存储时返回 ``None``。"""
        db = self._database()
        path = self._path(run_id)
        if db is None or path is None:
            return None
        payload = bytes(data or b"")
        try:
            path.write_bytes(payload)
        except OSError:
            return None

        row = {
            "run_id": str(run_id),
            "filename": str(filename or "")[:255],
            "content_type": str(content_type or "")[:128],
            "size": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "storage_key": path.name,
            "created_at": time.time(),
        }
        db.upsert("documents", {"run_id": row["run_id"]}, {k: v for k, v in row.items() if k != "run_id"})
        return row

    # -------------------------------------------------- 读取

    def info(self, run_id: str) -> dict[str, Any] | None:
        """只读元数据，不碰磁盘。"""
        db = self._database()
        if db is None:
            return None
        return db.query_one("SELECT * FROM documents WHERE run_id = ?", [run_id])

    def exists(self, run_id: str) -> bool:
        return self.info(run_id) is not None

    def load(self, run_id: str) -> dict[str, Any] | None:
        """读回原始文件：``{"filename", "content_type", "data", "size"}``。"""
        row = self.info(run_id)
        if row is None:
            return None
        path = self._path(row.get("storage_key") or run_id)
        if path is None or not path.is_file():
            return None
        try:
            payload = path.read_bytes()
        except OSError:
            return None
        return {
            "filename": row.get("filename") or "",
            "content_type": row.get("content_type") or "",
            "data": payload,
            "size": len(payload),
        }

    # -------------------------------------------------- 删除 / 统计

    def delete(self, run_id: str) -> bool:
        db = self._database()
        if db is None:
            return False
        row = self.info(run_id)
        if row is None:
            return False
        path = self._path(row.get("storage_key") or run_id)
        if path is not None and path.is_file():
            try:
                path.unlink()
            except OSError:
                pass
        db.delete("documents", {"run_id": run_id})
        return True

    def clear(self) -> int:
        """清空全部文档（数据库记录 + 磁盘文件）。"""
        db = self._database()
        if db is None:
            return 0
        rows = db.query("SELECT run_id, storage_key FROM documents")
        for row in rows:
            path = self._path(row.get("storage_key") or row.get("run_id") or "")
            if path is not None and path.is_file():
                try:
                    path.unlink()
                except OSError:
                    pass
        return db.clear("documents")

    def list(self, *, limit: int = 200) -> list[dict[str, Any]]:
        db = self._database()
        if db is None:
            return []
        return db.query("SELECT * FROM documents ORDER BY created_at DESC LIMIT ?", [limit])

    def stats(self) -> dict[str, Any]:
        db = self._database()
        if db is None:
            return {"count": 0, "bytes": 0}
        return {
            "count": db.count("documents"),
            "bytes": int(db.scalar("SELECT COALESCE(SUM(size), 0) AS n FROM documents", (), 0) or 0),
        }


#: 进程级默认实例；由 ``runtime.bootstrap`` 装配
documents = DocumentStore()


__all__ = ["DocumentStore", "documents", "get_database"]

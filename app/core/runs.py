"""运行记录：流水线执行的观测数据（落库留存）。

与 ``PipelineContext`` 的区别
----------------------------
``PipelineContext`` 是**执行期**对象，持有 chunks / embeddings 等大对象；
``RunRecord`` 是**观测期**对象，只保留摘要（耗时、产物清单、轨迹、统计）。

存储结构
--------
观测数据被拆成三张表，便于分页、筛选与长期留存：

* ``runs``         —— 一次运行的主记录（含 ``stats`` / ``errors`` JSON 与步数冗余列）；
* ``run_steps``    —— 逐步轨迹（agent / skill / 状态 / 耗时）；
* ``run_artifacts``—— 产物清单（kind / producer / 体积描述 / meta）。

主记录里冗余 ``step_count`` / ``failed_steps``，是为了让列表页不必为每一行
再去 join 子表。历史 ``data/runs.json`` 只在首次启动时作为迁移来源。
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .context import CHUNKS, EMBEDDINGS, TEXT, PipelineContext
from .db import Database, from_json, get_database, to_json


@dataclass
class RunRecord:
    """一次运行的可观测快照。"""

    run_id: str
    filename: str
    status: str                      # success | error
    source: str = "upload"           # upload | draft | replay
    pipeline: str = ""
    content_type: str = ""
    started_at: float = field(default_factory=time.time)
    elapsed_ms: float = 0.0
    stats: dict[str, Any] = field(default_factory=dict)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    trace: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @classmethod
    def from_context(
        cls, ctx: PipelineContext, *, status: str, pipeline: str = "", source: str = "upload"
    ) -> "RunRecord":
        payload = ctx.to_dict(max_chunks=0)
        return cls(
            run_id=ctx.run_id,
            filename=ctx.filename,
            status=status,
            source=source,
            pipeline=pipeline,
            content_type=ctx.content_type,
            started_at=ctx.started_at,
            elapsed_ms=payload["elapsed_ms"],
            stats=payload["stats"],
            artifacts=payload["artifacts"],
            trace=payload["trace"],
            errors=list(ctx.errors),
        )

    def summary(self) -> dict[str, Any]:
        """列表页用的轻量视图。"""
        return {
            "run_id": self.run_id,
            "filename": self.filename,
            "status": self.status,
            "source": self.source,
            "pipeline": self.pipeline,
            "started_at": self.started_at,
            "elapsed_ms": self.elapsed_ms,
            "chunk_count": self.stats.get("chunk_count", 0),
            "chars": self.stats.get("chars", 0),
            "embedding_dim": self.stats.get("embedding_dim", 0),
            "steps": len(self.trace),
            "failed_steps": sum(1 for t in self.trace if t.get("status") == "error"),
        }

    @classmethod
    def summary_from_row(cls, row: dict[str, Any]) -> dict[str, Any]:
        """直接用 ``runs`` 表的一行拼出列表视图，省去读子表。"""
        stats = from_json(row.get("stats"), {}) or {}
        return {
            "run_id": row.get("run_id"),
            "filename": row.get("filename") or "",
            "status": row.get("status") or "success",
            "source": row.get("source") or "upload",
            "pipeline": row.get("pipeline") or "",
            "started_at": float(row.get("started_at") or 0),
            "elapsed_ms": float(row.get("elapsed_ms") or 0),
            "chunk_count": stats.get("chunk_count", 0),
            "chars": stats.get("chars", 0),
            "embedding_dim": stats.get("embedding_dim", 0),
            "steps": int(row.get("step_count") or 0),
            "failed_steps": int(row.get("failed_steps") or 0),
        }

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _aggregate(items: list[tuple[str, float, int]]) -> dict[str, Any]:
    """按 ``(状态, 耗时, 切片数)`` 汇总运行统计。"""
    if not items:
        return {
            "total": 0, "success": 0, "error": 0,
            "success_rate": 0.0, "avg_elapsed_ms": 0.0, "avg_chunks": 0.0, "total_chunks": 0,
        }
    total = len(items)
    success = sum(1 for status, _, _ in items if status == "success")
    return {
        "total": total,
        "success": success,
        "error": total - success,
        "success_rate": round(success / total, 4),
        "avg_elapsed_ms": round(sum(elapsed for _, elapsed, _ in items) / total, 2),
        "avg_chunks": round(sum(chunks for _, _, chunks in items) / total, 2),
        "total_chunks": sum(chunks for _, _, chunks in items),
    }


class RunRegistry:
    """运行记录的容器。

    * 绑定了数据库（显式 ``db`` 或由 ``path`` 派生）时：写库 + 内存缓存，
      读走数据库 —— 重启与多实例都能看到同一份历史，且有 ``limit`` 保留策略；
    * 没有数据源时退化为纯内存容器，方便单测与临时脚本使用。
    """

    def __init__(
        self,
        path: str | Path | None = None,
        limit: int = 200,
        persist: bool = True,
        *,
        db: Database | None = None,
    ) -> None:
        self.path = Path(path) if path else None
        self.limit = max(10, int(limit))
        self.persist = bool(persist)
        self._db = db
        self._records: dict[str, RunRecord] = {}
        self._lock = threading.Lock()

    # -------------------------------------------------- 装配

    def attach(self, db: Database, path: str | Path | None = None) -> None:
        """绑定数据源；``bootstrap`` 每次都会把存储重定向到当前数据库。"""
        self._db = db
        if path is not None:
            self.path = Path(path)
        self._records = {}

    @property
    def db(self) -> Database | None:
        """当前是否有可用数据源；没有就是纯内存模式。"""
        if not self.persist:
            return None
        if self._db is None and self.path is not None:
            self._db = get_database(None, base_dir=self.path.parent)
        return self._db

    # -------------------------------------------------- 配置热更新

    def configure(self, *, limit: int | None = None, persist: bool | None = None) -> None:
        if limit is not None:
            self.limit = max(10, int(limit))
        if persist is not None:
            self.persist = bool(persist)
        with self._lock:
            self._trim()

    # -------------------------------------------------- 迁移

    def migrate_legacy(self) -> int:
        """把历史 ``data/runs.json`` 导入数据库；表非空或文件不存在时什么都不做。"""
        db = self.db
        if db is None or db.count("runs") or not (self.path and self.path.is_file()):
            return 0
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return 0

        imported = 0
        for item in raw if isinstance(raw, list) else []:
            try:
                record = RunRecord(**item)
            except TypeError:
                continue
            self.add(record)
            imported += 1
        return imported

    # -------------------------------------------------- 持久化

    def load(self) -> int:
        """从数据库载入记录（同时把步数 / 产物一次性拉齐，避免逐条查子表）。"""
        db = self.db
        if db is None:
            return 0

        rows = db.query("SELECT * FROM runs ORDER BY started_at DESC")
        traces: dict[str, list[dict[str, Any]]] = {}
        for row in db.query("SELECT * FROM run_steps ORDER BY run_id, position"):
            traces.setdefault(row["run_id"], []).append(self._step_view(row))
        artifacts: dict[str, list[dict[str, Any]]] = {}
        for row in db.query("SELECT * FROM run_artifacts ORDER BY run_id, position"):
            artifacts.setdefault(row["run_id"], []).append(self._artifact_view(row))

        loaded = {
            row["run_id"]: self._row_to_record(
                row, traces.get(row["run_id"], []), artifacts.get(row["run_id"], [])
            )
            for row in rows
        }
        with self._lock:
            self._records = loaded
            self._trim()
        return len(loaded)

    def _trim(self) -> None:
        """内存模式下按 ``started_at`` 丢弃最旧的记录。"""
        while len(self._records) > self.limit:
            oldest = min(self._records.values(), key=lambda r: r.started_at)
            self._records.pop(oldest.run_id, None)

    # -------------------------------------------------- 行 ↔ 记录

    @staticmethod
    def _step_view(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "agent": row.get("agent") or "",
            "skill": row.get("skill") or "",
            "status": row.get("status") or "",
            "duration_ms": round(float(row.get("duration_ms") or 0), 2),
            "detail": row.get("detail") or "",
        }

    @staticmethod
    def _artifact_view(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "kind": row.get("kind") or "",
            "producer": row.get("producer") or "",
            "size": row.get("size_label") or "",
            "meta": from_json(row.get("meta"), {}) or {},
        }

    @staticmethod
    def _row_to_record(
        row: dict[str, Any], steps: list[dict[str, Any]], artifacts: list[dict[str, Any]]
    ) -> RunRecord:
        return RunRecord(
            run_id=row["run_id"],
            filename=row.get("filename") or "",
            status=row.get("status") or "success",
            source=row.get("source") or "upload",
            pipeline=row.get("pipeline") or "",
            content_type=row.get("content_type") or "",
            started_at=float(row.get("started_at") or 0),
            elapsed_ms=float(row.get("elapsed_ms") or 0),
            stats=from_json(row.get("stats"), {}) or {},
            artifacts=list(artifacts),
            trace=list(steps),
            errors=from_json(row.get("errors"), []) or [],
        )

    def _save(self, record: RunRecord) -> None:
        """整条写库：主记录 upsert，子表先清后插（保证重放覆盖旧轨迹）。"""
        db = self.db
        if db is None:
            return
        trace = list(record.trace or [])
        artifacts = list(record.artifacts or [])
        with db.transaction() as tx:
            tx.upsert(
                "runs",
                {"run_id": record.run_id},
                {
                    "filename": record.filename,
                    "status": record.status,
                    "source": record.source,
                    "pipeline": record.pipeline,
                    "content_type": record.content_type,
                    "started_at": float(record.started_at or 0),
                    "elapsed_ms": float(record.elapsed_ms or 0),
                    "stats": to_json(record.stats or {}),
                    "errors": to_json(record.errors or []),
                    "step_count": len(trace),
                    "failed_steps": sum(1 for t in trace if t.get("status") == "error"),
                    "created_at": time.time(),
                },
            )
            tx.delete("run_steps", {"run_id": record.run_id})
            tx.delete("run_artifacts", {"run_id": record.run_id})
            for position, step in enumerate(trace):
                tx.insert(
                    "run_steps",
                    {
                        "run_id": record.run_id,
                        "position": position,
                        "agent": str(step.get("agent") or "")[:128],
                        "skill": str(step.get("skill") or "")[:128],
                        "status": str(step.get("status") or "")[:16],
                        "duration_ms": float(step.get("duration_ms") or 0),
                        "detail": str(step.get("detail") or ""),
                    },
                )
            for position, artifact in enumerate(artifacts):
                tx.insert(
                    "run_artifacts",
                    {
                        "run_id": record.run_id,
                        "position": position,
                        "kind": str(artifact.get("kind") or "")[:64],
                        "producer": str(artifact.get("producer") or "")[:128],
                        "size_label": str(artifact.get("size") or "")[:64],
                        "meta": to_json(artifact.get("meta") or {}),
                    },
                )

    def _delete_rows(self, run_id: str) -> None:
        db = self.db
        if db is None:
            return
        with db.transaction() as tx:
            tx.delete("run_steps", {"run_id": run_id})
            tx.delete("run_artifacts", {"run_id": run_id})
            tx.delete("runs", {"run_id": run_id})

    def _trim_db(self) -> None:
        """把超出保留上限的最旧记录连同子表一起删掉。"""
        db = self.db
        if db is None:
            return
        overflow = db.count("runs") - self.limit
        if overflow <= 0:
            return
        stale = db.query("SELECT run_id FROM runs ORDER BY started_at ASC LIMIT ?", [overflow])
        for row in stale:
            self._records.pop(row["run_id"], None)
            self._delete_rows(row["run_id"])

    # -------------------------------------------------- 增删查

    def add(self, record: RunRecord) -> RunRecord:
        with self._lock:
            self._records[record.run_id] = record
            if self.db is not None:
                self._save(record)
                self._trim_db()
            else:
                self._trim()
        return record

    def record_context(
        self, ctx: PipelineContext, *, status: str, pipeline: str = "", source: str = "upload"
    ) -> RunRecord:
        return self.add(RunRecord.from_context(ctx, status=status, pipeline=pipeline, source=source))

    def _ordered(self) -> list[RunRecord]:
        return sorted(self._records.values(), key=lambda r: r.started_at, reverse=True)

    def list(self, *, limit: int = 50, status: str | None = None, keyword: str = "") -> list[dict[str, Any]]:
        db = self.db
        if db is None:
            items = self._ordered()
            if status:
                items = [r for r in items if r.status == status]
            if keyword:
                needle = keyword.lower()
                items = [
                    r for r in items if needle in r.filename.lower() or needle in r.run_id.lower()
                ]
            return [r.summary() for r in items[:limit]]

        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if keyword:
            clauses.append("(filename LIKE ? OR run_id LIKE ?)")
            params.extend([f"%{keyword}%", f"%{keyword}%"])
        sql = "SELECT * FROM runs"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY started_at DESC LIMIT ?"
        params.append(max(1, int(limit)))
        return [RunRecord.summary_from_row(row) for row in db.query(sql, params)]

    def page(
        self,
        *,
        page: int = 1,
        size: int = 10,
        status: str | None = None,
        keyword: str = "",
    ) -> tuple[list[dict[str, Any]], int]:
        """分页读取运行记录，返回 ``(当页记录, 过滤后总数)``。"""
        page = max(1, int(page))
        size = max(1, int(size))
        offset = (page - 1) * size
        db = self.db

        if db is None:
            items = self._ordered()
            if status:
                items = [r for r in items if r.status == status]
            if keyword:
                needle = keyword.lower()
                items = [
                    r for r in items if needle in r.filename.lower() or needle in r.run_id.lower()
                ]
            return [r.summary() for r in items[offset : offset + size]], len(items)

        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if keyword:
            clauses.append("(filename LIKE ? OR run_id LIKE ?)")
            params.extend([f"%{keyword}%", f"%{keyword}%"])
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""

        total = int(db.scalar(f"SELECT COUNT(*) AS n FROM runs{where}", params, 0) or 0)
        rows = db.query(
            f"SELECT * FROM runs{where} ORDER BY started_at DESC LIMIT ? OFFSET ?",
            [*params, size, offset],
        )
        return [RunRecord.summary_from_row(row) for row in rows], total

    def get(self, run_id: str) -> RunRecord | None:
        db = self.db
        if db is None:
            return self._records.get(run_id)

        row = db.query_one("SELECT * FROM runs WHERE run_id = ?", [run_id])
        if row is None:
            self._records.pop(run_id, None)
            return None
        steps = [
            self._step_view(item)
            for item in db.query(
                "SELECT * FROM run_steps WHERE run_id = ? ORDER BY position", [run_id]
            )
        ]
        artifacts = [
            self._artifact_view(item)
            for item in db.query(
                "SELECT * FROM run_artifacts WHERE run_id = ? ORDER BY position", [run_id]
            )
        ]
        record = self._row_to_record(row, steps, artifacts)
        self._records[run_id] = record
        return record

    def delete(self, run_id: str) -> bool:
        with self._lock:
            db = self.db
            if db is None:
                return self._records.pop(run_id, None) is not None
            removed = db.exists("runs", {"run_id": run_id})
            if removed:
                self._delete_rows(run_id)
            self._records.pop(run_id, None)
            return removed

    def clear(self) -> int:
        with self._lock:
            db = self.db
            if db is None:
                count = len(self._records)
                self._records.clear()
                return count
            count = db.count("runs")
            with db.transaction() as tx:
                tx.clear("run_steps")
                tx.clear("run_artifacts")
                tx.clear("runs")
            self._records.clear()
            return count

    # -------------------------------------------------- 统计

    def stats(self) -> dict[str, Any]:
        db = self.db
        if db is None:
            return _aggregate(
                [(r.status, r.elapsed_ms, int(r.stats.get("chunk_count", 0) or 0)) for r in self._records.values()]
            )

        rows = db.query("SELECT status, elapsed_ms, stats FROM runs")
        return _aggregate(
            [
                (
                    row.get("status") or "",
                    float(row.get("elapsed_ms") or 0),
                    int((from_json(row.get("stats"), {}) or {}).get("chunk_count", 0) or 0),
                )
                for row in rows
            ]
        )


def context_metrics(ctx: PipelineContext) -> dict[str, int]:
    """从上下文提取用于统计的体量指标。"""
    return {
        "chars": len(ctx.get(TEXT) or ""),
        "chunks": len(list(ctx.get(CHUNKS) or [])),
        "vectors": len(list(ctx.get(EMBEDDINGS) or [])),
    }

"""编排方案存档：把「保存下来的编排草稿」落到数据库。

之前方案是一堆 ``config/orchestrations/*.yaml`` 文件：能存能读，但没法回答
「这个方案是谁建的、什么时候改的」，多个实例之间也不共享。现在改成三张表：

* ``orchestrations``          —— 方案主记录（描述、启停、规模、时间戳）；
* ``orchestration_agents``    —— 阶段（顺序 + 名称 + 角色）；
* ``orchestration_steps``     —— 步骤（插件、参数 JSON、是否启用）。

id 处理
-------
草稿里的 ``agent_xxx`` / ``step_xxx`` 是前端生成的，同一个草稿另存为两个方案时
id 会重复，直接拿来当主键必然冲突。所以表里存的是 ``{方案名}::{原始id}`` 形式的
合成长主键，原始 id 另存 ``spec_id``，读出来后原样还原，草稿对前端依旧稳定。

历史 ``config/orchestrations/*.yaml`` 只在首次启动时导入一次；方案内容仍然可以
按需导出成 YAML（``content()``），方便手工备份与版本管理。
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

import yaml

from .db import Database, from_json, get_database, to_json
from .orchestration import AgentSpec, Orchestration, StepSpec, load_orchestration

#: 方案名白名单，同时防路径穿越
PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9_\-]{1,40}$")

#: 当前生效方案名在 ``kv`` 表中的键
ACTIVE_PROFILE_KEY = "orchestration.active"


class ProfileError(ValueError):
    """方案名非法或方案不存在。"""


class ProfileStore:
    """编排方案的读写封装；``root`` 只用于首次启动时迁移历史 YAML。"""

    def __init__(self, db: Database | None = None, root: str | Path | None = None) -> None:
        self._db = db
        self.root = Path(root) if root else None

    # -------------------------------------------------- 装配

    def attach(self, db: Database, root: str | Path | None = None) -> None:
        self._db = db
        if root is not None:
            self.root = Path(root)
            self.root.mkdir(parents=True, exist_ok=True)

    @property
    def db(self) -> Database:
        return self._database()

    def _database(self) -> Database:
        if self._db is None:
            base = self.root.parent if self.root else None
            self._db = get_database(None, base_dir=base)
        return self._db

    # -------------------------------------------------- 名称校验

    @staticmethod
    def check_name(name: str) -> str:
        if not PROFILE_NAME_RE.match(name or ""):
            raise ProfileError("方案名只能包含字母、数字、下划线和短横线，长度 1-40")
        return name

    # -------------------------------------------------- 迁移

    def migrate_legacy(self) -> int:
        """把历史 YAML 方案导入数据库；已有方案或目录不存在时什么都不做。"""
        db = self._database()
        if db.count("orchestrations") or self.root is None or not self.root.is_dir():
            return 0

        imported = 0
        for path in sorted(self.root.glob("*.yaml")):
            try:
                orch = load_orchestration(path)
            except Exception:  # noqa: BLE001 - 坏文件跳过，不影响其它方案
                continue
            self.save(path.stem, orch)
            imported += 1
        return imported

    # -------------------------------------------------- 读

    def rows(self) -> list[dict[str, Any]]:
        return self._database().query("SELECT * FROM orchestrations ORDER BY name")

    def names(self) -> list[str]:
        return [row["name"] for row in self.rows()]

    def exists(self, name: str) -> bool:
        return self._database().exists("orchestrations", {"name": name})

    def get(self, name: str) -> Orchestration | None:
        """读出方案并还原成 :class:`Orchestration`；不存在返回 ``None``。"""
        db = self._database()
        row = db.query_one("SELECT * FROM orchestrations WHERE name = ?", [name])
        if row is None:
            return None

        steps: dict[str, list[dict[str, Any]]] = {}
        for item in db.query(
            "SELECT * FROM orchestration_steps WHERE orchestration = ? ORDER BY position", [name]
        ):
            steps.setdefault(item["agent_id"], []).append(item)

        agents: list[AgentSpec] = []
        for item in db.query(
            "SELECT * FROM orchestration_agents WHERE orchestration = ? ORDER BY position", [name]
        ):
            agents.append(
                AgentSpec(
                    id=item.get("spec_id") or item["id"],
                    name=item.get("name") or "agent",
                    role=item.get("role") or "",
                    steps=[
                        StepSpec(
                            id=step.get("spec_id") or step["id"],
                            skill=step.get("skill") or "",
                            options=from_json(step.get("options"), {}) or {},
                            enabled=bool(step.get("enabled")),
                        )
                        for step in steps.get(item["id"], [])
                    ],
                )
            )

        return Orchestration(
            name=name,
            description=row.get("description") or "",
            agents=agents,
        )

    def content(self, name: str) -> str:
        """导出为 YAML 文本（与历史 ``*.yaml`` 方案格式一致）。"""
        orch = self.get(name)
        if orch is None:
            raise ProfileError(f"方案 '{name}' 不存在")
        return yaml.safe_dump(orch.to_yaml(), allow_unicode=True, sort_keys=False)

    def list_paginated(
        self,
        page: int = 1,
        size: int = 10,
        query: str = "",
        registry: Any | None = None,
    ) -> dict[str, Any]:
        """分页列出方案，并附带契约诊断结果。"""
        db = self._database()
        where_clause = ""
        params: list[Any] = []
        if query:
            where_clause = "WHERE name LIKE ? OR description LIKE ?"
            params = [f"%{query}%", f"%{query}%"]

        total = db.scalar(f"SELECT COUNT(*) AS n FROM orchestrations {where_clause}".strip(), params, 0)
        total = int(total or 0)
        offset = max(0, page - 1) * size
        sql = f"SELECT * FROM orchestrations {where_clause} ORDER BY updated_at DESC LIMIT ? OFFSET ?".strip()
        rows = db.query(sql, [*params, size, offset])

        active = self.active
        items: list[dict[str, Any]] = []
        for row in rows:
            name = row["name"]
            try:
                orch = self.get(name)
                diagnosis = orch.diagnose(registry) if orch else {"ok": True, "issues": []}
                items.append(
                    {
                        "name": name,
                        "description": orch.description if orch else (row.get("description") or ""),
                        "agents": row.get("agents_count") or 0,
                        "steps": row.get("steps_count") or 0,
                        "ok": diagnosis["ok"],
                        "issues": len(diagnosis["issues"]),
                        "active": name == active,
                        "updated_at": float(row.get("updated_at") or 0),
                        "created_at": float(row.get("created_at") or 0),
                    }
                )
            except Exception as exc:  # noqa: BLE001 - 坏方案不应影响列表
                items.append(
                    {
                        "name": name,
                        "description": row.get("description") or "",
                        "agents": row.get("agents_count") or 0,
                        "steps": row.get("steps_count") or 0,
                        "ok": False,
                        "issues": 1,
                        "active": name == active,
                        "updated_at": float(row.get("updated_at") or 0),
                        "created_at": float(row.get("created_at") or 0),
                        "error": str(exc),
                    }
                )
        return {
            "items": items,
            "total": total,
            "page": page,
            "size": size,
            "pages": max(1, (total + size - 1) // size) if total else 1,
        }

    def get_detail(self, name: str, registry: Any | None = None) -> dict[str, Any] | None:
        """读出方案详情，包含完整编排与诊断。"""
        orch = self.get(name)
        if orch is None:
            return None
        diagnosis = orch.diagnose(registry)
        return {
            "name": name,
            "description": orch.description,
            "orchestration": orch.to_dict(),
            "diagnosis": diagnosis,
            "ok": diagnosis["ok"],
        }

    # -------------------------------------------------- 写

    def save(self, name: str, orch: Orchestration) -> None:
        """整体覆盖式保存：主记录 upsert，子表先清后插。"""
        self.check_name(name)
        db = self._database()
        now = time.time()
        active = 1 if self.active == name else 0

        with db.transaction() as tx:
            payload = {
                "description": orch.description or "",
                "active": active,
                "agents_count": len(orch.agents),
                "steps_count": sum(len(a.steps) for a in orch.agents),
                "updated_at": now,
            }
            if not tx.exists("orchestrations", {"name": name}):
                payload["created_at"] = now
            tx.upsert("orchestrations", {"name": name}, payload)

            tx.delete("orchestration_agents", {"orchestration": name})
            tx.delete("orchestration_steps", {"orchestration": name})
            for position, agent in enumerate(orch.agents):
                agent_pk = f"{name}::{agent.id}"[:64]
                tx.insert(
                    "orchestration_agents",
                    {
                        "id": agent_pk,
                        "spec_id": str(agent.id)[:64],
                        "orchestration": name,
                        "position": position,
                        "name": str(agent.name or "agent")[:128],
                        "role": str(agent.role or ""),
                    },
                )
                for step_position, step in enumerate(agent.steps):
                    tx.insert(
                        "orchestration_steps",
                        {
                            "id": f"{name}::{step.id}"[:64],
                            "spec_id": str(step.id)[:64],
                            "orchestration": name,
                            "agent_id": agent_pk,
                            "position": step_position,
                            "skill": str(step.skill or "")[:128],
                            "options": to_json(step.options or {}),
                            "enabled": bool(step.enabled),
                        },
                    )

    def rename(self, old: str, new: str) -> Orchestration | None:
        orch = self.get(old)
        if orch is None:
            return None
        was_active = self.active == old
        self.save(new, orch)
        self.delete(old)
        if was_active:
            self.set_active(new)
        return orch

    def delete(self, name: str) -> bool:
        db = self._database()
        if not db.exists("orchestrations", {"name": name}):
            return False
        with db.transaction() as tx:
            tx.delete("orchestration_agents", {"orchestration": name})
            tx.delete("orchestration_steps", {"orchestration": name})
            tx.delete("orchestrations", {"name": name})
        if self.active == name:
            self.set_active("")
        return True

    # -------------------------------------------------- 当前生效方案

    @property
    def active(self) -> str:
        return str(self._database().kv_get(ACTIVE_PROFILE_KEY) or "")

    def set_active(self, name: str) -> None:
        db = self._database()
        db.kv_set(ACTIVE_PROFILE_KEY, name or "")
        if name:
            db.update("orchestrations", {"name": name}, {"active": 1})
        db.execute("UPDATE orchestrations SET active = 0 WHERE name <> ?", [name or ""])


__all__ = ["ACTIVE_PROFILE_KEY", "PROFILE_NAME_RE", "ProfileError", "ProfileStore"]

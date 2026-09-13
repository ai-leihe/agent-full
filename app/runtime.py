"""运行时：插件发现 + 流水线装配 + 编排草稿 + 平台设置 + 运行记录。

状态分层与存储介质
------------------
============================  ==========================================
状态                          存储介质
============================  ==========================================
``settings``（平台设置 / 供应商）  数据库（``kv`` + ``providers`` 表）
``auth``（账号 / 会话）           数据库（``users`` / ``sessions`` 表）
``history``（运行记录）           数据库（``runs`` / ``run_steps`` / ``run_artifacts``）
``profiles``（编排方案存档）       数据库（``orchestrations`` 等三表）
向量索引                       数据库（``knowledge_indexes`` / ``knowledge_chunks``）
原始上传文件                      ``<data>/uploads`` + ``documents`` 表登记
``pipeline``（已生效流水线）       进程内存，由 YAML + 方案编译得到
``draft``（编辑中的编排草稿）      进程内存，可试跑但未生效
``contexts``（运行上下文）         进程内存（含大对象），重启后按需从文档库重建
============================  ==========================================

也就是说：**只有「可重建的运行时对象」留在内存**，凡是「用户资产、需要跨重启、
需要多实例共享」的数据一律落库。``config/*.json`` 与 ``config/orchestrations/*.yaml``
退化为首次启动时的迁移来源。

``draft_revision`` / ``applied_revision`` 用来判断草稿是否有未生效的改动。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from fastapi import Request

from .core.auth import DEFAULT_DOCS_ACCESS, AuthManager
from .core.config import build_pipeline_from_file
from .core.context import PipelineContext
from .core.db import Database, audit, audit_stats, get_database
from .core.orchestration import Orchestration
from .core.orchestrator import Pipeline
from .core.profiles import PROFILE_NAME_RE, ProfileError, ProfileStore
from .core.runs import RunRecord, RunRegistry
from .core.settings import MASK, SETTINGS_SCHEMA, SettingsStore
from .core.skill import registry
from .core.storage import documents
from .core.warehouse import warehouse

BUILTIN_PLUGIN_PACKAGE = "app.plugins"

PLATFORM_VERSION = "1.0.0"

__all__ = [
    "BUILTIN_PLUGIN_PACKAGE",
    "PLATFORM_VERSION",
    "PROFILE_NAME_RE",
    "ProfileError",
    "RuntimeState",
    "runtime",
]


class RuntimeState:
    def __init__(self) -> None:
        self.pipeline: Pipeline | None = None
        self.config_path: Path | None = None
        self.ext_dir: Path | None = None
        self.profiles_dir: Path | None = None
        self.uploads_dir: Path | None = None
        self.loaded_modules: list[str] = []
        self.started_at = time.time()

        # 数据库（bootstrap 时装配）；所有 Store 共享同一个实例
        self.db: Database | None = None

        # 运行期上下文缓存（含大对象，仅内存）
        self.contexts: dict[str, PipelineContext] = {}

        # 平台设置与运行记录
        self.settings = SettingsStore()
        self.history = RunRegistry()

        # 账号体系（用户 + 会话令牌）
        self.auth = AuthManager()

        # 编排方案存档与原始文档存储
        self.profiles = ProfileStore()
        self.documents = documents

        # 编排草稿
        self.draft: Orchestration | None = None
        self.draft_revision = 0
        self.applied_revision = 0

    # ================================================== 启动

    def bootstrap(
        self,
        config_path: str | Path,
        ext_dir: str | Path | None = None,
        profiles_dir: str | Path | None = None,
        settings_path: str | Path | None = None,
        runs_path: str | Path | None = None,
        users_path: str | Path | None = None,
        database_url: str | Path | None = None,
        uploads_dir: str | Path | None = None,
    ) -> Pipeline:
        """装配运行时。

        参数里的各种 ``*_path`` 只作为**历史数据的迁移来源**：第一次启动时
        如果数据库里还是空的，就从这些文件把数据导进去；之后它们不再被读写。
        ``database_url`` 支持 SQLite 路径、``mysql://``、``postgresql://``，
        留空时取环境变量 ``PLATFORM_DATABASE_URL``，再退化为
        ``<runs.json 所在目录>/platform.db``。
        """
        self.config_path = Path(config_path)
        self.ext_dir = Path(ext_dir) if ext_dir else None
        if profiles_dir:
            self.profiles_dir = Path(profiles_dir)
            self.profiles_dir.mkdir(parents=True, exist_ok=True)

        # 数据库放在运行记录同目录，便于「一个数据目录带走全部状态」
        data_dir = Path(runs_path).parent if runs_path else Path("data")
        self.uploads_dir = Path(uploads_dir) if uploads_dir else data_dir / "uploads"
        self.db = get_database(database_url, base_dir=data_dir)

        # --- 设置：迁移 → 载入 → 推送生效
        self.settings.attach(self.db, settings_path)
        self.settings.migrate_legacy()
        self.settings.load()
        self.apply_live_settings()

        # --- 运行记录
        self.history.attach(self.db, runs_path)
        self.history.migrate_legacy()
        self.history.load()

        # --- 账号与会话
        self.auth.attach(self.db, users_path, Path(users_path).with_name(".auth_secret") if users_path else None)
        self.auth.migrate_legacy()
        self.auth.load()

        # --- 编排方案与向量仓库、文档存储
        self.profiles.attach(self.db, profiles_dir)
        self.profiles.migrate_legacy()
        warehouse.attach(self.db)
        self.documents.attach(self.db, self.uploads_dir)

        # 关键顺序：先发现插件并按设置启停，再装配流水线（装配即校验契约）
        self.rebuild()

        # 首次启动由 YAML 生成草稿；后续重载保留用户正在编辑的草稿，避免误丢工作
        if self.draft is None:
            self.draft = Orchestration.from_pipeline(self.pipeline)
            self.draft_revision = 0
            self.applied_revision = 0

        # 恢复上次激活的流水线任务；失败则回退到 YAML 默认流水线
        active = self.active_profile or ""
        if active and active in {p["name"] for p in self.list_profiles()}:
            try:
                self.load_profile(active)
                self.apply_draft()
            except Exception:  # noqa: BLE001 - 坏流水线不应阻塞启动
                self.active_profile = None
                self.draft = Orchestration.from_pipeline(self.pipeline)
                self.draft_revision = 0
                self.applied_revision = 0

        # 若配置了「启动自动加载方案」，优先级更高，在这里套用并生效
        profile = self.settings.get("runtime.auto_apply_profile") or ""
        if profile and profile in {p["name"] for p in self.list_profiles()}:
            try:
                self.load_profile(profile)
                self.apply_draft()
            except Exception:  # noqa: BLE001 - 方案有问题不应阻塞启动
                self.active_profile = None

        return self.pipeline

    def rebuild(self) -> Pipeline:
        """按当前设置重新扫描插件并重建流水线。失败时抛异常，由调用方决定是否回滚。"""
        registry.reset()
        self.loaded_modules = registry.load_package(BUILTIN_PLUGIN_PACKAGE)
        if self.ext_dir is not None:
            self.loaded_modules += registry.load_directory(self.ext_dir)
        registry.set_disabled(self.settings.get("plugins.disabled", []))

        assert self.config_path is not None
        pipeline = build_pipeline_from_file(self.config_path, registry)
        pipeline.validate()
        self.pipeline = pipeline
        return pipeline

    # ================================================== 审计

    def audit(
        self,
        request: Request | None,
        action: str,
        *,
        target: str = "",
        detail: Any = None,
        actor: Any = None,
    ) -> None:
        """记录一次写操作；操作人优先取当前登录用户。审计失败不影响业务。"""
        actor_id = ""
        actor_name = ""
        if actor is not None:
            actor_id = str(getattr(actor, "id", "") or "")
            actor_name = str(getattr(actor, "username", "") or "")
        elif request is not None:
            try:
                user = self.auth.user_from_request(request)
            except Exception:  # noqa: BLE001 - 取不到操作人也要留痕
                user = None
            if user is not None:
                actor_id = str(user.id)
                actor_name = str(user.username)
        audit(
            self.db,
            action,
            target=target,
            actor_id=actor_id,
            actor_name=actor_name,
            detail=detail,
        )

    def recent_audit(self, *, limit: int = 50, action: str = "") -> list[dict[str, Any]]:
        from .core.db import list_audit

        return list_audit(self.db, limit=limit, action=action)

    def record_script_run(self, request: Request | None, result: dict[str, Any], code: str) -> None:
        """脚本验证属于「给用户开了一个终端」，必须留痕（代码只存摘要与哈希）。"""
        if self.db is None:
            return
        import hashlib

        user = None
        if request is not None:
            try:
                user = self.auth.user_from_request(request)
            except Exception:  # noqa: BLE001
                user = None
        payload = {
            "user_id": str(getattr(user, "id", "") or ""),
            "username": str(getattr(user, "username", "") or ""),
            "language": str(result.get("language") or ""),
            "code_hash": hashlib.sha256(str(code or "").encode("utf-8")).hexdigest(),
            "code_size": len(str(code or "")),
            "code_preview": str(code or "")[:2000],
            "ok": bool(result.get("ok")),
            "stage": str(result.get("stage") or ""),
            "exit_code": result.get("exit_code"),
            "duration_ms": float(result.get("duration_ms") or 0),
            "timed_out": bool(result.get("timed_out")),
            "error": str(result.get("error") or "")[:2000],
            "created_at": time.time(),
        }
        try:
            self.db.insert("script_runs", payload)
        except Exception:  # noqa: BLE001 - 审计写失败不该影响脚本验证结果
            pass

    # ================================================== 设置

    def apply_live_settings(self) -> None:
        """把设置里「立即生效」的部分推送到各子系统。"""
        self.history.configure(
            limit=self.settings.get("observability.run_history_limit", 200),
            persist=bool(self.settings.get("observability.persist_runs", True)),
        )
        self.auth.configure(
            require_login=bool(self.settings.get("auth.require_login", True)),
            session_hours=int(self.settings.get("auth.session_hours", 72) or 72),
            allow_registration=bool(self.settings.get("auth.allow_registration", True)),
            docs_access=str(self.settings.get("auth.docs_access", DEFAULT_DOCS_ACCESS) or DEFAULT_DOCS_ACCESS),
        )
        self._trim_contexts()

    def update_settings(self, payload: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
        """更新设置；校验失败不落库。插件停用清单变化会触发重建（失败自动回滚）。"""
        before_disabled = set(self.settings.get("plugins.disabled", []))
        before_data = dict(self.settings.data)

        updated, errors = self.settings.patch(payload)
        if errors:
            return self.settings_snapshot(), errors

        after_disabled = set(self.settings.get("plugins.disabled", []))
        if after_disabled != before_disabled:
            try:
                self.rebuild()
            except Exception as exc:  # noqa: BLE001 - 回滚到变更前
                self.settings.save(before_data)
                try:
                    self.rebuild()
                except Exception:  # noqa: BLE001 - 回滚也失败则保持内存态一致
                    pass
                return self.settings_snapshot(), [
                    f"插件启停导致流水线契约失效，已回滚：{exc}"
                ]

        self.apply_live_settings()
        return self.settings_snapshot(), []

    def set_plugin_enabled(self, name: str, enabled: bool) -> list[str]:
        """切换单个插件启停，契约失效时自动回滚。"""
        if not registry.raw_has(name):
            raise KeyError(f"未找到插件 '{name}'")

        disabled = set(self.settings.get("plugins.disabled", []))
        disabled.discard(name) if enabled else disabled.add(name)
        _, errors = self.update_settings({"plugins": {"disabled": sorted(disabled)}})
        return errors

    def settings_snapshot(self) -> dict[str, Any]:
        """对外的设置视图：密钥掩码、附带 Schema、附带存储位置信息。"""
        # 供应商单独走 providers 字段返回（密钥已掩码），避免从 values 泄露明文
        values = {k: v for k, v in self.settings.data.items() if k != "providers"}
        return {
            "values": values,
            "schema": SETTINGS_SCHEMA,
            "providers": self.settings.list_providers(masked=True),
            "meta": {
                "database": self.db.info() if self.db else None,
                "documents": self.documents.stats(),
                "uploads_dir": str(self.uploads_dir) if self.uploads_dir else None,
                "config_path": str(self.config_path) if self.config_path else None,
                "ext_plugins_dir": str(self.ext_dir) if self.ext_dir else None,
                "profiles_dir": str(self.profiles_dir) if self.profiles_dir else None,
                "profiles_store": "orchestrations / orchestration_agents / orchestration_steps",
                "settings_store": "kv.settings + providers",
                "runs_store": "runs / run_steps / run_artifacts",
                "version": PLATFORM_VERSION,
                "mask": MASK,
            },
        }

    # ================================================== 草稿

    @property
    def dirty(self) -> bool:
        """草稿是否有尚未「应用生效」的改动。"""
        return self.draft_revision != self.applied_revision

    def touch(self) -> None:
        """任何一次草稿变更都要调用它，驱动 dirty 状态。"""
        self.draft_revision += 1

    def reset_draft(self) -> Orchestration:
        """丢弃编辑，回到当前生效流水线的结构。"""
        assert self.pipeline is not None
        self.draft = Orchestration.from_pipeline(self.pipeline)
        self.draft_revision += 1
        self.active_profile = None
        return self.draft

    def draft_pipeline(self) -> Pipeline:
        """编译草稿但不改变生效配置（试跑用）。"""
        assert self.draft is not None
        return self.draft.compile(registry)

    def apply_draft(self) -> Pipeline:
        """把草稿编译为生效流水线；契约不通过会抛 ContractError。

        若当前有已激活的流水线任务，生效时会同步把草稿写回库，保证编辑结果持久化。
        """
        pipeline = self.draft_pipeline()
        self.pipeline = pipeline
        self.applied_revision = self.draft_revision
        if self.draft is not None and self.active_profile:
            self.profiles.save(self.active_profile, self.draft)
        return pipeline

    # ================================================== 方案存档

    @property
    def active_profile(self) -> str | None:
        """当前生效的方案名（存在数据库里，重启后仍然记得）。"""
        return self.profiles.active or None

    @active_profile.setter
    def active_profile(self, value: str | None) -> None:
        self.profiles.set_active(value or "")

    def list_profiles(self) -> list[dict[str, Any]]:
        """列出全部方案并顺带做一次契约诊断。"""
        active = self.active_profile
        items = []
        for row in self.profiles.rows():
            name = row["name"]
            try:
                orch = self.profiles.get(name)
                if orch is None:
                    continue
                diagnosis = orch.diagnose(registry)
                items.append(
                    {
                        "name": name,
                        "description": orch.description,
                        "agents": len(orch.agents),
                        "steps": sum(len(a.steps) for a in orch.agents),
                        "ok": diagnosis["ok"],
                        "issues": len(diagnosis["issues"]),
                        "active": name == active,
                        "size": len(self.profiles.content(name).encode("utf-8")),
                        "updated_at": float(row.get("updated_at") or 0),
                    }
                )
            except Exception as exc:  # noqa: BLE001 - 坏方案不应影响列表
                items.append({"name": name, "error": str(exc), "ok": False, "active": False})
        return items

    def save_profile(self, name: str) -> str:
        """把当前草稿另存为方案，返回方案名。"""
        assert self.draft is not None
        self.profiles.save(name, self.draft)
        self.active_profile = name
        return name

    def profile_content(self, name: str) -> str:
        """按 YAML 导出方案内容（便于手工备份 / 版本管理）。"""
        return self.profiles.content(name)

    def load_profile(self, name: str) -> Orchestration:
        orch = self.profiles.get(name)
        if orch is None:
            raise ProfileError(f"方案 '{name}' 不存在")
        self.draft = orch
        self.draft_revision += 1
        self.active_profile = name
        return self.draft

    def delete_profile(self, name: str) -> bool:
        return self.profiles.delete(name)

    # ================================================== 流水线任务

    def list_pipelines(
        self,
        page: int = 1,
        size: int = 10,
        query: str = "",
    ) -> dict[str, Any]:
        """分页列出已保存的流水线任务。"""
        return self.profiles.list_paginated(page=page, size=size, query=query, registry=registry)

    def get_pipeline(self, name: str) -> dict[str, Any] | None:
        """读取单个流水线详情。"""
        return self.profiles.get_detail(name, registry=registry)

    def create_pipeline(
        self,
        name: str,
        description: str = "",
        *,
        clone_from: str | None = None,
        blank: bool = False,
    ) -> dict[str, Any]:
        """创建新的流水线任务。

        * ``clone_from`` 指定时复制该流水线；
        * ``blank=True`` 时创建空流水线（0 个阶段），方便完全自定义；
        * 否则复制当前编辑中的草稿结构，保证新任务默认可运行；
        * 创建后自动激活为新草稿。
        """
        self.profiles.check_name(name)
        if self.profiles.exists(name):
            raise ProfileError(f"流水线 '{name}' 已存在")

        if clone_from is not None:
            source = self.profiles.get(clone_from)
            if source is None:
                raise ProfileError(f"源流水线 '{clone_from}' 不存在")
            orch = source.clone()
        elif not blank and self.draft is not None:
            # 基于当前编辑中的草稿新建，保留未点击「应用生效」的修改
            orch = self.draft.clone()
        else:
            orch = Orchestration(name=name, description=description, agents=[])

        orch.name = name
        orch.description = description
        self.profiles.save(name, orch)
        self.load_profile(name)
        # 新流水线默认继承当前生效结构，契约校验应通过；若失败仍保留草稿供用户调整
        try:
            self.apply_draft()
        except Exception:  # noqa: BLE001 - 创建成功但生效失败，不阻塞保存
            pass
        return self.get_pipeline(name)

    def update_pipeline(self, name: str, payload: dict[str, Any]) -> dict[str, Any]:
        """更新流水线元信息，支持重命名。"""
        self.profiles.check_name(name)
        orch = self.profiles.get(name)
        if orch is None:
            raise ProfileError(f"流水线 '{name}' 不存在")

        if "description" in payload:
            orch.description = str(payload["description"] or "")

        new_name = payload.get("name")
        if new_name and new_name != name:
            self.profiles.check_name(new_name)
            if self.profiles.exists(new_name):
                raise ProfileError(f"目标名称 '{new_name}' 已存在")
            self.profiles.rename(name, new_name)
            name = new_name
        else:
            self.profiles.save(name, orch)
        return self.get_pipeline(name)

    def delete_pipeline(self, name: str) -> bool:
        """删除流水线任务。若删除的是当前激活任务，回退到 YAML 默认流水线。"""
        was_active = self.active_profile == name
        removed = self.profiles.delete(name)
        if removed and was_active:
            self._fallback_to_config_pipeline()
        return removed

    def _fallback_to_config_pipeline(self) -> None:
        """没有可用流水线时回退到 YAML 默认流水线。"""
        if not self.config_path:
            return
        try:
            self.rebuild()
            self.draft = Orchestration.from_pipeline(self.pipeline)
            self.draft_revision = 0
            self.applied_revision = 0
        except Exception:  # noqa: BLE001 - 回退失败不阻塞
            pass

    def activate_pipeline(self, name: str) -> dict[str, Any]:
        """激活指定流水线为生效配置。"""
        if not self.profiles.exists(name):
            raise ProfileError(f"流水线 '{name}' 不存在")
        self.load_profile(name)
        self.apply_draft()
        return self.get_pipeline(name)

    # ================================================== 运行记录

    def _trim_contexts(self) -> None:
        limit = int(self.settings.get("runtime.max_cached_runs", 200) or 200)
        while len(self.contexts) > limit:
            oldest = next(iter(self.contexts))
            self.contexts.pop(oldest, None)

    def remember(
        self,
        ctx: PipelineContext,
        *,
        status: str = "success",
        source: str = "upload",
        owner: str = "",
    ) -> RunRecord:
        """缓存上下文（供详情/检索复用）、留存原始文件、写入运行记录。"""
        self.contexts[ctx.run_id] = ctx
        self._trim_contexts()

        # 原始文件落盘 + 登记：这是「重启后还能重放」的前提
        self.documents.save(ctx.run_id, ctx.filename, ctx.content_type, ctx.data)
        if owner:
            warehouse.annotate(ctx.run_id, owner_id=str(owner))

        return self.history.record_context(
            ctx,
            status=status,
            pipeline=self.pipeline.name if self.pipeline else "",
            source=source,
        )

    def context_of(self, run_id: str) -> PipelineContext | None:
        """当前进程里缓存的上下文；重启后为 ``None``（详情页走数据库兜底）。"""
        return self.contexts.get(run_id)

    def replay_context(self, run_id: str) -> PipelineContext | None:
        """重放用的上下文：内存没有就从对象存储把原始文件读回来。"""
        ctx = self.contexts.get(run_id)
        if ctx is not None:
            return ctx
        return self._context_from_storage(run_id)

    def _context_from_storage(self, run_id: str) -> PipelineContext | None:
        """按 ``documents`` 表里的登记把原始文件恢复成上下文。"""
        stored = self.documents.load(run_id)
        if stored is None:
            return None
        record = self.history.get(run_id)
        return PipelineContext(
            filename=stored["filename"] or (record.filename if record else ""),
            data=stored["data"],
            content_type=stored["content_type"] or (record.content_type if record else ""),
            run_id=run_id,
        )

    def has_document(self, run_id: str) -> bool:
        return self.documents.exists(run_id)

    def chunks_of(self, run_id: str) -> list[dict[str, Any]]:
        return warehouse.chunks_of(run_id)

    def forget(self, run_id: str) -> None:
        """删除一处运行带来的内存与文件痕迹（运行记录由调用方自行删除）。"""
        self.contexts.pop(run_id, None)
        self.documents.delete(run_id)

    # ================================================== 对外快照

    def state(self) -> dict[str, Any]:
        """前端编排工作台所需的完整状态，一次请求拿全。"""
        assert self.draft is not None
        return {
            "orchestration": self.draft.to_dict(),
            "diagnosis": self.draft.diagnose(registry),
            "dirty": self.dirty,
            "active_pipeline": self.pipeline.name if self.pipeline else None,
            "active_profile": self.active_profile,
            "catalog": registry.catalog(),
            "profiles": self.list_profiles(),
            "source_config": str(self.config_path) if self.config_path else None,
        }

    def snapshot(self) -> dict[str, Any]:
        assert self.pipeline is not None
        return {
            "pipeline": self.pipeline.describe(),
            "plugins": registry.manifests(),
            "slots": registry.slots(),
            "disabled": registry.disabled(),
            "config_path": str(self.config_path),
            "ext_plugins_dir": str(self.ext_dir) if self.ext_dir else None,
            "loaded_modules": self.loaded_modules,
        }

    def overview(self) -> dict[str, Any]:
        """工作台首页的聚合视图。"""
        diagnosis = self.draft.diagnose(registry) if self.draft else {"ok": True, "issues": []}
        indexed = warehouse.list()
        return {
            "platform": {
                "name": self.settings.get("platform.name", "Agent 平台"),
                "locale": self.settings.get("platform.locale", "zh-CN"),
                "timezone": self.settings.get("platform.timezone", ""),
                "version": PLATFORM_VERSION,
                "started_at": self.started_at,
                "uptime_seconds": round(time.time() - self.started_at, 1),
            },
            "health": {
                "status": "ok" if self.pipeline is not None else "down",
                "pipeline": self.pipeline.name if self.pipeline else None,
                "contract_ok": diagnosis["ok"],
                "dirty": self.dirty,
                "loaded_modules": len(self.loaded_modules),
            },
            "counts": {
                "agents": len(self.draft.agents) if self.draft else 0,
                "steps": sum(len(a.steps) for a in self.draft.agents) if self.draft else 0,
                "skills": len(registry.names()),
                "skills_total": len(registry.all_names()),
                "disabled_skills": len(registry.disabled()),
                "slots": len(registry.slots()),
                "profiles": len(self.list_profiles()),
                "indexed_runs": len(indexed),
                "indexed_chunks": sum(int(r.get("size") or 0) for r in indexed),
            },
            "diagnosis": diagnosis,
            "runs": self.history.stats(),
            "recent_runs": self.history.list(limit=8),
            "slots": registry.slots(),
            "storage": {
                "database": self.db.info() if self.db else None,
                "documents": self.documents.stats(),
                "audit": int(audit_stats(self.db).get("total", 0) or 0),
            },
        }


runtime = RuntimeState()

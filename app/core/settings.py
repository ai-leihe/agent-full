"""平台基础设置中心：Schema 驱动 + 数据库持久化 + 密钥掩码。

设计要点
--------
1. **Schema 驱动**：所有可配置项集中声明在 ``SETTINGS_SCHEMA``，默认值由 Schema 推导。
   前端直接按 Schema 渲染表单 —— 新增配置项不需要改一行前端代码。
2. **持久化落库**：Schema 内的分组以一份 JSON 存在 ``kv`` 表的 ``settings`` 键下；
   模型供应商拆到 ``providers`` 表（便于按类型检索、单独审计），
   ``api_key`` 一律加密后入库，不再有明文密钥文件。
3. **密钥掩码**：读出时统一掩码，只有显式传入非掩码值才会真正覆盖，
   避免前端把 ``sk-***`` 回写导致密钥被覆盖成掩码串。
4. **设置必须真正生效**：上传限制、检索条数、插件启停等都由运行时读取本模块的值，
   拒绝做「只存不读」的装饰性配置。
"""

from __future__ import annotations

import copy
import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .db import Database, from_json, get_database, secret_box, to_json

#: schema 内的设置在 ``kv`` 表中的键名
SETTINGS_KEY = "settings"

#: 密钥掩码占位符
MASK = "••••••"

#: 设置 Schema：分组 → 字段描述。默认值全部从 ``default`` 推导。
SETTINGS_SCHEMA: dict[str, dict[str, Any]] = {
    "platform": {
        "label": "平台信息",
        "description": "工作台展示用的基础标识",
        "fields": {
            "name": {"type": "str", "default": "Agent 平台", "label": "平台名称"},
            "locale": {
                "type": "str",
                "default": "zh-CN",
                "label": "界面语言",
                "choices": ["zh-CN", "en-US"],
            },
            "timezone": {"type": "str", "default": "Asia/Shanghai", "label": "时区"},
        },
    },
    "upload": {
        "label": "上传与解析",
        "description": "控制文件入口的准入规则，超限会在接口层直接拒绝",
        "fields": {
            "max_mb": {"type": "int", "default": 20, "label": "单文件上限 (MB)", "min": 1, "max": 500},
            "allowed_extensions": {
                "type": "list",
                "default": [".txt", ".md", ".markdown", ".csv", ".json", ".log", ".xml", ".html", ".pdf", ".docx"],
                "label": "允许的扩展名",
                "help": "逗号分隔；留空表示不限制",
            },
            "max_chunks_returned": {
                "type": "int",
                "default": 50,
                "label": "接口返回切片上限",
                "min": 1,
                "max": 500,
                "help": "仅影响接口响应体积，不影响入库",
            },
        },
    },
    "runtime": {
        "label": "运行时行为",
        "description": "流水线执行与热重载相关",
        "fields": {
            "max_cached_runs": {
                "type": "int",
                "default": 200,
                "label": "内存中保留的运行数",
                "min": 10,
                "max": 2000,
            },
            "enable_draft_trial": {
                "type": "bool",
                "default": True,
                "label": "允许草稿试跑",
                "help": "关闭后 /api/upload?use=draft 会被拒绝",
            },
            "auto_apply_profile": {
                "type": "str",
                "default": "",
                "label": "启动自动加载方案",
                "help": "留空则使用 config/pipeline.yaml；填写方案名则启动即套用并生效",
            },
        },
    },
    "retrieval": {
        "label": "检索默认值",
        "description": "检索接口未显式传参时使用的默认值",
        "fields": {
            "default_top_k": {"type": "int", "default": 5, "label": "默认返回条数", "min": 1, "max": 50},
            "score_threshold": {
                "type": "float",
                "default": 0.0,
                "label": "相似度下限",
                "min": 0.0,
                "max": 1.0,
                "help": "低于该分数的命中会被过滤",
            },
        },
    },
    "observability": {
        "label": "可观测性",
        "description": "运行记录与轨迹的留存策略",
        "fields": {
            "persist_runs": {
                "type": "bool",
                "default": True,
                "label": "持久化运行记录",
                "help": "写入数据库 runs 表，重启后可回看，多实例也能共享",
            },
            "run_history_limit": {
                "type": "int",
                "default": 200,
                "label": "历史记录条数上限",
                "min": 10,
                "max": 1000,
            },
        },
    },
    "auth": {
        "label": "登录与安全",
        "description": "访问门槛与会话策略；关闭「登录校验」后接口对匿名请求开放，仅建议本地调试时使用",
        "fields": {
            "require_login": {
                "type": "bool",
                "default": True,
                "label": "启用登录校验",
                "help": "开启后，除健康检查与登录注册外的接口都必须携带有效令牌",
            },
            "allow_registration": {
                "type": "bool",
                "default": True,
                "label": "允许自助注册",
                "help": "关闭后只能由管理员在个人中心创建账号",
            },
            "session_hours": {
                "type": "int",
                "default": 72,
                "label": "会话有效期（小时）",
                "min": 1,
                "max": 720,
                "help": "勾选「记住我」登录时至少保留 30 天",
            },
            "docs_access": {
                "type": "str",
                "default": "member",
                "label": "接口文档访问角色",
                "choices": ["member", "admin", "public"],
                "choice_labels": {
                    "member": "所有登录用户",
                    "admin": "仅管理员",
                    "public": "匿名公开",
                },
                "help": "控制 /docs、/redoc、/openapi.json 的可见范围；选「匿名公开」时"
                        "即使开启登录校验也允许匿名查看",
            },
        },
    },
    "script": {
        "label": "脚本验证",
        "description": "技能库的「脚本验证」会在本机真实执行 Python / Shell 代码，"
                       "等同于给使用者一个终端；对外部署前请确认这里的每一项",
        "fields": {
            "enabled": {
                "type": "bool",
                "default": True,
                "label": "启用脚本验证",
                "help": "关闭后 /api/scripts/* 全部返回 403",
            },
            "admin_only": {
                "type": "bool",
                "default": False,
                "label": "仅管理员可执行",
                "help": "开启后普通账号无法执行；身份无法确认的匿名请求也一律拒绝",
            },
            "timeout_sec": {
                "type": "int",
                "default": 15,
                "label": "单次执行超时（秒）",
                "min": 1,
                "max": 120,
                "help": "超时会强杀整个进程组，避免子进程变孤儿",
            },
            "max_output_kb": {
                "type": "int",
                "default": 64,
                "label": "输出上限 (KB)",
                "min": 1,
                "max": 1024,
                "help": "stdout / stderr 各自截断，超出部分丢弃并标记",
            },
            "max_script_kb": {
                "type": "int",
                "default": 64,
                "label": "脚本体积上限 (KB)",
                "min": 1,
                "max": 1024,
                "help": "超过上限直接拒绝，不会落盘执行",
            },
        },
    },
    "plugins": {
        "label": "插件开关",
        "description": "被停用的插件不会出现在编排下拉框中，已有的编排会立即显示契约断裂",
        "fields": {
            "disabled": {"type": "list", "default": [], "label": "已停用插件", "help": "由技能库页面管理"},
        },
    },
}


# ---------------------------------------------------------------- 默认值


def default_settings() -> dict[str, Any]:
    """按 Schema 推导一份完整默认设置，并带上 Schema 外的动态分组。"""
    settings = {
        group: {key: copy.deepcopy(spec.get("default")) for key, spec in schema["fields"].items()}
        for group, schema in SETTINGS_SCHEMA.items()
    }
    settings["providers"] = []
    return settings


# ---------------------------------------------------------------- 模型供应商


PROVIDER_KINDS = ["openai", "azure_openai", "ollama", "custom"]


def default_provider() -> dict[str, Any]:
    return {
        "id": "",
        "name": "",
        "kind": "openai",
        "base_url": "",
        "api_key": "",
        "models": [],
        "enabled": True,
    }


def validate_provider(raw: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
    """校验单个模型供应商条目。"""
    errors: list[str] = []
    provider = default_provider()
    provider.update({k: raw[k] for k in provider if k in (raw or {})})

    provider["id"] = str(provider["id"] or "").strip()
    provider["name"] = str(provider["name"] or "").strip()
    if not provider["name"]:
        errors.append("供应商名称不能为空")

    provider["kind"] = str(provider["kind"] or "openai")
    if provider["kind"] not in PROVIDER_KINDS:
        errors.append(f"供应商类型必须是 {PROVIDER_KINDS} 之一")

    provider["base_url"] = str(provider["base_url"] or "").strip()
    if provider["base_url"] and not provider["base_url"].startswith(("http://", "https://")):
        errors.append("Base URL 必须以 http:// 或 https:// 开头")

    models = provider["models"]
    if isinstance(models, str):
        models = [m.strip() for m in models.split(",") if m.strip()]
    if not isinstance(models, list):
        errors.append("模型列表格式不正确")
        models = []
    provider["models"] = [str(m) for m in models]

    provider["api_key"] = str(provider["api_key"] or "").strip()
    provider["enabled"] = bool(provider["enabled"])

    return (None, errors) if errors else (provider, [])


def mask_provider(provider: dict[str, Any]) -> dict[str, Any]:
    """对外输出时掩码密钥，并附带 ``has_key`` 便于前端展示。"""
    data = copy.deepcopy(provider)
    key = data.get("api_key", "")
    data["has_key"] = bool(key)
    data["api_key"] = mask_secret(key)
    return data


def merge_provider(existing: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """更新供应商时，若前端回传的是掩码值则保留原密钥。"""
    merged = {**existing, **{k: v for k, v in (patch or {}).items() if v is not None}}
    incoming = merged.get("api_key", "")
    if is_masked(str(incoming)) or incoming == "":
        merged["api_key"] = existing.get("api_key", "")
    return merged


# ---------------------------------------------------------------- 深合并 / 校验


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """把 patch 合并进 base 的副本。

    Schema 内的分组按字段白名单合并；Schema 外的顶层键（如 ``providers``）
    原样保留，避免被设置更新顺手抹掉。
    """
    result = copy.deepcopy(base)
    for group, values in (patch or {}).items():
        if group not in SETTINGS_SCHEMA or not isinstance(values, dict):
            continue
        for key, value in values.items():
            if key in SETTINGS_SCHEMA[group]["fields"]:
                result[group][key] = value
    return result


def _coerce(value: Any, spec: dict[str, Any]) -> tuple[Any, str | None]:
    """按字段描述做类型/取值校验，返回 (规整后的值, 错误信息)。"""
    kind = spec.get("type", "str")

    if kind == "bool":
        if isinstance(value, bool):
            return value, None
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on"), None
        return bool(value), None

    if kind == "int":
        try:
            number = int(value)
        except (TypeError, ValueError):
            return None, f"{spec.get('label', '')} 需要整数"
        if "min" in spec and number < spec["min"]:
            return None, f"{spec.get('label', '')} 不能小于 {spec['min']}"
        if "max" in spec and number > spec["max"]:
            return None, f"{spec.get('label', '')} 不能大于 {spec['max']}"
        return number, None

    if kind == "float":
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None, f"{spec.get('label', '')} 需要数字"
        if "min" in spec and number < spec["min"]:
            return None, f"{spec.get('label', '')} 不能小于 {spec['min']}"
        if "max" in spec and number > spec["max"]:
            return None, f"{spec.get('label', '')} 不能大于 {spec['max']}"
        return number, None

    if kind == "list":
        if isinstance(value, str):
            value = [item.strip() for item in value.split(",") if item.strip()]
        if not isinstance(value, list):
            return None, f"{spec.get('label', '')} 需要列表"
        return [str(item) for item in value], None

    # str
    text = "" if value is None else str(value)
    choices = spec.get("choices")
    if choices and text not in choices:
        return None, f"{spec.get('label', '')} 取值必须是 {choices} 之一"
    return text, None


def validate_settings(candidate: dict[str, Any], base: dict[str, Any] | None = None) -> tuple[dict[str, Any], list[str]]:
    """校验并规整一份完整设置，返回 (设置, 错误列表)。"""
    merged = _deep_merge(base or default_settings(), candidate)
    errors: list[str] = []
    for group, schema in SETTINGS_SCHEMA.items():
        for key, spec in schema["fields"].items():
            value, error = _coerce(merged[group][key], spec)
            if error:
                errors.append(error)
            else:
                merged[group][key] = value
    return merged, errors


def get_path(settings: dict[str, Any], path: str, default: Any = None) -> Any:
    """按 ``"upload.max_mb"`` 形式取值。"""
    node: Any = settings
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


# ---------------------------------------------------------------- 密钥掩码


def mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return MASK
    return f"{value[:4]}{MASK}{value[-4:]}"


def is_masked(value: str) -> bool:
    return MASK in (value or "")


# ---------------------------------------------------------------- 持久化


class SettingsStore:
    """平台设置的读写封装（``kv`` 表 + ``providers`` 表）。

    ``path`` 只用于首次启动时迁移历史 ``settings.json``。
    """

    def __init__(self, path: str | Path | None = None, *, db: Database | None = None) -> None:
        self.path = Path(path) if path else None
        self._db = db
        self._lock = threading.Lock()
        self._data: dict[str, Any] = default_settings()

    # -------------------------------------------------- 装配

    def attach(self, db: Database, path: str | Path | None = None) -> None:
        self._db = db
        if path is not None:
            self.path = Path(path)

    @property
    def db(self) -> Database:
        return self._database()

    def _database(self) -> Database:
        """未绑定数据库时，按 ``path`` 所在目录派生一个 SQLite 库。"""
        if self._db is None:
            base = self.path.parent if self.path else None
            self._db = get_database(None, base_dir=base)
        return self._db

    # -------------------------------------------------- 读写

    def load(self) -> dict[str, Any]:
        raw = self._database().kv_get(SETTINGS_KEY)
        raw = raw if isinstance(raw, dict) else {}

        # Schema 外的顶层键原样保留，避免被 Schema 校验抹掉
        extras = {k: v for k, v in raw.items() if k not in SETTINGS_SCHEMA}
        merged, errors = validate_settings(raw, base={**default_settings(), **extras})
        if errors:
            # 坏字段回退到默认值，而不是让整个平台起不来
            merged = {**_deep_merge(default_settings(), _strip_invalid(raw)), **extras}
        merged["providers"] = self._load_providers()
        self._data = merged
        return self._data

    def migrate_legacy(self) -> int:
        """把历史 ``settings.json`` 导入数据库；已迁移或文件不存在时什么都不做。"""
        db = self._database()
        if db.kv_get(SETTINGS_KEY) is not None or not (self.path and self.path.is_file()):
            return 0
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return 0
        if not isinstance(raw, dict):
            return 0

        extras = {k: v for k, v in raw.items() if k not in SETTINGS_SCHEMA}
        merged, errors = validate_settings(raw, base={**default_settings(), **extras})
        if errors:
            merged = {**_deep_merge(default_settings(), _strip_invalid(raw)), **extras}
        merged.pop("providers", None)
        db.kv_set(SETTINGS_KEY, merged)

        providers: list[dict[str, Any]] = []
        for item in raw.get("providers") or []:
            if not isinstance(item, dict):
                continue
            validated, _ = validate_provider(item)
            if validated is not None:
                providers.append(validated)
        if providers:
            self._save_providers(providers)
        return len(providers)

    def save(self, settings: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._data = copy.deepcopy(settings)
            db = self._database()
            body = {k: v for k, v in self._data.items() if k != "providers"}
            db.kv_set(SETTINGS_KEY, body)
            self._save_providers(self._data.get("providers") or [])
        return self._data

    # -------------------------------------------------- 供应商表

    def _load_providers(self) -> list[dict[str, Any]]:
        db = self._database()
        box = secret_box(db)
        rows = db.query("SELECT * FROM providers ORDER BY position, created_at")
        return [
            {
                "id": row["id"],
                "name": row.get("name") or "",
                "kind": row.get("kind") or "openai",
                "base_url": row.get("base_url") or "",
                "api_key": box.decrypt(row.get("api_key") or ""),
                "models": from_json(row.get("models"), []) or [],
                "enabled": bool(row.get("enabled")),
            }
            for row in rows
        ]

    def _save_providers(self, providers: list[dict[str, Any]]) -> None:
        """整体同步供应商表：先按位置 upsert，再删掉已移除的条目。"""
        db = self._database()
        box = secret_box(db)
        now = time.time()
        alive: list[str] = []
        with db.transaction() as tx:
            for position, provider in enumerate(providers):
                provider_id = str(provider.get("id") or "").strip() or uuid.uuid4().hex[:8]
                provider["id"] = provider_id
                alive.append(provider_id)
                payload = {
                    "name": str(provider.get("name") or "")[:128],
                    "kind": str(provider.get("kind") or "openai"),
                    "base_url": str(provider.get("base_url") or "")[:255],
                    "api_key": box.encrypt(str(provider.get("api_key") or "")),
                    "models": to_json(provider.get("models") or []),
                    "enabled": bool(provider.get("enabled", True)),
                    "position": position,
                    "updated_at": now,
                }
                if not tx.exists("providers", {"id": provider_id}):
                    payload["created_at"] = now
                tx.upsert("providers", {"id": provider_id}, payload)

            for row in tx.query("SELECT id FROM providers"):
                if row["id"] not in alive:
                    tx.delete("providers", {"id": row["id"]})

    # -------------------------------------------------- 变更

    def patch(self, partial: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
        """局部更新；校验失败时不落盘，返回错误列表。"""
        merged, errors = validate_settings(partial, self._data)
        if errors:
            return self._data, errors
        return self.save(merged), []

    def reset(self) -> dict[str, Any]:
        return self.save(default_settings())

    @property
    def data(self) -> dict[str, Any]:
        return self._data

    def get(self, path: str, default: Any = None) -> Any:
        return get_path(self._data, path, default)

    # -------------------------------------------------- 模型供应商

    def list_providers(self, *, masked: bool = True) -> list[dict[str, Any]]:
        items = self._data.get("providers", [])
        return [mask_provider(p) if masked else copy.deepcopy(p) for p in items]

    def get_provider(self, provider_id: str) -> dict[str, Any] | None:
        return next((p for p in self._data.get("providers", []) if p["id"] == provider_id), None)

    def upsert_provider(self, payload: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
        provider_id = str((payload or {}).get("id") or "").strip()
        providers = copy.deepcopy(self._data.get("providers", []))

        existing = next((p for p in providers if p["id"] == provider_id), None) if provider_id else None
        candidate = merge_provider(existing, payload) if existing else dict(payload or {})
        validated, errors = validate_provider(candidate)
        if validated is None:
            return {}, errors

        if existing is None:
            validated["id"] = provider_id or uuid.uuid4().hex[:8]
            providers.append(validated)
        else:
            providers[providers.index(existing)] = validated

        self.save({**self._data, "providers": providers})
        return mask_provider(validated), []

    def delete_provider(self, provider_id: str) -> bool:
        providers = self._data.get("providers", [])
        remaining = [p for p in providers if p["id"] != provider_id]
        if len(remaining) == len(providers):
            return False
        self.save({**self._data, "providers": remaining})
        return True


def _strip_invalid(raw: dict[str, Any]) -> dict[str, Any]:
    """丢弃类型非法的字段，让其余合法设置仍能生效。"""
    cleaned: dict[str, Any] = {}
    for group, schema in SETTINGS_SCHEMA.items():
        values = raw.get(group)
        if not isinstance(values, dict):
            continue
        kept = {}
        for key, spec in schema["fields"].items():
            if key in values and _coerce(values[key], spec)[1] is None:
                kept[key] = values[key]
        cleaned[group] = kept
    return cleaned

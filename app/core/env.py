"""公共配置的环境变量管理：加载 ``.env``，并把 ``${VAR}`` 展开进插件参数。

为什么需要它
------------
数据库连接串、API Key 这类「跨环境不同 + 敏感」的配置，不应该在
``config/pipeline.yaml`` 和 ``config/orchestrations/*.yaml`` 里各写一份。
本模块只做两件事：

1. :func:`load_dotenv` / :func:`ensure_dotenv` —— 启动（或首次展开）时把项目根目录的
   ``.env`` 读进 ``os.environ``；
2. :func:`expand_env_tree` —— 把配置对象里的 ``${VAR}`` 占位符展开为真实值。

展开发生在哪一步（关键设计）
----------------------------
只发生在 :meth:`app.core.skill.SkillRegistry.create` 里，而且是在 **options 的副本**
上展开。编排草稿持有的始终是 ``${MYSQL_DSN}`` 原文，因此：

* 工作台「保存方案」不会把密钥回写进 YAML；
* ``GET /api/orchestration`` 不会把密钥吐给前端；
* 插件运行时拿到的却已是解析后的真实连接串。

设计取舍
--------
* **零依赖**：标准库实现，不引入 ``python-dotenv``，保持开箱即跑。
* **真实环境变量优先**：``os.environ`` 中已存在的键不会被 ``.env`` 覆盖，
  便于生产环境用系统环境变量 / CI 注入覆盖本地默认值。
* **只认 ``${VAR}``**：不支持 ``$VAR`` 裸写，避免口令或连接串里的 ``$`` 被误判成变量引用。
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Callable, Mapping

#: 仅识别 ``${VAR}``，避免把口令里的裸 ``$`` 当成变量引用
_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

#: 项目根目录（app/core/env.py → app/core → app → 项目根）
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

#: 默认的 .env 位置
DEFAULT_ENV_FILE = PROJECT_ROOT / ".env"


class EnvVarError(ValueError):
    """配置引用了未定义（或循环引用）的环境变量。"""


# ---------------------------------------------------------------- 解析


def _unquote(value: str) -> str:
    """去掉包裹的引号；未加引号时允许 `` # 行尾注释``。"""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    head, marker, _ = value.partition(" #")
    return (head if marker else value).rstrip()


def parse_env_text(text: str) -> dict[str, str]:
    """把 ``.env`` 文本解析为键值对（此阶段不做变量展开）。"""
    items: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not key:
            continue
        items[key] = _unquote(value.strip())
    return items


def interpolate(value: str, lookup: Callable[[str], str | None], *, source: str = "") -> str:
    """把字符串里的 ``${VAR}`` 替换为 ``lookup`` 提供的值；缺失则抛异常。"""

    def _replace(match: re.Match[str]) -> str:
        name = match.group(1)
        resolved = lookup(name)
        if resolved is None:
            where = f"（来自 {source}）" if source else ""
            raise EnvVarError(
                f"环境变量 '{name}' 未定义{where}：请在 .env 中补上该键，"
                f"或用系统环境变量 / CI 注入"
            )
        return resolved

    return _VAR_RE.sub(_replace, value)


def resolve_env_items(
    items: dict[str, str],
    *,
    source: str = "",
    fallback: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """解析 ``.env`` 条目，支持条目之间互相引用，且与书写顺序无关。

    反复轮询直到没有新的条目可解析：能解出来的先解，剩下的下一轮再试。
    一轮下来毫无进展，说明存在未定义变量或循环引用，直接报错而不是静默留个占位符。
    """
    env = fallback if fallback is not None else os.environ
    resolved: dict[str, str] = {}
    pending = dict(items)

    while pending:
        progressed = False
        for key in list(pending):
            def lookup(name: str, _resolved: dict[str, str] = resolved, _env: Mapping[str, str] = env) -> str | None:
                if name in _resolved:
                    return _resolved[name]
                return _env.get(name)

            try:
                resolved[key] = interpolate(pending[key], lookup, source=source)
            except EnvVarError:
                continue  # 引用了尚未解析的同文件变量，下一轮再试
            del pending[key]
            progressed = True
        if not progressed:
            break

    if pending:
        first = next(iter(pending))
        where = f"（来自 {source}）" if source else ""
        raise EnvVarError(
            f"环境变量 '{first}' 无法解析{where}："
            f"它引用了未定义的变量，或存在循环引用"
        )
    return resolved


def load_dotenv(path: str | Path | None = None, *, override: bool = False) -> dict[str, str]:
    """把 ``.env`` 读进 ``os.environ``，返回本次实际写入的键值对。

    ``override=False``（默认）时 ``os.environ`` 里已有的键不会被覆盖——便于用
    系统环境变量临时覆盖本地默认值。文件不存在则直接返回空字典，生产环境
    完全可以只用系统环境变量。
    """
    env_path = Path(path) if path else DEFAULT_ENV_FILE
    if not env_path.is_file():
        return {}

    items = parse_env_text(env_path.read_text(encoding="utf-8"))
    resolved = resolve_env_items(items, source=str(env_path))

    loaded: dict[str, str] = {}
    for key, value in resolved.items():
        if not override and key in os.environ:
            continue
        os.environ[key] = value
        loaded[key] = value
    return loaded


#: ``ensure_dotenv`` 是否已经跑过（幂等标记）
_DOTENV_LOADED = False


def ensure_dotenv(path: str | Path | None = None, *, override: bool = False) -> dict[str, str]:
    """幂等地加载 ``.env``：首次调用真正读文件，之后直接返回空字典。

    服务入口 ``app.main.create_app()`` 启动时已经调过 :func:`load_dotenv`；本函数是
    给「不经服务入口」的调用方（单测、脚本、CI 里直接跑流水线）兜底的——只要有人
    要展开 ``${VAR}``，就先确保 ``.env`` 已进 ``os.environ``。显式传入 ``path`` 时
    强制按该文件重新加载一次（便于测试指向临时文件）。
    """
    global _DOTENV_LOADED
    if _DOTENV_LOADED and path is None:
        return {}
    loaded = load_dotenv(path, override=override)
    _DOTENV_LOADED = True
    return loaded


# ---------------------------------------------------------------- 展开


def expand_env_tree(value: Any, *, source: str = "", env: Mapping[str, str] | None = None) -> Any:
    """递归展开配置对象里的 ``${VAR}``。

    字典/列表一律返回**新对象**，调用方持有的原始配置不受影响——这正是
    「密钥不回写 YAML、不外泄接口」的前提。缺失变量抛 :class:`EnvVarError`。

    ``env=None``（默认，即用真实环境）时会先 :func:`ensure_dotenv`，因此无论进程是
    经 ``app.main`` 启动的，还是被单测 / 脚本直接引入的，``.env`` 里的值都能解析到。
    """
    if env is None:
        ensure_dotenv()
    lookup = (env if env is not None else os.environ).get
    if isinstance(value, str):
        return interpolate(value, lookup, source=source)
    if isinstance(value, dict):
        return {key: expand_env_tree(item, source=source, env=env) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [expand_env_tree(item, source=source, env=env) for item in value]
    return value


__all__ = [
    "DEFAULT_ENV_FILE",
    "PROJECT_ROOT",
    "EnvVarError",
    "ensure_dotenv",
    "expand_env_tree",
    "interpolate",
    "load_dotenv",
    "parse_env_text",
    "resolve_env_items",
]

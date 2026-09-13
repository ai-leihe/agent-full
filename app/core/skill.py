"""Skill 插件协议与注册中心（插拔式的核心）。

插件即能力。一个 Skill 是一段可以独立开发、独立测试、独立替换的最小能力单元。
它只做三件事：

1. 声明元信息：``slot`` / ``name`` / ``consumes`` / ``produces``
2. 从 :class:`PipelineContext` 读取上游产物
3. 把本环节产物写回 :class:`PipelineContext`

关于槽位（slot）
----------------
槽位是「能力接口」而非「具体实现」。例如清洗环节的槽位是 ``cleaner``，
它可以由 ``basic_cleaner`` / ``markdown_normalizer`` / 任意第三方实现填充。
流水线配置只声明槽位，因此替换实现无需改动任何业务代码。
"""

from __future__ import annotations

import copy
import importlib
import importlib.util
import pkgutil
import sys
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Iterable

from .context import PipelineContext
from .env import expand_env_tree


class Skill(ABC):
    """所有插件的基类。"""

    name: str = ""
    version: str = "1.0.0"
    slot: str = "generic"
    description: str = ""
    #: 本插件需要上游提供的产物类型
    consumes: tuple[str, ...] = ()
    #: 本插件产出的产物类型
    produces: tuple[str, ...] = ()
    #: 是否为可选插件（未安装依赖时应跳过而非报错）
    optional: bool = False
    #: 参数表单描述，供前端编排工作台自动生成配置表单。
    #: 形如 ``{"chunk_size": {"type": "int", "default": 500, "label": "切片长度", "min": 1}}``
    #: 支持的 type：int / float / str / bool / list；str 可附带 choices 变成下拉框。
    param_schema: dict[str, dict[str, Any]] = {}

    #: 实例化时传入的**展开前**参数原文（可能含 ``${VAR}``），由注册中心填写。
    #: :attr:`options` 是展开后的真实值，仅供执行使用；要把实例反推回编排草稿
    #: （``Orchestration.from_pipeline``）时必须用这一份，否则会把 .env 里的
    #: 地址 / 口令写回方案文件。
    raw_options: dict[str, Any] = {}

    def __init__(self, **options: Any) -> None:
        self.options: dict[str, Any] = dict(options)
        self.configure(self.options)

    # -------------------------------------------------- 生命周期钩子

    def configure(self, options: dict[str, Any]) -> None:
        """子类可覆盖：校验并归一化配置项。"""

    @abstractmethod
    def run(self, ctx: PipelineContext) -> None:
        """执行插件逻辑，从 ctx 读入上游产物并写回本环节产物。"""

    def close(self) -> None:
        """资源释放钩子，可选。"""

    # -------------------------------------------------- 元信息

    @classmethod
    def default_options(cls) -> dict[str, Any]:
        """按 param_schema 生成默认参数，便于前端「一键添加」。

        逐项深拷贝：``param_schema`` 是类级共享状态，像 ``["a", "b"]`` 这样的
        可变默认值若直接交出引用，任何一次就近修改（``opts["words"].append(...)``）
        都会污染此后所有实例的默认值，且这种污染跨请求、跨用户。
        """
        return {
            key: copy.deepcopy(spec["default"])
            for key, spec in cls.param_schema.items()
            if "default" in spec
        }

    @classmethod
    def manifest(cls) -> dict[str, Any]:
        return {
            "name": cls.name,
            "version": cls.version,
            "slot": cls.slot,
            "description": cls.description,
            "consumes": list(cls.consumes),
            "produces": list(cls.produces),
            "optional": cls.optional,
            # 与 default_options 同理：不把类级的可变 schema 直接交出去
            "params": copy.deepcopy(cls.param_schema),
            "defaults": cls.default_options(),
        }

    def __repr__(self) -> str:  # pragma: no cover
        return f"<{type(self).__name__} name={self.name!r} slot={self.slot!r}>"


class SkillRegistry:
    """插件注册中心，支持内置包扫描与外部目录热插拔。"""

    def __init__(self) -> None:
        self._skills: dict[str, type[Skill]] = {}
        self._disabled: set[str] = set()
        self._origin: dict[str, str] = {}

    # -------------------------------------------------- 注册

    def register(self, skill_cls: type[Skill]) -> type[Skill]:
        if not (isinstance(skill_cls, type) and issubclass(skill_cls, Skill)):
            raise TypeError(
                f"只能注册 Skill 子类，收到 {skill_cls!r}：此类缺少 run/configure 契约，"
                f"放进来只会在实例化或流水线装配时才炸，错误离现场很远"
            )
        if not skill_cls.name:
            raise ValueError(f"插件 {skill_cls.__name__} 未声明 name")
        if skill_cls.name in self._skills:
            raise ValueError(f"插件名冲突：{skill_cls.name} 已被 {self._skills[skill_cls.name]} 占用")
        self._skills[skill_cls.name] = skill_cls
        return skill_cls

    def __call__(self, skill_cls: type[Skill]) -> type[Skill]:
        """支持 ``@registry`` 装饰器写法。"""
        return self.register(skill_cls)

    def reset(self) -> None:
        """清空注册表，用于热重载（会重新扫描并执行插件模块）。"""
        self._skills.clear()
        self._disabled.clear()
        self._origin.clear()

    def unregister(self, name: str) -> None:
        self._skills.pop(name, None)
        self._disabled.discard(name)
        self._origin.pop(name, None)

    # -------------------------------------------------- 启停

    def set_disabled(self, names: Iterable[str] | None) -> set[str]:
        """批量设置停用清单（只保留真实存在的插件名）。"""
        self._disabled = {n for n in (names or []) if n in self._skills}
        return set(self._disabled)

    def disable(self, name: str) -> bool:
        if name not in self._skills:
            raise KeyError(f"未找到插件 '{name}'")
        self._disabled.add(name)
        return True

    def enable(self, name: str) -> bool:
        self._disabled.discard(name)
        return True

    def is_disabled(self, name: str) -> bool:
        return name in self._disabled

    def disabled(self) -> list[str]:
        return sorted(self._disabled)

    # -------------------------------------------------- 查询

    def _not_found(self, name: str) -> KeyError:
        """统一的「插件不存在」错误，附上可用插件清单，便于定位拼写错误。"""
        return KeyError(f"未找到插件 '{name}'，可用插件：{sorted(self._skills)}")

    def get(self, name: str) -> type[Skill]:
        if name in self._disabled:
            raise KeyError(f"插件 '{name}' 已被停用，请在「技能库」中重新启用")
        skill_cls = self._skills.get(name)
        if skill_cls is None:
            raise self._not_found(name)
        return skill_cls

    def raw_get(self, name: str) -> type[Skill] | None:
        """无视启停状态取插件类（用于读取被停用插件的元信息）。"""
        return self._skills.get(name)

    def has(self, name: str) -> bool:
        """是否可用（存在且未停用）。"""
        return name in self._skills and name not in self._disabled

    def raw_has(self, name: str) -> bool:
        return name in self._skills

    def names(self) -> list[str]:
        """可用插件名。"""
        return sorted(n for n in self._skills if n not in self._disabled)

    def all_names(self) -> list[str]:
        return sorted(self._skills)

    def by_slot(self, slot: str) -> list[type[Skill]]:
        return [s for s in self._skills.values() if s.slot == slot and s.name not in self._disabled]

    def all_by_slot(self, slot: str) -> list[type[Skill]]:
        """无视启停状态，返回某槽位下的全部插件（技能库展示用）。"""
        return [s for s in self._skills.values() if s.slot == slot]

    def producers_of(self, artifact: str) -> list[type[Skill]]:
        """反查：哪些插件能生产指定产物（用于断裂点补全建议，自动排除已停用插件）。"""
        return [s for s in self._skills.values() if artifact in s.produces and s.name not in self._disabled]

    def consumers_of(self, artifact: str) -> list[type[Skill]]:
        """反查：哪些插件需要消费指定产物。"""
        return [s for s in self._skills.values() if artifact in s.consumes and s.name not in self._disabled]

    def slots(self) -> dict[str, list[str]]:
        result: dict[str, list[str]] = {}
        for s in self._skills.values():
            if s.name in self._disabled:
                continue
            result.setdefault(s.slot, []).append(s.name)
        return {k: sorted(v) for k, v in sorted(result.items())}

    def catalog(self) -> dict[str, Any]:
        """给前端编排工作台用的插件目录：槽位 → 插件清单（含参数表单）。

        ``slots`` 只含可用插件（供下拉框），``skills`` 含全部插件并带 ``enabled`` 标记
        （供技能库页面展示停用项）。
        """
        return {
            "slots": self.slots(),
            "skills": {m["name"]: m for m in self.manifests()},
            "disabled": self.disabled(),
        }

    def manifest_of(self, name: str) -> dict[str, Any]:
        # 用 raw_get 而非 get：被停用的插件也要能读出元信息（技能库要展示它），
        # 但「不存在」仍要走统一的友好报错，别把裸 KeyError('ghost') 抛给调用方。
        skill_cls = self.raw_get(name)
        if skill_cls is None:
            raise self._not_found(name)
        data = skill_cls.manifest()
        data["enabled"] = name not in self._disabled
        data["origin"] = self._origin.get(name, "builtin")
        return data

    def manifests(self) -> list[dict[str, Any]]:
        return [self.manifest_of(name) for name in self.all_names()]

    # -------------------------------------------------- 实例化

    def create(self, name: str, options: dict[str, Any] | None = None) -> Skill:
        """实例化插件，并把参数里的 ``${VAR}`` 展开为环境变量值。

        展开**只在副本上进行**：调用方（编排草稿 / YAML 配置）持有的原始
        ``options`` 始终保持 ``${MYSQL_DSN}`` 原文不变，因此密钥不会被
        「保存方案」回写进 YAML，也不会经 ``/api/orchestration`` 泄露给前端。
        实例上会额外记一份 :attr:`Skill.raw_options`，供
        :meth:`Orchestration.from_pipeline` 从生效流水线反推草稿时保留占位符原文。

        这里是整个平台唯一的插件实例化入口，所以也是唯一需要接环境变量的地方。
        """
        skill = self.get(name)(**expand_env_tree(options or {}))
        skill.raw_options = dict(options or {})
        return skill

    # -------------------------------------------------- 发现（插拔入口）

    def load_package(self, package: str) -> list[str]:
        """扫描并导入一个包下的所有模块，触发装饰器注册（支持重复调用热重载）。

        热重载时模块体会重新执行，装饰器会再次注册同名插件；若不清掉上一轮的
        注册，就会撞上 :meth:`register` 的名称冲突校验。因此每个模块重载前先
        摘除它上一轮注册的插件——``register`` 的冲突校验本身不放松，只是把
        「同一模块重新注册自己」和「另一个模块抢同名」这两种情况区分开。
        """
        module = importlib.import_module(package)
        loaded: list[str] = []
        for info in pkgutil.iter_modules(module.__path__, prefix=f"{package}."):
            self.unregister_module(info.name)
            existing = sys.modules.get(info.name)
            if existing is not None:
                importlib.reload(existing)
            else:
                importlib.import_module(info.name)
            loaded.append(info.name)
        self._mark_origin("builtin")
        return loaded

    def unregister_module(self, module_name: str) -> list[str]:
        """摘除某个模块上一轮注册的插件，返回被摘掉的插件名（热重载前置步骤）。

        只比对 ``__module__``：只有「同一个模块重新注册自己」才会被清掉，
        不同模块注册同名插件依旧会被 :meth:`register` 判为冲突，校验不打折。
        """
        dropped = [
            name
            for name, skill_cls in self._skills.items()
            if getattr(skill_cls, "__module__", "") == module_name
        ]
        for name in dropped:
            self.unregister(name)
        return dropped

    def _mark_origin(self, origin: str) -> None:
        """把尚未标记来源的插件标记为 origin（内置 / 外部目录）。"""
        for name in self._skills:
            self._origin.setdefault(name, origin)

    def load_directory(self, directory: str | Path) -> list[str]:
        """加载外部目录中的单文件插件（真正的运行时插拔）。

        只需把 ``*.py`` 丢进该目录，重启即生效，无需改动平台任何代码。
        """
        path = Path(directory)
        if not path.is_dir():
            return []
        loaded: list[str] = []
        for file in sorted(path.glob("*.py")):
            if file.name.startswith("_"):
                continue
            module_name = f"{path.name}.{file.stem}"
            # 同一文件被改动后再次扫描：先摘掉上一轮注册的插件，否则名称冲突
            self.unregister_module(module_name)
            spec = importlib.util.spec_from_file_location(module_name, file)
            if spec is None or spec.loader is None:
                continue
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
            loaded.append(module_name)
        self._mark_origin("ext")
        return loaded


#: 全局注册中心
registry = SkillRegistry()


def skill(cls: type[Skill]) -> type[Skill]:
    """把插件类注册进全局注册中心。"""
    return registry.register(cls)


__all__ = ["Skill", "SkillRegistry", "registry", "skill"]

"""Agent 平台内核。

对外暴露四个抽象：

* :class:`Skill` —— 最小能力单元（插件），声明槽位与产物契约
* :class:`Agent` —— 一组 Skill 的串联单元
* :class:`Pipeline` —— 多个 Agent 串联成的不可变执行体
* :class:`Orchestration` —— 可运行时编辑的编排草稿，可编译为 Pipeline

以及配套基础设施：

* :class:`PipelineContext` —— 贯穿全链的数据总线
* :class:`SettingsStore` —— 平台基础设置
* :class:`RunRegistry` —— 运行记录与可观测
"""

from .agent import Agent
from .context import Artifact, Chunk, ContractError, MissingArtifactError, PipelineContext, TraceRecord
from .orchestration import AgentSpec, Orchestration, StepSpec
from .orchestrator import Pipeline
from .runs import RunRecord, RunRegistry
from .settings import SETTINGS_SCHEMA, SettingsStore, default_settings
from .skill import Skill, SkillRegistry, registry, skill
from .warehouse import Warehouse, warehouse

__all__ = [
    "SETTINGS_SCHEMA",
    "Agent",
    "AgentSpec",
    "Artifact",
    "Chunk",
    "ContractError",
    "MissingArtifactError",
    "Orchestration",
    "Pipeline",
    "PipelineContext",
    "RunRecord",
    "RunRegistry",
    "SettingsStore",
    "Skill",
    "SkillRegistry",
    "StepSpec",
    "TraceRecord",
    "Warehouse",
    "default_settings",
    "registry",
    "skill",
    "warehouse",
]

"""运行时可编排：把「写死在 YAML 里的流水线」变成「可手动增删改的编排草稿」。

为什么需要它
------------
``config/pipeline.yaml`` 描述的是**启动时**的静态编排，改一次要动文件、重启。
而真实场景里，用户希望在界面上按阶段自由调整：

* 在这个阶段**加**一个功能（比如清洗后再补一道敏感词过滤）
* 把这一步的**实现换掉**（recursive_splitter → markdown_splitter）
* **插入一个新的 Agent 阶段**（比如向量化之前先做一次摘要）
* 调整参数后**立刻试跑**，满意再「应用生效」

因此引入 :class:`Orchestration` 数据结构，它与 :class:`Pipeline` 的区别是：

============  ====================  ==========================
              Orchestration         Pipeline
============  ====================  ==========================
性质          可变草稿（用户编辑）   不可变执行体
校验          ``diagnose()`` 软校验  ``validate()`` 硬校验
校验失败      返回断裂点 + 补全建议   直接抛 ContractError
持久化        YAML 方案存档          内存对象
============  ====================  ==========================

``diagnose()`` 是这套「手动灵活调整」体验的关键：它**从不抛异常**，而是把链路中
哪一步接不上、缺什么产物、该插哪个槽位的插件，全部结构化返回给前端实时渲染。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .agent import Agent
from .context import RAW_FILE, ContractError
from .orchestrator import Pipeline
from .skill import SkillRegistry, registry as default_registry


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


# =============================================================== 数据结构


@dataclass
class StepSpec:
    """一个可调整的功能步骤：选哪个插件、参数是什么、是否启用。"""

    id: str = field(default_factory=lambda: _new_id("step"))
    skill: str = ""
    options: dict[str, Any] = field(default_factory=dict)
    enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "skill": self.skill, "options": self.options, "enabled": self.enabled}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StepSpec":
        return cls(
            id=data.get("id") or _new_id("step"),
            skill=data.get("skill", ""),
            options=dict(data.get("options") or {}),
            enabled=bool(data.get("enabled", True)),
        )


@dataclass
class AgentSpec:
    """一个可调整的执行阶段。"""

    id: str = field(default_factory=lambda: _new_id("agent"))
    name: str = "agent"
    role: str = ""
    steps: list[StepSpec] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "role": self.role,
            "steps": [s.to_dict() for s in self.steps],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], index: int = 0) -> "AgentSpec":
        return cls(
            id=data.get("id") or _new_id("agent"),
            name=data.get("name") or f"agent_{index + 1}",
            role=data.get("role", ""),
            steps=[StepSpec.from_dict(s) for s in data.get("steps", [])],
        )


@dataclass
class Orchestration:
    """可手动调整的编排草稿。"""

    name: str = "custom"
    description: str = ""
    agents: list[AgentSpec] = field(default_factory=list)

    # -------------------------------------------------- 定位

    def find_agent(self, agent_id: str) -> AgentSpec | None:
        return next((a for a in self.agents if a.id == agent_id), None)

    def find_step(self, agent_id: str, step_id: str) -> tuple[AgentSpec, int, StepSpec] | None:
        agent = self.find_agent(agent_id)
        if agent is None:
            return None
        for index, step in enumerate(agent.steps):
            if step.id == step_id:
                return agent, index, step
        return None

    # -------------------------------------------------- 序列化

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "agents": [a.to_dict() for a in self.agents],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Orchestration":
        node = data.get("orchestration", data)
        return cls(
            name=node.get("name", "custom"),
            description=node.get("description", ""),
            agents=[AgentSpec.from_dict(a, i) for i, a in enumerate(node.get("agents", []))],
        )

    def clone(self) -> "Orchestration":
        return Orchestration.from_dict(self.to_dict())

    @classmethod
    def from_pipeline(cls, pipeline: Pipeline) -> "Orchestration":
        """从已生效的流水线反向生成草稿（用于「重置」）。

        参数取实例上的 ``raw_options``（展开前原文），这样 ``${MYSQL_DSN}`` /
        ``${MILVUS_URI}`` 这类占位符会原样回到草稿：工作台看到的是占位符，
        「保存方案」也不会把 .env 里的真实地址与口令固化进 YAML。
        """
        return cls(
            name=pipeline.name,
            description=pipeline.description,
            agents=[
                AgentSpec(
                    name=agent.name,
                    role=agent.role,
                    steps=[
                        StepSpec(skill=s.name, options=dict(s.raw_options or s.options))
                        for s in agent.skills
                    ],
                )
                for agent in pipeline.agents
            ],
        )

    def to_yaml(self) -> dict[str, Any]:
        return {"orchestration": self.to_dict()}

    # -------------------------------------------------- 软校验（核心）

    def diagnose(self, registry: SkillRegistry | None = None) -> dict[str, Any]:
        """静态推演整条链的产物流动，**不抛异常**，返回结构化诊断。

        对每个步骤给出：能否执行、缺什么产物、参数是否合法、是否覆盖了上游产物。
        对每个断裂点给出「该在哪个槽位补插件」的建议。
        """
        reg = registry or default_registry
        available: set[str] = {RAW_FILE}
        agent_reports: list[dict[str, Any]] = []
        issues: list[str] = []
        suggestions: list[str] = []
        blocking = False

        for spec in self.agents:
            step_reports: list[dict[str, Any]] = []
            agent_ok = True

            for step in spec.steps:
                report: dict[str, Any] = {
                    "id": step.id,
                    "skill": step.skill,
                    "enabled": step.enabled,
                    "ok": True,
                    "slot": None,
                    "description": "",
                    "consumes": [],
                    "produces": [],
                    "missing": [],
                    "overrides": [],
                    "options": step.options,
                    "issues": [],
                }

                if not step.enabled:
                    report["issues"].append("已停用，执行时会跳过")
                    step_reports.append(report)
                    continue

                if not step.skill:
                    report["ok"] = False
                    report["issues"].append("尚未选择插件")
                    issues.append(f"[{spec.name}] 存在未选择插件的步骤")
                    blocking = True
                    agent_ok = False
                    step_reports.append(report)
                    continue

                if not reg.has(step.skill):
                    report["ok"] = False
                    report["issues"].append(f"插件 '{step.skill}' 未注册或已被移除")
                    issues.append(f"[{spec.name}] 插件 '{step.skill}' 不存在")
                    blocking = True
                    agent_ok = False
                    step_reports.append(report)
                    continue

                skill_cls = reg.get(step.skill)
                report["slot"] = skill_cls.slot
                report["description"] = skill_cls.description
                report["consumes"] = list(skill_cls.consumes)
                report["produces"] = list(skill_cls.produces)

                # ① 参数合法性（复用插件自身的 configure 校验）
                try:
                    reg.create(step.skill, step.options)
                except Exception as exc:  # noqa: BLE001
                    report["ok"] = False
                    report["issues"].append(f"参数非法：{exc}")
                    issues.append(f"[{spec.name}/{skill_cls.name}] 参数非法：{exc}")
                    blocking = True
                    agent_ok = False

                # ② 上游产物契约
                missing = [kind for kind in skill_cls.consumes if kind not in available]
                report["missing"] = missing
                if missing:
                    report["ok"] = False
                    msg = f"[{spec.name}/{skill_cls.name}] 需要产物 {missing}，但上游没有"
                    report["issues"].append(msg)
                    issues.append(msg)
                    blocking = True
                    agent_ok = False
                    for kind in missing:
                        suggestions.append(self._suggest(reg, kind, spec))
                else:
                    # ③ 只有接得上才向下游传播产物
                    overrides = [kind for kind in skill_cls.produces if kind in available]
                    report["overrides"] = overrides
                    if overrides:
                        report["issues"].append(f"将会覆盖上游已有的产物 {overrides}")
                    available.update(skill_cls.produces)

                step_reports.append(report)

            if not spec.steps:
                # 空阶段执行时会被跳过，属于警示而非阻断——不应阻碍自动补全
                agent_ok = False
                issues.append(f"[{spec.name}] 该阶段没有任何步骤，执行时会被跳过")

            agent_reports.append(
                {
                    "id": spec.id,
                    "name": spec.name,
                    "role": spec.role,
                    "ok": agent_ok,
                    "steps": step_reports,
                }
            )

        return {
            "ok": not blocking,
            "name": self.name,
            "description": self.description,
            "start_artifacts": sorted({RAW_FILE}),
            "final_artifacts": sorted(available),
            "agents": agent_reports,
            "issues": issues,
            # 去重但保持顺序
            "suggestions": list(dict.fromkeys(suggestions)),
        }

    @staticmethod
    def _suggest(registry: SkillRegistry, kind: str, agent: AgentSpec) -> str:
        """为缺失的产物类型给出可落地的补全建议。"""
        providers = registry.producers_of(kind)
        if not providers:
            return f"缺少产物 '{kind}'，当前没有任何插件能生产它，需要新增插件"
        slots = sorted({cls.slot for cls in providers})
        names = " / ".join(cls.name for cls in providers)
        return (
            f"缺少产物 '{kind}'：请在「{agent.name}」这一步之前插入 "
            f"{'、'.join(slots)} 槽位的插件（{names}）"
        )

    # -------------------------------------------------- 自动补全

    def autofill(self, registry: SkillRegistry | None = None, max_rounds: int = 8) -> list[dict[str, str]]:
        """按契约诊断自动补齐断裂环节，返回实际插入的步骤清单。

        每轮只修一处断裂并重新诊断，最多 ``max_rounds`` 轮；若某个缺失产物
        没有任何插件能生产，则停止（保留在诊断结果里交给用户处理）。
        """
        reg = registry or default_registry
        inserted: list[dict[str, str]] = []

        for _ in range(max_rounds):
            diagnosis = self.diagnose(reg)
            if diagnosis["ok"]:
                break

            fixed = False
            for agent_report in diagnosis["agents"]:
                agent = self.find_agent(agent_report["id"])
                if agent is None:
                    continue
                for position, step_report in enumerate(agent_report["steps"]):
                    missing = step_report["missing"]
                    if not missing:
                        continue
                    providers = reg.producers_of(missing[0])
                    if not providers:
                        continue
                    chosen = providers[0]
                    agent.steps.insert(
                        position, StepSpec(skill=chosen.name, options=chosen.default_options())
                    )
                    inserted.append(
                        {"agent": agent.name, "skill": chosen.name, "produces": missing[0]}
                    )
                    fixed = True
                    break
                if fixed:
                    break
            if not fixed:
                break

        return inserted

    # -------------------------------------------------- 编译（硬校验）

    def compile(self, registry: SkillRegistry | None = None) -> Pipeline:
        """把草稿编译为可执行的 Pipeline；契约不通过时抛 ContractError。"""
        reg = registry or default_registry
        agents: list[Agent] = []

        for spec in self.agents:
            skills = []
            for step in spec.steps:
                if not step.enabled or not step.skill:
                    continue
                if not reg.has(step.skill):
                    raise ContractError(f"[{spec.name}] 插件 '{step.skill}' 未注册")
                skills.append(reg.create(step.skill, step.options))
            if skills:
                agents.append(Agent(name=spec.name, role=spec.role, skills=skills))

        if not agents:
            raise ContractError("编排为空：至少需要一个启用的步骤才能编译")

        pipeline = Pipeline(name=self.name, description=self.description, agents=agents)
        pipeline.validate()  # 硬校验，失败直接抛错
        return pipeline


# =============================================================== 方案存档


def load_orchestration(path: str | Path) -> Orchestration:
    path = Path(path)
    with path.open("r", encoding="utf-8") as fp:
        data = yaml.safe_load(fp) or {}
    return Orchestration.from_dict(data)


def save_orchestration(orch: Orchestration, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fp:
        yaml.safe_dump(orch.to_yaml(), fp, allow_unicode=True, sort_keys=False)
    return path

"""Agent：一组 Skill 的编排单元。

Agent 负责「串联」——按声明顺序执行自己持有的插件，并在每一步前后做契约校验。
Agent 不关心插件的具体实现，只关心槽位与产物契约，因此插件可自由替换。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .context import ContractError, PipelineContext
from .skill import Skill


@dataclass
class Agent:
    """一个具备角色定位的插件编排单元。"""

    name: str
    role: str = ""
    skills: list[Skill] = field(default_factory=list)

    # -------------------------------------------------- 契约校验（编译期）

    def validate(self, available: set[str]) -> set[str]:
        """检查本 Agent 的插件链能否在给定上游产物集上跑通。

        返回执行完本 Agent 后可用的产物集合，供下游 Agent 继续校验。
        """
        produced = set(available)
        for skill in self.skills:
            missing = [kind for kind in skill.consumes if kind not in produced]
            if missing:
                raise ContractError(
                    f"[{self.name}] 插件 '{skill.name}' 需要产物 {missing}，"
                    f"但上游只提供了 {sorted(produced) or '空'}。"
                    f"（该插件的槽位为 '{skill.slot}'，请检查流水线顺序或替换上游实现）"
                )
            produced.update(skill.produces)
        return produced

    # -------------------------------------------------- 执行（运行期）

    def run(self, ctx: PipelineContext) -> None:
        for skill in self.skills:
            # 运行期再校验一次：上游插件可能因配置原因未产出预期产物
            for kind in skill.consumes:
                ctx.require(kind)

            started = time.perf_counter()
            try:
                skill.run(ctx)
            except Exception as exc:  # noqa: BLE001 - 统一记录后向上抛出
                elapsed = (time.perf_counter() - started) * 1000
                ctx.record(self.name, skill.name, "error", elapsed, f"{type(exc).__name__}: {exc}")
                ctx.errors.append(f"[{self.name}/{skill.name}] {exc}")
                raise
            elapsed = (time.perf_counter() - started) * 1000
            detail = "、".join(f"{k}" for k in skill.produces) or "无产物"
            ctx.record(self.name, skill.name, "ok", elapsed, f"产出：{detail}")

    # -------------------------------------------------- 元信息

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "role": self.role,
            "skills": [s.manifest() for s in self.skills],
        }

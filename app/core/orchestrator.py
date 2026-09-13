"""Pipeline：把多个 Agent 串联成一条可校验、可观测的任务链。"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .agent import Agent
from .context import PipelineContext, RAW_FILE


@dataclass
class Pipeline:
    """多个 Agent 串联而成的任务流水线。

    生命周期分两阶段：

    * ``validate()`` —— 编译期静态校验。模拟整条链的产物流动，
      在真正跑数据之前就发现「上下游不匹配」这类联动错误。
    * ``run()`` —— 运行期执行，并把每一步写入 ctx.trace 便于前端展示。
    """

    name: str
    agents: list[Agent] = field(default_factory=list)
    description: str = ""

    # -------------------------------------------------- 编译期

    def validate(self) -> set[str]:
        """静态校验整条链的产物契约，返回最终可用产物集合。"""
        produced: set[str] = {RAW_FILE}
        for agent in self.agents:
            produced = agent.validate(produced)
        return produced

    # -------------------------------------------------- 运行期

    def run(self, ctx: PipelineContext) -> PipelineContext:
        self.validate()
        for agent in self.agents:
            agent.run(ctx)
        ctx.put("pipeline_meta", {"pipeline": self.name}, producer="orchestrator")
        return ctx

    # -------------------------------------------------- 元信息

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "agents": [a.describe() for a in self.agents],
            "contract": sorted(self.validate()),
        }

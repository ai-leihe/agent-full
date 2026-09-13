"""从 YAML 配置装配流水线。

配置即编排。平台代码不写死任何一条链，全部由 YAML 声明：

.. code-block:: yaml

    pipeline:
      name: doc-ingestion
      agents:
        - name: ingestion
          role: 文件加载与清洗
          skills:
            - use: text_loader
            - use: basic_cleaner
              with: { lowercase: false }

``use`` 指向插件注册名，``with`` 是该插件的初始化参数。
把 ``use`` 换成另一个同槽位插件，即可完成插拔替换。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .agent import Agent
from .orchestrator import Pipeline
from .skill import SkillRegistry, registry as default_registry


class ConfigError(RuntimeError):
    """流水线配置非法。"""


def load_config(path: str | Path) -> dict[str, Any]:
    path = Path(path)
    if not path.is_file():
        raise ConfigError(f"流水线配置文件不存在：{path}")
    with path.open("r", encoding="utf-8") as fp:
        data = yaml.safe_load(fp) or {}
    if "pipeline" not in data:
        raise ConfigError("配置文件缺少顶层 'pipeline' 字段")
    return data


def build_pipeline(config: dict[str, Any], registry: SkillRegistry | None = None) -> Pipeline:
    """根据已解析的配置字典装配 Pipeline。"""
    registry = registry or default_registry
    node = config["pipeline"]

    agents: list[Agent] = []
    for idx, raw_agent in enumerate(node.get("agents", [])):
        agent_name = raw_agent.get("name") or f"agent_{idx}"
        skills = []
        for raw_skill in raw_agent.get("skills", []):
            if isinstance(raw_skill, str):
                use, options = raw_skill, {}
            else:
                use = raw_skill.get("use")
                options = raw_skill.get("with", {}) or {}
            if not use:
                raise ConfigError(f"[{agent_name}] 存在未指定 'use' 的插件声明")
            try:
                skills.append(registry.create(use, options))
            except KeyError as exc:
                raise ConfigError(str(exc)) from None
        if not skills:
            raise ConfigError(f"[{agent_name}] 未配置任何插件")
        agents.append(Agent(name=agent_name, role=raw_agent.get("role", ""), skills=skills))

    if not agents:
        raise ConfigError("流水线至少需要一个 Agent")

    pipeline = Pipeline(
        name=node.get("name", "default"),
        description=node.get("description", ""),
        agents=agents,
    )
    # 装配即校验：配置错误在启动阶段就暴露，不留到运行时
    pipeline.validate()
    return pipeline


def build_pipeline_from_file(path: str | Path, registry: SkillRegistry | None = None) -> Pipeline:
    return build_pipeline(load_config(path), registry)

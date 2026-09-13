"""编排接口：在运行时手动调整 Agent 与步骤，实时诊断契约，试跑并应用生效。

设计约定
--------
* 所有**写操作**都返回完整的 ``state()``（草稿 + 诊断 + 目录 + 方案列表），
  前端一次往返即可拿到最新编排与校验结果，无需自行维护状态。
* 诊断走 ``Orchestration.diagnose()`` **软校验**：允许存在断裂的中间态，
  只有 ``/apply`` 与 ``/run`` 才做硬校验。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..core.context import ContractError
from ..core.orchestration import AgentSpec, Orchestration, StepSpec
from ..core.skill import registry
from ..runtime import ProfileError, runtime

router = APIRouter(prefix="/api/orchestration", tags=["orchestration"])


# =============================================================== 请求模型


class AgentCreate(BaseModel):
    name: str | None = Field(None, description="阶段名，留空自动生成")
    role: str = Field("", description="阶段职责说明")
    after: str | None = Field(None, description="插入到哪个 Agent 之后，留空追加到末尾")


class AgentUpdate(BaseModel):
    name: str | None = None
    role: str | None = None


class MoveRequest(BaseModel):
    direction: str = Field("up", pattern="^(up|down)$")


class StepCreate(BaseModel):
    skill: str = Field(..., description="插件名")
    options: dict[str, Any] | None = None
    at: int | None = Field(None, ge=0, description="插入位置，留空追加到末尾")
    use_defaults: bool = Field(True, description="未提供 options 时是否套用插件默认参数")


class StepUpdate(BaseModel):
    skill: str | None = None
    options: dict[str, Any] | None = None
    enabled: bool | None = None


class ProfileSave(BaseModel):
    name: str


class NameUpdate(BaseModel):
    name: str
    description: str | None = None


# =============================================================== 工具


def _require_draft() -> Orchestration:
    if runtime.draft is None:  # pragma: no cover
        raise HTTPException(status_code=503, detail="编排草稿尚未初始化")
    return runtime.draft


def _require_agent(agent_id: str) -> AgentSpec:
    agent = _require_draft().find_agent(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail=f"未找到阶段 {agent_id}")
    return agent


def _state(**extra: Any) -> dict[str, Any]:
    return {**runtime.state(), **extra}


def _move(items: list[Any], index: int, direction: str) -> bool:
    target = index - 1 if direction == "up" else index + 1
    if 0 <= target < len(items):
        items[index], items[target] = items[target], items[index]
        return True
    return False


# =============================================================== 读取


@router.get("", summary="获取完整编排状态（草稿 + 诊断 + 插件目录 + 方案列表）")
def get_state() -> dict[str, Any]:
    return _state()


@router.post("/validate", summary="重新诊断契约（不改变任何状态）")
def validate() -> dict[str, Any]:
    return _state()


# =============================================================== 阶段（Agent）

@router.post("/agents", summary="新增一个 Agent 阶段")
def create_agent(payload: AgentCreate) -> dict[str, Any]:
    draft = _require_draft()
    agent = AgentSpec(
        name=payload.name or f"stage_{len(draft.agents) + 1}",
        role=payload.role,
    )
    if payload.after:
        anchor = draft.find_agent(payload.after)
        position = draft.agents.index(anchor) + 1 if anchor else len(draft.agents)
    else:
        position = len(draft.agents)
    draft.agents.insert(position, agent)
    runtime.touch()
    return _state(created=agent.id)


@router.patch("/agents/{agent_id}", summary="重命名阶段 / 修改职责")
def update_agent(agent_id: str, payload: AgentUpdate) -> dict[str, Any]:
    agent = _require_agent(agent_id)
    if payload.name is not None:
        agent.name = payload.name or agent.name
    if payload.role is not None:
        agent.role = payload.role
    runtime.touch()
    return _state()


@router.delete("/agents/{agent_id}", summary="删除阶段")
def delete_agent(agent_id: str) -> dict[str, Any]:
    draft = _require_draft()
    agent = _require_agent(agent_id)
    draft.agents.remove(agent)
    runtime.touch()
    return _state(deleted=agent_id)


@router.post("/agents/{agent_id}/move", summary="上移 / 下移阶段")
def move_agent(agent_id: str, payload: MoveRequest) -> dict[str, Any]:
    draft = _require_draft()
    agent = _require_agent(agent_id)
    if not _move(draft.agents, draft.agents.index(agent), payload.direction):
        raise HTTPException(status_code=409, detail="已经到边界，无法继续移动")
    runtime.touch()
    return _state()


# =============================================================== 步骤（Skill）

@router.post("/agents/{agent_id}/steps", summary="在某阶段内添加一个功能步骤")
def create_step(agent_id: str, payload: StepCreate) -> dict[str, Any]:
    agent = _require_agent(agent_id)
    if not registry.has(payload.skill):
        raise HTTPException(status_code=400, detail=f"插件 '{payload.skill}' 未注册")

    options = payload.options
    if options is None:
        options = registry.get(payload.skill).default_options() if payload.use_defaults else {}

    step = StepSpec(skill=payload.skill, options=options)
    position = len(agent.steps) if payload.at is None else min(payload.at, len(agent.steps))
    agent.steps.insert(position, step)
    runtime.touch()
    return _state(created=step.id)


@router.patch("/agents/{agent_id}/steps/{step_id}", summary="换插件 / 改参数 / 启停步骤")
def update_step(agent_id: str, step_id: str, payload: StepUpdate) -> dict[str, Any]:
    found = _require_draft().find_step(agent_id, step_id)
    if found is None:
        raise HTTPException(status_code=404, detail=f"未找到步骤 {step_id}")
    _, _, step = found

    if payload.skill is not None and payload.skill != step.skill:
        if not registry.has(payload.skill):
            raise HTTPException(status_code=400, detail=f"插件 '{payload.skill}' 未注册")
        step.skill = payload.skill
        # 换插件后旧参数通常不再适用，默认套用新插件默认值
        if payload.options is None:
            step.options = registry.get(payload.skill).default_options()
    if payload.options is not None:
        step.options = payload.options
    if payload.enabled is not None:
        step.enabled = payload.enabled

    runtime.touch()
    return _state()


@router.delete("/agents/{agent_id}/steps/{step_id}", summary="删除步骤")
def delete_step(agent_id: str, step_id: str) -> dict[str, Any]:
    found = _require_draft().find_step(agent_id, step_id)
    if found is None:
        raise HTTPException(status_code=404, detail=f"未找到步骤 {step_id}")
    agent, index, _ = found
    agent.steps.pop(index)
    runtime.touch()
    return _state(deleted=step_id)


@router.post("/agents/{agent_id}/steps/{step_id}/move", summary="上移 / 下移步骤")
def move_step(agent_id: str, step_id: str, payload: MoveRequest) -> dict[str, Any]:
    found = _require_draft().find_step(agent_id, step_id)
    if found is None:
        raise HTTPException(status_code=404, detail=f"未找到步骤 {step_id}")
    agent, index, _ = found
    if not _move(agent.steps, index, payload.direction):
        raise HTTPException(status_code=409, detail="已经到边界，无法继续移动")
    runtime.touch()
    return _state()


# =============================================================== 自动补全


@router.post("/autofill", summary="按契约诊断自动补齐断裂环节")
def autofill() -> dict[str, Any]:
    """在每个断裂点之前，自动插入能生产缺失产物的插件。

    无法修复的情况（没有任何插件能生产该产物）会保留在 diagnosis.issues 中交给用户处理。
    """
    draft = _require_draft()
    inserted = draft.autofill(registry)
    if inserted:
        runtime.touch()
    return _state(inserted=inserted)


# =============================================================== 生效 / 试跑


@router.post("/apply", summary="把草稿编译为生效流水线")
def apply() -> dict[str, Any]:
    try:
        pipeline = runtime.apply_draft()
    except ContractError as exc:
        raise HTTPException(
            status_code=422,
            detail={"message": f"契约校验未通过，无法应用：{exc}", "diagnosis": runtime.state()["diagnosis"]},
        ) from exc
    return _state(applied=pipeline.name)


@router.post("/reset", summary="丢弃编辑，回到当前生效流水线的结构")
def reset() -> dict[str, Any]:
    runtime.reset_draft()
    return _state()


@router.post("/rename", summary="修改编排名称与描述")
def rename(payload: NameUpdate) -> dict[str, Any]:
    draft = _require_draft()
    draft.name = payload.name or draft.name
    if payload.description is not None:
        draft.description = payload.description
    runtime.touch()
    return _state()


# =============================================================== 方案存档


@router.get("/profiles", summary="列出已保存的编排方案")
def list_profiles() -> dict[str, Any]:
    return {"profiles": runtime.list_profiles()}


@router.post("/profiles", summary="把当前草稿保存为方案")
def save_profile(payload: ProfileSave) -> dict[str, Any]:
    try:
        path = runtime.save_profile(payload.name)
    except ProfileError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _state(saved=str(path))


@router.post("/profiles/{name}/load", summary="加载某个方案到草稿")
def load_profile(name: str) -> dict[str, Any]:
    try:
        runtime.load_profile(name)
    except ProfileError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _state()


@router.delete("/profiles/{name}", summary="删除方案")
def delete_profile(name: str) -> dict[str, Any]:
    try:
        removed = runtime.delete_profile(name)
    except ProfileError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not removed:
        raise HTTPException(status_code=404, detail=f"方案 '{name}' 不存在")
    return _state(deleted=name)

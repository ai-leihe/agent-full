"""流水线任务管理：创建、分页列表、切换、删除流水线。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from ..core.context import ContractError
from ..core.profiles import ProfileError
from ..runtime import runtime

router = APIRouter(prefix="/api/pipelines", tags=["pipelines"])


class PipelineCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=40, description="流水线名称")
    description: str = Field("", max_length=255, description="流水线描述")
    clone_from: str | None = Field(None, description="复制现有流水线，留空则基于当前生效流水线或空白创建")
    blank: bool = Field(False, description="创建空白流水线；为 True 时 clone_from 无效")


class PipelineUpdate(BaseModel):
    name: str | None = Field(None, max_length=40, description="新名称，留空不重命名")
    description: str | None = Field(None, max_length=255, description="新描述")


@router.get("", summary="流水线任务分页列表")
def list_pipelines(
    page: int = Query(1, ge=1, description="页码"),
    size: int = Query(10, ge=1, le=100, description="每页条数"),
    query: str = Query("", description="名称/描述关键字过滤"),
) -> dict[str, Any]:
    return runtime.list_pipelines(page=page, size=size, query=query)


@router.post("", summary="创建流水线任务")
def create_pipeline(payload: PipelineCreate) -> dict[str, Any]:
    try:
        return runtime.create_pipeline(
            payload.name,
            payload.description or "",
            clone_from=payload.clone_from,
            blank=payload.blank,
        )
    except ProfileError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ContractError as exc:
        raise HTTPException(
            status_code=422,
            detail=f"流水线创建成功但契约校验未通过：{exc}",
        ) from exc


@router.get("/{name}", summary="流水线详情")
def get_pipeline(name: str) -> dict[str, Any]:
    item = runtime.get_pipeline(name)
    if item is None:
        raise HTTPException(status_code=404, detail=f"流水线 '{name}' 不存在")
    return item


@router.put("/{name}", summary="更新流水线信息")
def update_pipeline(name: str, payload: PipelineUpdate) -> dict[str, Any]:
    try:
        return runtime.update_pipeline(name, payload.model_dump(exclude_unset=True))
    except ProfileError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/{name}", summary="删除流水线")
def delete_pipeline(name: str) -> dict[str, Any]:
    if not runtime.delete_pipeline(name):
        raise HTTPException(status_code=404, detail=f"流水线 '{name}' 不存在")
    return {"deleted": name}


@router.post("/{name}/activate", summary="激活流水线")
def activate_pipeline(name: str) -> dict[str, Any]:
    try:
        return runtime.activate_pipeline(name)
    except ProfileError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ContractError as exc:
        raise HTTPException(
            status_code=422,
            detail={"message": f"流水线契约校验未通过：{exc}", "diagnosis": runtime.state()["diagnosis"]},
        ) from exc

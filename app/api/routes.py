"""核心业务接口：文件上传触发流水线，并暴露插件/流水线元信息。

管理类接口（设置、技能库、运行记录、知识库）见 ``platform_routes.py``。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from ..core.context import ContractError, PipelineContext
from ..core.skill import registry
from ..runtime import PLATFORM_VERSION, runtime

router = APIRouter(prefix="/api", tags=["agent-platform"])


# ------------------------------------------------------------- 元信息


@router.get("/health", summary="健康检查")
def health() -> dict[str, Any]:
    return {
        "status": "ok" if runtime.pipeline is not None else "down",
        "version": PLATFORM_VERSION,
        "pipeline": runtime.pipeline.name if runtime.pipeline else None,
    }


@router.get("/skills", summary="列出已注册的插件（含槽位分组与启停状态）")
def list_skills() -> dict[str, Any]:
    return {
        "count": len(registry.names()),
        "total": len(registry.all_names()),
        "disabled": registry.disabled(),
        "slots": registry.slots(),
        "skills": registry.manifests(),
    }


@router.get("/pipeline", summary="查看当前流水线编排与产物契约")
def describe_pipeline() -> dict[str, Any]:
    if runtime.pipeline is None:  # pragma: no cover
        raise HTTPException(status_code=503, detail="流水线尚未初始化")
    return runtime.snapshot()


@router.post("/reload", summary="重新扫描插件并重建流水线（热插拔）")
def reload_platform() -> dict[str, Any]:
    try:
        runtime.rebuild()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"重载失败：{exc}") from exc
    runtime.apply_live_settings()
    return {
        "status": "reloaded",
        "loaded_modules": runtime.loaded_modules,
        "pipeline": runtime.pipeline.describe(),
    }


# ------------------------------------------------------------- 核心：文件上传


@router.post("/upload", summary="上传文件并跑完整条 Agent 流水线")
async def upload(
    file: UploadFile = File(...),
    use: str = Query("active", pattern="^(active|draft)$", description="active=用已生效流水线，draft=用当前编排草稿试跑"),
) -> dict[str, Any]:
    if runtime.pipeline is None:  # pragma: no cover
        raise HTTPException(status_code=503, detail="流水线尚未初始化")

    if use == "draft":
        if not runtime.settings.get("runtime.enable_draft_trial", True):
            raise HTTPException(status_code=403, detail="平台设置已关闭「草稿试跑」")
        try:
            pipeline = runtime.draft_pipeline()
            source = "draft"
        except ContractError as exc:
            raise HTTPException(
                status_code=422,
                detail={"message": f"当前编排无法执行：{exc}", "diagnosis": runtime.state()["diagnosis"]},
            ) from exc
    else:
        pipeline = runtime.pipeline
        source = "upload"

    # 准入校验：大小与扩展名都取自平台设置
    max_bytes = int(runtime.settings.get("upload.max_mb", 20)) * 1024 * 1024
    allowed: list[str] = runtime.settings.get("upload.allowed_extensions", []) or []
    filename = file.filename or "unnamed"
    suffix = Path(filename).suffix.lower()
    if allowed and suffix not in [item.lower() for item in allowed]:
        raise HTTPException(
            status_code=415,
            detail=f"不支持的文件类型 '{suffix or '未知'}'，允许：{', '.join(allowed)}",
        )

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="上传文件为空")
    if len(data) > max_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"文件过大（{len(data) / 1024 / 1024:.2f} MB），上限 {max_bytes // 1024 // 1024} MB",
        )

    ctx = PipelineContext(filename=filename, data=data, content_type=file.content_type or "")

    try:
        # 流水线是 CPU 密集的同步代码，放到线程池避免阻塞事件循环
        await run_in_threadpool(pipeline.run, ctx)
    except Exception as exc:  # noqa: BLE001
        runtime.remember(ctx, status="error", source=source)
        raise HTTPException(
            status_code=422,
            detail={
                "message": f"流水线执行失败：{exc}",
                "run_id": ctx.run_id,
                "trace": [t.to_dict() for t in ctx.trace],
                "errors": ctx.errors,
            },
        ) from exc

    runtime.remember(ctx, status="success", source=source)
    return ctx.to_dict(max_chunks=int(runtime.settings.get("upload.max_chunks_returned", 50)))


# ------------------------------------------------------------- 检索


class SearchRequest(BaseModel):
    run_id: str = Field(..., description="上传接口返回的 run_id")
    query: str = Field(..., min_length=1, description="查询语句")
    top_k: int | None = Field(None, ge=1, le=50, description="留空则使用平台设置里的默认值")


@router.post("/search", summary="在指定运行的向量索引上做相似度检索")
def search(payload: SearchRequest) -> dict[str, Any]:
    from ..core.warehouse import warehouse

    record = warehouse.get(payload.run_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"未找到向量索引 {payload.run_id}")

    index = record.index
    embedder_name = getattr(index, "embedder", "")
    if not embedder_name or not registry.has(embedder_name):
        raise HTTPException(status_code=500, detail=f"索引绑定的向量化插件 '{embedder_name}' 不可用")

    top_k = payload.top_k or int(runtime.settings.get("retrieval.default_top_k", 5))
    threshold = float(runtime.settings.get("retrieval.score_threshold", 0.0) or 0.0)

    # 关键：查询必须用与建索引时相同的 embedder，保证向量空间一致
    embedder = registry.create(embedder_name)
    hits = index.search(embedder.encode_one(payload.query), top_k)
    filtered = [h for h in hits if float(h.get("score", 0)) >= threshold]
    return {
        "run_id": payload.run_id,
        "query": payload.query,
        "embedder": embedder_name,
        "dimension": getattr(index, "dimension", 0),
        "top_k": top_k,
        "score_threshold": threshold,
        "hits": filtered,
        "filtered_out": len(hits) - len(filtered),
    }

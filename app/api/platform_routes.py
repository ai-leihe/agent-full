"""平台管理接口：概览、设置中心、技能库、运行记录、知识库。

这一层对应行业 Agent 平台工作台里的「系统管理」区域：
* **概览** —— 聚合健康度 / 契约状态 / 运行统计，供首页看板使用
* **设置** —— Schema 驱动的平台配置 + 模型供应商凭据
* **技能库** —— 插件启停、详情、沙盒试跑
* **运行记录** —— 历史查询、详情、重放、清理
* **知识库** —— 向量索引的浏览、检索与清理
"""

from __future__ import annotations

import base64
import binascii
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from ..core.context import CHUNKS, CLEAN_TEXT, EMBEDDINGS, TEXT, Chunk, PipelineContext
from ..core.settings import PROVIDER_KINDS
from ..core.skill import registry
from ..core.warehouse import warehouse
from ..runtime import PLATFORM_VERSION, runtime

router = APIRouter(prefix="/api", tags=["platform"])


# =============================================================== 请求模型


class ProviderPayload(BaseModel):
    id: str | None = None
    name: str = ""
    kind: str = Field("openai", description=f"取值：{PROVIDER_KINDS}")
    base_url: str = ""
    api_key: str | None = Field(None, description="留空或传掩码值表示不修改")
    models: list[str] | str = Field(default_factory=list)
    enabled: bool = True


class SkillToggle(BaseModel):
    enabled: bool


class SkillTestRequest(BaseModel):
    options: dict[str, Any] | None = None
    text: str = Field(
        "这是一段用于试跑插件的示例文本。Agent 平台支持多 Agent 串联与插拔式插件。",
        description="沙盒输入文本，会同时用于合成上游产物",
    )
    #: 在线文件验证：把本地文件的真实字节当 ``raw_file`` 送进沙盒，
    #: 这样 loader 等「吃文件」的插件也能在浏览器里直接验证，而不必先跑整条流水线。
    filename: str | None = Field(None, description="上传文件名（含后缀），决定 loader 走哪个解析分支")
    content_type: str | None = Field(None, description="上传文件的 MIME 类型")
    data_base64: str | None = Field(None, description="上传文件内容的 base64 编码")


class SearchRequest(BaseModel):
    run_id: str
    query: str = Field(..., min_length=1)
    top_k: int | None = Field(None, ge=1, le=50)


# =============================================================== 概览


@router.get("/platform/overview", summary="工作台首页聚合视图")
def overview() -> dict[str, Any]:
    return {**runtime.overview(), "server_time": time.time()}


@router.get("/platform/export", summary="导出平台配置（密钥已掩码）")
def export_bundle() -> dict[str, Any]:
    snapshot = runtime.settings_snapshot()
    return {
        "exported_at": time.time(),
        "version": PLATFORM_VERSION,
        "settings": snapshot["values"],
        "providers": snapshot["providers"],
        "orchestration": runtime.state()["orchestration"],
        "profiles": [
            {"name": p["name"], "content": runtime.profile_content(p["name"])}
            for p in runtime.list_profiles()
            if "error" not in p
        ],
    }


# =============================================================== 设置中心


@router.get("/settings", summary="读取平台设置（含 Schema 与掩码后的供应商）")
def get_settings() -> dict[str, Any]:
    return runtime.settings_snapshot()


@router.get("/settings/schema", summary="仅读取设置 Schema（前端渲染表单用）")
def get_settings_schema() -> dict[str, Any]:
    snapshot = runtime.settings_snapshot()
    return {"schema": snapshot["schema"], "values": snapshot["values"]}


@router.put("/settings", summary="更新平台设置")
def update_settings(payload: dict[str, Any]) -> dict[str, Any]:
    snapshot, errors = runtime.update_settings(payload)
    if errors:
        raise HTTPException(status_code=422, detail={"message": "；".join(errors), "errors": errors})
    return snapshot


@router.post("/settings/reset", summary="恢复默认设置")
def reset_settings() -> dict[str, Any]:
    runtime.settings.reset()
    try:
        runtime.rebuild()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=f"恢复默认后流水线无法装配：{exc}") from exc
    runtime.apply_live_settings()
    return runtime.settings_snapshot()


@router.post("/settings/providers", summary="新增或更新模型供应商")
def upsert_provider(payload: ProviderPayload) -> dict[str, Any]:
    provider, errors = runtime.settings.upsert_provider(payload.model_dump())
    if errors:
        raise HTTPException(status_code=422, detail={"message": "；".join(errors), "errors": errors})
    return {"provider": provider, "providers": runtime.settings.list_providers()}


@router.delete("/settings/providers/{provider_id}", summary="删除模型供应商")
def delete_provider(provider_id: str) -> dict[str, Any]:
    if not runtime.settings.delete_provider(provider_id):
        raise HTTPException(status_code=404, detail=f"未找到供应商 {provider_id}")
    return {"providers": runtime.settings.list_providers()}


# =============================================================== 技能库


@router.get("/skills/{name}", summary="查看单个插件详情")
def get_skill(name: str) -> dict[str, Any]:
    if not registry.raw_has(name):
        raise HTTPException(status_code=404, detail=f"未找到插件 '{name}'")
    manifest = registry.manifest_of(name)
    disabled = set(registry.disabled())
    manifest["peers"] = [
        {"name": s.name, "enabled": s.name not in disabled, "description": s.description}
        for s in registry.all_by_slot(manifest["slot"])
        if s.name != name
    ]
    # 哪些编排步骤正在使用该插件
    usage: list[dict[str, str]] = []
    if runtime.draft is not None:
        for agent in runtime.draft.agents:
            for step in agent.steps:
                if step.skill == name:
                    usage.append({"agent": agent.name, "agent_id": agent.id, "step_id": step.id})
    manifest["usage"] = usage
    return manifest


@router.post("/skills/{name}/toggle", summary="启用 / 停用插件")
def toggle_skill(name: str, payload: SkillToggle) -> dict[str, Any]:
    if not registry.raw_has(name):
        raise HTTPException(status_code=404, detail=f"未找到插件 '{name}'")
    errors = runtime.set_plugin_enabled(name, payload.enabled)
    if errors:
        raise HTTPException(status_code=422, detail={"message": "；".join(errors), "errors": errors})
    return {
        "skill": registry.manifest_of(name),
        "catalog": registry.catalog(),
        "overview": runtime.overview(),
    }


@router.post("/skills/{name}/test", summary="在沙盒中单独试跑一个插件")
async def test_skill(name: str, payload: SkillTestRequest) -> dict[str, Any]:
    if not registry.raw_has(name):
        raise HTTPException(status_code=404, detail=f"未找到插件 '{name}'")
    if registry.is_disabled(name):
        raise HTTPException(status_code=400, detail=f"插件 '{name}' 已被停用，请先启用")

    skill_cls = registry.raw_get(name)
    # 先铺默认值再叠加用户配置：试跑表单只回传被渲染出的字段，插件若有未上表单的新参数，
    # 也能拿到 param_schema 的默认值，而不是依赖各插件自己在 configure 里兜底。
    options = {**skill_cls.default_options(), **(payload.options or {})}
    try:
        # 走注册中心的统一实例化入口：与生效流水线一致地展开 ${VAR}，沙盒结论才和真实链路同源
        instance = registry.create(name, options)
    except Exception as exc:  # noqa: BLE001 - 参数非法属于用户输入问题
        raise HTTPException(status_code=422, detail=f"参数不合法：{exc}") from exc

    # 沙盒：合成一份满足该插件 consumes 的上游产物，让它能独立跑起来
    raw, filename, content_type = _resolve_sandbox_input(payload)
    ctx = PipelineContext(filename=filename, data=raw, content_type=content_type)
    _seed_upstream(ctx, payload.text)
    # 快照「产出者 + 值的身份」而不是「产物键是否新增」：清洗 / 切片 / 加载类插件覆写的正是
    # 沙盒预置的 clean_text / chunks / text，只比对键是否存在会把它们误判成「无产出」。
    before = {kind: (a.producer, id(a.value)) for kind, a in ctx.artifacts.items()}

    started = time.perf_counter()
    error: str | None = None
    try:
        await run_in_threadpool(instance.run, ctx)
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
    duration_ms = round((time.perf_counter() - started) * 1000, 2)

    outputs = [
        {"kind": kind, "producer": artifact.producer, **_describe(artifact.value, kind)}
        for kind, artifact in ctx.artifacts.items()
        if before.get(kind) != (artifact.producer, id(artifact.value))
    ]
    return {
        "skill": name,
        "slot": skill_cls.slot,
        "ok": error is None,
        "error": error,
        "duration_ms": duration_ms,
        "options": options,
        "inputs": sorted(skill_cls.consumes),
        "outputs": outputs,
        "file": (
            {"name": filename, "content_type": content_type, "bytes": len(raw)}
            if payload.data_base64 is not None
            else None
        ),
    }


def _resolve_sandbox_input(payload: SkillTestRequest) -> tuple[bytes, str, str]:
    """把沙盒请求还原成 ``(原始字节, 文件名, MIME)``。

    没有上传文件时退回「文本即文件」的老行为，保持接口向后兼容；
    上传了文件就用真实字节当 ``raw_file``，后缀与 MIME 也一并透传，
    这样 loader 才能按正确分支解析，而不是永远走 text/plain。
    """
    if payload.data_base64 is None:
        return payload.text.encode("utf-8"), "<sandbox>", "text/plain"

    try:
        # btoa / base64.b64encode 都不带空白，validate=True 能顺带拦下被截断的内容
        raw = base64.b64decode(payload.data_base64.strip(), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=422, detail=f"文件内容不是合法的 base64：{exc}") from exc

    if not raw:
        raise HTTPException(status_code=400, detail="上传文件为空")

    max_bytes = int(runtime.settings.get("upload.max_mb", 20)) * 1024 * 1024
    if len(raw) > max_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"文件过大（{len(raw) / 1024 / 1024:.2f} MB），上限 {max_bytes // 1024 // 1024} MB",
        )

    return raw, payload.filename or "<sandbox>", payload.content_type or "application/octet-stream"


def _seed_upstream(ctx: PipelineContext, text: str) -> None:
    """为沙盒试跑合成上游产物。

    这是**刻意为之的简化**：真实链路里这些产物来自上游插件，
    沙盒为了让任意插件可独立试跑，这里按最小合法形态补出来。
    """
    ctx.put(TEXT, text, producer="sandbox")
    ctx.put(CLEAN_TEXT, text, producer="sandbox")
    ctx.put(CHUNKS, [Chunk(id="sandbox-0", index=0, text=text)], producer="sandbox")
    ctx.put(EMBEDDINGS, [[0.0] * 8], producer="sandbox")


def _describe(value: Any, kind: str) -> dict[str, Any]:
    """产物的轻量描述（沙盒试跑回显用）。"""
    if isinstance(value, str):
        return {"size": f"{len(value)} chars", "preview": value[:200]}
    if isinstance(value, list):
        head = value[0] if value else None
        preview = head.to_dict() if isinstance(head, Chunk) else (head if not isinstance(head, list) else f"[{len(head)} floats]")
        return {"size": f"{len(value)} items", "preview": preview}
    if kind == "vector_index":
        return {"size": f"{getattr(value, 'size', '?')} vectors", "preview": type(value).__name__}
    return {"size": type(value).__name__, "preview": None}


# =============================================================== 运行记录


@router.get("/runs", summary="运行记录列表")
def list_runs(
    limit: int = Query(50, ge=1, le=500, description="未传 size 时按此条数返回，兼容旧调用"),
    page: int = Query(1, ge=1, description="页码"),
    size: int | None = Query(None, ge=1, le=500, description="每页条数，传值即启用分页"),
    status: str | None = Query(None, pattern="^(success|error)$"),
    keyword: str = "",
) -> dict[str, Any]:
    effective_size = size if size is not None else limit
    runs, total = runtime.history.page(
        page=page, size=effective_size, status=status, keyword=keyword
    )
    return {
        "runs": runs,
        "stats": runtime.history.stats(),
        "total": total,
        "page": page,
        "size": effective_size,
        "pages": max(1, (total + effective_size - 1) // effective_size),
    }


@router.get("/runs/{run_id}", summary="运行记录详情")
def get_run(run_id: str) -> dict[str, Any]:
    record = runtime.history.get(run_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"未找到运行记录 {run_id}")
    data: dict[str, Any] = {"record": record.to_dict()}
    ctx = runtime.context_of(run_id)
    if ctx is not None:
        payload = ctx.to_dict(max_chunks=int(runtime.settings.get("upload.max_chunks_returned", 50)))
        data["chunks"] = payload["chunks"]
        data["stats"] = payload["stats"]
        data["replayable"] = True
    else:
        data["chunks"] = []
        data["replayable"] = False
    return data


@router.delete("/runs/{run_id}", summary="删除一条运行记录")
def delete_run(run_id: str) -> dict[str, Any]:
    if not runtime.history.delete(run_id):
        raise HTTPException(status_code=404, detail=f"未找到运行记录 {run_id}")
    runtime.contexts.pop(run_id, None)
    return {"deleted": run_id, "stats": runtime.history.stats()}


@router.delete("/runs", summary="清空运行记录")
def clear_runs() -> dict[str, Any]:
    count = runtime.history.clear()
    runtime.contexts.clear()
    return {"deleted": count}


@router.post("/runs/{run_id}/replay", summary="用同一份文件与当前生效编排重放")
async def replay_run(run_id: str) -> dict[str, Any]:
    ctx = runtime.context_of(run_id)
    if ctx is None:
        raise HTTPException(
            status_code=410,
            detail="原始文件已不在内存中（服务重启或被淘汰），无法重放。请重新上传。",
        )
    if runtime.pipeline is None:  # pragma: no cover
        raise HTTPException(status_code=503, detail="流水线尚未初始化")

    replay_ctx = PipelineContext(filename=ctx.filename, data=ctx.data, content_type=ctx.content_type)
    try:
        await run_in_threadpool(runtime.pipeline.run, replay_ctx)
    except Exception as exc:  # noqa: BLE001
        runtime.remember(replay_ctx, status="error", source="replay")
        raise HTTPException(status_code=422, detail=f"重放失败：{exc}") from exc

    runtime.remember(replay_ctx, status="success", source="replay")
    return replay_ctx.to_dict(max_chunks=int(runtime.settings.get("upload.max_chunks_returned", 50)))


# =============================================================== 知识库


@router.get("/knowledge/indexes", summary="已建立的向量索引")
def list_indexes(
    page: int = Query(1, ge=1, description="页码"),
    size: int | None = Query(None, ge=1, le=200, description="每页条数，传值即启用分页"),
) -> dict[str, Any]:
    items = warehouse.list()
    total = len(items)
    payload: dict[str, Any] = {
        "count": total,
        "total_vectors": sum(int(i.get("size") or 0) for i in items),
    }
    if size is None:
        payload["indexes"] = items
        return payload

    pages = max(1, (total + size - 1) // size)
    current = min(max(1, page), pages)
    start = (current - 1) * size
    payload.update(
        {
            "indexes": items[start : start + size],
            "total": total,
            "page": current,
            "size": size,
            "pages": pages,
        }
    )
    return payload


@router.delete("/knowledge/indexes/{run_id}", summary="删除某个向量索引")
def delete_index(run_id: str) -> dict[str, Any]:
    if not warehouse.delete(run_id):
        raise HTTPException(status_code=404, detail=f"未找到向量索引 {run_id}")
    return {"deleted": run_id, "count": len(warehouse.list())}


@router.post("/knowledge/search", summary="在指定索引上检索（默认值取自设置）")
def knowledge_search(payload: SearchRequest) -> dict[str, Any]:
    record = warehouse.get(payload.run_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"未找到向量索引 {payload.run_id}")

    index = record.index
    embedder_name = getattr(index, "embedder", "")
    if not embedder_name or not registry.has(embedder_name):
        raise HTTPException(status_code=500, detail=f"索引绑定的向量化插件 '{embedder_name}' 不可用")

    top_k = payload.top_k or int(runtime.settings.get("retrieval.default_top_k", 5))
    threshold = float(runtime.settings.get("retrieval.score_threshold", 0.0) or 0.0)

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

"""技能库「脚本验证」接口：在线执行一小段 Python / Shell 代码并回显结果。

这里只做**准入把关**——总开关、仅管理员、超时与体积上限全部取自设置中心，
改完立即生效；真正的执行与可控性（超时强杀 / 输出截断 / 临时文件回收）
在 :mod:`app.core.script` 里。

安全声明见模块 ``app.core.script`` 顶部：它执行的是调用者自己的代码，
等同于给出一个终端，因此默认按「本机开发工具」定位，对外部署前务必收紧设置。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from ..core.auth import ROLE_ADMIN
from ..core.script import detect_runtimes, run_script
from ..runtime import runtime

router = APIRouter(prefix="/api/scripts", tags=["scripts"])


class ScriptRequest(BaseModel):
    language: str = Field("python", description="python / sh / powershell")
    code: str = Field(..., description="待验证的脚本内容")
    stdin: str = Field("", description="可选的标准输入")
    timeout_sec: float | None = Field(
        None, ge=1, le=120, description="留空则使用平台设置里的默认超时"
    )


def _limits() -> dict[str, int]:
    """执行上限统一读设置中心，不在这里硬编码第二份默认值。"""
    return {
        "timeout_sec": int(runtime.settings.get("script.timeout_sec", 15)),
        "max_output_kb": int(runtime.settings.get("script.max_output_kb", 64)),
        "max_script_kb": int(runtime.settings.get("script.max_script_kb", 64)),
    }


def _guard(request: Request) -> None:
    """按设置中心把关「谁可以执行脚本」。"""
    if not runtime.settings.get("script.enabled", True):
        raise HTTPException(status_code=403, detail="平台设置已关闭「脚本验证」")

    if not runtime.settings.get("script.admin_only", False):
        return

    user = getattr(request.state, "user", None)
    if user is None:
        # 即便整体关了登录校验，「仅管理员」这个开关也必须是硬的：
        # 身份无法确认时不放行，而不是把匿名当成管理员。
        raise HTTPException(status_code=401, detail="「脚本验证」已限制仅管理员可用，请先登录")
    if user.role != ROLE_ADMIN:
        raise HTTPException(status_code=403, detail="「脚本验证」已限制仅管理员可用")


@router.get("/runtimes", summary="可用的脚本解释器与执行上限")
def list_runtimes(request: Request) -> dict[str, Any]:
    _guard(request)
    return {
        "runtimes": detect_runtimes(),
        "limits": _limits(),
        "enabled": bool(runtime.settings.get("script.enabled", True)),
        "admin_only": bool(runtime.settings.get("script.admin_only", False)),
    }


@router.post("/validate", summary="在线执行并验证一段脚本")
async def validate_script(payload: ScriptRequest, request: Request) -> dict[str, Any]:
    _guard(request)
    limits = _limits()
    timeout = min(float(payload.timeout_sec or limits["timeout_sec"]), 120.0)

    try:
        # 执行是阻塞的（等子进程退出），放线程池里，别把事件循环卡住
        return await run_in_threadpool(
            run_script,
            payload.language,
            payload.code,
            stdin=payload.stdin,
            timeout=timeout,
            max_output=limits["max_output_kb"] * 1024,
            max_code=limits["max_script_kb"] * 1024,
        )
    except ValueError as exc:
        # 语言不支持 / 内容为空 / 超出体积上限：都是调用方的输入问题
        raise HTTPException(status_code=422, detail=str(exc)) from exc

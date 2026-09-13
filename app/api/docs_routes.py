"""受保护的接口文档：Swagger UI / ReDoc / OpenAPI 规范。

与 FastAPI 内置文档的差别
------------------------
1. 三个入口都纳入全局登录校验（见 ``app/main.py`` 的 ``auth_guard``）：
   未登录访问时，浏览器会回到工作台的登录门，其它客户端拿到 401。
2. 页面顶部显示**当前登录账号**，「文档 ↔ 账号会话」的绑定关系可见。
3. 规范里补上 ``BearerAuth`` 安全方案，Swagger 的 Authorize 可直接贴令牌调试；
   浏览器场景下更省事——会话 Cookie 会被自动携带。
4. ``info.description`` 会按当前账号动态生成，因此只在 ``app.state`` 上缓存
   基础规范（每次返回深拷贝后再定制）。
"""

from __future__ import annotations

import copy
import html
import re
from typing import Any

from fastapi import APIRouter, Request
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.openapi.utils import get_openapi
from fastapi.responses import HTMLResponse, JSONResponse

from ..runtime import runtime

router = APIRouter(tags=["docs"], include_in_schema=False)

OPENAPI_URL = "/openapi.json"

#: 可见范围的中文说明，与设置项 ``auth.docs_access`` 一一对应
ACCESS_LABELS = {"member": "所有登录用户", "admin": "仅管理员", "public": "匿名公开"}

_BODY_RE = re.compile(r"<body[^>]*>")


# ---------------------------------------------------------------- 规范


def _base_schema(app: Any) -> dict[str, Any]:
    """生成（并缓存）基础 OpenAPI 规范，附带 Bearer 安全方案。"""
    cached = getattr(app.state, "doc_schema", None)
    if cached is None:
        cached = get_openapi(
            title=app.title,
            version=app.version,
            description=app.description,
            routes=app.routes,
        )
        components = cached.setdefault("components", {})
        components.setdefault("securitySchemes", {})["BearerAuth"] = {
            "type": "http",
            "scheme": "bearer",
            "description": "登录后签发的会话令牌，用法：`Authorization: Bearer <token>`",
        }
        cached["security"] = [{"BearerAuth": []}]
        app.state.doc_schema = cached
    return copy.deepcopy(cached)


def _role_label(user: Any) -> str:
    return "管理员" if getattr(user, "is_admin", False) else "普通用户"


def _viewer(request: Request) -> dict[str, str] | None:
    """当前登录账号的展示信息；未登录返回 ``None``。"""
    user = getattr(request.state, "user", None)
    if user is None:
        return None
    return {
        "username": str(user.username),
        "display_name": str(user.nickname or user.username),
        "role": _role_label(user),
        "initial": (user.nickname or user.username or "?")[:1].upper(),
        "color": str(user.color or "#5b8cff"),
    }


def _docs_access_label() -> str:
    """当前文档可见范围的中文说明。"""
    return ACCESS_LABELS.get(runtime.auth.docs_access, runtime.auth.docs_access)


def _banner(viewer: dict[str, str] | None) -> str:
    """文档页顶部的账号条；匿名公开时退化成可见范围提示。"""
    access = html.escape(_docs_access_label())
    shell = (
        '<div style="display:flex;align-items:center;gap:12px;padding:10px 20px;'
        "background:#0f172a;color:#e2e8f0;border-bottom:1px solid #1e293b;"
        'font:13px/1.6 -apple-system,BlinkMacSystemFont,\'Segoe UI\',Roboto,sans-serif">'
    )
    back = '<a href="/" style="color:#7dd3fc;text-decoration:none">← 返回工作台</a></div>'
    if viewer is None:
        return (
            shell
            + f"<span>当前未登录 · 文档可见范围：<b>{access}</b></span>"
            + '<span style="margin-left:auto;opacity:.7">接口调试需要登录后携带令牌</span>'
            + back
        )
    name = html.escape(viewer["display_name"])
    account = html.escape(viewer["username"])
    return (
        shell
        + '<span style="display:inline-flex;width:26px;height:26px;border-radius:50%;'
        f'background:{html.escape(viewer["color"])};color:#fff;align-items:center;'
        f'justify-content:center;font-weight:600">{html.escape(viewer["initial"])}</span>'
        f"<span>当前登录：<b>{name}</b>（{account}）· {html.escape(viewer['role'])}</span>"
        f'<span style="margin-left:auto;opacity:.7">文档可见范围：{access}，'
        "接口调试会自动携带当前账号的会话</span>"
        + back
    )


def _inject_banner(page: str, banner: str) -> str:
    if not banner:
        return page
    return _BODY_RE.sub(lambda m: m.group(0) + banner, page, count=1)


# ---------------------------------------------------------------- 路由


@router.get("/docs", summary="Swagger UI（需登录）", response_class=HTMLResponse)
def swagger_ui(request: Request) -> HTMLResponse:
    page = get_swagger_ui_html(
        openapi_url=OPENAPI_URL,
        title=f"{request.app.title} · 接口文档",
    ).body.decode("utf-8")
    return HTMLResponse(_inject_banner(page, _banner(_viewer(request))))


@router.get("/redoc", summary="ReDoc（需登录）", response_class=HTMLResponse)
def redoc(request: Request) -> HTMLResponse:
    page = get_redoc_html(
        openapi_url=OPENAPI_URL,
        title=f"{request.app.title} · 接口文档",
    ).body.decode("utf-8")
    return HTMLResponse(_inject_banner(page, _banner(_viewer(request))))


@router.get(OPENAPI_URL, summary="OpenAPI 规范（需登录）")
def openapi_spec(request: Request) -> JSONResponse:
    schema = _base_schema(request.app)
    viewer = _viewer(request)
    if viewer is not None:
        description = schema.get("info", {}).get("description") or ""
        schema["info"]["description"] = (
            f"{description}\n\n---\n\n"
            f"**当前登录**：{viewer['display_name']}（{viewer['username']}）· {viewer['role']}\n\n"
            f"**文档可见范围**：{_docs_access_label()}\n\n"
            "> 接口调试可依赖浏览器会话 Cookie，"
            "也可在 Swagger 右上角 Authorize 填入 Bearer 令牌。"
        )
    return JSONResponse(schema)

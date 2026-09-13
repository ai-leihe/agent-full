"""应用入口：装配插件 → 校验流水线 → 暴露 HTTP 服务与工作台前端。"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .api.auth_routes import router as auth_router
from .api.docs_routes import router as docs_router
from .api.orchestration_routes import router as orchestration_router
from .api.pipeline_routes import router as pipeline_router
from .api.platform_routes import router as platform_router
from .api.routes import router
from .api.script_routes import router as script_router
from .core.auth import is_doc_path
from .core.env import load_dotenv
from .runtime import PLATFORM_VERSION, runtime

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config" / "pipeline.yaml"
EXT_PLUGINS_DIR = BASE_DIR / "ext_plugins"
PROFILES_DIR = BASE_DIR / "config" / "orchestrations"
SETTINGS_PATH = BASE_DIR / "config" / "settings.json"
USERS_PATH = BASE_DIR / "config" / "users.json"
RUNS_PATH = BASE_DIR / "data" / "runs.json"
WEB_DIR = BASE_DIR / "web"


def create_app() -> FastAPI:
    # 必须先加载 .env：流水线配置里的 ${VAR} 全部依赖它。文件不存在也照常启动
    # （生产环境可以只用系统环境变量），只有真正引用到缺失变量时才会报错。
    load_dotenv()

    # 启动即完成「读取设置 → 插件发现 → 流水线装配 → 契约校验」，配置错误直接启动失败
    runtime.bootstrap(
        config_path=CONFIG_PATH,
        ext_dir=EXT_PLUGINS_DIR,
        profiles_dir=PROFILES_DIR,
        settings_path=SETTINGS_PATH,
        runs_path=RUNS_PATH,
        users_path=USERS_PATH,
    )

    app = FastAPI(
        title="Agent 平台",
        version=PLATFORM_VERSION,
        description="多 Agent 串联 + 插拔式 Skill 的编排平台：文件清洗 / 切片 / 向量化 / 检索",
        # 内置文档入口一律关闭，改由 app/api/docs_routes.py 提供「需登录」的版本
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def auth_guard(request: Request, call_next):
        """全局登录校验 + 接口文档按角色放开。

        静态页面与前端资源始终放行（未登录时由前端登录门接管），白名单接口
        （健康检查、登录/注册）放行；其余 ``/api`` 接口在「启用登录校验」时
        要求携带有效会话令牌——请求头或会话 Cookie 均可。

        **接口文档**（``/docs``、``/redoc``、``/openapi.json``）的可见范围由设置项
        ``auth.docs_access`` 决定：``member``（所有登录用户，默认）、``admin``（仅管理员）、
        ``public``（匿名公开）。浏览器直接打开文档页时，未登录回到工作台登录门
        （``?auth=required``）、已登录但无权提示 ``?auth=forbidden``；接口客户端
        分别拿到 401 / 403 JSON。
        """
        user = runtime.auth.user_from_request(request)
        request.state.user = user
        path = request.url.path

        if is_doc_path(path):
            gate = runtime.auth.docs_gate(user)
            if gate != "allow":
                wants_html = "text/html" in (request.headers.get("accept") or "").lower()
                if request.method == "GET" and wants_html:
                    target = "/?auth=required" if gate == "login" else "/?auth=forbidden"
                    return RedirectResponse(url=target, status_code=302)
                if gate == "forbidden":
                    return JSONResponse(
                        {"detail": "当前账号无权查看接口文档，请联系管理员", "code": "forbidden"},
                        status_code=403,
                    )
                return JSONResponse(
                    {"detail": "未登录或会话已过期，请重新登录", "code": "unauthorized"},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
            return await call_next(request)

        if runtime.auth.require_login and user is None and runtime.auth.requires_login(path):
            return JSONResponse(
                {"detail": "未登录或会话已过期，请重新登录", "code": "unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        return await call_next(request)

    app.include_router(docs_router)
    app.include_router(router)
    app.include_router(auth_router)
    app.include_router(orchestration_router)
    app.include_router(pipeline_router)
    app.include_router(platform_router)
    app.include_router(script_router)

    # 静态页面挂在最后，避免 "/" 通配吞掉 /api 路由
    if WEB_DIR.is_dir():
        app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
    return app


app = create_app()

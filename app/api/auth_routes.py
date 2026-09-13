"""账号与会话接口：登录 / 注册 / 个人中心 / 用户管理。

接口分层
--------
* **公开**：``/api/auth/config``、``/api/auth/login``、``/api/auth/register``
  （全局登录校验中间件里白名单放行，见 ``app/main.py``）
* **登录后本人**：``me / profile / password / preferences / sessions``
* **仅管理员**：``/api/auth/users`` 增删改查

所有写操作都会回传最新的 ``user``（或 ``users``）对象，前端一次往返即可刷新视图。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from ..core.auth import (
    DEFAULT_THEME,
    PASSWORD_MIN_LENGTH,
    ROLE_ADMIN,
    THEME_AXES,
    TOKEN_COOKIE,
    sanitize_theme,
)
from ..runtime import runtime

router = APIRouter(prefix="/api/auth", tags=["auth"])


# =============================================================== 请求模型


class LoginPayload(BaseModel):
    username: str = Field(..., min_length=1, description="用户名")
    password: str = Field(..., min_length=1, description="密码")
    remember: bool = Field(False, description="勾选后会话至少保留 30 天")


class RegisterPayload(BaseModel):
    username: str = Field(..., min_length=3, max_length=32)
    password: str = Field(..., min_length=PASSWORD_MIN_LENGTH)
    nickname: str = ""
    email: str = ""


class ProfilePayload(BaseModel):
    nickname: str | None = None
    email: str | None = None
    avatar: str | None = None
    color: str | None = None
    bio: str | None = None


class PasswordPayload(BaseModel):
    current_password: str = Field(..., min_length=1)
    new_password: str = Field(..., min_length=1)
    logout_others: bool = True


class PreferencesPayload(BaseModel):
    theme: dict[str, Any] | None = None


class UserCreatePayload(BaseModel):
    username: str = Field(..., min_length=3, max_length=32)
    password: str = Field(..., min_length=PASSWORD_MIN_LENGTH)
    nickname: str = ""
    email: str = ""
    role: str = "user"
    must_change_password: bool = False


class UserPatchPayload(BaseModel):
    nickname: str | None = None
    email: str | None = None
    role: str | None = None
    active: bool | None = None
    new_password: str | None = None


# =============================================================== 依赖


def current_user(request: Request):
    """取当前登录用户；中间件已解析过，这里只做缺省保护。"""
    user = getattr(request.state, "user", None)
    if user is None:
        raise HTTPException(status_code=401, detail="未登录或会话已过期，请重新登录")
    return user


def admin_user(request: Request):
    user = current_user(request)
    if user.role != ROLE_ADMIN:
        raise HTTPException(status_code=403, detail="该操作仅管理员可用")
    return user


def _session_view(user, request: Request) -> dict[str, Any]:
    token = runtime.auth.token_from_request(request)
    payload = runtime.auth.parse(token) or {}
    return {
        "jti": payload.get("jti"),
        "issued_at": payload.get("iat"),
        "expires_at": payload.get("exp"),
        "active_count": len(runtime.auth.sessions_of(user.id)),
    }


# =============================================================== 公开接口


@router.get("/config", summary="登录页所需的公开配置")
def auth_config() -> dict[str, Any]:
    return {
        "require_login": runtime.auth.require_login,
        "allow_registration": runtime.auth.allow_registration,
        "session_hours": runtime.auth.session_hours,
        "docs_access": runtime.auth.docs_access,
        "user_count": runtime.auth.users.count(),
        "has_users": runtime.auth.users.count() > 0,
        "password_min": PASSWORD_MIN_LENGTH,
        "default_theme": DEFAULT_THEME,
        "theme_axes": {k: sorted(v) for k, v in THEME_AXES.items()},
        "first_run_hint": runtime.auth.users.has_default_admin_password(),
    }


@router.post("/login", summary="登录并签发会话令牌")
def login(payload: LoginPayload, response: Response) -> dict[str, Any]:
    user, errors = runtime.auth.users.authenticate(payload.username, payload.password)
    if user is None:
        raise HTTPException(status_code=401, detail={"message": "；".join(errors), "errors": errors})

    token, expires_at = runtime.auth.mint(user, remember=payload.remember)
    runtime.auth.touch_login(user)
    # 同时下发会话 Cookie：浏览器直接打开 /docs 时靠它带上当前账号的鉴权
    response.set_cookie(**runtime.auth.session_cookie(token, expires_at))
    return {"token": token, "expires_at": expires_at, "user": user.to_public()}


@router.post("/register", summary="自助注册（受平台设置开关约束）")
def register(payload: RegisterPayload, response: Response) -> dict[str, Any]:
    if not runtime.auth.allow_registration:
        raise HTTPException(status_code=403, detail="平台已关闭自助注册，请联系管理员开通账号")

    # 平台还没有任何账号时，第一个注册者直接成为管理员
    role = ROLE_ADMIN if runtime.auth.users.count() == 0 else "user"
    user, errors = runtime.auth.users.create(
        {"username": payload.username, "nickname": payload.nickname, "email": payload.email, "role": role},
        password=payload.password,
    )
    if user is None:
        raise HTTPException(status_code=422, detail={"message": "；".join(errors), "errors": errors})

    token, expires_at = runtime.auth.mint(user)
    runtime.auth.touch_login(user)
    response.set_cookie(**runtime.auth.session_cookie(token, expires_at))
    return {"token": token, "expires_at": expires_at, "user": user.to_public()}


# =============================================================== 个人中心


@router.get("/me", summary="当前登录用户 + 会话信息")
def me(request: Request) -> dict[str, Any]:
    user = current_user(request)
    return {"user": user.to_public(), "session": _session_view(user, request)}


@router.post("/logout", summary="退出登录（吊销当前令牌）")
def logout(request: Request, response: Response) -> dict[str, Any]:
    runtime.auth.revoke(runtime.auth.token_from_request(request))
    response.delete_cookie(TOKEN_COOKIE, path="/")
    return {"ok": True}


@router.put("/profile", summary="更新个人资料")
def update_profile(payload: ProfilePayload, request: Request) -> dict[str, Any]:
    user = current_user(request)
    patch = payload.model_dump(exclude_none=True)
    updated, errors = runtime.auth.users.update(user.id, patch)
    if updated is None:
        raise HTTPException(status_code=422, detail={"message": "；".join(errors), "errors": errors})
    return {"user": updated.to_public()}


@router.put("/password", summary="修改本人密码")
def update_password(payload: PasswordPayload, request: Request) -> dict[str, Any]:
    from ..core.auth import verify_password  # 局部导入，避免顶部堆砌

    user = current_user(request)
    if not verify_password(payload.current_password, user.password_hash):
        raise HTTPException(status_code=422, detail="当前密码不正确")
    if payload.current_password == payload.new_password:
        raise HTTPException(status_code=422, detail="新密码不能与当前密码相同")

    errors = runtime.auth.users.set_password(user.id, payload.new_password, must_change=False)
    if errors:
        raise HTTPException(status_code=422, detail={"message": "；".join(errors), "errors": errors})

    if payload.logout_others:
        runtime.auth.revoke_others(user.id, keep_token=runtime.auth.token_from_request(request))
    return {"user": user.to_public()}


@router.put("/preferences", summary="更新个性化偏好（主题等）")
def update_preferences(payload: PreferencesPayload, request: Request) -> dict[str, Any]:
    user = current_user(request)
    theme = sanitize_theme(payload.theme or {})
    updated, errors = runtime.auth.users.update(user.id, {"preferences": {"theme": theme}})
    if updated is None:
        raise HTTPException(status_code=422, detail={"message": "；".join(errors), "errors": errors})
    return {"user": updated.to_public()}


@router.get("/sessions", summary="当前账号的活跃会话")
def list_sessions(request: Request) -> dict[str, Any]:
    user = current_user(request)
    current = runtime.auth.parse(runtime.auth.token_from_request(request)) or {}
    sessions = runtime.auth.sessions_of(user.id)
    for item in sessions:
        item["current"] = item["jti"] == current.get("jti")
    return {"sessions": sessions, "count": len(sessions)}


@router.post("/sessions/revoke-others", summary="退出其他设备")
def revoke_other_sessions(request: Request) -> dict[str, Any]:
    user = current_user(request)
    revoked = runtime.auth.revoke_others(user.id, keep_token=runtime.auth.token_from_request(request))
    return {"revoked": revoked}


# =============================================================== 用户管理（管理员）


def _users_payload() -> dict[str, Any]:
    users = runtime.auth.users.list()
    return {
        "users": [u.to_public() for u in users],
        "counts": {
            "total": len(users),
            "active": sum(1 for u in users if u.active),
            "admins": sum(1 for u in users if u.is_admin),
        },
    }


@router.get("/users", summary="账号列表（管理员）")
def list_users(request: Request) -> dict[str, Any]:
    admin_user(request)
    return _users_payload()


@router.post("/users", summary="创建账号（管理员）")
def create_user(payload: UserCreatePayload, request: Request) -> dict[str, Any]:
    admin_user(request)
    user, errors = runtime.auth.users.create(payload.model_dump(), password=payload.password)
    if user is None:
        raise HTTPException(status_code=422, detail={"message": "；".join(errors), "errors": errors})
    return {**_users_payload(), "created": user.to_public()}


@router.patch("/users/{user_id}", summary="修改账号（管理员）")
def patch_user(user_id: str, payload: UserPatchPayload, request: Request) -> dict[str, Any]:
    admin_user(request)
    patch = payload.model_dump(exclude_none=True)
    new_password = patch.pop("new_password", None)

    user, errors = runtime.auth.users.update(user_id, patch)
    if user is None:
        raise HTTPException(status_code=422, detail={"message": "；".join(errors), "errors": errors})

    if new_password:
        errors = runtime.auth.users.set_password(user_id, new_password, must_change=True)
        if errors:
            raise HTTPException(status_code=422, detail={"message": "；".join(errors), "errors": errors})
        runtime.auth.revoke_others(user_id)  # 改密后强制重新登录
    return _users_payload()


@router.delete("/users/{user_id}", summary="删除账号（管理员）")
def delete_user(user_id: str, request: Request) -> dict[str, Any]:
    admin = admin_user(request)
    if admin.id == user_id:
        raise HTTPException(status_code=422, detail="不能删除当前登录的账号")
    deleted, errors = runtime.auth.users.delete(user_id)
    if not deleted:
        raise HTTPException(status_code=422, detail={"message": "；".join(errors), "errors": errors})
    runtime.auth.revoke_others(user_id)
    return _users_payload()

"""账号体系：用户存储 + 密码哈希 + 无状态会话令牌。

设计要点
--------
1. **密码哈希**：``PBKDF2-HMAC-SHA256`` + 随机 16 字节盐，迭代次数写进哈希串
   (``pbkdf2_sha256$迭代$盐$摘要``)，日后调参不会让旧密码失效。
2. **会话令牌**：``base64url(payload).hmac_sha256``，payload 含 ``jti/uid/iat/exp``。
   校验本身不查库（无状态），因此重启服务后仍可用；服务端另外把会话记录写进
   数据库 ``sessions`` 表，用于个人中心展示「登录设备」以及
   「退出登录 / 退出其他设备」的即时吊销——多实例部署时吊销也能全局生效。
3. **持久化**：用户与会话写入数据库（``users`` / ``sessions`` 表），签名密钥
   写入 ``kv`` 表，保证重启后令牌依旧有效。历史 ``config/users.json`` 与
   ``config/.auth_secret`` 只在首次启动时作为迁移来源。
4. **首次运行**：自动播种管理员 ``admin``，并标记 ``must_change_password``，
   登录页在未改密码前会给出提示。
5. **个人偏好**：主题等个性化配置挂在 ``user.preferences``，跟随账号同步。
"""

from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .db import Database, from_json, get_database, to_json

# ---------------------------------------------------------------- 常量

#: 令牌签名密钥在 ``kv`` 表中的键名
AUTH_SECRET_KEY = "auth.secret"

PBKDF2_ITERATIONS = 120_000
PASSWORD_MIN_LENGTH = 6

DEFAULT_ADMIN_USERNAME = "admin"
DEFAULT_ADMIN_PASSWORD = "admin123"

ROLE_ADMIN = "admin"
ROLE_USER = "user"
ROLES = (ROLE_ADMIN, ROLE_USER)

#: 主题取值的白名单：前端提交的偏好必须落在这些集合里，避免脏数据
THEME_AXES: dict[str, set[str]] = {
    "mode": {"dark", "light", "auto"},
    "accent": {"blue", "violet", "cyan", "emerald", "amber", "rose"},
    "radius": {"sharp", "standard", "round"},
    "density": {"compact", "standard", "relaxed"},
    "glass": {"on", "off"},
}

DEFAULT_THEME: dict[str, str] = {
    "mode": "dark",
    "accent": "blue",
    "radius": "standard",
    "density": "standard",
    "glass": "on",
}

#: 不要求登录的接口：健康检查与登录/注册本身
PUBLIC_API_PATHS = frozenset(
    {"/api/health", "/api/auth/config", "/api/auth/login", "/api/auth/register"}
)

#: 会话令牌 Cookie：浏览器直连的接口文档页（/docs）无法自定义请求头，
#: 只能靠 Cookie 携带当前账号的鉴权信息。
TOKEN_COOKIE = "agent_platform_session"


def is_doc_path(path: str) -> bool:
    """是否为接口文档相关路径（含 ``/docs`` 下的子资源）。"""
    text = str(path or "")
    return text in ("/docs", "/redoc", "/openapi.json") or text.startswith("/docs/")


#: 接口文档的可见范围：member=所有登录用户，admin=仅管理员，public=匿名公开
DOCS_ACCESS_CHOICES = ("member", "admin", "public")
DEFAULT_DOCS_ACCESS = "member"

#: 令牌有效期上限（开启「记住我」时）
REMEMBER_SECONDS = 30 * 24 * 3600


# ---------------------------------------------------------------- 密码


def hash_password(password: str, *, iterations: int = PBKDF2_ITERATIONS, salt: bytes | None = None) -> str:
    """把明文口令转成可存档的哈希串。"""
    salt = salt if salt is not None else os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", str(password).encode("utf-8"), salt, int(iterations))
    return f"pbkdf2_sha256${int(iterations)}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    """恒定时间比对，任何格式异常一律视为不匹配。"""
    try:
        algo, iterations, salt_hex, digest_hex = str(encoded).split("$")
        if algo != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", str(password).encode("utf-8"), bytes.fromhex(salt_hex), int(iterations)
        )
    except (ValueError, TypeError, AttributeError):
        return False
    return hmac.compare_digest(digest.hex(), digest_hex)


def validate_password(password: str) -> list[str]:
    text = str(password or "")
    if len(text) < PASSWORD_MIN_LENGTH:
        return [f"密码至少 {PASSWORD_MIN_LENGTH} 位"]
    if text.strip() != text:
        return ["密码首尾不能包含空白字符"]
    return []


def sanitize_theme(raw: Any) -> dict[str, str]:
    """只保留白名单内的取值，缺项用默认值补齐。"""
    theme = dict(DEFAULT_THEME)
    if isinstance(raw, dict):
        for key, allowed in THEME_AXES.items():
            value = raw.get(key)
            if isinstance(value, str) and value in allowed:
                theme[key] = value
    return theme


def validate_username(username: str) -> list[str]:
    text = str(username or "").strip()
    if not (3 <= len(text) <= 32):
        return ["用户名长度需在 3-32 之间"]
    if not all(ch.isalnum() or ch in "_-." for ch in text):
        return ["用户名只能包含字母、数字、下划线、短横线和点"]
    return []


# ---------------------------------------------------------------- 用户


@dataclass
class User:
    """一个账号（``password_hash`` 仅内部使用，绝不对外返回）。"""

    id: str
    username: str
    nickname: str = ""
    email: str = ""
    role: str = ROLE_USER
    avatar: str = ""
    color: str = "#5b8cff"
    bio: str = ""
    active: bool = True
    must_change_password: bool = False
    created_at: float = field(default_factory=time.time)
    last_login_at: float = 0.0
    preferences: dict[str, Any] = field(default_factory=dict)
    password_hash: str = ""

    @property
    def is_admin(self) -> bool:
        return self.role == ROLE_ADMIN

    def preferences_view(self) -> dict[str, Any]:
        prefs = copy.deepcopy(self.preferences or {})
        prefs["theme"] = sanitize_theme(prefs.get("theme"))
        return prefs

    def to_public(self) -> dict[str, Any]:
        """对外视图：隐去哈希，并给出「显示名」「首字母」等前端便利字段。"""
        data = {
            "id": self.id,
            "username": self.username,
            "nickname": self.nickname,
            "email": self.email,
            "role": self.role,
            "avatar": self.avatar,
            "color": self.color,
            "bio": self.bio,
            "active": self.active,
            "must_change_password": self.must_change_password,
            "created_at": self.created_at,
            "last_login_at": self.last_login_at,
            "preferences": self.preferences_view(),
        }
        data["display_name"] = self.nickname or self.username
        data["initial"] = (self.nickname or self.username or "?")[:1].upper()
        return data

    def to_storage(self) -> dict[str, Any]:
        return asdict(self)


#: 新建用户的默认偏好
DEFAULT_PREFERENCES: dict[str, Any] = {"theme": dict(DEFAULT_THEME)}


def _normalize_color(value: str, fallback: str = "#5b8cff") -> str:
    text = str(value or "").strip()
    if len(text) == 7 and text.startswith("#") and all(ch in "0123456789abcdefABCDEF" for ch in text[1:]):
        return text.lower()
    return fallback


def _normalize_avatar(value: str) -> str:
    """头像只接受 1-2 个字符（emoji 通常占 2 个 UTF-16 码元）。"""
    text = str(value or "").strip()
    return text[:4]


def build_user(payload: dict[str, Any], *, password: str | None = None) -> tuple[User | None, list[str]]:
    """校验并构造一个用户（不含 id 分配）。"""
    payload = payload or {}
    errors: list[str] = []

    username = str(payload.get("username") or "").strip()
    errors += validate_username(username)

    role = str(payload.get("role") or ROLE_USER)
    if role not in ROLES:
        errors.append(f"角色必须是 {list(ROLES)} 之一")

    email = str(payload.get("email") or "").strip()
    if email and ("@" not in email or "." not in email.split("@")[-1]):
        errors.append("邮箱格式不正确")

    password_hash = ""
    if password is not None:
        errors += validate_password(password)
        if not errors:
            password_hash = hash_password(password)

    if errors:
        return None, errors

    user = User(
        id=uuid.uuid4().hex[:12],
        username=username,
        nickname=str(payload.get("nickname") or "").strip(),
        email=email,
        role=role,
        avatar=_normalize_avatar(payload.get("avatar") or ""),
        color=_normalize_color(payload.get("color") or ""),
        bio=str(payload.get("bio") or "").strip()[:200],
        active=bool(payload.get("active", True)),
        must_change_password=bool(payload.get("must_change_password", False)),
        preferences={"theme": sanitize_theme((payload.get("preferences") or {}).get("theme"))},
        password_hash=password_hash,
    )
    return user, []


# ---------------------------------------------------------------- 用户存储


class UserStore:
    """用户表 ``users`` 的读写封装（内存缓存 + 数据库持久化）。

    内存里保留一份 ``_users`` 缓存，是为了让 ``by_username`` / ``admin_count``
    这类查询保持 O(1)，同时把「唯一管理员不可停用」等业务规则写得更直白；
    真正的持久化由 ``save()`` 整体同步到数据库完成。
    ``path`` 仅用于首次启动时把历史 ``users.json`` 迁移进库。
    """

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        autosave: bool = True,
        db: Database | None = None,
    ) -> None:
        self.path = Path(path) if path else None
        self.autosave = autosave
        self._db = db
        self._lock = threading.Lock()
        self._users: dict[str, User] = {}

    # ------------------------------------------------ 装配

    def attach(self, db: Database, path: str | Path | None = None) -> None:
        """绑定数据源；``bootstrap`` 每次调用都会把存储重定向到当前数据库。"""
        self._db = db
        if path is not None:
            self.path = Path(path)
        self._users = {}

    @property
    def db(self) -> Database:
        return self._database()

    def _database(self) -> Database:
        """未显式绑定数据库时，按 ``path`` 所在目录派生一个 SQLite 库。"""
        if self._db is None:
            base = self.path.parent if self.path else None
            self._db = get_database(None, base_dir=base)
        return self._db

    # ------------------------------------------------ 读写

    def load(self, *, seed_default: bool = True) -> dict[str, User]:
        users: dict[str, User] = {}
        for row in self._database().query("SELECT * FROM users ORDER BY created_at"):
            user = self._from_row(row)
            if user is not None:
                users[user.id] = user

        self._users = users
        if seed_default and not self._users:
            self.seed_admin()
        return self._users

    def migrate_legacy(self) -> int:
        """把历史 ``users.json`` 导入数据库；表非空或文件不存在时什么都不做。"""
        db = self._database()
        if db.count("users") or not (self.path and self.path.is_file()):
            return 0
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return 0
        items = raw.get("users") if isinstance(raw, dict) else None
        if not isinstance(items, list):
            return 0

        imported = 0
        with db.transaction() as tx:
            for item in items:
                user = self._from_item(item)
                if user is None:
                    continue
                tx.upsert("users", {"id": user.id}, self._to_row(user))
                imported += 1
        return imported

    def _from_item(self, item: dict[str, Any]) -> User | None:
        if not isinstance(item, dict) or not item.get("username"):
            return None
        try:
            user = User(
                id=str(item.get("id") or uuid.uuid4().hex[:12]),
                username=str(item["username"]),
                nickname=str(item.get("nickname") or ""),
                email=str(item.get("email") or ""),
                role=str(item.get("role") or ROLE_USER),
                avatar=_normalize_avatar(item.get("avatar") or ""),
                color=_normalize_color(item.get("color") or ""),
                bio=str(item.get("bio") or "")[:200],
                active=bool(item.get("active", True)),
                must_change_password=bool(item.get("must_change_password", False)),
                created_at=float(item.get("created_at") or time.time()),
                last_login_at=float(item.get("last_login_at") or 0.0),
                preferences={"theme": sanitize_theme((item.get("preferences") or {}).get("theme"))},
                password_hash=str(item.get("password_hash") or ""),
            )
        except (TypeError, ValueError):
            return None
        if user.role not in ROLES:
            user.role = ROLE_USER
        return user

    def _from_row(self, row: dict[str, Any]) -> User | None:
        """数据库行 → :class:`User`（``preferences`` 需先解 JSON）。"""
        item = dict(row)
        item["preferences"] = from_json(row.get("preferences"), {})
        return self._from_item(item)

    def _to_row(self, user: User) -> dict[str, Any]:
        """ :class:`User` → 数据库行；``username_key`` 保留小写副本用于唯一约束。"""
        return {
            "username": user.username,
            "username_key": user.username.strip().lower(),
            "nickname": user.nickname,
            "email": user.email,
            "role": user.role,
            "avatar": user.avatar,
            "color": user.color,
            "bio": user.bio,
            "active": bool(user.active),
            "must_change_password": bool(user.must_change_password),
            "created_at": float(user.created_at or 0),
            "last_login_at": float(user.last_login_at or 0),
            "preferences": to_json(user.preferences or {}),
            "password_hash": user.password_hash or "",
        }

    def save(self) -> None:
        """把内存中的用户集合整体同步到数据库（含删除已移出缓存的账号）。"""
        if not self.autosave:
            return
        db = self._database()
        with self._lock:
            users = self._ordered()
            alive = {u.id for u in users}
            with db.transaction() as tx:
                for user in users:
                    tx.upsert("users", {"id": user.id}, self._to_row(user))
                for row in tx.query("SELECT id FROM users"):
                    if row["id"] not in alive:
                        tx.delete("users", {"id": row["id"]})

    def seed_admin(self) -> User:
        """播种默认管理员；仅当库里一个用户都没有时调用。"""
        user = User(
            id=uuid.uuid4().hex[:12],
            username=DEFAULT_ADMIN_USERNAME,
            nickname="平台管理员",
            role=ROLE_ADMIN,
            avatar="🛡️",
            color="#5b8cff",
            bio="首次运行自动创建，请尽快修改密码。",
            must_change_password=True,
            preferences={"theme": dict(DEFAULT_THEME)},
            password_hash=hash_password(DEFAULT_ADMIN_PASSWORD),
        )
        self._users[user.id] = user
        self.save()
        return user

    # ------------------------------------------------ 查询

    def _ordered(self) -> list[User]:
        return sorted(self._users.values(), key=lambda u: (not u.is_admin, u.created_at))

    def list(self) -> list[User]:
        return self._ordered()

    def count(self) -> int:
        return len(self._users)

    def admin_count(self) -> int:
        return sum(1 for u in self._users.values() if u.is_admin)

    def get(self, user_id: str) -> User | None:
        return self._users.get(user_id)

    def by_username(self, username: str) -> User | None:
        needle = str(username or "").strip().lower()
        return next((u for u in self._users.values() if u.username.lower() == needle), None)

    def has_default_admin_password(self) -> bool:
        admin = self.by_username(DEFAULT_ADMIN_USERNAME)
        return bool(admin and verify_password(DEFAULT_ADMIN_PASSWORD, admin.password_hash))

    # ------------------------------------------------ 变更

    def create(self, payload: dict[str, Any], *, password: str) -> tuple[User | None, list[str]]:
        user, errors = build_user(payload, password=password)
        if user is None:
            return None, errors
        if self.by_username(user.username):
            return None, [f"用户名 '{user.username}' 已被占用"]
        self._users[user.id] = user
        self.save()
        return user, []

    def update(self, user_id: str, patch: dict[str, Any]) -> tuple[User | None, list[str]]:
        user = self._users.get(user_id)
        if user is None:
            return None, ["用户不存在"]
        patch = patch or {}
        errors: list[str] = []

        if "nickname" in patch:
            user.nickname = str(patch["nickname"] or "").strip()[:32]
        if "email" in patch:
            email = str(patch["email"] or "").strip()
            if email and ("@" not in email or "." not in email.split("@")[-1]):
                errors.append("邮箱格式不正确")
            else:
                user.email = email
        if "bio" in patch:
            user.bio = str(patch["bio"] or "").strip()[:200]
        if "avatar" in patch:
            user.avatar = _normalize_avatar(patch["avatar"])
        if "color" in patch:
            user.color = _normalize_color(patch["color"], user.color)
        if "role" in patch:
            role = str(patch["role"])
            if role not in ROLES:
                errors.append(f"角色必须是 {list(ROLES)} 之一")
            elif user.is_admin and role != ROLE_ADMIN and self.admin_count() <= 1:
                errors.append("至少需要保留一个管理员账号")
            else:
                user.role = role
        if "active" in patch:
            active = bool(patch["active"])
            if not active and user.is_admin and self.admin_count() <= 1:
                errors.append("不能停用唯一的管理员账号")
            else:
                user.active = active
        if "preferences" in patch and isinstance(patch["preferences"], dict):
            theme = patch["preferences"].get("theme")
            if theme is not None:
                user.preferences = {**(user.preferences or {}), "theme": sanitize_theme(theme)}

        if errors:
            return None, errors
        self.save()
        return user, []

    def set_password(self, user_id: str, password: str, *, must_change: bool = False) -> list[str]:
        user = self._users.get(user_id)
        if user is None:
            return ["用户不存在"]
        errors = validate_password(password)
        if errors:
            return errors
        user.password_hash = hash_password(password)
        user.must_change_password = must_change
        self.save()
        return []

    def rename(self, user_id: str, username: str) -> list[str]:
        user = self._users.get(user_id)
        if user is None:
            return ["用户不存在"]
        username = str(username or "").strip()
        errors = validate_username(username)
        if errors:
            return errors
        other = self.by_username(username)
        if other is not None and other.id != user_id:
            return [f"用户名 '{username}' 已被占用"]
        user.username = username
        self.save()
        return []

    def delete(self, user_id: str) -> tuple[bool, list[str]]:
        user = self._users.get(user_id)
        if user is None:
            return False, ["用户不存在"]
        if user.is_admin and self.admin_count() <= 1:
            return False, ["不能删除唯一的管理员账号"]
        self._users.pop(user_id, None)
        self.save()
        return True, []

    def authenticate(self, username: str, password: str) -> tuple[User | None, list[str]]:
        user = self.by_username(username)
        if user is None or not verify_password(password, user.password_hash):
            return None, ["用户名或密码不正确"]
        if not user.active:
            return None, ["该账号已被停用，请联系管理员"]
        return user, []


# ---------------------------------------------------------------- 会话


class AuthManager:
    """用户存储 + 会话令牌的组合入口（账号、会话、签名密钥全部落库）。"""

    #: ``sessions`` 表里对外暴露的列
    SESSION_COLUMNS = ("jti", "user_id", "created_at", "last_seen", "expires_at")

    def __init__(
        self,
        path: str | Path | None = None,
        secret_path: str | Path | None = None,
        *,
        db: Database | None = None,
    ) -> None:
        self.users = UserStore(path, db=db)
        self.secret_path = Path(secret_path) if secret_path else None
        self.require_login = True
        self.allow_registration = True
        self.session_hours = 72
        self.docs_access = DEFAULT_DOCS_ACCESS

        self._db = db
        self._secret = os.urandom(32)
        self._lock = threading.Lock()

    # ------------------------------------------------ 装配

    def attach(self, db: Database, path: str | Path | None = None, secret_path: str | Path | None = None) -> None:
        """绑定数据源（账号表、会话表、签名密钥都跟着走）。"""
        self._db = db
        self.users.attach(db, path)
        if secret_path is not None:
            self.secret_path = Path(secret_path)

    @property
    def db(self) -> Database:
        if self._db is None:
            self._db = self.users._database()
        return self._db

    # ------------------------------------------------ 生命周期

    def load(self) -> None:
        self.users.load()
        self._secret = self._load_secret()
        self.purge_sessions()

    def migrate_legacy(self) -> int:
        """迁移历史 ``users.json``（签名密钥的兼容在 :meth:`_load_secret` 里顺带处理）。"""
        return self.users.migrate_legacy()

    def _load_secret(self) -> bytes:
        """签名密钥优先读库；库里没有时兼容读取历史 ``.auth_secret`` 文件。"""
        db = self.db
        stored = db.kv_get(AUTH_SECRET_KEY)
        if isinstance(stored, str) and stored:
            try:
                raw = bytes.fromhex(stored)
                if len(raw) >= 16:
                    return raw
            except ValueError:
                pass

        if self.secret_path and self.secret_path.is_file():
            try:
                raw = bytes.fromhex(self.secret_path.read_text(encoding="utf-8").strip())
                if len(raw) >= 16:
                    db.kv_set(AUTH_SECRET_KEY, raw.hex())
                    return raw
            except (ValueError, OSError):
                pass

        secret = os.urandom(32)
        db.kv_set(AUTH_SECRET_KEY, secret.hex())
        return secret

    def purge_sessions(self) -> int:
        """清理已过期的会话记录。

        注意：**已吊销但尚未过期**的记录必须保留，否则 ``resolve`` 会把它
        当成「重启后自恢复」重新接纳，吊销就形同虚设。
        """
        return self.db.execute("DELETE FROM sessions WHERE expires_at <= ?", [time.time()])

    def configure(
        self,
        *,
        require_login: bool,
        session_hours: int,
        allow_registration: bool,
        docs_access: str = DEFAULT_DOCS_ACCESS,
    ) -> None:
        self.require_login = bool(require_login)
        self.session_hours = max(1, int(session_hours))
        self.allow_registration = bool(allow_registration)
        self.docs_access = docs_access if docs_access in DOCS_ACCESS_CHOICES else DEFAULT_DOCS_ACCESS

    # ------------------------------------------------ 令牌

    @staticmethod
    def _b64e(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    @staticmethod
    def _b64d(text: str) -> bytes:
        padding = "=" * (-len(text) % 4)
        return base64.urlsafe_b64decode(text + padding)

    def _sign(self, payload: bytes) -> str:
        return self._b64e(hmac.new(self._secret, payload, hashlib.sha256).digest())

    def mint(self, user: User, *, remember: bool = False) -> tuple[str, float]:
        """签发令牌，返回 (token, 过期时间戳)。"""
        ttl = self.session_hours * 3600
        if remember:
            ttl = max(ttl, REMEMBER_SECONDS)
        now = time.time()
        expires_at = now + ttl
        payload = {
            "jti": uuid.uuid4().hex,
            "uid": user.id,
            "iat": int(now),
            "exp": int(expires_at),
        }
        body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        token = f"{self._b64e(body)}.{self._sign(body)}"
        self.db.upsert(
            "sessions",
            {"jti": payload["jti"]},
            {
                "user_id": user.id,
                "created_at": now,
                "last_seen": now,
                "expires_at": expires_at,
                "revoked_at": None,
            },
        )
        return token, expires_at

    def parse(self, token: str) -> dict[str, Any] | None:
        """校验签名与有效期，返回 payload；任何异常都返回 None。"""
        text = str(token or "").strip()
        if "." not in text:
            return None
        body_b64, signature = text.split(".", 1)
        try:
            body = self._b64d(body_b64)
        except (ValueError, TypeError):
            return None
        if not hmac.compare_digest(self._sign(body), signature):
            return None
        try:
            payload = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None
        if not isinstance(payload, dict) or "uid" not in payload or "jti" not in payload:
            return None
        if float(payload.get("exp") or 0) <= time.time():
            return None
        return payload

    def resolve(self, token: str) -> User | None:
        payload = self.parse(token)
        if payload is None:
            return None
        jti = str(payload["jti"])
        now = time.time()
        session = self.db.query_one("SELECT * FROM sessions WHERE jti = ?", [jti])
        if session is not None and session.get("revoked_at"):
            return None
        if session is None:
            # 服务重启后按令牌自恢复会话记录，让「登录设备」展示与主动下线保持一致
            self.db.upsert(
                "sessions",
                {"jti": jti},
                {
                    "user_id": str(payload["uid"]),
                    "created_at": float(payload.get("iat") or now),
                    "last_seen": now,
                    "expires_at": float(payload.get("exp") or now),
                    "revoked_at": None,
                },
            )
        else:
            self.db.update("sessions", {"jti": jti}, {"last_seen": now})
        user = self.users.get(str(payload["uid"]))
        if user is None or not user.active:
            return None
        return user

    # ------------------------------------------------ 吊销

    def _forget(self, jti: str, expires_at: float) -> None:
        """吊销会话：保留记录并打上吊销时间，令牌到期后再由清理任务删除。"""
        now = time.time()
        if self.db.exists("sessions", {"jti": jti}):
            self.db.update("sessions", {"jti": jti}, {"revoked_at": now})
        else:
            self.db.insert(
                "sessions",
                {
                    "jti": jti,
                    "user_id": "",
                    "created_at": now,
                    "last_seen": now,
                    "expires_at": float(expires_at or 0),
                    "revoked_at": now,
                },
            )

    # ------------------------------------------------ 请求 / 会话

    @staticmethod
    def token_from_request(request: Any) -> str:
        """按 ``Authorization`` → ``X-Auth-Token`` → 会话 Cookie 的顺序取令牌。

        Cookie 是给浏览器直连场景用的（例如打开 ``/docs`` 调试接口），
        API 调用方依旧推荐显式携带请求头。
        """
        header = ""
        try:
            header = request.headers.get("authorization") or ""
        except AttributeError:  # pragma: no cover - 非 HTTP 场景
            header = ""
        if header.lower().startswith("bearer "):
            return header[7:].strip()
        try:
            explicit = (request.headers.get("x-auth-token") or "").strip()
        except AttributeError:  # pragma: no cover
            explicit = ""
        if explicit:
            return explicit
        try:
            return str(request.cookies.get(TOKEN_COOKIE) or "").strip()
        except AttributeError:  # pragma: no cover
            return ""

    def user_from_request(self, request: Any) -> User | None:
        return self.resolve(self.token_from_request(request))

    def requires_login(self, path: str) -> bool:
        """该路径是否必须登录才能访问。

        * 白名单接口（健康检查、登录/注册）放行；
        * 接口文档（``/docs``、``/redoc``、``/openapi.json``）默认**必须登录**，
          选「匿名公开」时不再要求登录；
        * 其余 ``/api`` 接口必须登录；
        * 静态页面与前端资源放行（未登录时由前端登录门接管）。
        """
        text = str(path or "")
        if text in PUBLIC_API_PATHS:
            return False
        if is_doc_path(text):
            return self.docs_access != "public"
        return text.startswith("/api/")

    def is_public(self, path: str) -> bool:
        """``requires_login`` 的反面，供中间件之外的地方复用。"""
        return not self.requires_login(path)

    def docs_gate(self, user: User | None) -> str:
        """接口文档的访问决策，让中间件能区分「未登录」与「已登录但无权」。

        * ``allow``     —— 放行（关闭登录校验 / 匿名公开 / 角色满足）
        * ``login``     —— 未登录，先去登录
        * ``forbidden`` —— 已登录但角色不足（``admin`` 策略下的普通用户）
        """
        if not self.require_login or self.docs_access == "public":
            return "allow"
        if user is None:
            return "login"
        if self.docs_access == ROLE_ADMIN and not user.is_admin:
            return "forbidden"
        return "allow"

    def can_view_docs(self, user: User | None) -> bool:
        """当前账号能否查看接口文档。"""
        return self.docs_gate(user) == "allow"

    @staticmethod
    def session_cookie(token: str, expires_at: float) -> dict[str, Any]:
        """会话 Cookie 的属性：HttpOnly + SameSite=Lax，不写 ``secure`` 以兼容本地 HTTP。"""
        return {
            "key": TOKEN_COOKIE,
            "value": token,
            "max_age": max(0, int(float(expires_at or 0) - time.time())),
            "httponly": True,
            "samesite": "lax",
            "path": "/",
        }

    def revoke(self, token: str) -> bool:
        payload = self.parse(token)
        if payload is None:
            return False
        self._forget(str(payload["jti"]), float(payload.get("exp") or 0))
        return True

    def sessions_of(self, user_id: str) -> list[dict[str, Any]]:
        """列出某账号当前有效的登录会话（已吊销 / 已过期的不算）。"""
        rows = self.db.query(
            f"SELECT {', '.join(self.SESSION_COLUMNS)} FROM sessions "
            "WHERE user_id = ? AND revoked_at IS NULL AND expires_at > ? ORDER BY last_seen DESC",
            [user_id, time.time()],
        )
        return [dict(row) for row in rows]

    def revoke_others(self, user_id: str, keep_token: str = "") -> int:
        """吊销该账号的其它会话，只保留 ``keep_token`` 对应的那一个。"""
        keep_jti = (self.parse(keep_token) or {}).get("jti") if keep_token else None
        now = time.time()
        rows = self.db.query(
            "SELECT jti FROM sessions WHERE user_id = ? AND revoked_at IS NULL", [user_id]
        )
        targets = [row["jti"] for row in rows if row["jti"] != keep_jti]
        for jti in targets:
            self.db.update("sessions", {"jti": jti}, {"revoked_at": now})
        return len(targets)

    def touch_login(self, user: User) -> None:
        user.last_login_at = time.time()
        self.users.save()

"""平台层测试：设置中心、运行记录、技能库启停、工作台 HTTP 接口。

覆盖三块：

* ``SettingsStore`` —— Schema 校验、原子持久化、密钥掩码与回写保护
* ``RunRegistry``   —— 运行记录的增删查、持久化、数量裁剪
* 平台 HTTP 接口    —— 概览 / 设置 / 技能库 / 上传 → 运行记录 → 知识库 → 重放

平台接口测试统一把全局 ``runtime`` 单例重新 bootstrap 到临时目录，
数据库 / 设置 / 账号 / 运行记录都落在临时目录，避免污染仓库里的
``data/platform.db``：

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import base64
import importlib
import importlib.util
import io
import json
import os
import socket
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.env import ensure_dotenv  # noqa: E402

#: Milvus 地址取自 .env 的 ${MILVUS_URI}，与生效流水线共用同一份配置
ensure_dotenv()
MILVUS_URI = os.environ.get("MILVUS_URI") or "http://localhost:19530"


def _milvus_available() -> bool:
    """Milvus 是否就绪：pymilvus 已安装且 ``${MILVUS_URI}`` 指向的端口可连。

    生效流水线的入库环节是 ``milvus_store``，因此「上传成功」类用例依赖 Milvus
    （``cd deploy/milvus && docker compose up -d``）；未启动时自动跳过，
    拒绝上传、设置中心等其余用例照常执行。
    """
    if importlib.util.find_spec("pymilvus") is None:
        return False
    parsed = urlparse(MILVUS_URI)
    try:
        with socket.create_connection((parsed.hostname or "127.0.0.1", parsed.port or 19530), timeout=1.5):
            return True
    except OSError:
        return False


MILVUS_OK = _milvus_available()
NEEDS_MILVUS = f"需要 Milvus（{MILVUS_URI}）：cd deploy/milvus && docker compose up -d"

#: 「上传成功」类用例会走生效流水线真正落库 Milvus，这里把集合临时改到一个测试专用名，
#: 跑完整组用例再删掉——向量库是持久化的，不隔离就会把测试数据留在真实集合里。
TEST_COLLECTION = f"agent_full_test_{uuid.uuid4().hex[:8]}"
os.environ["MILVUS_COLLECTION"] = TEST_COLLECTION


def tearDownModule() -> None:
    if not MILVUS_OK:
        return
    from pymilvus import MilvusClient

    client = MilvusClient(uri=MILVUS_URI)
    try:
        if client.has_collection(TEST_COLLECTION):
            client.drop_collection(TEST_COLLECTION)
    finally:
        client.close()

from app.core.auth import (  # noqa: E402
    DEFAULT_ADMIN_PASSWORD,
    DEFAULT_ADMIN_USERNAME,
    TOKEN_COOKIE,
    AuthManager,
    hash_password,
    sanitize_theme,
    verify_password,
)
from app.core.context import CHUNKS, TEXT, PipelineContext  # noqa: E402
from app.core.runs import RunRecord, RunRegistry  # noqa: E402
from app.core.settings import (  # noqa: E402
    MASK,
    SETTINGS_SCHEMA,
    SettingsStore,
    default_settings,
    is_masked,
    mask_secret,
    validate_settings,
)

CONFIG = ROOT / "config" / "pipeline.yaml"
SKILLS_DIR = ROOT / "ext_plugins"

SAMPLE = "平台支持多 Agent 串联与插拔式插件。向量检索命中关键词。".encode("utf-8")


# =============================================================== 设置中心


class TestSettingsStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "settings.json"
        self.store = SettingsStore(self.path)
        self.store.load()

    def tearDown(self):
        self.tmp.cleanup()

    def test_defaults_follow_schema(self):
        data = default_settings()
        for group, schema in SETTINGS_SCHEMA.items():
            self.assertIn(group, data)
            for key in schema["fields"]:
                self.assertIn(key, data[group], f"{group}.{key} 缺少默认值")
        self.assertEqual(data["providers"], [])

    def test_patch_persists_and_reloads(self):
        _, errors = self.store.patch({"upload": {"max_mb": 33}})
        self.assertEqual(errors, [])
        self.assertFalse(self.path.is_file(), "设置已改为落库，不应再写 JSON 文件")
        self.assertEqual(self.store.db.kv_get("settings")["upload"]["max_mb"], 33)

        reloaded = SettingsStore(self.path)
        reloaded.load()
        self.assertEqual(reloaded.get("upload.max_mb"), 33)

    def test_invalid_value_not_persisted(self):
        before = self.store.get("upload.max_mb")
        _, errors = self.store.patch({"upload": {"max_mb": 99999}})
        self.assertTrue(errors, "越界值应被拒绝")
        self.assertEqual(self.store.get("upload.max_mb"), before)

        reloaded = SettingsStore(self.path)
        reloaded.load()
        self.assertEqual(reloaded.get("upload.max_mb"), before, "校验失败不应落库")

    def test_list_field_accepts_comma_string(self):
        _, errors = self.store.patch({"upload": {"allowed_extensions": ".md, .txt"}})
        self.assertEqual(errors, [])
        self.assertEqual(self.store.get("upload.allowed_extensions"), [".md", ".txt"])

    def test_validate_merges_partial(self):
        merged, errors = validate_settings({"retrieval": {"default_top_k": 9}})
        self.assertEqual(errors, [])
        self.assertEqual(merged["retrieval"]["default_top_k"], 9)
        # 未提交的分组保持默认
        self.assertEqual(merged["upload"]["max_mb"], 20)

    def test_reset_restores_defaults(self):
        self.store.patch({"upload": {"max_mb": 5}})
        self.store.reset()
        self.assertEqual(self.store.get("upload.max_mb"), 20)


class TestSecretMasking(unittest.TestCase):
    def test_mask_secret(self):
        self.assertEqual(mask_secret(""), "")
        self.assertEqual(mask_secret("short"), MASK)
        masked = mask_secret("sk-1234567890abcdef")
        self.assertTrue(is_masked(masked))
        self.assertNotIn("1234567890", masked)

    def test_provider_key_is_masked_and_never_plaintext(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SettingsStore(Path(tmp) / "s.json")
            store.load()
            secret = "sk-abcdef1234567890"
            _, errors = store.upsert_provider(
                {"name": "OpenAI", "kind": "openai", "base_url": "https://api.openai.com", "api_key": secret}
            )
            self.assertEqual(errors, [])

            listed = store.list_providers(masked=True)
            self.assertNotIn(secret, str(listed), "对外列表不得出现明文密钥")
            self.assertTrue(listed[0]["has_key"])

            raw = store.list_providers(masked=False)
            self.assertEqual(raw[0]["api_key"], secret, "内部仍需保留真实密钥")

    def test_masked_value_on_update_keeps_original_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SettingsStore(Path(tmp) / "s.json")
            store.load()
            secret = "sk-abcdef1234567890"
            store.upsert_provider({"id": "p1", "name": "P1", "api_key": secret})

            masked = store.list_providers(masked=True)[0]["api_key"]
            # 前端把掩码值原样回写（改个名字），真实密钥不应被覆盖
            _, errors = store.upsert_provider({"id": "p1", "name": "P1-renamed", "api_key": masked})
            self.assertEqual(errors, [])
            self.assertEqual(store.get_provider("p1")["api_key"], secret)
            self.assertEqual(store.get_provider("p1")["name"], "P1-renamed")

    def test_provider_validation_and_delete(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SettingsStore(Path(tmp) / "s.json")
            store.load()
            _, errors = store.upsert_provider({"name": "", "kind": "openai"})
            self.assertTrue(any("名称" in e for e in errors))

            _, errors = store.upsert_provider({"name": "X", "kind": "nope"})
            self.assertTrue(any("类型" in e for e in errors))

            _, errors = store.upsert_provider({"name": "X", "base_url": "ftp://x"})
            self.assertTrue(any("Base URL" in e for e in errors))

            store.upsert_provider({"id": "p2", "name": "P2"})
            self.assertTrue(store.delete_provider("p2"))
            self.assertFalse(store.delete_provider("p2"))


# =============================================================== 运行记录


class TestRunRegistry(unittest.TestCase):
    def _ctx(self, filename: str) -> PipelineContext:
        ctx = PipelineContext(filename, SAMPLE, "text/plain")
        ctx.put(TEXT, SAMPLE.decode("utf-8"), producer="sandbox")
        ctx.put(CHUNKS, [], producer="sandbox")
        return ctx

    def test_record_list_get_stats(self):
        reg = RunRegistry()
        ctx = self._ctx("a.txt")
        reg.record_context(ctx, status="success", pipeline="p")

        record = reg.get(ctx.run_id)
        self.assertIsNotNone(record)
        summary = reg.list()[0]
        self.assertEqual(summary["run_id"], ctx.run_id)
        self.assertEqual(summary["filename"], "a.txt")

        stats = reg.stats()
        self.assertEqual(stats["total"], 1)
        self.assertEqual(stats["success_rate"], 1.0)

    def test_persist_and_reload(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runs.json"
            reg = RunRegistry(path)
            ctx = self._ctx("b.txt")
            reg.record_context(ctx, status="success")
            self.assertEqual(reg.db.count("runs"), 1, "运行记录应落库")

            again = RunRegistry(path)
            loaded = again.load()
            self.assertEqual(loaded, 1)
            self.assertIsNotNone(again.get(ctx.run_id))

    def test_limit_trims_oldest(self):
        reg = RunRegistry(limit=10)
        ids = []
        for i in range(15):
            ctx = self._ctx(f"f{i}.txt")
            ctx.started_at = 1000 + i  # 递增，确保 oldest 可判定
            ids.append(ctx.run_id)
            reg.record_context(ctx, status="success")
        self.assertEqual(len(reg.list(limit=100)), 10)
        self.assertIsNone(reg.get(ids[0]), "最旧的记录应被裁剪")

    def test_delete_and_clear(self):
        reg = RunRegistry()
        ctx = self._ctx("c.txt")
        reg.record_context(ctx, status="success")
        self.assertTrue(reg.delete(ctx.run_id))
        self.assertFalse(reg.delete(ctx.run_id))
        reg.record_context(self._ctx("d.txt"), status="success")
        self.assertEqual(reg.clear(), 1)
        self.assertEqual(reg.stats()["total"], 0)

    def test_error_record_metrics(self):
        reg = RunRegistry()
        ctx = self._ctx("e.txt")
        ctx.errors.append("boom")
        record = reg.record_context(ctx, status="error")
        self.assertEqual(record.summary()["failed_steps"], 0)
        self.assertEqual(reg.stats()["error"], 1)
        self.assertIsInstance(record, RunRecord)
        self.assertEqual(record.to_dict()["errors"], ["boom"])


# =============================================================== 向量仓库


class TestWarehouse(unittest.TestCase):
    def test_put_records_created_at(self):
        """warehouse.put 自动记录创建时间，知识库列表才能正常展示。"""
        from app.core.warehouse import Warehouse

        w = Warehouse()
        w.put("r1", "a.txt", object())
        items = w.list()
        self.assertEqual(len(items), 1)
        self.assertIn("created_at", items[0])
        self.assertIsInstance(items[0]["created_at"], (int, float))
        self.assertGreater(items[0]["created_at"], 1_700_000_000)

    def test_put_preserves_explicit_created_at(self):
        """调用方已提供 created_at 时不应被覆盖。"""
        from app.core.warehouse import Warehouse

        w = Warehouse()
        w.put("r1", "a.txt", object(), created_at=1_234_567_890.0)
        self.assertEqual(w.list()[0]["created_at"], 1_234_567_890.0)


# =============================================================== 账号体系


class TestAuthManager(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.auth = AuthManager(root / "users.json", root / ".auth_secret")
        self.auth.load()
        self.admin = self.auth.users.by_username(DEFAULT_ADMIN_USERNAME)

    def tearDown(self):
        self.tmp.cleanup()

    def test_default_admin_seeded_on_first_run(self):
        self.assertEqual(self.auth.users.count(), 1)
        self.assertTrue(self.admin.is_admin)
        self.assertTrue(self.auth.users.has_default_admin_password())
        self.assertNotIn("password_hash", self.admin.to_public(), "对外视图不得泄露哈希")

    def test_password_hash_roundtrip(self):
        encoded = hash_password("s3cret!")
        self.assertTrue(verify_password("s3cret!", encoded))
        self.assertFalse(verify_password("s3cret", encoded))
        self.assertNotIn("s3cret!", encoded, "明文不得出现在哈希串里")
        self.assertFalse(verify_password("whatever", "不是哈希"))
        self.assertFalse(verify_password("whatever", ""))

    def test_token_mint_resolve_and_tamper(self):
        token, expires_at = self.auth.mint(self.admin)
        self.assertGreater(expires_at, time.time())
        self.assertEqual(self.auth.resolve(token).id, self.admin.id)
        self.assertIsNone(self.auth.resolve(token + "x"), "签名被篡改应拒绝")
        self.assertIsNone(self.auth.resolve(""))
        self.assertIsNone(self.auth.resolve("a.b.c"))

    def test_expired_token_rejected(self):
        body = json.dumps(
            {"jti": "j1", "uid": self.admin.id, "iat": 0, "exp": 1},
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        token = f"{AuthManager._b64e(body)}.{self.auth._sign(body)}"
        self.assertIsNone(self.auth.parse(token), "过期令牌不应通过校验")

    def test_revoke_invalidates_token(self):
        token, _ = self.auth.mint(self.admin)
        self.assertEqual(len(self.auth.sessions_of(self.admin.id)), 1)
        self.assertTrue(self.auth.revoke(token))
        self.assertIsNone(self.auth.resolve(token))
        self.assertEqual(len(self.auth.sessions_of(self.admin.id)), 0)

    def test_session_survives_restart_but_revocation_does_not(self):
        """无状态令牌在重启后依旧可用；会话记录按令牌自恢复。"""
        token, _ = self.auth.mint(self.admin)
        restarted = AuthManager(self.auth.users.path, self.auth.secret_path)
        restarted.load()
        self.assertIsNotNone(restarted.resolve(token))
        self.assertEqual(len(restarted.sessions_of(self.admin.id)), 1)

    def test_revoke_others_keeps_current(self):
        first, _ = self.auth.mint(self.admin)
        second, _ = self.auth.mint(self.admin)
        self.assertEqual(self.auth.revoke_others(self.admin.id, keep_token=first), 1)
        self.assertIsNotNone(self.auth.resolve(first))
        self.assertIsNone(self.auth.resolve(second))

    def test_user_crud_and_uniqueness(self):
        user, errors = self.auth.users.create({"username": "alice", "nickname": "Alice"}, password="alice123")
        self.assertEqual(errors, [])
        self.assertIsNotNone(user)

        _, errors = self.auth.users.create({"username": "ALICE"}, password="alice123")
        self.assertTrue(any("占用" in e for e in errors), "用户名应不区分大小写唯一")

        _, errors = self.auth.users.create({"username": "bob"}, password="123")
        self.assertTrue(errors, "弱密码应被拒绝")

        updated, errors = self.auth.users.update(user.id, {"nickname": "小红", "email": "a@b.com"})
        self.assertEqual(errors, [])
        self.assertEqual(updated.nickname, "小红")

        _, errors = self.auth.users.update(user.id, {"email": "bad-email"})
        self.assertTrue(errors)

        deleted, errors = self.auth.users.delete(user.id)
        self.assertTrue(deleted)
        self.assertEqual(errors, [])

    def test_last_admin_is_protected(self):
        _, errors = self.auth.users.update(self.admin.id, {"role": "user"})
        self.assertTrue(any("管理员" in e for e in errors))
        _, errors = self.auth.users.update(self.admin.id, {"active": False})
        self.assertTrue(any("管理员" in e for e in errors))
        deleted, errors = self.auth.users.delete(self.admin.id)
        self.assertFalse(deleted)
        self.assertTrue(any("管理员" in e for e in errors))

    def test_authenticate_respects_active_flag(self):
        user, _ = self.auth.users.create({"username": "carol"}, password="carol123")
        self.assertIsNotNone(self.auth.users.authenticate("carol", "carol123")[0])
        self.auth.users.update(user.id, {"active": False})
        found, errors = self.auth.users.authenticate("carol", "carol123")
        self.assertIsNone(found)
        self.assertTrue(any("停用" in e for e in errors))

    def test_theme_sanitized_against_whitelist(self):
        theme = sanitize_theme({"mode": "light", "accent": "neon", "density": "compact", "extra": "x"})
        self.assertEqual(theme["mode"], "light")
        self.assertEqual(theme["density"], "compact")
        self.assertEqual(theme["accent"], "blue", "非法取值回退默认")
        self.assertNotIn("extra", theme)
        self.assertEqual(sanitize_theme(None)["mode"], "dark")

    def test_persistence_roundtrip(self):
        self.auth.users.create({"username": "dave", "nickname": "Dave"}, password="dave1234")
        again = AuthManager(self.auth.users.path, self.auth.secret_path)
        again.load()
        self.assertIsNotNone(again.users.by_username("dave"))
        self.assertEqual(again.users.count(), 2)


# =============================================================== 平台 HTTP


class PlatformHttpTest(unittest.TestCase):
    """通过 TestClient 走完整 HTTP 链路，运行时落在临时目录。

    平台默认「启用登录校验」，因此每个用例都在 ``setUp`` 里以默认管理员登录，
    并把令牌塞进客户端默认请求头；需要验证匿名访问的用例用
    ``headers={"Authorization": ""}`` 覆盖掉即可。
    """

    ANON = {"Authorization": ""}

    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient

        main = importlib.import_module("app.main")
        cls.runtime = main.runtime
        try:
            cls.client = TestClient(main.app, follow_redirects=False)
        except TypeError:  # pragma: no cover - 兼容旧版 TestClient 的参数名
            cls.client = TestClient(main.app, allow_redirects=False)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.runtime.draft = None
        self.runtime.contexts.clear()
        self.runtime.bootstrap(
            config_path=CONFIG,
            ext_dir=SKILLS_DIR,
            profiles_dir=root / "profiles",
            settings_path=root / "settings.json",
            runs_path=root / "runs.json",
            users_path=root / "users.json",
        )
        self.client.cookies.clear()
        self.token = self.login()

    def tearDown(self):
        self.client.cookies.clear()
        self.tmp.cleanup()

    def login(self, username=DEFAULT_ADMIN_USERNAME, password=DEFAULT_ADMIN_PASSWORD) -> str:
        r = self.client.post("/api/auth/login", json={"username": username, "password": password}, headers=self.ANON)
        self.assertEqual(r.status_code, 200, r.text)
        token = r.json()["token"]
        self.client.headers.update({"Authorization": f"Bearer {token}"})
        # 登录会下发会话 Cookie；本类其余用例统一走 Bearer 令牌，
        # 因此清掉 Cookie，保证「匿名访问」类断言真的匿名（Cookie 单独有用例覆盖）。
        self.client.cookies.clear()
        return token

    # -------------------------------------------------- 登录门槛

    def test_anonymous_requests_are_rejected(self):
        r = self.client.get("/api/platform/overview", headers=self.ANON)
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json()["code"], "unauthorized")
        self.assertEqual(self.client.get("/api/runs", headers=self.ANON).status_code, 401)

    def test_public_endpoints_stay_open(self):
        self.assertEqual(self.client.get("/api/health", headers=self.ANON).status_code, 200)
        cfg = self.client.get("/api/auth/config", headers=self.ANON).json()
        self.assertTrue(cfg["require_login"])
        self.assertTrue(cfg["allow_registration"])
        self.assertTrue(cfg["first_run_hint"], "默认管理员仍使用初始密码，登录页应给出提示")
        self.assertIn("theme_axes", cfg)
        # 静态页与前端资源不受影响
        self.assertEqual(self.client.get("/", headers=self.ANON).status_code, 200)

    def test_login_failure_and_bad_token(self):
        r = self.client.post(
            "/api/auth/login", json={"username": "admin", "password": "wrong"}, headers=self.ANON
        )
        self.assertEqual(r.status_code, 401)
        self.assertEqual(self.client.get("/api/auth/me", headers={"Authorization": "Bearer nope"}).status_code, 401)

    def test_require_login_can_be_disabled(self):
        self.client.put("/api/settings", json={"auth": {"require_login": False}})
        self.assertEqual(self.client.get("/api/platform/overview", headers=self.ANON).status_code, 200)

    # -------------------------------------------------- 接口文档鉴权

    def test_docs_require_login(self):
        """文档页未登录：浏览器跳回登录门，接口客户端拿到 401。"""
        browse = {"Accept": "text/html", **self.ANON}
        r = self.client.get("/docs", headers=browse)
        self.assertEqual(r.status_code, 302)
        self.assertIn("auth=required", r.headers["location"])
        self.assertEqual(self.client.get("/redoc", headers=browse).status_code, 302)
        # 非浏览器请求（无 text/html 偏好）保持接口语义
        self.assertEqual(self.client.get("/docs", headers=self.ANON).status_code, 401)
        self.assertEqual(self.client.get("/openapi.json", headers=self.ANON).status_code, 401)

    def test_logout_revokes_session_cookie(self):
        """退出登录会清除 Cookie，文档页随即不可访问。"""
        self.client.cookies.clear()
        self.client.headers.pop("Authorization", None)
        login = self.client.post(
            "/api/auth/login",
            json={"username": DEFAULT_ADMIN_USERNAME, "password": DEFAULT_ADMIN_PASSWORD},
            headers=self.ANON,
        )
        self.assertEqual(login.status_code, 200, login.text)
        self.assertIn(TOKEN_COOKIE, self.client.cookies)
        self.assertEqual(self.client.get("/docs").status_code, 200)

        self.assertEqual(self.client.post("/api/auth/logout").status_code, 200)
        self.assertNotIn(TOKEN_COOKIE, self.client.cookies)
        self.assertEqual(self.client.get("/docs", headers={"Accept": "text/html"}).status_code, 302)
        self.assertEqual(self.client.get("/openapi.json").status_code, 401)

    def test_docs_follow_current_user_session(self):
        """登录后文档页可用，并展示当前账号与 Bearer 授权入口。"""
        self.client.cookies.clear()
        self.client.headers.pop("Authorization", None)
        r = self.client.post(
            "/api/auth/login",
            json={"username": DEFAULT_ADMIN_USERNAME, "password": DEFAULT_ADMIN_PASSWORD},
            headers=self.ANON,
        )
        self.assertEqual(r.status_code, 200, r.text)

        page = self.client.get("/docs", headers={"Accept": "text/html"})
        self.assertEqual(page.status_code, 200)
        self.assertIn("swagger-ui", page.text)
        self.assertIn(DEFAULT_ADMIN_USERNAME, page.text, "文档页应显示当前登录账号")

        spec = self.client.get("/openapi.json")
        self.assertEqual(spec.status_code, 200)
        payload = spec.json()
        self.assertIn("BearerAuth", payload["components"]["securitySchemes"])
        self.assertIn("当前登录", payload["info"]["description"])

    def test_docs_open_when_login_disabled(self):
        self.client.put("/api/settings", json={"auth": {"require_login": False}})
        self.assertEqual(self.client.get("/docs", headers=self.ANON).status_code, 200)
        self.assertEqual(self.client.get("/openapi.json", headers=self.ANON).status_code, 200)

    def test_docs_access_admin_only(self):
        """按角色放开：设为「仅管理员」后普通账号被挡，管理员照常。"""
        plain = self.client.post(
            "/api/auth/register",
            json={"username": "viewer", "password": "viewer123"},
            headers=self.ANON,
        ).json()["token"]
        self.client.put("/api/settings", json={"auth": {"docs_access": "admin"}})

        as_plain = {"Authorization": f"Bearer {plain}"}
        denied = self.client.get("/openapi.json", headers=as_plain)
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(denied.json()["code"], "forbidden")

        page = self.client.get("/docs", headers={"Accept": "text/html", **as_plain})
        self.assertEqual(page.status_code, 302)
        self.assertIn("auth=forbidden", page.headers["location"])

        # 管理员不受限制，且文档里能读到当前可见范围
        self.assertEqual(self.client.get("/openapi.json").status_code, 200)
        self.assertIn("仅管理员", self.client.get("/openapi.json").json()["info"]["description"])

    def test_docs_access_public(self):
        """按角色放开：设为「匿名公开」后无需登录即可查看，业务接口仍需登录。"""
        self.client.put("/api/settings", json={"auth": {"docs_access": "public"}})
        self.assertEqual(self.client.get("/docs", headers=self.ANON).status_code, 200)
        self.assertEqual(self.client.get("/openapi.json", headers=self.ANON).status_code, 200)
        self.assertEqual(self.client.get("/api/runs", headers=self.ANON).status_code, 401)

    def test_docs_access_setting_validated(self):
        """角色取值必须在白名单内，非法值不落盘。"""
        r = self.client.put("/api/settings", json={"auth": {"docs_access": "everyone"}})
        self.assertEqual(r.status_code, 422)
        self.assertTrue(any("接口文档访问角色" in e for e in r.json()["detail"]["errors"]))
        self.assertEqual(self.runtime.auth.docs_access, "member")

    # -------------------------------------------------- 个人中心

    def test_me_profile_and_color_sanitize(self):
        me = self.client.get("/api/auth/me").json()
        self.assertEqual(me["user"]["username"], "admin")
        self.assertEqual(me["user"]["role"], "admin")
        self.assertIn("session", me)
        self.assertTrue(me["user"]["must_change_password"])

        r = self.client.put(
            "/api/auth/profile",
            json={"nickname": "平台主管", "email": "a@b.com", "avatar": "🚀", "bio": "hi"},
        )
        self.assertEqual(r.status_code, 200)
        user = r.json()["user"]
        self.assertEqual(user["nickname"], "平台主管")
        self.assertEqual(user["display_name"], "平台主管")
        self.assertEqual(user["avatar"], "🚀")

        # 颜色注入被挡下，回退到原值
        r = self.client.put("/api/auth/profile", json={"color": "javascript:alert(1)"})
        self.assertTrue(r.json()["user"]["color"].startswith("#"))

        self.assertEqual(self.client.put("/api/auth/profile", json={"email": "nope"}).status_code, 422)

    def test_password_change_flow(self):
        r = self.client.put(
            "/api/auth/password",
            json={"current_password": DEFAULT_ADMIN_PASSWORD, "new_password": "brand-new-1", "logout_others": False},
        )
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["user"]["must_change_password"])

        wrong = self.client.post(
            "/api/auth/login",
            json={"username": "admin", "password": DEFAULT_ADMIN_PASSWORD},
            headers=self.ANON,
        )
        self.assertEqual(wrong.status_code, 401, "旧密码应立即失效")
        ok = self.client.post(
            "/api/auth/login", json={"username": "admin", "password": "brand-new-1"}, headers=self.ANON
        )
        self.assertEqual(ok.status_code, 200)

        self.assertEqual(
            self.client.put(
                "/api/auth/password", json={"current_password": "zzz", "new_password": "another-1"}
            ).status_code,
            422,
        )

    def test_theme_preferences_persist(self):
        r = self.client.put(
            "/api/auth/preferences",
            json={"theme": {"mode": "light", "accent": "rose", "radius": "round", "density": "compact", "glass": "off"}},
        )
        self.assertEqual(r.status_code, 200)
        theme = r.json()["user"]["preferences"]["theme"]
        self.assertEqual(theme["mode"], "light")
        self.assertEqual(theme["accent"], "rose")

        # 重新登录后偏好仍在
        token = self.login()
        me = self.client.get("/api/auth/me").json()
        self.assertEqual(me["user"]["preferences"]["theme"]["accent"], "rose")
        self.assertTrue(token)

        r = self.client.put("/api/auth/preferences", json={"theme": {"mode": "../etc", "accent": "neon"}})
        theme = r.json()["user"]["preferences"]["theme"]
        self.assertEqual(theme["mode"], "dark")
        self.assertEqual(theme["accent"], "blue")

    def test_logout_revokes_token(self):
        token = self.token
        self.assertEqual(self.client.post("/api/auth/logout").status_code, 200)
        self.assertEqual(self.client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code, 401)

    def test_sessions_and_revoke_others(self):
        other = self.client.post(
            "/api/auth/login", json={"username": "admin", "password": DEFAULT_ADMIN_PASSWORD}, headers=self.ANON
        ).json()["token"]

        sessions = self.client.get("/api/auth/sessions").json()
        self.assertGreaterEqual(sessions["count"], 2)
        self.assertEqual(sum(1 for s in sessions["sessions"] if s["current"]), 1)

        r = self.client.post("/api/auth/sessions/revoke-others")
        self.assertGreaterEqual(r.json()["revoked"], 1)
        self.assertEqual(self.client.get("/api/auth/me", headers={"Authorization": f"Bearer {other}"}).status_code, 401)
        # 当前设备不受影响
        self.assertEqual(self.client.get("/api/auth/me").status_code, 200)

    # -------------------------------------------------- 注册与用户管理

    def test_register_and_admin_management(self):
        r = self.client.post(
            "/api/auth/register",
            json={"username": "newbie", "password": "newbie123", "nickname": "新人"},
            headers=self.ANON,
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["user"]["role"], "user", "非首个账号应为普通用户")

        listing = self.client.get("/api/auth/users").json()
        self.assertEqual(listing["counts"]["total"], 2)
        newbie = next(u for u in listing["users"] if u["username"] == "newbie")

        promoted = self.client.patch(f"/api/auth/users/{newbie['id']}", json={"role": "admin", "nickname": "小新"}).json()
        self.assertEqual(promoted["counts"]["admins"], 2)
        self.assertTrue(any(u["nickname"] == "小新" for u in promoted["users"]))

        disabled = self.client.patch(f"/api/auth/users/{newbie['id']}", json={"active": False}).json()
        self.assertEqual(disabled["counts"]["active"], 1)

        self.assertEqual(self.client.delete(f"/api/auth/users/{newbie['id']}").status_code, 200)
        self.assertEqual(self.client.get("/api/auth/users").json()["counts"]["total"], 1)

    def test_registration_can_be_closed(self):
        self.client.put("/api/settings", json={"auth": {"allow_registration": False}})
        r = self.client.post(
            "/api/auth/register", json={"username": "nobody", "password": "nobody123"}, headers=self.ANON
        )
        self.assertEqual(r.status_code, 403)

    def test_non_admin_cannot_manage_users(self):
        token = self.client.post(
            "/api/auth/register", json={"username": "plain", "password": "plain123"}, headers=self.ANON
        ).json()["token"]
        self.assertEqual(self.client.get("/api/auth/users", headers={"Authorization": f"Bearer {token}"}).status_code, 403)

    def test_admin_cannot_delete_self(self):
        me = self.client.get("/api/auth/me").json()["user"]
        r = self.client.delete(f"/api/auth/users/{me['id']}")
        self.assertEqual(r.status_code, 422)

    # -------------------------------------------------- 概览 / 设置

    def test_health_and_overview(self):
        self.assertEqual(self.client.get("/api/health").status_code, 200)

        ov = self.client.get("/api/platform/overview").json()
        self.assertEqual(ov["health"]["status"], "ok")
        self.assertTrue(ov["health"]["contract_ok"])
        self.assertGreater(ov["counts"]["skills"], 0)
        self.assertIn("slots", ov)

    def test_settings_schema_and_roundtrip(self):
        snap = self.client.get("/api/settings").json()
        self.assertIn("schema", snap)
        self.assertEqual(set(snap["schema"].keys()), set(SETTINGS_SCHEMA.keys()))
        self.assertIn("providers", snap)

        r = self.client.put("/api/settings", json={"upload": {"max_mb": 9}})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.client.get("/api/settings").json()["values"]["upload"]["max_mb"], 9)

    def test_settings_invalid_returns_422(self):
        r = self.client.put("/api/settings", json={"upload": {"max_mb": -1}})
        self.assertEqual(r.status_code, 422)
        self.assertIn("errors", r.json()["detail"])

    def test_settings_schema_endpoint(self):
        r = self.client.get("/api/settings/schema")
        self.assertEqual(r.status_code, 200)
        self.assertIn("values", r.json())
        self.assertNotIn("providers", r.json()["values"], "供应商不应混在 values 里")

    def test_settings_export_has_no_plaintext_key(self):
        self.client.post(
            "/api/settings/providers",
            json={"id": "x", "name": "X", "kind": "openai", "api_key": "sk-zzz1234567890"},
        )
        bundle = self.client.get("/api/platform/export").json()
        self.assertNotIn("sk-zzz1234567890", str(bundle))

    def test_settings_reset(self):
        self.client.put("/api/settings", json={"upload": {"max_mb": 3}})
        r = self.client.post("/api/settings/reset")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.client.get("/api/settings").json()["values"]["upload"]["max_mb"], 20)

    # -------------------------------------------------- 技能库

    def test_skill_detail_and_peers(self):
        data = self.client.get("/api/skills/text_loader").json()
        self.assertEqual(data["slot"], "loader")
        self.assertIn("peers", data)
        self.assertIn("usage", data)
        self.assertTrue(data["enabled"])

    def test_skill_sandbox_test_runs(self):
        # memory_store 消费 chunks、产出 vector_index（沙盒未预置该产物，故能回显）
        r = self.client.post("/api/skills/memory_store/test", json={"text": "句子。" * 40})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"], body.get("error"))
        self.assertIn("chunks", body["inputs"])
        self.assertTrue(any(o["kind"] == "vector_index" for o in body["outputs"]), body["outputs"])

    def test_skill_sandbox_test_reports_overwritten_artifact(self):
        """清洗类插件覆写的是沙盒预置的 clean_text，也必须回显为产出而非「无产出」。"""
        r = self.client.post("/api/skills/basic_cleaner/test", json={"text": "abc   def"})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["ok"], body.get("error"))
        self.assertTrue(any(o["kind"] == "clean_text" for o in body["outputs"]), body["outputs"])

    def test_skill_sandbox_test_accepts_typed_options(self):
        """参数按 param_schema 的类型传入：list 保持列表，单元素不退化成字符串。"""
        r = self.client.post(
            "/api/skills/sensitive_word_cleaner/test",
            json={"options": {"words": ["secret"], "mask_char": "#"}, "text": "secret data"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["ok"], body.get("error"))
        self.assertEqual(body["options"]["words"], ["secret"], "单词敏感词表不应被拆成单字符")
        masked = next(o for o in body["outputs"] if o["kind"] == "clean_text")
        self.assertIn("######", masked["preview"])
        self.assertNotIn("secret", masked["preview"])

    def test_skill_sandbox_runs_with_uploaded_file(self):
        """在线文件验证：上传的真实字节作为 raw_file，loader 依据后缀解析。"""
        csv_bytes = "name,score\nAlice,90\nBob,85\n".encode("utf-8")
        r = self.client.post(
            "/api/skills/text_loader/test",
            json={
                "filename": "scores.csv",
                "content_type": "text/csv",
                "data_base64": base64.b64encode(csv_bytes).decode("ascii"),
            },
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["ok"], body.get("error"))
        self.assertEqual(body["file"]["name"], "scores.csv")
        self.assertEqual(body["file"]["bytes"], len(csv_bytes))
        loaded = next(o for o in body["outputs"] if o["kind"] == "text")
        self.assertIn("name | score", loaded["preview"], "CSV 应被转成表格文本")
        self.assertIn("Alice", loaded["preview"])

    def test_skill_sandbox_file_metadata_feeds_raw_file(self):
        """没有文件名时用 <sandbox> 兜底，且 content_type 缺省为二进制。"""
        r = self.client.post(
            "/api/skills/text_loader/test",
            json={"data_base64": base64.b64encode("raw bytes".encode("utf-8")).decode("ascii")},
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["ok"], body.get("error"))
        self.assertEqual(body["file"]["name"], "<sandbox>")
        self.assertEqual(body["file"]["content_type"], "application/octet-stream")

    def test_skill_sandbox_rejects_invalid_base64(self):
        r = self.client.post(
            "/api/skills/text_loader/test",
            json={"filename": "x.txt", "data_base64": "!!!not-base64!!!"},
        )
        self.assertEqual(r.status_code, 422)
        self.assertIn("base64", r.json()["detail"])

    def test_skill_sandbox_rejects_empty_upload(self):
        r = self.client.post(
            "/api/skills/text_loader/test",
            json={"filename": "empty.txt", "data_base64": ""},
        )
        self.assertEqual(r.status_code, 400)
        self.assertIn("空", r.json()["detail"])

    def test_skill_sandbox_without_file_keeps_text_fallback(self):
        """不传文件字段时仍走「文本即文件」的老路径，保证向后兼容。"""
        r = self.client.post("/api/skills/text_loader/test", json={"text": "hello world"})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["ok"], body.get("error"))
        self.assertIsNone(body["file"])
        loaded = next(o for o in body["outputs"] if o["kind"] == "text")
        self.assertIn("hello world", loaded["preview"])

    def test_skill_toggle_unused_plugin(self):
        # markdown_splitter 未被默认流水线使用，停用应成功
        r = self.client.post("/api/skills/markdown_splitter/toggle", json={"enabled": False})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(self.client.get("/api/skills/markdown_splitter").json()["enabled"])
        self.assertIn("markdown_splitter", self.runtime.settings.get("plugins.disabled"))

        self.client.post("/api/skills/markdown_splitter/toggle", json={"enabled": True})
        self.assertNotIn("markdown_splitter", self.runtime.settings.get("plugins.disabled"))

    def test_skill_toggle_rollback_on_contract_break(self):
        """停用默认流水线正在用的插件会导致契约失效，应自动回滚。"""
        before = self.runtime.pipeline
        r = self.client.post("/api/skills/recursive_splitter/toggle", json={"enabled": False})
        self.assertEqual(r.status_code, 422)
        self.assertIn("回滚", r.json()["detail"]["message"])
        # 回滚后：插件仍启用、流水线结构不变且依旧可执行
        self.assertNotIn("recursive_splitter", self.runtime.settings.get("plugins.disabled"))
        self.assertEqual(self.runtime.pipeline.name, before.name)
        self.assertIn("vector_index", self.runtime.pipeline.validate())

    def test_skill_test_rejects_disabled(self):
        self.client.post("/api/skills/fixed_splitter/toggle", json={"enabled": False})
        r = self.client.post("/api/skills/fixed_splitter/test", json={"text": "x"})
        self.assertEqual(r.status_code, 400)
        self.client.post("/api/skills/fixed_splitter/toggle", json={"enabled": True})

    def test_unknown_skill_404(self):
        self.assertEqual(self.client.get("/api/skills/nope").status_code, 404)
        self.assertEqual(self.client.post("/api/skills/nope/toggle", json={"enabled": True}).status_code, 404)

    # -------------------------------------------------- 上传 → 运行记录 → 知识库

    def _upload(self, name="sample.txt", data=SAMPLE):
        return self.client.post(
            "/api/upload?use=active",
            files={"file": (name, io.BytesIO(data), "text/plain")},
        )

    @unittest.skipUnless(MILVUS_OK, NEEDS_MILVUS)
    def test_upload_creates_run_and_knowledge(self):
        r = self._upload()
        self.assertEqual(r.status_code, 200)
        body = r.json()
        run_id = body["run_id"]
        self.assertEqual([a["kind"] for a in body["artifacts"]][:2], ["raw_file", "text"])

        runs = self.client.get("/api/runs").json()
        self.assertTrue(any(x["run_id"] == run_id for x in runs["runs"]))
        self.assertGreaterEqual(runs["stats"]["total"], 1)

        detail = self.client.get(f"/api/runs/{run_id}").json()
        self.assertEqual(detail["record"]["status"], "success")
        self.assertTrue(detail["replayable"])

        indexes = self.client.get("/api/knowledge/indexes").json()
        self.assertGreaterEqual(indexes["count"], 1)

        r = self.client.post("/api/knowledge/search", json={"run_id": run_id, "query": "插件"})
        self.assertEqual(r.status_code, 200)
        self.assertGreaterEqual(len(r.json()["hits"]), 1)

    def test_upload_rejects_empty_and_bad_extension(self):
        empty = self.client.post(
            "/api/upload", files={"file": ("empty.txt", io.BytesIO(b""), "text/plain")}
        )
        self.assertEqual(empty.status_code, 400)

        bad = self._upload(name="evil.exe")
        self.assertEqual(bad.status_code, 415)

    def test_upload_respects_max_mb_setting(self):
        self.client.put("/api/settings", json={"upload": {"max_mb": 1}})
        too_big = b"x" * (int(1.5 * 1024 * 1024))
        r = self._upload(name="big.txt", data=too_big)
        self.assertEqual(r.status_code, 413)

    def test_allowed_extensions_setting_is_enforced(self):
        self.client.put("/api/settings", json={"upload": {"allowed_extensions": [".md"]}})
        r = self._upload(name="sample.txt")
        self.assertEqual(r.status_code, 415)

    def test_disabled_draft_trial_setting(self):
        self.client.put("/api/settings", json={"runtime": {"enable_draft_trial": False}})
        r = self.client.post(
            "/api/upload?use=draft",
            files={"file": ("sample.txt", io.BytesIO(SAMPLE), "text/plain")},
        )
        self.assertEqual(r.status_code, 403)

    @unittest.skipUnless(MILVUS_OK, NEEDS_MILVUS)
    def test_retrieval_settings_are_read(self):
        run_id = self._upload().json()["run_id"]

        self.client.put("/api/settings", json={"retrieval": {"default_top_k": 1, "score_threshold": 0.0}})
        r = self.client.post("/api/knowledge/search", json={"run_id": run_id, "query": "插件"})
        self.assertEqual(r.json()["top_k"], 1)

        # 阈值被读取：返回命中的分数都不得低于阈值
        self.client.put("/api/settings", json={"retrieval": {"score_threshold": 0.999}})
        r = self.client.post("/api/knowledge/search", json={"run_id": run_id, "query": "插件"})
        body = r.json()
        self.assertEqual(body["score_threshold"], 0.999)
        self.assertTrue(all(h["score"] >= 0.999 for h in body["hits"]))

    @unittest.skipUnless(MILVUS_OK, NEEDS_MILVUS)
    def test_replay_and_delete_run(self):
        run_id = self._upload().json()["run_id"]

        r = self.client.post(f"/api/runs/{run_id}/replay")
        self.assertEqual(r.status_code, 200)
        self.assertNotEqual(r.json()["run_id"], run_id, "重放应生成新的 run_id")

        self.assertEqual(self.client.delete(f"/api/runs/{run_id}").status_code, 200)
        self.assertEqual(self.client.get(f"/api/runs/{run_id}").status_code, 404)

    @unittest.skipUnless(MILVUS_OK, NEEDS_MILVUS)
    def test_run_filters(self):
        self._upload(name="keep.txt")
        runs = self.client.get("/api/runs", params={"keyword": "keep"}).json()
        self.assertTrue(all("keep" in r["filename"] for r in runs["runs"]))

    # -------------------------------------------------- 工作台前端

    def test_workbench_page_and_assets(self):
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn('class="sidebar"', page.text)
        self.assertIn('id="view"', page.text)

        js = self.client.get("/assets/app.js")
        self.assertEqual(js.status_code, 200)
        self.assertIn("ROUTES", js.text)

        css = self.client.get("/assets/app.css")
        self.assertEqual(css.status_code, 200)


if __name__ == "__main__":
    unittest.main(verbosity=2)

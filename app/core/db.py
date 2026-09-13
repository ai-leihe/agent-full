"""平台数据库层：连接管理 + Schema 定义 + 密钥封装 + 操作审计。

为什么需要它
------------
平台早期把「账号 / 设置 / 运行记录 / 编排方案 / 向量索引」分别写在
``config/*.json`` 与 ``config/orchestrations/*.yaml`` 里，顶多再加一层进程内存。
这在单机演示时够用，但有三个绕不开的硬伤：

* **多实例**：两个进程各持一份 JSON，写操作互相覆盖；会话吊销只在本进程生效。
* **体积**：``data/runs.json`` 每次写入都要全量重写，并有条数硬上限。
* **丢失**：向量索引仓库纯内存，进程一重启「知识库」列表就空了，重放也随之失效。

所以引入本模块，把有状态的数据统一落到一张关系型数据库上。

设计要点
--------
1. **零依赖起步**：默认走标准库 ``sqlite3``，开箱即跑；设置环境变量
   ``PLATFORM_DATABASE_URL``（或 ``bootstrap(database_url=...)``）即可切到
   MySQL / PostgreSQL，驱动 ``pymysql`` / ``psycopg2`` 已在本项目依赖清单中。
2. **连接按需开关**：每次操作现开现关，不在进程里长期持有连接句柄。
   平台是低并发的工作台场景，这点开销可以忽略，却换来了「临时目录可删、
   连接不会跨线程串号」的确定性；真到高并发再换成连接池即可，接口不变。
3. **方言收敛**：所有 SQL 统一用 ``?`` 占位符书写，由 :class:`Database`
   按方言改写为 ``%s``；建表语句由一处 DDL 生成，避免三份方言各写一遍。
4. **密钥不落明文**：模型供应商的 ``api_key`` 经 :func:`secret_box` 加密后再入库，
   密钥材料本身放在 ``kv`` 表中，与业务数据同行同库，便于整体备份 / 迁移。
5. **审计留痕**：设置变更、账号增删、插件启停、方案应用等写操作统一走
   :func:`audit`，落到 ``audit_log`` 表，事后可查「谁在什么时候动了什么」。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence
from urllib.parse import unquote, urlsplit

#: 覆盖数据库连接串的环境变量
PLATFORM_DB_ENV = "PLATFORM_DATABASE_URL"

#: 数据库文件默认名（放在数据目录下）
DEFAULT_DB_NAME = "platform.db"

#: Schema 版本；升级时在这里 +1，并在 :func:`Database.init_schema` 里补迁移分支
SCHEMA_VERSION = 1


# =============================================================== 连接串


def _sqlite_dsn(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def resolve_dsn(value: str | os.PathLike[str] | None = None, *, base_dir: str | os.PathLike[str] | None = None) -> str:
    """把「配置值 / 环境变量 / 默认目录」收敛成一个标准连接串。

    * 显式给了 ``mysql://`` / ``postgresql://`` / ``sqlite://`` → 原样使用；
    * 给的是普通文件路径 → 包装成 SQLite 连接串（父目录不存在会自动建）；
    * 什么都没给 → 取环境变量，再退化为 ``<base_dir>/platform.db``。
    """
    raw = str(value or "").strip() or str(os.environ.get(PLATFORM_DB_ENV) or "").strip()
    if raw:
        if "://" in raw:
            return raw
        path = Path(raw)
        path.parent.mkdir(parents=True, exist_ok=True)
        return _sqlite_dsn(path)

    root = Path(base_dir) if base_dir else Path.cwd()
    root.mkdir(parents=True, exist_ok=True)
    return _sqlite_dsn(root / DEFAULT_DB_NAME)


def parse_dsn(dsn: str) -> tuple[str, dict[str, Any]]:
    """解析连接串，返回 ``(方言, 连接参数)``。

    方言只有三种：``sqlite`` / ``mysql`` / ``postgresql``。MySQL 与 PostgreSQL
    的连接参数直接交给各自驱动，因此这里只做方言判定与 SQLite 的路径抽取。
    """
    text = str(dsn or "").strip()
    if text.startswith("sqlite"):
        _, _, rest = text.partition(":///")
        if not rest:
            rest = text.partition("sqlite://")[2]
        path = unquote(rest)
        if not path:
            raise ValueError("SQLite 连接串缺少文件路径")
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        return "sqlite", {"path": path}
    if text.startswith(("mysql://", "mysql+pymysql://")):
        return "mysql", _split_url(text.split("://", 1)[1])
    if text.startswith(("postgres://", "postgresql://", "postgresql+psycopg2://")):
        return "postgresql", _split_url(text.split("://", 1)[1])
    raise ValueError(f"暂不支持的数据库连接串：{text}")


def _split_url(rest: str) -> dict[str, Any]:
    """拆分 ``user:pass@host:port/dbname?charset=...`` 形式的连接参数。"""
    credentials, _, location = rest.rpartition("@")
    if not credentials:
        credentials, location = "", rest
    user, _, password = credentials.partition(":")
    hostport, _, database = location.partition("/")
    database = database.split("?", 1)[0]
    host, _, port = hostport.partition(":")
    return {
        "user": unquote(user),
        "password": unquote(password),
        "host": host or "127.0.0.1",
        "port": int(port) if port.isdigit() else None,
        "database": database,
    }


def mask_dsn(dsn: str) -> str:
    """把连接串里的口令抹掉，便于在界面上展示数据源。"""
    text = str(dsn or "")
    if "@" in text and "://" in text:
        scheme, _, rest = text.partition("://")
        credentials, _, location = rest.rpartition("@")
        user, _, _ = credentials.partition(":")
        return f"{scheme}://{user}:***@{location}"
    return text


# =============================================================== Schema


_VARCHAR = "VARCHAR"
_TEXT = "TEXT"
_INT = "INT"
_REAL = {"sqlite": "REAL", "mysql": "DOUBLE", "postgresql": "DOUBLE PRECISION"}
_AUTO_PK = {
    "sqlite": "INTEGER PRIMARY KEY AUTOINCREMENT",
    "mysql": "INT AUTO_INCREMENT PRIMARY KEY",
    "postgresql": "SERIAL PRIMARY KEY",
}


#: 各家方言的标识符引号：MySQL 用反引号，SQLite / PostgreSQL 用双引号
_IDENT_QUOTES = {"sqlite": '"', "mysql": "`", "postgresql": '"'}


def quote_ident(dialect: str, name: str) -> str:
    """给标识符套上方言引号，避免撞上保留字。

    ``kv`` 表的 ``key`` 列就是典型：SQLite 允许裸写，MySQL 会直接报语法错误，
    因此凡是 SQL 里由 Python 拼进去的表名 / 列名都应经这里处理。
    """
    quote = _IDENT_QUOTES.get(dialect, '"')
    return f"{quote}{name}{quote}"


def _ddl(dialect: str) -> list[str]:
    """生成与方言无关的建表语句（只有自增主键与浮点类型需要分方言）。"""
    pk = _AUTO_PK[dialect]
    real = _REAL[dialect]
    return [
        f"""
        CREATE TABLE IF NOT EXISTS users (
            id {_VARCHAR}(64) PRIMARY KEY,
            username {_VARCHAR}(64) NOT NULL,
            username_key {_VARCHAR}(64) NOT NULL,
            nickname {_VARCHAR}(128) NOT NULL DEFAULT '',
            email {_VARCHAR}(255) NOT NULL DEFAULT '',
            role {_VARCHAR}(16) NOT NULL DEFAULT 'user',
            avatar {_VARCHAR}(16) NOT NULL DEFAULT '',
            color {_VARCHAR}(16) NOT NULL DEFAULT '#5b8cff',
            bio {_TEXT},
            active {_INT} NOT NULL DEFAULT 1,
            must_change_password {_INT} NOT NULL DEFAULT 0,
            created_at {real} NOT NULL DEFAULT 0,
            last_login_at {real} NOT NULL DEFAULT 0,
            preferences {_TEXT},
            password_hash {_TEXT}
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS sessions (
            jti {_VARCHAR}(64) PRIMARY KEY,
            user_id {_VARCHAR}(64) NOT NULL,
            created_at {real} NOT NULL DEFAULT 0,
            last_seen {real} NOT NULL DEFAULT 0,
            expires_at {real} NOT NULL DEFAULT 0,
            revoked_at {real}
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS kv (
            {quote_ident(dialect, 'key')} {_VARCHAR}(128) PRIMARY KEY,
            {quote_ident(dialect, 'value')} {_TEXT},
            updated_at {real} NOT NULL DEFAULT 0
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS providers (
            id {_VARCHAR}(64) PRIMARY KEY,
            name {_VARCHAR}(128) NOT NULL DEFAULT '',
            kind {_VARCHAR}(32) NOT NULL DEFAULT 'openai',
            base_url {_VARCHAR}(255) NOT NULL DEFAULT '',
            api_key {_TEXT},
            models {_TEXT},
            enabled {_INT} NOT NULL DEFAULT 1,
            position {_INT} NOT NULL DEFAULT 0,
            created_at {real} NOT NULL DEFAULT 0,
            updated_at {real} NOT NULL DEFAULT 0
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS runs (
            run_id {_VARCHAR}(64) PRIMARY KEY,
            owner_id {_VARCHAR}(64) NOT NULL DEFAULT '',
            filename {_VARCHAR}(255) NOT NULL DEFAULT '',
            status {_VARCHAR}(16) NOT NULL DEFAULT 'success',
            source {_VARCHAR}(16) NOT NULL DEFAULT 'upload',
            pipeline {_VARCHAR}(128) NOT NULL DEFAULT '',
            content_type {_VARCHAR}(128) NOT NULL DEFAULT '',
            started_at {real} NOT NULL DEFAULT 0,
            elapsed_ms {real} NOT NULL DEFAULT 0,
            stats {_TEXT},
            errors {_TEXT},
            step_count {_INT} NOT NULL DEFAULT 0,
            failed_steps {_INT} NOT NULL DEFAULT 0,
            created_at {real} NOT NULL DEFAULT 0
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS run_steps (
            id {pk},
            run_id {_VARCHAR}(64) NOT NULL,
            position {_INT} NOT NULL DEFAULT 0,
            agent {_VARCHAR}(128) NOT NULL DEFAULT '',
            skill {_VARCHAR}(128) NOT NULL DEFAULT '',
            status {_VARCHAR}(16) NOT NULL DEFAULT '',
            duration_ms {real} NOT NULL DEFAULT 0,
            detail {_TEXT}
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS run_artifacts (
            id {pk},
            run_id {_VARCHAR}(64) NOT NULL,
            position {_INT} NOT NULL DEFAULT 0,
            kind {_VARCHAR}(64) NOT NULL DEFAULT '',
            producer {_VARCHAR}(128) NOT NULL DEFAULT '',
            size_label {_VARCHAR}(64) NOT NULL DEFAULT '',
            meta {_TEXT}
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS documents (
            run_id {_VARCHAR}(64) PRIMARY KEY,
            filename {_VARCHAR}(255) NOT NULL DEFAULT '',
            content_type {_VARCHAR}(128) NOT NULL DEFAULT '',
            size {_INT} NOT NULL DEFAULT 0,
            sha256 {_VARCHAR}(64) NOT NULL DEFAULT '',
            storage_key {_VARCHAR}(255) NOT NULL DEFAULT '',
            created_at {real} NOT NULL DEFAULT 0
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS knowledge_indexes (
            run_id {_VARCHAR}(64) PRIMARY KEY,
            filename {_VARCHAR}(255) NOT NULL DEFAULT '',
            collection {_VARCHAR}(128) NOT NULL DEFAULT '',
            embedder {_VARCHAR}(128) NOT NULL DEFAULT '',
            dimension {_INT} NOT NULL DEFAULT 0,
            vec_size {_INT} NOT NULL DEFAULT 0,
            backend {_VARCHAR}(32) NOT NULL DEFAULT '',
            owner_id {_VARCHAR}(64) NOT NULL DEFAULT '',
            meta {_TEXT},
            created_at {real} NOT NULL DEFAULT 0
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS knowledge_chunks (
            id {pk},
            run_id {_VARCHAR}(64) NOT NULL,
            position {_INT} NOT NULL DEFAULT 0,
            chunk_id {_VARCHAR}(64) NOT NULL DEFAULT '',
            chunk_index {_INT} NOT NULL DEFAULT 0,
            text {_TEXT},
            meta {_TEXT},
            embedding {_TEXT}
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS orchestrations (
            name {_VARCHAR}(64) PRIMARY KEY,
            description {_TEXT},
            active {_INT} NOT NULL DEFAULT 0,
            agents_count {_INT} NOT NULL DEFAULT 0,
            steps_count {_INT} NOT NULL DEFAULT 0,
            created_at {real} NOT NULL DEFAULT 0,
            updated_at {real} NOT NULL DEFAULT 0
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS orchestration_agents (
            id {_VARCHAR}(64) PRIMARY KEY,
            spec_id {_VARCHAR}(64) NOT NULL DEFAULT '',
            orchestration {_VARCHAR}(64) NOT NULL,
            position {_INT} NOT NULL DEFAULT 0,
            name {_VARCHAR}(128) NOT NULL DEFAULT '',
            role {_TEXT}
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS orchestration_steps (
            id {_VARCHAR}(64) PRIMARY KEY,
            spec_id {_VARCHAR}(64) NOT NULL DEFAULT '',
            orchestration {_VARCHAR}(64) NOT NULL,
            agent_id {_VARCHAR}(64) NOT NULL DEFAULT '',
            position {_INT} NOT NULL DEFAULT 0,
            skill {_VARCHAR}(128) NOT NULL DEFAULT '',
            options {_TEXT},
            enabled {_INT} NOT NULL DEFAULT 1
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS audit_log (
            id {pk},
            actor_id {_VARCHAR}(64) NOT NULL DEFAULT '',
            actor_name {_VARCHAR}(128) NOT NULL DEFAULT '',
            action {_VARCHAR}(64) NOT NULL DEFAULT '',
            target {_VARCHAR}(255) NOT NULL DEFAULT '',
            detail {_TEXT},
            created_at {real} NOT NULL DEFAULT 0
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS script_runs (
            id {pk},
            user_id {_VARCHAR}(64) NOT NULL DEFAULT '',
            username {_VARCHAR}(64) NOT NULL DEFAULT '',
            language {_VARCHAR}(32) NOT NULL DEFAULT '',
            code_hash {_VARCHAR}(64) NOT NULL DEFAULT '',
            code_size {_INT} NOT NULL DEFAULT 0,
            code_preview {_TEXT},
            ok {_INT} NOT NULL DEFAULT 0,
            stage {_VARCHAR}(32) NOT NULL DEFAULT '',
            exit_code {_INT},
            duration_ms {real} NOT NULL DEFAULT 0,
            timed_out {_INT} NOT NULL DEFAULT 0,
            error {_TEXT},
            created_at {real} NOT NULL DEFAULT 0
        )
        """,
    ]


#: 索引：(名字, 表, 列, 是否唯一)
_INDEXES: list[tuple[str, str, tuple[str, ...], bool]] = [
    ("ux_users_username_key", "users", ("username_key",), True),
    ("ix_sessions_user_id", "sessions", ("user_id",), False),
    ("ix_runs_started_at", "runs", ("started_at",), False),
    ("ix_run_steps_run_id", "run_steps", ("run_id",), False),
    ("ix_run_artifacts_run_id", "run_artifacts", ("run_id",), False),
    ("ix_knowledge_chunks_run_id", "knowledge_chunks", ("run_id",), False),
    ("ix_agents_orchestration", "orchestration_agents", ("orchestration",), False),
    ("ix_steps_orchestration", "orchestration_steps", ("orchestration",), False),
    ("ix_steps_agent_id", "orchestration_steps", ("agent_id",), False),
    ("ix_audit_created_at", "audit_log", ("created_at",), False),
    ("ix_script_runs_created_at", "script_runs", ("created_at",), False),
]


# =============================================================== JSON 助手


def to_json(value: Any) -> str:
    """把任意可序列化对象压成 JSON 文本（入库统一入口）。"""
    return json.dumps(value if value is not None else None, ensure_ascii=False)


def from_json(text: Any, default: Any = None) -> Any:
    """把 JSON 文本还原成对象；空值或坏数据一律回落到默认值。"""
    if text is None or text == "":
        return default
    if isinstance(text, (dict, list)):
        return text
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


# =============================================================== 连接


class Database:
    """一个数据库门面：方言改写 + 建表 + 通用增删改查。

    连接「按需开、用完关」：不持有长连接，因此临时目录随时可删、
    多线程也不会互相串号。代价是每次操作多一次建连，对本平台的
    访问量来说可以忽略。
    """

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self.dialect, self.options = parse_dsn(dsn)
        self.placeholder = "?" if self.dialect == "sqlite" else "%s"
        self._lock = threading.RLock()
        #: 事务进行中的线程持有同一个连接，保证 BEGIN...COMMIT 覆盖内部所有语句
        self._local = threading.local()
        self.init_schema()

    # -------------------------------------------------- 属性

    @property
    def is_sqlite(self) -> bool:
        return self.dialect == "sqlite"

    def info(self) -> dict[str, Any]:
        """对外的数据源描述（口令已掩码），供概览与设置页展示。"""
        data: dict[str, Any] = {"dialect": self.dialect, "dsn": mask_dsn(self.dsn)}
        if self.is_sqlite:
            data["path"] = self.options.get("path")
        else:
            data["database"] = self.options.get("database")
            data["host"] = self.options.get("host")
        return data

    # -------------------------------------------------- 连接

    def _connect(self) -> Any:
        if self.dialect == "sqlite":
            conn = sqlite3.connect(self.options["path"], timeout=30.0, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")
            # 关闭自动开启的隐式事务，写操作由 execute/transaction 显式控制
            conn.isolation_level = None
            return conn
        if self.dialect == "mysql":
            import pymysql
            from pymysql.cursors import DictCursor

            conn = pymysql.connect(
                host=self.options["host"],
                port=self.options["port"] or 3306,
                user=self.options["user"],
                password=self.options["password"],
                database=self.options["database"],
                charset="utf8mb4",
                cursorclass=DictCursor,
                autocommit=True,
            )
            return conn
        import psycopg2
        from psycopg2.extras import RealDictCursor

        conn = psycopg2.connect(
            host=self.options["host"],
            port=self.options["port"] or 5432,
            user=self.options["user"],
            password=self.options["password"],
            dbname=self.options["database"],
            cursor_factory=RealDictCursor,
        )
        conn.autocommit = True
        return conn

    def _sql(self, sql: str) -> str:
        return sql if self.placeholder == "?" else sql.replace("?", self.placeholder)

    @staticmethod
    def _bind(value: Any) -> Any:
        """把 Python 值转换成驱动能接受的类型（dict/list → JSON，bool → int）。"""
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, (dict, list, tuple)):
            return json.dumps(value, ensure_ascii=False)
        return value

    @contextmanager
    def _cursor(self) -> Iterator[Any]:
        """借出一个游标。

        * 外部没有事务时：现开连接、用完关闭；
        * 处于 :meth:`transaction` 中时：复用那个连接，且**不**关闭它，
          否则事务内的第二条语句就会落到另一个连接上。
        """
        held = getattr(self._local, "conn", None)
        if held is not None:
            cursor = held.cursor()
            try:
                yield cursor
            finally:
                try:
                    cursor.close()
                except Exception:  # noqa: BLE001
                    pass
            return

        conn = self._connect()
        cursor = conn.cursor()
        try:
            yield cursor
        finally:
            try:
                cursor.close()
            except Exception:  # noqa: BLE001 - 关闭失败不该掩盖业务异常
                pass
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    # -------------------------------------------------- 基础执行

    def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        """执行写语句，返回影响行数。"""
        bound = tuple(self._bind(p) for p in params)
        with self._lock, self._cursor() as cursor:
            cursor.execute(self._sql(sql), bound)
            return int(cursor.rowcount or 0)

    def executemany(self, sql: str, rows: Sequence[Sequence[Any]]) -> int:
        payload = [tuple(self._bind(p) for p in row) for row in rows]
        if not payload:
            return 0
        with self._lock, self._cursor() as cursor:
            cursor.executemany(self._sql(sql), payload)
            return int(cursor.rowcount or 0)

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        bound = tuple(self._bind(p) for p in params)
        with self._lock, self._cursor() as cursor:
            cursor.execute(self._sql(sql), bound)
            rows = cursor.fetchall() or []
            return [dict(row) for row in rows]

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def scalar(self, sql: str, params: Sequence[Any] = (), default: Any = None) -> Any:
        row = self.query_one(sql, params)
        if not row:
            return default
        return next(iter(row.values()), default)

    @contextmanager
    def transaction(self) -> Iterator["Database"]:
        """显式事务：要么整体提交，要么整体回滚。

        事务期间把连接挂到当前线程上，:meth:`_cursor` 会自动复用它，
        因此 ``with db.transaction() as tx: tx.insert(...)`` 里的每条语句
        都在同一个事务里。嵌套调用会直接复用外层事务，不会重复 BEGIN。
        """
        with self._lock:
            if getattr(self._local, "conn", None) is not None:
                yield self
                return

            conn = self._connect()
            self._local.conn = conn
            cursor = conn.cursor()
            try:
                cursor.execute("BEGIN")
                yield self
            except Exception:
                try:
                    cursor.execute("ROLLBACK")
                except Exception:  # noqa: BLE001
                    pass
                raise
            else:
                cursor.execute("COMMIT")
            finally:
                self._local.conn = None
                try:
                    cursor.close()
                except Exception:  # noqa: BLE001
                    pass
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass


    # -------------------------------------------------- 通用增删改查

    def insert(self, table: str, values: dict[str, Any]) -> None:
        columns = list(values)
        marks = ", ".join(["?"] * len(columns))
        names = ", ".join(quote_ident(self.dialect, c) for c in columns)
        sql = f"INSERT INTO {quote_ident(self.dialect, table)} ({names}) VALUES ({marks})"
        self.execute(sql, [values[c] for c in columns])

    def update(self, table: str, where: dict[str, Any], values: dict[str, Any]) -> int:
        if not values:
            return 0
        assignments = ", ".join(f"{quote_ident(self.dialect, k)} = ?" for k in values)
        return self.execute(
            f"UPDATE {quote_ident(self.dialect, table)} SET {assignments} WHERE {self._where(where)}",
            [*values.values(), *where.values()],
        )

    def delete(self, table: str, where: dict[str, Any]) -> int:
        return self.execute(
            f"DELETE FROM {quote_ident(self.dialect, table)} WHERE {self._where(where)}", list(where.values())
        )

    def clear(self, table: str) -> int:
        return self.execute(f"DELETE FROM {quote_ident(self.dialect, table)}")

    def exists(self, table: str, where: dict[str, Any]) -> bool:
        sql = f"SELECT 1 AS hit FROM {quote_ident(self.dialect, table)} WHERE {self._where(where)}"
        return self.query_one(sql, list(where.values())) is not None

    def count(self, table: str, where: dict[str, Any] | None = None) -> int:
        name = quote_ident(self.dialect, table)
        if where:
            sql = f"SELECT COUNT(*) AS n FROM {name} WHERE {self._where(where)}"
            return int(self.scalar(sql, list(where.values()), 0) or 0)
        return int(self.scalar(f"SELECT COUNT(*) AS n FROM {name}", (), 0) or 0)

    def _where(self, where: dict[str, Any]) -> str:
        """把 ``{列: 值}`` 拼成带方言引号的等值条件。"""
        return " AND ".join(f"{quote_ident(self.dialect, k)} = ?" for k in where)

    def upsert(self, table: str, key: dict[str, Any], values: dict[str, Any]) -> None:
        """按键存在则更新、不存在则插入（先查后写，三种方言通用）。"""
        if self.exists(table, key):
            self.update(table, key, values)
        else:
            self.insert(table, {**key, **values})

    # -------------------------------------------------- KV

    def kv_get(self, key: str, default: Any = None) -> Any:
        row = self.query_one(
            f"SELECT {quote_ident(self.dialect, 'value')} FROM {quote_ident(self.dialect, 'kv')} "
            f"WHERE {quote_ident(self.dialect, 'key')} = ?",
            [key],
        )
        if row is None:
            return default
        return from_json(row["value"], default)

    def kv_set(self, key: str, value: Any) -> None:
        self.upsert("kv", {"key": key}, {"value": to_json(value), "updated_at": time.time()})

    # -------------------------------------------------- Schema

    def init_schema(self) -> None:
        """建表 + 建索引 + 记录版本号；重复执行无副作用。"""
        with self._lock, self._cursor() as cursor:
            for statement in _ddl(self.dialect):
                cursor.execute(statement)

        for name, table, columns, unique in _INDEXES:
            cols = ", ".join(quote_ident(self.dialect, c) for c in columns)
            kind = "UNIQUE INDEX" if unique else "INDEX"
            # MySQL 不支持 CREATE INDEX IF NOT EXISTS，只能建完忽略「已存在」错误
            guard = "" if self.dialect == "mysql" else "IF NOT EXISTS "
            try:
                self.execute(
                    f"CREATE {kind} {guard}{quote_ident(self.dialect, name)} "
                    f"ON {quote_ident(self.dialect, table)} ({cols})"
                )
            except Exception:  # noqa: BLE001 - 索引已存在时无需求助用户
                pass

        if self.kv_get("schema.version") is None:
            self.kv_set("schema.version", SCHEMA_VERSION)

    # -------------------------------------------------- 兼容旧接口

    def close(self) -> None:
        """没有长连接可关；保留该方法是为了让调用方语义完整。"""


# =============================================================== 实例缓存


_CACHE: dict[str, Database] = {}
_CACHE_LOCK = threading.Lock()


def get_database(
    value: str | os.PathLike[str] | None = None, *, base_dir: str | os.PathLike[str] | None = None
) -> Database:
    """按连接串复用同一个 :class:`Database`（多个 Store 共享一份 Schema）。"""
    dsn = resolve_dsn(value, base_dir=base_dir)
    with _CACHE_LOCK:
        db = _CACHE.get(dsn)
        if db is None:
            db = Database(dsn)
            _CACHE[dsn] = db
        return db


def release_database(db: Database | None) -> None:
    """从缓存里摘掉某个数据库（测试切换临时目录时用）。"""
    if db is None:
        return
    with _CACHE_LOCK:
        for key, cached in list(_CACHE.items()):
            if cached is db:
                _CACHE.pop(key, None)


# =============================================================== 密钥封装


class SecretBox:
    """轻量对称封装：``HMAC-SHA256`` 派生密钥流做异或，外加完整性校验。

    用途是「不把供应商 API Key 以明文写进库」。构造方式是
    encrypt-then-MAC（先流加密、再整体签名），能挡住拖库后的直接可读。

    需要说明的边界：这是**不引入第三方密码库时的务实方案**，不是经过
    审计的密码学实现。生产环境请把 :func:`secret_box` 换成 KMS 或
    ``cryptography`` 的 Fernet，本模块其余代码无需改动。
    """

    def __init__(self, key: bytes) -> None:
        self._key = key

    def _keystream(self, nonce: bytes, length: int) -> bytes:
        out = bytearray()
        counter = 0
        while len(out) < length:
            out.extend(
                hashlib.pbkdf2_hmac("sha256", self._key, nonce + counter.to_bytes(4, "big"), 1, 32)
            )
            counter += 1
        return bytes(out[:length])

    def encrypt(self, plaintext: str) -> str:
        if not plaintext:
            return ""
        raw = str(plaintext).encode("utf-8")
        nonce = os.urandom(16)
        cipher = bytes(a ^ b for a, b in zip(raw, self._keystream(nonce, len(raw))))
        tag = hmac.new(self._key, nonce + cipher, hashlib.sha256).digest()[:16]
        return base64.urlsafe_b64encode(nonce + tag + cipher).decode("ascii")

    def decrypt(self, token: str) -> str:
        if not token:
            return ""
        try:
            blob = base64.urlsafe_b64decode(str(token).encode("ascii"))
        except (ValueError, TypeError):
            return ""
        if len(blob) < 32:
            return ""
        nonce, tag, cipher = blob[:16], blob[16:32], blob[32:]
        expected = hmac.new(self._key, nonce + cipher, hashlib.sha256).digest()[:16]
        if not hmac.compare_digest(tag, expected):
            # 密钥被换过或数据被篡改：宁可空值，也不返回可疑明文
            return ""
        raw = bytes(a ^ b for a, b in zip(cipher, self._keystream(nonce, len(cipher))))
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return ""


#: 密钥材料的 KV 键
_SECRET_BOX_KEY = "security.key"


def secret_box(db: Database) -> SecretBox:
    """取（或首次生成）平台的密钥封装器，密钥材料存在 ``kv`` 表里。"""
    material = db.kv_get(_SECRET_BOX_KEY)
    if not material:
        material = os.urandom(32).hex()
        db.kv_set(_SECRET_BOX_KEY, material)
    try:
        return SecretBox(bytes.fromhex(str(material)))
    except ValueError:
        # 存档被手工改坏时重新生成，避免整个平台起不来
        material = os.urandom(32).hex()
        db.kv_set(_SECRET_BOX_KEY, material)
        return SecretBox(bytes.fromhex(material))


# =============================================================== 审计


def audit(
    db: Database | None,
    action: str,
    *,
    target: str = "",
    actor_id: str = "",
    actor_name: str = "",
    detail: Any = None,
) -> None:
    """写一条操作审计；失败不影响主流程（审计不该成为业务的单点故障）。

    ``detail`` 会自动转 JSON，因此可以直接塞 dict（例如改动前后的值）。
    """
    if db is None:
        return
    try:
        db.insert(
            "audit_log",
            {
                "actor_id": actor_id or "",
                "actor_name": actor_name or "",
                "action": action,
                "target": str(target or "")[:255],
                "detail": to_json(detail),
                "created_at": time.time(),
            },
        )
    except Exception:  # noqa: BLE001 - 审计写失败不应回滚业务
        pass


def list_audit(db: Database | None, *, limit: int = 50, action: str = "") -> list[dict[str, Any]]:
    """按时间倒序读审计记录。"""
    if db is None:
        return []
    if action:
        rows = db.query(
            "SELECT * FROM audit_log WHERE action = ? ORDER BY created_at DESC LIMIT ?", [action, limit]
        )
    else:
        rows = db.query("SELECT * FROM audit_log ORDER BY created_at DESC LIMIT ?", [limit])
    for row in rows:
        row["detail"] = from_json(row.get("detail"), None)
    return rows


def audit_stats(db: Database | None) -> dict[str, Any]:
    if db is None:
        return {"total": 0}
    return {"total": db.count("audit_log")}


__all__ = [
    "DEFAULT_DB_NAME",
    "PLATFORM_DB_ENV",
    "SCHEMA_VERSION",
    "Database",
    "SecretBox",
    "audit",
    "audit_stats",
    "from_json",
    "get_database",
    "list_audit",
    "mask_dsn",
    "parse_dsn",
    "quote_ident",
    "release_database",
    "resolve_dsn",
    "secret_box",
    "to_json",
]

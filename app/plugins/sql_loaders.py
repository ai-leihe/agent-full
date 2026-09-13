"""loader 槽位插件：连接关系型数据库，把查询结果集加载为纯文本。

与 ``text_loader`` / ``pdf_loader`` 等「文件来源」的 loader 不同，本插件的数据
来源是**外部数据库**，因此 ``consumes`` 为空——它可以直接作为整条流水线的起点，
把「从库里取数 → 清洗 → 切片 → 向量化 → 入库」串起来：

    ingestion_agent:  [sql_loader]
    chunking_agent:   [recursive_splitter]
    indexing_agent:   [hash_embedder, memory_store]

与 ``text_loader`` 同槽位，因此在编排工作台里可自由互换，替换数据源无需改动代码。

支持三类数据库：

* ``sqlite``     —— 标准库 ``sqlite3``，零依赖，``dsn`` 填数据库文件路径（或 ``:memory:``）；
* ``postgresql`` —— 需可选依赖 ``psycopg2``；
* ``mysql``      —— 需可选依赖 ``pymysql``。
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from ..core.context import TEXT, PipelineContext
from ..core.skill import Skill, skill

#: 支持的数据库方言
DIALECTS = ("sqlite", "postgresql", "mysql")

#: 表名合法形态：``table`` 或 ``schema.table``，仅限字母/数字/下划线，防止拼接注入
_TABLE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*(\.[A-Za-z_][A-Za-z0-9_$]*)?$")


def _detect_dialect(dsn: str) -> str | None:
    """从 DSN 前缀推断数据库类型。返回 None 表示无法识别或无前缀。"""
    d = dsn.strip().lower()
    if d.startswith(("sqlite://", "file:")):
        return "sqlite"
    if d.startswith(("mysql+pymysql://", "mysql://")):
        return "mysql"
    if d.startswith(("postgresql://", "postgres://")):
        return "postgresql"
    return None


def parse_mysql_dsn(dsn: str) -> dict[str, Any]:
    """把 ``mysql://user:pass@host:3306/db?charset=utf8mb4`` 解析为 pymysql 参数。"""
    info = urlparse(dsn)
    params: dict[str, Any] = {
        "host": info.hostname or "127.0.0.1",
        "port": info.port or 3306,
        "user": unquote(info.username or ""),
        "password": unquote(info.password or ""),
        "database": (info.path or "/").lstrip("/"),
    }
    for key, values in parse_qs(info.query).items():
        if values:
            params[key] = values[0]
    return params


def render_cell(value: Any) -> str:
    """把单个单元格渲染为一行内的文本片段（换行压平，避免破坏「一行一条记录」）。"""
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", errors="replace").replace("\r", " ").replace("\n", " ")
    return str(value).replace("\r", " ").replace("\n", " ")


@skill
class SqlLoader(Skill):
    """SQL 数据库加载器：执行查询并把结果集渲染为纯文本产物。"""

    name = "sql_loader"
    slot = "loader"  # 与 text_loader / pdf_loader 同槽位 → 可直接替换数据源
    description = "连接 SQL 数据库执行查询，把结果集渲染为文本（SQLite 零依赖，PostgreSQL/MySQL 需可选驱动）。"
    consumes = ()  # 数据来自数据库而非上传文件
    produces = (TEXT,)
    param_schema = {
        "dialect": {
            "type": "str",
            "default": "sqlite",
            "label": "数据库类型",
            "choices": list(DIALECTS),
        },
        "dsn": {
            "type": "str",
            "default": "",
            "label": "连接串 / 文件路径",
            "help": "SQLite 填文件路径（:memory: 为内存库）；PostgreSQL/MySQL 填连接串；"
                     "系统会根据连接串前缀和「数据库类型」是否匹配给出明确提示",
        },
        "query": {
            "type": "str",
            "default": "",
            "label": "SQL 查询",
            "help": "留空则按 table 读取全表",
        },
        "table": {
            "type": "str",
            "default": "",
            "label": "表名",
            "help": "query 为空时使用，支持 schema.table",
        },
        "max_rows": {
            "type": "int",
            "default": 200,
            "label": "最多读取行数",
            "min": 1,
            "max": 100000,
        },
        "include_header": {"type": "bool", "default": True, "label": "包含列名"},
        "separator": {"type": "str", "default": " | ", "label": "列分隔符"},
    }

    def configure(self, options: dict) -> None:
        self.dialect = str(options.get("dialect") or "sqlite").strip().lower()
        if self.dialect not in DIALECTS:
            raise ValueError(f"不支持的数据库类型 '{self.dialect}'，可选：{'、'.join(DIALECTS)}")
        self.dsn = str(options.get("dsn") or "").strip()

        inferred = _detect_dialect(self.dsn)
        if inferred and inferred != self.dialect:
            raise ValueError(
                f"数据库类型选择为「{self.dialect}」，但连接串看起来是 {inferred}。"
                f"请把「数据库类型」改为 {inferred}，或修正连接串。"
            )
        self.query = str(options.get("query") or "").strip()
        self.table = str(options.get("table") or "").strip()
        self.max_rows = int(options.get("max_rows", 200))
        if self.max_rows < 1:
            raise ValueError("max_rows 必须为正整数")
        self.include_header = bool(options.get("include_header", True))
        self.separator = str(options.get("separator") or " | ")

    # -------------------------------------------------- 连接
    def _connect(self):
        """按方言建立 DB-API 连接。"""
        if self.dialect == "sqlite":
            return self._connect_sqlite()
        if self.dialect == "postgresql":
            return self._connect_postgres()
        return self._connect_mysql()

    def _connect_sqlite(self):
        path = self.dsn
        for prefix in ("sqlite:///", "sqlite://"):
            if path.startswith(prefix):
                path = path[len(prefix) :]
                break
        if not path:
            raise ValueError("SQLite 需要提供 dsn（数据库文件路径或 :memory:）")
        return sqlite3.connect(path)

    def _connect_postgres(self):
        if not self.dsn:
            raise ValueError("PostgreSQL 需要提供连接串，例如 postgresql://user:pass@host:5432/db")
        try:
            import psycopg2  # type: ignore
        except ImportError:  # pragma: no cover - 取决于运行环境
            raise RuntimeError(
                "sql_loader 连接 PostgreSQL 需要额外依赖，请执行：pip install psycopg2-binary"
            ) from None
        return psycopg2.connect(self.dsn)

    def _connect_mysql(self):
        if not self.dsn:
            raise ValueError("MySQL 需要提供连接串，例如 mysql://user:pass@host:3306/db")
        try:
            import pymysql  # type: ignore
        except ImportError:  # pragma: no cover - 取决于运行环境
            raise RuntimeError("sql_loader 连接 MySQL 需要额外依赖，请执行：pip install pymysql") from None
        return pymysql.connect(**parse_mysql_dsn(self.dsn))

    # -------------------------------------------------- 执行
    def _build_sql(self) -> str:
        if self.query:
            return self.query
        if not self.table:
            raise ValueError("必须提供 query 或 table 之一")
        if not _TABLE_RE.match(self.table):
            raise ValueError(f"表名 '{self.table}' 不合法：仅支持字母/数字/下划线，可带 schema 前缀")
        return f"SELECT * FROM {self.table}"

    def run(self, ctx: PipelineContext) -> None:
        sql = self._build_sql()
        conn = self._connect()
        try:
            cursor = conn.cursor()
            try:
                cursor.execute(sql)
                columns = [desc[0] for desc in (cursor.description or [])]
                rows = cursor.fetchmany(self.max_rows)
            finally:
                cursor.close()
        finally:
            conn.close()

        if not rows:
            raise ValueError(f"数据库查询未返回任何数据：{sql}")

        lines: list[str] = []
        if self.include_header and columns:
            lines.append(self.separator.join(str(col) for col in columns))
        lines.extend(self.separator.join(render_cell(cell) for cell in row) for row in rows)
        text = "\n".join(lines)

        ctx.put(
            TEXT,
            text,
            producer=self.name,
            dialect=self.dialect,
            source=self.table or self.dsn,
            columns=columns,
            rows=len(rows),
        )

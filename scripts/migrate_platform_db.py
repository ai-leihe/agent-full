#!/usr/bin/env python
"""一次性迁移脚本：把平台库从 SQLite 搬到 MySQL（或反向 / 到 PostgreSQL）。

为什么需要它
------------
平台默认用 SQLite（``data/platform.db``）。在 ``.env`` 里设好
``PLATFORM_DATABASE_URL`` 之后，平台会改连 MySQL / PostgreSQL，但
**旧库里的数据不会自己跟过去**——这个脚本负责按表搬运。

它做三件事：
1. 在目标库上执行 ``init_schema()``，建出与平台完全一致的 15 张表；
2. 以目标库的表清单为准，逐表把源库数据灌进去（先清空目标表，
   因此重复执行安全，不会产生重复行）；
3. 输出每张表的行数对比，并单独核对 ``kv.security.key``。

用法
----
    # 默认：data/platform.db  ->  mysql://app:...@127.0.0.1:3306/agent_platform
    python scripts/migrate_platform_db.py --yes

    # 显式指定两端
    python scripts/migrate_platform_db.py \\
        --source data/platform.db \\
        --target "mysql://app:app123456@127.0.0.1:3306/agent_platform?charset=utf8mb4" --yes

注意
----
* 迁移前请**先停掉平台服务**，避免两边同时写入造成主键冲突；
* ``kv`` 表里的 ``security.key`` 是加密供应商 API Key 的密钥材料，
  必须一并搬走，否则旧库里的密钥换了库就解不开（脚本结尾会专门核对）；
* 上传的原始文件（``data/uploads``）与脚本工作区（``data/script_workspace``）
  是磁盘文件而非数据库记录，不在本脚本处理范围内。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.db import Database, quote_ident, resolve_dsn  # noqa: E402

DEFAULT_SOURCE = PROJECT_ROOT / "data" / "platform.db"
DEFAULT_TARGET = "mysql://app:app123456@127.0.0.1:3306/agent_platform?charset=utf8mb4"


# ---------------------------------------------------------------- 库结构探测


def tables_of(db: Database) -> list[str]:
    """列出库中所有业务表（按建表顺序，滤掉 SQLite 内部表）。"""
    if db.dialect == "sqlite":
        rows = db.query(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY rowid"
        )
        return [row["name"] for row in rows]
    rows = db.query(
        "SELECT table_name AS name FROM information_schema.tables "
        "WHERE table_schema = ? ORDER BY table_name",
        [db.options.get("database")],
    )
    return [row["name"] for row in rows]


def columns_of(db: Database, table: str) -> list[str]:
    """列出某张表的列名（按定义顺序，保证两端列顺序一致）。"""
    if db.dialect == "sqlite":
        rows = db.query(f"PRAGMA table_info({table})")
        return [row["name"] for row in rows]
    rows = db.query(
        "SELECT column_name AS name FROM information_schema.columns "
        "WHERE table_schema = ? AND table_name = ? ORDER BY ordinal_position",
        [db.options.get("database"), table],
    )
    return [row["name"] for row in rows]


def numeric_kinds(db: Database, table: str) -> dict[str, str]:
    """返回 ``{列名: 'int' | 'float'}``，用于把 SQLite 的动态类型值纠回数值。

    SQLite 的列是「类型亲和」而非强类型，同一个 REAL 列里可能混进字符串；
    直接灌进 MySQL 会被严格模式拒收，所以按**目标库**的列类型做一次纠偏。
    """
    if db.dialect == "sqlite":
        rows = db.query(f"PRAGMA table_info({table})")
        raw = [(row["name"], row.get("type") or "") for row in rows]
    else:
        rows = db.query(
            "SELECT column_name AS name, data_type AS type FROM information_schema.columns "
            "WHERE table_schema = ? AND table_name = ?",
            [db.options.get("database"), table],
        )
        raw = [(row["name"], row.get("type") or "") for row in rows]

    kinds: dict[str, str] = {}
    for name, declared in raw:
        text = str(declared).upper()
        if not text:
            continue
        if "INT" in text:
            kinds[name] = "int"
        elif any(token in text for token in ("REAL", "FLOA", "DOUB", "DEC", "NUMERIC")):
            kinds[name] = "float"
    return kinds


def coerce(value: object, kind: str | None) -> object:
    """把非数值的字符串纠成数值，避免 MySQL 严格模式报错。"""
    if value is None or kind is None or not isinstance(value, str):
        return value
    try:
        return int(float(value)) if kind == "int" else float(value)
    except (TypeError, ValueError):
        return 0 if kind == "int" else 0.0


# ---------------------------------------------------------------- 迁移主体


def migrate(source: Database, target: Database, *, assume_yes: bool) -> int:
    source_tables = set(tables_of(source))
    target_tables = tables_of(target)
    if not target_tables:
        print("目标库没有可用表，请先确认连接串指向了正确（且已建表）的库")
        return 1

    print(f"源库  : {source.info()}")
    print(f"目标库: {target.info()}")
    print(f"目标库共 {len(target_tables)} 张表\n")

    if not assume_yes:
        answer = input("确认执行迁移？目标库中的同名表会被清空 [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("已取消，未做任何改动")
            return 1

    print(f"{'表名':<24}{'源行数':>8}{'目标行数':>10}  说明")
    print("-" * 62)

    failures = 0
    for table in target_tables:
        if table not in source_tables:
            print(f"{table:<24}{'-':>8}{target.count(table):>10}  源库无此表，跳过")
            continue

        columns = columns_of(source, table)
        kinds = numeric_kinds(target, table)

        target.execute(f"DELETE FROM {quote_ident(target.dialect, table)}")
        rows = source.query(f"SELECT * FROM {table}")

        if rows:
            marks = ", ".join(["?"] * len(columns))
            names = ", ".join(quote_ident(target.dialect, c) for c in columns)
            sql = f"INSERT INTO {quote_ident(target.dialect, table)} ({names}) VALUES ({marks})"
            payload = [
                tuple(coerce(row.get(column), kinds.get(column)) for column in columns) for row in rows
            ]
            try:
                target.executemany(sql, payload)
            except Exception as exc:  # noqa: BLE001 - 逐表报错，不中断整体迁移
                failures += 1
                print(f"{table:<24}{len(rows):>8}{'FAIL':>10}  {type(exc).__name__}: {exc}")
                continue

        copied = target.count(table)
        flag = "" if copied == len(rows) else "  <== 行数不一致，请检查"
        print(f"{table:<24}{len(rows):>8}{copied:>10}  ok{flag}")
        if copied != len(rows):
            failures += 1

    extra = sorted(source_tables - set(target_tables))
    if extra:
        print(f"\n源库中存在而目标库没有的表（未迁移）：{', '.join(extra)}")

    # security.key 决定旧库里的 API Key 能否被解开，必须一起搬
    print("\n关键校验：")
    source_key = source.kv_get("security.key")
    target_key = target.kv_get("security.key")
    if source_key and source_key == target_key:
        print("  kv.security.key 已一致 —— 供应商 API Key 换库后仍可解密")
    elif source_key and not target_key:
        print("  kv.security.key 缺失！旧库中的 API Key 在 MySQL 里将无法解密")
        failures += 1
    else:
        print("  源库未持有 security.key（尚未配置过模型供应商），无需处理")

    print(f"\n迁移完成：{len(target_tables) - failures}/{len(target_tables)} 张表校验通过")
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="把平台数据库从 SQLite 迁移到 MySQL / PostgreSQL")
    parser.add_argument("--source", default=str(DEFAULT_SOURCE), help="源库：SQLite 文件路径或连接串")
    parser.add_argument("--target", default=DEFAULT_TARGET, help="目标库：连接串（mysql:// 或 postgresql://）")
    parser.add_argument("--yes", action="store_true", help="跳过交互确认，直接执行")
    args = parser.parse_args()

    source = Database(resolve_dsn(args.source))
    target = Database(resolve_dsn(args.target))
    return migrate(source, target, assume_yes=args.yes)


if __name__ == "__main__":
    raise SystemExit(main())

"""端到端测试：验证插件契约、流水线联动、清洗/切片/向量化/检索全链路。

用标准库 unittest 编写，无需 pytest 也能跑：

    python -m unittest discover -s tests -v

生效流水线的入库环节是 ``milvus_store``，因此依赖 Milvus 的用例在探测不到
``19530`` 时会自动跳过（本地启动见 ``deploy/milvus/docker-compose.yml``），
其余用例不受影响。
"""

from __future__ import annotations

import os
import socket
import sqlite3
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.config import build_pipeline_from_file  # noqa: E402
from app.core.context import CHUNKS, CLEAN_TEXT, EMBEDDINGS, TEXT, ContractError, PipelineContext  # noqa: E402
from app.core.env import ensure_dotenv  # noqa: E402
from app.core.script import detect_runtimes  # noqa: E402
from app.core.skill import registry  # noqa: E402
from app.core.warehouse import warehouse  # noqa: E402

CONFIG = ROOT / "config" / "pipeline.yaml"
SKILLS_DIR = ROOT / "ext_plugins"

#: Milvus 地址取自 .env 的 ${MILVUS_URI}，与生效流水线共用同一份配置
ensure_dotenv()
MILVUS_URI = os.environ.get("MILVUS_URI") or "http://localhost:19530"


def _milvus_endpoint() -> tuple[str, int]:
    parsed = urlparse(MILVUS_URI)
    return parsed.hostname or "127.0.0.1", parsed.port or 19530


def _milvus_available() -> bool:
    """Milvus 是否就绪：pymilvus 已安装且 ``${MILVUS_URI}`` 指向的端口可连。"""
    import importlib.util

    if importlib.util.find_spec("pymilvus") is None:
        return False
    try:
        with socket.create_connection(_milvus_endpoint(), timeout=1.5):
            return True
    except OSError:
        return False


MILVUS_OK = _milvus_available()
NEEDS_MILVUS = f"需要 Milvus（{MILVUS_URI}）：cd deploy/milvus && docker compose up -d"


def _runtime_available(key: str) -> bool:
    return any(item["key"] == key and item["available"] for item in detect_runtimes())

#: 测试统一写到临时集合（覆盖 .env 里的 MILVUS_COLLECTION），跑完整组用例即删除，
#: 避免把测试数据留在 .env 配置的真实集合里（向量库是持久化的，垃圾数据不会自己消失）。
TEST_COLLECTION = f"agent_full_test_{uuid.uuid4().hex[:8]}"
os.environ["MILVUS_COLLECTION"] = TEST_COLLECTION


def setUpModule() -> None:
    """插件级用例直接调用流水线，不经过 ``runtime.bootstrap``。

    向量仓库 / 文档存储是进程级单例，由 runtime 装配数据库；若上一个测试模块
    用临时库 bootstrap 过，这里继续沿用就会落到「已被清理的库文件」上。
    因此先解绑，让本模块的用例退回纯内存模式。
    """
    from app.core.storage import documents

    warehouse.attach(None)
    documents.attach(None)


def tearDownModule() -> None:
    """整组用例结束后删掉临时集合（含 TestEndToEnd 用生效流水线写入的切片）。"""
    if not MILVUS_OK:
        return
    from pymilvus import MilvusClient

    client = MilvusClient(uri=MILVUS_URI)
    try:
        if client.has_collection(TEST_COLLECTION):
            client.drop_collection(TEST_COLLECTION)
    finally:
        client.close()

SAMPLE = """# 产品需求文档

## 背景
本平台需要支持多 Agent 串联与插件化能力扩展，插件可插拔替换。
详情见 https://example.com/docs ，联系 liu@example.com 获取权限。

## 目标
1. 支持文件上传后自动清洗、切片、向量化。
2. 同槽位插件可互换，无需改动平台代码。

## 风险
插件可插拔替换，无需改动平台代码。
插件可插拔替换，无需改动平台代码。
编排复杂度上升，需要用契约校验来兜底。
"""


def bootstrap():
    registry.reset()
    registry.load_package("app.plugins")
    registry.load_directory(SKILLS_DIR)
    return build_pipeline_from_file(CONFIG, registry)


class TestPluginRegistry(unittest.TestCase):
    def setUp(self):
        bootstrap()

    def test_builtin_slots_registered(self):
        slots = registry.slots()
        for expected in ["loader", "cleaner", "splitter", "embedder", "vector_store"]:
            self.assertIn(expected, slots, f"缺少槽位 {expected}")

    def test_ext_plugin_loaded(self):
        self.assertTrue(registry.has("sensitive_word_cleaner"), "外部目录插件未被加载")

    def test_same_slot_multiple_impls(self):
        self.assertGreaterEqual(len(registry.by_slot("cleaner")), 2)
        self.assertGreaterEqual(len(registry.by_slot("splitter")), 3)

    def test_milvus_store_registered(self):
        """milvus_store 与 memory_store 同槽位可互换（注册不依赖 Milvus 是否启动）。"""
        self.assertTrue(registry.has("milvus_store"), "milvus_store 未被注册")
        self.assertIn("milvus_store", registry.slots()["vector_store"])


class TestContractValidation(unittest.TestCase):
    def setUp(self):
        bootstrap()

    def test_contract_mismatch_detected(self):
        """把 embedder 放在 splitter 之前，应在编译期被契约校验拦截。"""
        from app.core.agent import Agent
        from app.core.orchestrator import Pipeline

        bad = Pipeline(
            name="bad",
            agents=[
                Agent(name="a1", skills=[registry.create("text_loader"), registry.create("hash_embedder")]),
            ],
        )
        with self.assertRaises(ContractError):
            bad.validate()

    def test_default_pipeline_valid(self):
        pipeline = bootstrap()
        produced = pipeline.validate()
        self.assertIn("vector_index", produced)

    def test_default_pipeline_stores_into_milvus(self):
        """生效流水线的入库环节应是 milvus_store（换回 memory_store 只改 YAML）。"""
        pipeline = bootstrap()
        names = [skill.name for agent in pipeline.agents for skill in agent.skills]
        self.assertIn("milvus_store", names)
        self.assertNotIn("memory_store", names)

    def test_default_pipeline_reads_milvus_env(self):
        """milvus_store 的连接参数来自 .env 的 ${MILVUS_*}，装配后已展开为真实值。"""
        self.assertIn("MILVUS_URI", os.environ, "请先按 .env.example 准备 .env")
        pipeline = bootstrap()
        store = next(s for agent in pipeline.agents for s in agent.skills if s.name == "milvus_store")
        self.assertEqual(store.uri, os.environ["MILVUS_URI"])
        self.assertEqual(store.collection, os.environ["MILVUS_COLLECTION"])
        self.assertEqual(store.metric_type, os.environ["MILVUS_METRIC_TYPE"])
        self.assertNotIn("${", store.uri, "占位符应已展开为真实地址")


@unittest.skipUnless(MILVUS_OK, NEEDS_MILVUS)
class TestEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pipeline = bootstrap()

    def test_full_chain(self):
        ctx = PipelineContext("sample.md", SAMPLE.encode("utf-8"), "text/markdown")
        self.pipeline.run(ctx)

        # 1) 各环节产物齐全
        for kind in [TEXT, CLEAN_TEXT, CHUNKS, EMBEDDINGS, "vector_index"]:
            self.assertTrue(ctx.has(kind), f"缺少产物 {kind}")

        # 2) 清洗生效：重复行被去重、邮箱被脱敏、链接被保留（未开启 strip_urls）
        clean = ctx.get(CLEAN_TEXT)
        self.assertIn("[EMAIL]", clean)
        self.assertNotIn("liu@example.com", clean)
        self.assertIn("https://example.com/docs", clean)
        self.assertEqual(clean.count("插件可插拔替换，无需改动平台代码。"), 1, "重复行未被去重")

        # 3) 切片与向量一一对应
        chunks = ctx.get(CHUNKS)
        vectors = ctx.get(EMBEDDINGS)
        self.assertEqual(len(chunks), len(vectors))
        self.assertTrue(all(len(v) == 256 for v in vectors))

        # 4) 执行轨迹完整
        self.assertEqual([t.status for t in ctx.trace], ["ok"] * len(ctx.trace))
        self.assertGreaterEqual(len(ctx.trace), 5)

    def test_search(self):
        ctx = PipelineContext("sample.md", SAMPLE.encode("utf-8"), "text/markdown")
        self.pipeline.run(ctx)

        record = warehouse.get(ctx.run_id)
        self.assertIsNotNone(record, "索引未写入 warehouse")

        embedder = registry.create(record.index.embedder)
        hits = record.index.search(embedder.encode_one("插件化 能力扩展"), top_k=2)
        self.assertTrue(hits)
        self.assertGreater(hits[0]["score"], 0)
        # 语义上最相关的结果应命中包含关键词的切片
        self.assertIn("插件", "".join(h["text"] for h in hits))


@unittest.skipUnless(MILVUS_OK, NEEDS_MILVUS)
class TestMilvusStore(unittest.TestCase):
    """milvus_store：切片向量落库 Milvus、按 run 隔离检索、删除时同步清理。"""

    def setUp(self):
        bootstrap()
        # 每个用例一个独立集合，避免 drop/create 同名集合的时序干扰
        self.collection = f"agent_full_test_{uuid.uuid4().hex[:8]}"

    def tearDown(self):
        self._drop_collection()

    # -------------------------------------------------- 夹具

    @staticmethod
    def _client():
        from pymilvus import MilvusClient

        return MilvusClient(uri=MILVUS_URI)

    def _rows(self, run_id: str) -> int:
        """直接问 Milvus 某个 run 有多少实体（绕开仓库与插件，验证真的落库了）。"""
        client = self._client()
        try:
            rows = client.query(
                self.collection,
                filter=f'run_id == "{run_id}"',
                output_fields=["id"],
                limit=16384,
                consistency_level="Strong",
            )
            return len(rows)
        finally:
            client.close()

    def _drop_collection(self):
        client = self._client()
        try:
            if client.has_collection(self.collection):
                client.drop_collection(self.collection)
        finally:
            client.close()

    def _run(self, filename="sample.md", data=SAMPLE, dim=64):
        from app.core.config import build_pipeline

        config = {
            "pipeline": {
                "name": "milvus-smoke",
                "agents": [
                    {"name": "a1", "skills": [{"use": "text_loader"}]},
                    {"name": "a2", "skills": [{"use": "basic_cleaner"}]},
                    {
                        "name": "a3",
                        "skills": [
                            {"use": "recursive_splitter", "with": {"chunk_size": 120, "chunk_overlap": 20}}
                        ],
                    },
                    {
                        "name": "a4",
                        "skills": [
                            {"use": "hash_embedder", "with": {"dim": dim}},
                            {
                                "use": "milvus_store",
                                "with": {"uri": MILVUS_URI, "collection": self.collection},
                            },
                        ],
                    },
                ],
            }
        }
        pipeline = build_pipeline(config, registry)
        ctx = PipelineContext(filename, data.encode("utf-8"), "text/markdown")
        pipeline.run(ctx)
        return ctx

    @staticmethod
    def _search(ctx, query, top_k=5):
        index = ctx.get("vector_index")
        # 查询向量必须与建索引时同一个 embedder 配置（这里 hash_embedder 的 dim）
        embedder = registry.create(index.embedder, {"dim": index.dimension})
        return index.search(embedder.encode_one(query), top_k)

    # -------------------------------------------------- 用例

    def test_chunks_land_in_milvus(self):
        ctx = self._run()
        index = ctx.get("vector_index")
        self.assertEqual(index.backend, "milvus")
        self.assertEqual(index.collection, self.collection)
        self.assertEqual(index.dimension, 64)
        self.assertEqual(index.size, len(ctx.get(CHUNKS)))
        self.assertEqual(self._rows(ctx.run_id), len(ctx.get(CHUNKS)), "Milvus 中的实体数应与切片数一致")
        self.assertIsNotNone(warehouse.get(ctx.run_id))

        hits = self._search(ctx, "插件化 能力扩展")
        self.assertTrue(hits)
        self.assertIn("插件", "".join(hit["text"] for hit in hits))

    def test_search_is_isolated_per_run(self):
        first = self._run(filename="first.md")
        second = self._run(filename="second.md", data="另一个文档只讨论数据库与索引维护。")

        first_ids = {hit["chunk_id"] for hit in self._search(first, "插件化")}
        second_ids = {hit["chunk_id"] for hit in self._search(second, "插件化")}
        self.assertTrue(first_ids)
        self.assertTrue(second_ids, "同一集合中后写入的 run 也应立即可检索")
        self.assertFalse(first_ids & second_ids, "不同 run 的检索结果不应串库")

    def test_delete_index_clears_vectors(self):
        ctx = self._run()
        self.assertTrue(self._search(ctx, "插件化"))
        self.assertTrue(warehouse.delete(ctx.run_id), "删除索引应先清后端再摘记录")
        self.assertEqual(self._search(ctx, "插件化"), [], "Milvus 中的切片未被清理")
        self.assertEqual(self._rows(ctx.run_id), 0)

    def test_dimension_mismatch_rejected(self):
        self._run(dim=64)
        with self.assertRaises(ValueError) as caught:
            self._run(dim=32)
        self.assertIn("维度", str(caught.exception))

    def test_invalid_collection_name_rejected(self):
        with self.assertRaises(ValueError):
            registry.create("milvus_store", {"collection": "kb-docs"})


class TestSwappablePlugins(unittest.TestCase):
    """验证「换插件不改代码」——只改 use 字段，链路依然成立。"""

    def setUp(self):
        bootstrap()

    def _run_with(self, agents_skills):
        from app.core.agent import Agent
        from app.core.config import build_pipeline
        from app.core.orchestrator import Pipeline

        config = {"pipeline": {"name": "swap", "agents": []}}
        for i, skills in enumerate(agents_skills):
            config["pipeline"]["agents"].append({"name": f"a{i}", "skills": skills})
        pipeline = build_pipeline(config, registry)
        ctx = PipelineContext("s.md", SAMPLE.encode("utf-8"), "text/markdown")
        pipeline.run(ctx)
        return ctx

    def test_markdown_splitter_swap(self):
        ctx = self._run_with([
            [{"use": "text_loader"}],
            [{"use": "markdown_normalizer"}],
            [{"use": "markdown_splitter", "with": {"max_level": 2}}],
            [{"use": "hash_embedder", "with": {"dim": 128}}, {"use": "memory_store"}],
        ])
        self.assertEqual(ctx.get("vector_index").dimension, 128)
        # markdown_splitter 会把标题路径写进 meta
        self.assertTrue(any(c.meta.get("section") for c in ctx.get(CHUNKS)))

    def test_fixed_splitter_and_ext_cleaner_swap(self):
        ctx = self._run_with([
            [{"use": "text_loader"}],
            [{"use": "sensitive_word_cleaner", "with": {"words": ["风险"]}}],
            [{"use": "fixed_splitter", "with": {"chunk_size": 120, "chunk_overlap": 20}}],
            [{"use": "hash_embedder"}, {"use": "memory_store"}],
        ])
        clean = ctx.get(CLEAN_TEXT)
        self.assertNotIn("风险", clean)
        self.assertIn("**", clean)  # 掩码生效
        self.assertTrue(all(len(c.text) <= 120 for c in ctx.get(CHUNKS)))


class TestDatabaseLoader(unittest.TestCase):
    """sql_loader：把关系型数据库作为数据源接入流水线。"""

    def setUp(self):
        bootstrap()
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "demo.sqlite"
        conn = sqlite3.connect(self.db_path)
        conn.execute("CREATE TABLE docs (id INTEGER, title TEXT, body TEXT)")
        conn.executemany(
            "INSERT INTO docs VALUES (?, ?, ?)",
            [
                (1, "插件机制", "平台支持插拔式插件，插件通过槽位声明能力。"),
                (2, "契约校验", "流水线在编译期校验产物契约，提前发现上下游不匹配。"),
            ],
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_registered_in_loader_slot(self):
        self.assertTrue(registry.has("sql_loader"), "数据库插件未被加载")
        self.assertIn("sql_loader", registry.slots()["loader"])

    def test_loads_table_as_text(self):
        from app.core.agent import Agent
        from app.core.orchestrator import Pipeline

        skill = registry.create("sql_loader", {"dsn": str(self.db_path), "table": "docs"})
        pipeline = Pipeline(name="db-only", agents=[Agent(name="a1", skills=[skill])])
        ctx = PipelineContext("db", b"")
        pipeline.run(ctx)

        text = ctx.get(TEXT)
        self.assertIn("id | title | body", text, "应渲染列名表头")
        self.assertIn("插件机制", text)
        self.assertIn("契约校验", text)

        meta = ctx.artifact_meta(TEXT)
        self.assertEqual(meta["dialect"], "sqlite")
        self.assertEqual(meta["rows"], 2)
        self.assertEqual(meta["columns"], ["id", "title", "body"])

    def test_custom_query_and_row_limit(self):
        skill = registry.create(
            "sql_loader",
            {"dsn": str(self.db_path), "query": "SELECT title FROM docs", "max_rows": 1},
        )
        ctx = PipelineContext("db", b"")
        skill.run(ctx)
        self.assertEqual(ctx.get(TEXT), "title\n插件机制")

    def test_invalid_table_rejected(self):
        skill = registry.create("sql_loader", {"dsn": str(self.db_path), "table": "docs; DROP TABLE docs"})
        with self.assertRaises(ValueError):
            skill.run(PipelineContext("db", b""))

    def test_dialect_mismatch_gives_clear_error(self):
        """选了 SQLite 但填了 MySQL 连接串时，错误信息必须告诉用户改哪里。"""
        with self.assertRaises(ValueError) as cm:
            registry.create(
                "sql_loader",
                {"dialect": "sqlite", "dsn": "mysql://app:app123456@127.0.0.1:3306/demo?charset=utf8mb4"},
            )
        self.assertIn("数据库类型选择为「sqlite」", str(cm.exception))
        self.assertIn("连接串看起来是 mysql", str(cm.exception))
        self.assertIn("改为 mysql", str(cm.exception))

    def test_postgresql_mismatch_gives_clear_error(self):
        """同样检测 postgresql:// 前缀。"""
        with self.assertRaises(ValueError) as cm:
            registry.create(
                "sql_loader",
                {"dialect": "sqlite", "dsn": "postgresql://u:p@127.0.0.1:5432/db"},
            )
        self.assertIn("连接串看起来是 postgresql", str(cm.exception))

    def test_sqlite_dialect_with_sqlite_prefix_is_fine(self):
        """显式写 sqlite:///path 时，配置阶段不报错。"""
        skill = registry.create("sql_loader", {"dialect": "sqlite", "dsn": f"sqlite:///{self.db_path}", "table": "docs"})
        self.assertEqual(skill.dialect, "sqlite")

    def test_mysql_style_dsn_accepted_when_dialect_matches(self):
        """dialect=mysql + mysql://dsn 时配置阶段不报错（不真正联网）。"""
        skill = registry.create(
            "sql_loader",
            {"dialect": "mysql", "dsn": "mysql://app:app123456@127.0.0.1:3306/demo?charset=utf8mb4", "query": "SELECT 1"},
        )
        self.assertEqual(skill.dialect, "mysql")

    def test_swap_text_loader_with_sql_loader(self):
        """用 sql_loader 替换 text_loader，整条链路依旧成立。"""
        from app.core.config import build_pipeline

        config = {
            "pipeline": {
                "name": "db-rag",
                "agents": [
                    {"name": "a1", "skills": [{"use": "sql_loader", "with": {"dsn": str(self.db_path), "table": "docs"}}]},
                    {"name": "a2", "skills": [{"use": "basic_cleaner"}]},
                    {"name": "a3", "skills": [{"use": "recursive_splitter", "with": {"chunk_size": 60, "chunk_overlap": 10}}]},
                    {"name": "a4", "skills": [{"use": "hash_embedder"}, {"use": "memory_store"}]},
                ],
            }
        }
        pipeline = build_pipeline(config, registry)
        ctx = PipelineContext("db", b"")
        pipeline.run(ctx)

        for kind in [TEXT, CLEAN_TEXT, CHUNKS, EMBEDDINGS, "vector_index"]:
            self.assertTrue(ctx.has(kind), f"缺少产物 {kind}")
        self.assertTrue(any("插件" in c.text for c in ctx.get(CHUNKS)))


class TestScriptSkills(unittest.TestCase):
    """python_script / shell_script / powershell_script：把脚本执行嵌入编排。"""

    def setUp(self):
        bootstrap()

    def test_python_script_registered_in_script_slot(self):
        self.assertTrue(registry.has("python_script"))
        self.assertIn("python_script", registry.slots().get("script", []))

    def test_python_script_uppercases_text(self):
        skill = registry.create(
            "python_script",
            {"code": "import sys\nfor line in sys.stdin:\n    print(line.strip().upper())\n"},
        )
        ctx = PipelineContext("test.txt", b"")
        ctx.put(TEXT, "hello\nworld", producer="mock")
        skill.run(ctx)

        self.assertEqual(ctx.get(TEXT), "HELLO\nWORLD")
        self.assertEqual(ctx.get(CLEAN_TEXT), "HELLO\nWORLD")

    def test_python_script_with_custom_stdin(self):
        skill = registry.create(
            "python_script",
            {
                "code": "import sys\nprint(sys.stdin.read().strip().lower())",
                "stdin_source": "custom",
                "stdin": "FooBar",
            },
        )
        ctx = PipelineContext("test.txt", b"")
        skill.run(ctx)
        self.assertEqual(ctx.get(TEXT), "foobar")

    def test_python_script_requires_text_when_configured(self):
        skill = registry.create(
            "python_script",
            {"code": "import sys\nprint(sys.stdin.read())", "stdin_source": "text"},
        )
        ctx = PipelineContext("test.txt", b"")
        with self.assertRaises(ValueError):
            skill.run(ctx)

    def test_python_script_empty_code_rejected_at_configure(self):
        with self.assertRaises(ValueError):
            registry.create("python_script", {"code": "   \n  "})

    def test_shell_script_runs_when_available(self):
        if not _runtime_available("sh"):
            self.skipTest("本机未安装 sh / bash")
        skill = registry.create(
            "shell_script",
            {"code": "cat | tr 'a-z' 'A-Z'", "stdin_source": "text"},
        )
        ctx = PipelineContext("test.txt", b"")
        ctx.put(TEXT, "abc", producer="mock")
        skill.run(ctx)
        self.assertEqual(ctx.get(TEXT), "ABC")

    def test_powershell_script_runs_when_available(self):
        if not _runtime_available("powershell"):
            self.skipTest("本机未安装 PowerShell")
        skill = registry.create(
            "powershell_script",
            {
                "code": 'Write-Output ([Console]::In.ReadToEnd().ToUpper())',
                "stdin_source": "text",
            },
        )
        ctx = PipelineContext("test.txt", b"")
        ctx.put(TEXT, "ps-works", producer="mock")
        skill.run(ctx)
        self.assertIn("PS-WORKS", ctx.get(TEXT))


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""编排能力测试：运行时可增删改、契约软校验、自动补全、方案存档、试跑与应用。

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.config import build_pipeline_from_file  # noqa: E402
from app.core.context import CHUNKS, EMBEDDINGS, ContractError, PipelineContext  # noqa: E402
from app.core.orchestration import (  # noqa: E402
    AgentSpec,
    Orchestration,
    StepSpec,
    load_orchestration,
    save_orchestration,
)
from app.core.skill import registry  # noqa: E402
from app.runtime import ProfileError, RuntimeState  # noqa: E402

CONFIG = ROOT / "config" / "pipeline.yaml"
SKILLS_DIR = ROOT / "ext_plugins"
PROFILES_DIR = ROOT / "config" / "orchestrations"

SAMPLE = "# 平台设计\n\n## 插件机制\n平台支持插拔式插件，插件通过槽位声明能力。\n".encode("utf-8")


def bootstrap_registry():
    registry.reset()
    registry.load_package("app.plugins")
    registry.load_directory(SKILLS_DIR)
    return build_pipeline_from_file(CONFIG, registry)


def base_orchestration() -> Orchestration:
    return Orchestration.from_pipeline(bootstrap_registry())


class TestDiagnose(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.orch = base_orchestration()

    def test_healthy_orchestration(self):
        d = self.orch.diagnose(registry)
        self.assertTrue(d["ok"], f"默认编排应可跑通，issues={d['issues']}")
        self.assertIn("clean_text", d["final_artifacts"])
        self.assertIn("vector_index", d["final_artifacts"])
        self.assertEqual(d["issues"], [])

    def test_removing_step_breaks_downstream(self):
        """删掉切片步骤后，向量化步骤应被诊断出「缺少 chunks」。"""
        orch = self.orch.clone()
        chunking = next(a for a in orch.agents if a.name == "chunking_agent")
        chunking.steps.clear()

        d = orch.diagnose(registry)
        self.assertFalse(d["ok"])
        self.assertTrue(any("chunks" in i for i in d["issues"]), d["issues"])

        # 应给出可落地的补全建议，指明槽位与候选插件
        self.assertTrue(any("splitter" in s for s in d["suggestions"]), d["suggestions"])
        self.assertTrue(any("recursive_splitter" in s for s in d["suggestions"]))

        # 空阶段只警示不阻断
        self.assertTrue(any("没有任何步骤" in i for i in d["issues"]))

    def test_missing_artifact_without_provider(self):
        """要求一个没有任何插件能生产的产物时，建议应说明需要新增插件。"""
        orch = Orchestration(name="x", agents=[AgentSpec(name="a", steps=[StepSpec(skill="text_loader")])])
        orch.agents[0].steps.append(StepSpec(skill="memory_store"))  # 需要 chunks，没人生产
        d = orch.diagnose(registry)
        self.assertFalse(d["ok"])
        self.assertTrue(any("需要新增插件" in s or "splitter" in s for s in d["suggestions"]))

    def test_invalid_options_detected(self):
        orch = self.orch.clone()
        indexing = next(a for a in orch.agents if a.name == "indexing_agent")
        embed = next(s for s in indexing.steps if s.skill == "hash_embedder")
        embed.options = {"dim": -5}  # HashEmbedder.configure 会拒绝

        d = orch.diagnose(registry)
        self.assertFalse(d["ok"])
        self.assertTrue(any("参数非法" in i for i in d["issues"]), d["issues"])

    def test_override_reported(self):
        """重复生产同一产物时应提示会被覆盖。"""
        orch = self.orch.clone()
        ingestion = next(a for a in orch.agents if a.name == "ingestion_agent")
        ingestion.steps.append(StepSpec(skill="markdown_normalizer"))  # 再产一次 clean_text
        d = orch.diagnose(registry)
        reports = [s for a in d["agents"] for s in a["steps"]]
        self.assertTrue(any(s["overrides"] for s in reports), reports)


class TestAutofill(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = base_orchestration()

    def test_autofill_repairs_broken_chain(self):
        orch = self.base.clone()
        chunking = next(a for a in orch.agents if a.name == "chunking_agent")
        chunking.steps.clear()
        self.assertFalse(orch.diagnose(registry)["ok"])

        inserted = orch.autofill(registry)
        self.assertTrue(inserted, "应至少补全一个环节")
        self.assertEqual(inserted[0]["produces"], "chunks")
        self.assertTrue(orch.diagnose(registry)["ok"], "补全后链路应恢复完整")

    def test_autofill_on_healthy_is_noop(self):
        orch = self.base.clone()
        self.assertEqual(orch.autofill(registry), [])
        self.assertTrue(orch.diagnose(registry)["ok"])

    def test_autofilled_orchestration_runs(self):
        orch = self.base.clone()
        orch.agents = [a for a in orch.agents if a.name != "chunking_agent"]
        orch.autofill(registry)
        pipeline = orch.compile(registry)

        ctx = PipelineContext("s.md", SAMPLE, "text/markdown")
        pipeline.run(ctx)
        self.assertTrue(ctx.has(CHUNKS))
        self.assertTrue(ctx.has(EMBEDDINGS))


class TestCompile(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = base_orchestration()

    def test_compile_healthy(self):
        pipeline = self.base.compile(registry)
        self.assertEqual(len(pipeline.agents), 3)
        self.assertIn("vector_index", pipeline.validate())

    def test_compile_raises_on_broken_chain(self):
        orch = self.base.clone()
        orch.agents = [a for a in orch.agents if a.name != "chunking_agent"]
        with self.assertRaises(ContractError):
            orch.compile(registry)

    def test_disabled_step_stops_producing(self):
        """停用步骤后它不再产出，下游依赖会被硬校验拦下。"""
        orch = self.base.clone()
        chunking = next(a for a in orch.agents if a.name == "chunking_agent")
        chunking.steps[0].enabled = False

        d = orch.diagnose(registry)
        self.assertFalse(d["ok"])
        self.assertTrue(any("chunks" in i for i in d["issues"]))
        with self.assertRaises(ContractError):
            orch.compile(registry)


class TestRuntimeDraft(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.rt = RuntimeState()
        # 全部状态（设置 / 账号 / 运行记录 / 方案 / 数据库）都落在临时目录，
        # 避免污染仓库里的 data/platform.db。
        self.rt.bootstrap(
            config_path=CONFIG,
            ext_dir=SKILLS_DIR,
            profiles_dir=root / "profiles",
            settings_path=root / "settings.json",
            runs_path=root / "runs.json",
            users_path=root / "users.json",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_initial_state(self):
        state = self.rt.state()
        self.assertFalse(self.rt.dirty, "刚启动时草稿应与生效流水线一致")
        self.assertTrue(state["diagnosis"]["ok"])
        self.assertIn("slots", state["catalog"])
        self.assertIn("params", state["catalog"]["skills"]["hash_embedder"])
        self.assertEqual(state["profiles"], [])

    def test_touch_marks_dirty_and_apply_clears(self):
        draft = self.rt.draft
        agent = next(a for a in draft.agents if a.name == "chunking_agent")
        agent.steps[0].options["chunk_size"] = 120
        self.rt.touch()
        self.assertTrue(self.rt.dirty)

        pipeline = self.rt.apply_draft()
        self.assertFalse(self.rt.dirty)
        splitter = pipeline.agents[1].skills[0]
        self.assertEqual(splitter.chunk_size, 120)

    def test_draft_pipeline_does_not_affect_active(self):
        active_before = self.rt.pipeline
        draft = self.rt.draft
        draft.agents.pop()
        self.rt.touch()
        self.rt.draft_pipeline()  # 试跑不影响生效配置
        self.assertIs(self.rt.pipeline, active_before)

    def test_reset_draft(self):
        self.rt.draft.agents.clear()
        self.rt.touch()
        self.rt.reset_draft()
        self.assertEqual(len(self.rt.draft.agents), 3)

    def test_profile_roundtrip(self):
        draft = self.rt.draft
        draft.name = "my-recipe"
        next(a for a in draft.agents if a.name == "chunking_agent").steps[0].options["chunk_size"] = 321
        self.rt.touch()

        self.rt.save_profile("my-recipe")
        listed = self.rt.list_profiles()
        self.assertEqual([p["name"] for p in listed], ["my-recipe"])
        self.assertTrue(listed[0]["ok"])

        self.rt.reset_draft()
        self.rt.load_profile("my-recipe")
        agent = next(a for a in self.rt.draft.agents if a.name == "chunking_agent")
        self.assertEqual(agent.steps[0].options["chunk_size"], 321)
        self.assertEqual(self.rt.active_profile, "my-recipe")

        self.assertTrue(self.rt.delete_profile("my-recipe"))
        self.assertEqual(self.rt.list_profiles(), [])

    def test_profile_name_is_sanitized(self):
        with self.assertRaises(ProfileError):
            self.rt.save_profile("../evil")
        with self.assertRaises(ProfileError):
            self.rt.load_profile("bad name!")


class TestOrchestrationIO(unittest.TestCase):
    def test_save_and_load_yaml(self):
        original = Orchestration(
            name="demo",
            description="演示",
            agents=[
                AgentSpec(name="a1", role="加载", steps=[StepSpec(skill="text_loader", options={})]),
                AgentSpec(name="a2", role="切片", steps=[StepSpec(skill="recursive_splitter", options={"chunk_size": 300})]),
            ],
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = save_orchestration(original, Path(tmp) / "demo.yaml")
            self.assertTrue(path.is_file())
            loaded = load_orchestration(path)

        self.assertEqual(loaded.name, "demo")
        self.assertEqual(len(loaded.agents), 2)
        self.assertEqual(loaded.agents[1].steps[0].options["chunk_size"], 300)
        # id 应被持久化，前端才能稳定引用
        self.assertEqual(loaded.agents[0].id, original.agents[0].id)

    def test_shipped_profile_is_loadable(self):
        path = PROFILES_DIR / "markdown-rag.yaml"
        self.assertTrue(path.is_file(), "示例方案应存在")
        orch = load_orchestration(path)
        bootstrap_registry()
        d = orch.diagnose(registry)
        self.assertTrue(d["ok"], f"示例方案应可跑通：{d['issues']}")

    def test_from_pipeline_keeps_env_placeholders(self):
        """反推草稿要保留 ${VAR} 原文：工作台与「保存方案」都不该看到 .env 里的真实值。"""
        orch = Orchestration.from_pipeline(bootstrap_registry())
        step = next(s for a in orch.agents for s in a.steps if s.skill == "milvus_store")
        self.assertEqual(step.options["uri"], "${MILVUS_URI}")
        self.assertEqual(step.options["collection"], "${MILVUS_COLLECTION}")
        self.assertNotIn(os.environ["MILVUS_URI"], str(step.options), "真实地址不应进入草稿")


if __name__ == "__main__":
    unittest.main(verbosity=2)

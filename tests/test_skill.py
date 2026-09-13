"""针对 ``app/core/skill.py`` 的单元测试：插件基类契约 + 注册中心的功能与健壮性。

覆盖范围
--------
* :class:`Skill` 元信息：``param_schema`` / ``default_options`` / ``manifest`` / 抽象约束
* :class:`SkillRegistry`：注册、冲突检测、启停、查询、反查、目录、实例化
* 发现机制：``load_package`` / ``load_directory``（含热重载、缺失目录、下划线文件）

用标准库 unittest 编写，无需 pytest 也能跑：

    python -m unittest tests.test_skill -v
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.context import (  # noqa: E402
    CLEAN_TEXT,
    TEXT,
    MissingArtifactError,
    PipelineContext,
)
from app.core.skill import Skill, SkillRegistry  # noqa: E402
from app.core.skill import registry as global_registry  # noqa: E402


# ------------------------------------------------------------------ 测试夹具


def make_skill(
    name,
    *,
    slot="generic",
    consumes=(),
    produces=(),
    param_schema=None,
    optional=False,
    version="1.0.0",
    configure=None,
    run=None,
):
    """动态构造一个可直接实例化的 Skill 子类，避免为每个用例手写样板类。"""
    attrs = {
        "name": name,
        "version": version,
        "slot": slot,
        "consumes": tuple(consumes),
        "produces": tuple(produces),
        "optional": optional,
        "param_schema": dict(param_schema or {}),
        "run": run or (lambda self, ctx: None),
    }
    if configure is not None:
        attrs["configure"] = configure
    return type(f"Skill_{name}", (Skill,), attrs)


_PLUGIN_TEMPLATE = '''
from app.core.skill import Skill, skill


@skill
class Probe(Skill):
    name = "{name}"
    slot = "cleaner"
    description = "probe plugin"
    consumes = ("text",)
    produces = ("clean_text",)
    param_schema = {{"level": {{"type": "int", "default": 1}}}}

    def run(self, ctx):
        ctx.put("clean_text", ctx.require("text"), producer=self.name)
'''


def _write_plugin(path: Path, name: str) -> None:
    path.write_text(_PLUGIN_TEMPLATE.format(name=name), encoding="utf-8")


# ------------------------------------------------------------------ Skill 基类


class TestSkillMetadata(unittest.TestCase):
    def test_abstract_skill_cannot_be_instantiated(self):
        with self.assertRaises(TypeError):
            Skill()  # type: ignore[abstract]

    def test_subclass_without_run_is_not_instantiable(self):
        no_run = type("NoRun", (Skill,), {"name": "no_run"})
        with self.assertRaises(TypeError):
            no_run()

    def test_options_passed_to_configure_and_stored(self):
        captured: dict = {}

        def configure(self, options):
            captured.update(options)
            self.normalized = int(options.get("n", 0))

        inst = make_skill("cfg", configure=configure)(n=3)
        self.assertEqual(captured, {"n": 3})
        self.assertEqual(inst.options, {"n": 3})
        self.assertEqual(inst.normalized, 3)

    def test_default_options_from_param_schema(self):
        schema = {
            "chunk_size": {"type": "int", "default": 500},
            "flag": {"type": "bool"},  # 无 default → 应被跳过
            "mode": {"type": "str", "default": "a"},
        }
        self.assertEqual(make_skill("schema", param_schema=schema).default_options(), {"chunk_size": 500, "mode": "a"})

    def test_default_options_returns_isolated_values(self):
        """可变默认值（list/dict）不应在多次调用间共享同一对象。"""
        schema = {"words": {"type": "list", "default": ["a", "b"]}}
        cls = make_skill("isolation", param_schema=schema)
        first, second = cls.default_options(), cls.default_options()
        self.assertIsNot(first["words"], second["words"])
        first["words"].append("mutated")
        self.assertEqual(cls.default_options()["words"], ["a", "b"])
        self.assertEqual(cls.param_schema["words"]["default"], ["a", "b"])

    def test_manifest_content(self):
        cls = make_skill(
            "manifested",
            slot="cleaner",
            consumes=(TEXT,),
            produces=(CLEAN_TEXT,),
            optional=True,
            version="2.3.4",
            param_schema={"x": {"type": "int", "default": 1}},
        )
        m = cls.manifest()
        self.assertEqual(m["name"], "manifested")
        self.assertEqual(m["version"], "2.3.4")
        self.assertEqual(m["slot"], "cleaner")
        self.assertEqual(m["consumes"], [TEXT])
        self.assertEqual(m["produces"], [CLEAN_TEXT])
        self.assertTrue(m["optional"])
        self.assertEqual(m["params"], {"x": {"type": "int", "default": 1}})
        self.assertEqual(m["defaults"], {"x": 1})

    def test_manifest_returns_detached_lists(self):
        cls = make_skill("copies", consumes=(TEXT,))
        m = cls.manifest()
        m["consumes"].append("bogus")
        self.assertEqual(list(cls.consumes), [TEXT], "manifest 不应暴露内部元组的可变引用")

    def test_repr_contains_name_and_slot(self):
        text = repr(make_skill("repr_skill", slot="cleaner")())
        self.assertIn("repr_skill", text)
        self.assertIn("cleaner", text)


# ------------------------------------------------------------------ 注册 / 注销


class TestRegistryRegister(unittest.TestCase):
    def setUp(self):
        self.reg = SkillRegistry()

    def test_register_and_get(self):
        cls = make_skill("a")
        self.assertIs(self.reg.register(cls), cls)
        self.assertIs(self.reg.get("a"), cls)
        self.assertTrue(self.reg.has("a"))

    def test_register_rejects_empty_name(self):
        with self.assertRaises(ValueError):
            self.reg.register(make_skill(""))

    def test_register_rejects_non_skill_class(self):
        fake = type("Fake", (), {"name": "fake"})
        with self.assertRaises(TypeError):
            self.reg.register(fake)

    def test_duplicate_name_rejected(self):
        self.reg.register(make_skill("dup"))
        with self.assertRaises(ValueError):
            self.reg.register(make_skill("dup"))

    def test_decorator_syntax_registers(self):
        @self.reg
        class Decorated(Skill):
            name = "decorated"
            slot = "x"

            def run(self, ctx):
                pass

        self.assertTrue(self.reg.raw_has("decorated"))

    def test_unregister_is_idempotent(self):
        self.reg.register(make_skill("a"))
        self.reg.disable("a")
        self.reg.unregister("a")
        self.assertFalse(self.reg.raw_has("a"))
        self.assertFalse(self.reg.is_disabled("a"))
        self.reg.unregister("a")  # 再次注销不应报错

    def test_reset_clears_everything(self):
        self.reg.register(make_skill("a"))
        self.reg.disable("a")
        self.reg.reset()
        self.assertEqual(self.reg.all_names(), [])
        self.assertEqual(self.reg.disabled(), [])

    def test_registries_are_isolated(self):
        self.reg.register(make_skill("only_here"))
        self.assertFalse(SkillRegistry().raw_has("only_here"))


# ------------------------------------------------------------------ 启停


class TestRegistryToggle(unittest.TestCase):
    def setUp(self):
        self.reg = SkillRegistry()
        self.reg.register(make_skill("a", slot="cleaner"))
        self.reg.register(make_skill("b", slot="cleaner"))
        self.reg.register(make_skill("c", slot="splitter"))

    def test_disable_enable_roundtrip(self):
        self.assertTrue(self.reg.disable("a"))
        self.assertTrue(self.reg.is_disabled("a"))
        self.assertFalse(self.reg.has("a"))
        self.assertTrue(self.reg.raw_has("a"))
        self.assertTrue(self.reg.enable("a"))
        self.assertFalse(self.reg.is_disabled("a"))
        self.assertTrue(self.reg.has("a"))

    def test_disable_unknown_raises(self):
        with self.assertRaises(KeyError):
            self.reg.disable("nope")

    def test_enable_unknown_is_noop(self):
        self.assertTrue(self.reg.enable("nope"))
        self.assertFalse(self.reg.is_disabled("nope"))

    def test_get_disabled_raises_friendly_message(self):
        self.reg.disable("a")
        with self.assertRaises(KeyError) as cm:
            self.reg.get("a")
        self.assertIn("已被停用", str(cm.exception))

    def test_get_unknown_raises_friendly_message(self):
        with self.assertRaises(KeyError) as cm:
            self.reg.get("nope")
        self.assertIn("未找到插件", str(cm.exception))

    def test_raw_get_ignores_disabled(self):
        self.reg.disable("a")
        self.assertIsNotNone(self.reg.raw_get("a"))
        self.assertIsNone(self.reg.raw_get("nope"))

    def test_set_disabled_filters_unknown_names(self):
        self.assertEqual(self.reg.set_disabled(["a", "ghost"]), {"a"})
        self.assertEqual(self.reg.disabled(), ["a"])

    def test_set_disabled_none_clears(self):
        self.reg.set_disabled(["a", "b"])
        self.reg.set_disabled(None)
        self.assertEqual(self.reg.disabled(), [])

    def test_names_vs_all_names(self):
        self.reg.disable("b")
        self.assertEqual(self.reg.names(), ["a", "c"])
        self.assertEqual(self.reg.all_names(), ["a", "b", "c"])


# ------------------------------------------------------------------ 查询 / 反查


class TestRegistryQuery(unittest.TestCase):
    def setUp(self):
        self.reg = SkillRegistry()
        plugins = [
            make_skill("text_loader", slot="loader", produces=(TEXT,)),
            make_skill("basic_cleaner", slot="cleaner", consumes=(TEXT,), produces=(CLEAN_TEXT,)),
            make_skill("markdown_normalizer", slot="cleaner", consumes=(TEXT,), produces=(CLEAN_TEXT,)),
            make_skill("hash_embedder", slot="embedder", consumes=(CLEAN_TEXT,), produces=("embeddings",)),
        ]
        for cls in plugins:
            self.reg.register(cls)

    def test_by_slot_excludes_disabled(self):
        self.reg.disable("markdown_normalizer")
        self.assertEqual([c.name for c in self.reg.by_slot("cleaner")], ["basic_cleaner"])
        self.assertEqual(
            [c.name for c in self.reg.all_by_slot("cleaner")],
            ["basic_cleaner", "markdown_normalizer"],
        )

    def test_by_slot_unknown_is_empty(self):
        self.assertEqual(self.reg.by_slot("ghost"), [])

    def test_producers_and_consumers_of(self):
        self.assertEqual(
            [c.name for c in self.reg.producers_of(CLEAN_TEXT)],
            ["basic_cleaner", "markdown_normalizer"],
        )
        self.assertEqual(
            [c.name for c in self.reg.consumers_of(TEXT)],
            ["basic_cleaner", "markdown_normalizer"],
        )
        self.assertEqual(self.reg.producers_of("ghost"), [])

    def test_producers_exclude_disabled(self):
        self.reg.disable("basic_cleaner")
        self.assertEqual([c.name for c in self.reg.producers_of(CLEAN_TEXT)], ["markdown_normalizer"])

    def test_slots_grouping_and_sorted(self):
        self.assertEqual(
            self.reg.slots(),
            {
                "cleaner": ["basic_cleaner", "markdown_normalizer"],
                "embedder": ["hash_embedder"],
                "loader": ["text_loader"],
            },
        )

    def test_slots_excludes_disabled(self):
        self.reg.disable("hash_embedder")
        self.assertNotIn("embedder", self.reg.slots())

    def test_manifest_of_unknown_raises_friendly_message(self):
        with self.assertRaises(KeyError) as cm:
            self.reg.manifest_of("ghost")
        self.assertIn("未找到插件", str(cm.exception))

    def test_manifest_of_marks_enabled_and_origin(self):
        self.reg.disable("hash_embedder")
        m = self.reg.manifest_of("hash_embedder")
        self.assertFalse(m["enabled"])
        self.assertEqual(m["origin"], "builtin")

    def test_manifests_sorted_and_complete(self):
        names = [m["name"] for m in self.reg.manifests()]
        self.assertEqual(names, sorted(names))
        self.assertEqual(len(names), 4)

    def test_catalog_structure(self):
        self.reg.disable("hash_embedder")
        cat = self.reg.catalog()
        self.assertEqual(cat["disabled"], ["hash_embedder"])
        self.assertEqual(cat["slots"], self.reg.slots())
        self.assertIn("hash_embedder", cat["skills"])
        self.assertFalse(cat["skills"]["hash_embedder"]["enabled"])
        self.assertIn("params", cat["skills"]["basic_cleaner"])


# ------------------------------------------------------------------ 实例化


class TestRegistryCreate(unittest.TestCase):
    def setUp(self):
        self.reg = SkillRegistry()
        self.reg.register(
            make_skill(
                "sized",
                slot="splitter",
                param_schema={"size": {"type": "int", "default": 100}},
                configure=lambda self, options: setattr(self, "size", int(options.get("size", 100))),
            )
        )

    def test_create_uses_defaults_when_no_options(self):
        self.assertEqual(self.reg.create("sized").size, 100)

    def test_create_with_options(self):
        self.assertEqual(self.reg.create("sized", {"size": 42}).size, 42)

    def test_create_returns_new_instances(self):
        self.assertIsNot(self.reg.create("sized"), self.reg.create("sized"))

    def test_create_unknown_raises(self):
        with self.assertRaises(KeyError):
            self.reg.create("nope")

    def test_create_disabled_raises(self):
        self.reg.disable("sized")
        with self.assertRaises(KeyError):
            self.reg.create("sized")


# ------------------------------------------------------------------ 与上下文协作


class TestSkillRuntimeInteraction(unittest.TestCase):
    def test_skill_reads_and_writes_context(self):
        def run(self, ctx):
            ctx.put(CLEAN_TEXT, ctx.require(TEXT).upper(), producer=self.name)

        reg = SkillRegistry()
        reg.register(make_skill("upper_cleaner", slot="cleaner", consumes=(TEXT,), produces=(CLEAN_TEXT,), run=run))

        ctx = PipelineContext("a.txt", b"hello", "text/plain")
        ctx.put(TEXT, "hello", producer="loader")
        reg.create("upper_cleaner").run(ctx)
        self.assertEqual(ctx.get(CLEAN_TEXT), "HELLO")

    def test_skill_missing_upstream_artifact_raises(self):
        def run(self, ctx):
            ctx.require(TEXT)

        reg = SkillRegistry()
        reg.register(make_skill("needs_text", consumes=(TEXT,), run=run))

        ctx = PipelineContext("a.bin", b"")
        with self.assertRaises(MissingArtifactError):
            reg.create("needs_text").run(ctx)


# ------------------------------------------------------------------ 发现 / 热插拔


class TestDiscovery(unittest.TestCase):
    def setUp(self):
        global_registry.reset()

    def tearDown(self):
        global_registry.reset()
        global_registry.load_package("app.plugins")  # 恢复内置插件，避免污染其它用例

    def test_load_package_registers_builtin_plugins(self):
        loaded = global_registry.load_package("app.plugins")
        self.assertTrue(loaded)
        self.assertTrue(global_registry.raw_has("text_loader"))
        self.assertTrue(global_registry.raw_has("basic_cleaner"))

    def test_load_package_is_repeatable(self):
        """文档承诺 load_package 支持重复调用热重载，不应因重复注册而失败。"""
        global_registry.load_package("app.plugins")
        first = global_registry.all_names()
        global_registry.load_package("app.plugins")  # 关键：不 reset 直接再加载
        self.assertEqual(global_registry.all_names(), first)
        self.assertTrue(global_registry.raw_has("basic_cleaner"))

    def test_load_package_unknown_package_raises(self):
        with self.assertRaises(ModuleNotFoundError):
            global_registry.load_package("app.not_a_real_package")

    def test_load_directory_missing_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(global_registry.load_directory(Path(tmp) / "nope"), [])

    def test_load_directory_on_file_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp) / "a.py"
            file.write_text("", encoding="utf-8")
            self.assertEqual(global_registry.load_directory(file), [])

    def test_load_directory_registers_and_marks_origin(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_plugin(Path(tmp) / "probe_cleaner.py", "probe_cleaner")
            loaded = global_registry.load_directory(tmp)
            self.assertEqual(len(loaded), 1)
            self.assertTrue(global_registry.raw_has("probe_cleaner"))
            self.assertEqual(global_registry.manifest_of("probe_cleaner")["origin"], "ext")

    def test_load_directory_skips_underscore_modules(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_plugin(Path(tmp) / "_private.py", "private_cleaner")
            self.assertEqual(global_registry.load_directory(tmp), [])
            self.assertFalse(global_registry.raw_has("private_cleaner"))

    def test_load_directory_is_repeatable(self):
        """外部插件（热插入）重复加载不应因重复注册而失败。"""
        with tempfile.TemporaryDirectory() as tmp:
            _write_plugin(Path(tmp) / "probe_cleaner.py", "probe_cleaner")
            global_registry.load_directory(tmp)
            global_registry.load_directory(tmp)
            self.assertTrue(global_registry.raw_has("probe_cleaner"))


if __name__ == "__main__":
    unittest.main(verbosity=2)

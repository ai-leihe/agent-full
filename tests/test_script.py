"""脚本验证测试：执行器可控性 + ``/api/scripts`` 准入把关。

分两层：

* ``TestScriptRunner`` —— 直接打 ``app.core.script``，不经过 HTTP。
  重点是「可控性」：语法错误不起进程、超时能强杀、输出会截断、临时文件必回收、
  中文输出不因 Windows 默认 GBK 编码而炸掉。
* ``ScriptHttpTest``  —— 打 ``/api/scripts/*``，重点是「准可执行」：
  总开关、仅管理员、体积上限是否真的生效。

执行器会把临时脚本写到 ``data/script_workspace``；用例通过 ``patch.object``
把 ``WORKSPACE`` 指到临时目录，顺便验证「跑完不留文件」。

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import importlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core import script  # noqa: E402
from app.core.auth import (  # noqa: E402
    DEFAULT_ADMIN_PASSWORD,
    DEFAULT_ADMIN_USERNAME,
)
from app.core.script import detect_runtimes, run_script  # noqa: E402

CONFIG = ROOT / "config" / "pipeline.yaml"
SKILLS_DIR = ROOT / "ext_plugins"


def sh_available() -> bool:
    return any(item["key"] == "sh" and item["available"] for item in detect_runtimes())


def ps_available() -> bool:
    return any(item["key"] == "powershell" and item["available"] for item in detect_runtimes())


# =============================================================== 执行器


class TestScriptRunner(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name) / "script_workspace"
        patcher = mock.patch.object(script, "WORKSPACE", self.workspace)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.tmp.cleanup()

    # ---------------------------------------------------------- 语言探测

    def test_detect_runtimes_shape(self):
        items = detect_runtimes()
        self.assertEqual([i["key"] for i in items], ["python", "sh", "powershell"])
        python = next(i for i in items if i["key"] == "python")
        self.assertTrue(python["available"], "跑本服务的解释器必然可用")
        self.assertTrue(python["command"])
        self.assertEqual(python["extensions"], [".py"])
        for item in items:
            self.assertIsInstance(item["available"], bool)
            self.assertIn("hint", item)

    def test_path_hit_is_not_enough_it_must_actually_run(self):
        """``which`` 命中的同名文件不一定能执行，必须真探一次才算「可用」。

        本机就踩到过：``bash`` 解析到 ``C:\\Windows\\System32\\bash.exe``（WSL 入口），
        没装发行版时每次都报 ``execvpe(/bin/bash) failed``。
        """
        script._probe.cache_clear()
        self.addCleanup(script._probe.cache_clear)
        with mock.patch.object(script.shutil, "which", return_value=str(ROOT / "README.md")):
            items = detect_runtimes()
        for key in ("sh", "powershell"):
            item = next(i for i in items if i["key"] == key)
            self.assertFalse(item["available"], f"README.md 跑不出 0，不该被当成可用的 {key}")
            self.assertEqual(item["command"], "")

    # ---------------------------------------------------------- 输入校验

    def test_empty_code_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            run_script("python", "   \n  ")
        self.assertIn("为空", str(ctx.exception))

    def test_unknown_language_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            run_script("ruby", "puts 1")
        self.assertIn("不支持的脚本语言", str(ctx.exception))

    def test_oversize_code_rejected_before_running(self):
        with self.assertRaises(ValueError) as ctx:
            run_script("python", "x = 1\n" * 200, max_code=64)
        self.assertIn("超过上限", str(ctx.exception))
        self.assertFalse(self.workspace.exists(), "体积超限应在落盘之前就被拒绝")

    # ---------------------------------------------------------- Python

    def test_syntax_error_short_circuits_without_spawning(self):
        result = run_script("python", "def broken(:\n    pass\n")
        self.assertFalse(result["ok"])
        self.assertEqual(result["stage"], "syntax")
        self.assertIsNotNone(result["syntax"])
        self.assertEqual(result["syntax"]["line"], 1)
        self.assertEqual(result["exit_code"], None)
        self.assertEqual(result["duration_ms"], 0.0, "语法错误不该起进程")
        self.assertTrue(result["hints"])

    def test_stdout_stderr_and_success(self):
        result = run_script("python", "import sys\nprint('out-line')\nprint('err-line', file=sys.stderr)\n")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["stage"], "run")
        self.assertEqual(result["exit_code"], 0)
        self.assertIn("out-line", result["stdout"])
        self.assertIn("err-line", result["stderr"])
        self.assertGreaterEqual(result["duration_ms"], 0)

    def test_nonzero_exit_is_reported_not_raised(self):
        result = run_script("python", "import sys\nsys.exit(3)\n")
        self.assertFalse(result["ok"])
        self.assertEqual(result["exit_code"], 3)
        self.assertIn("退出码 3", result["error"])
        self.assertTrue(result["hints"], "stderr 为空时应给出排查提示")

    def test_stdin_is_forwarded(self):
        result = run_script("python", "import sys\nprint(sys.stdin.read().strip().upper())\n", stdin="ping")
        self.assertTrue(result["ok"], result)
        self.assertIn("PING", result["stdout"])

    def test_chinese_output_survives_windows_encoding(self):
        """Windows 下不设 PYTHONIOENCODING 时，print 中文会以 UnicodeEncodeError 收场。"""
        result = run_script("python", "print('中文回显正常')\n")
        self.assertTrue(result["ok"], result)
        self.assertIn("中文回显正常", result["stdout"])

    def test_script_can_import_platform_module(self):
        """PYTHONPATH 指向项目根，插件脚本里的 app.core.* 导入必须真跑通。"""
        result = run_script(
            "python",
            "from app.core.context import CLEAN_TEXT\nprint('bus-key:', CLEAN_TEXT)\n",
        )
        self.assertTrue(result["ok"], result["stderr"])
        self.assertIn("clean_text", result["stdout"])

    def test_plugin_declaration_yields_hint(self):
        code = (
            "from app.core.skill import Skill, skill\n\n\n"
            "@skill\n"
            "class Demo(Skill):\n"
            "    name = 'demo'\n"
            "    slot = 'cleaner'\n"
            "    consumes = ()\n"
            "    produces = ()\n"
            "\n"
            "    def run(self, ctx):\n"
            "        pass\n\n"
            "print('registered:', Demo.name)\n"
        )
        result = run_script("python", code)
        self.assertTrue(result["ok"], result["stderr"])
        self.assertTrue(any("ext_plugins" in h for h in result["hints"]), result["hints"])

    def test_cwd_is_project_root(self):
        result = run_script("python", "import os\nprint(os.path.basename(os.getcwd()))\n")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["stdout"].strip(), ROOT.name)

    # ---------------------------------------------------------- 可控性

    def test_timeout_kills_runaway_script(self):
        result = run_script("python", "import time\ntime.sleep(30)\n", timeout=1.0)
        self.assertFalse(result["ok"])
        self.assertTrue(result["timed_out"])
        self.assertIsNone(result["exit_code"], "被强杀的进程不给退出码，避免误读成正常退出")
        self.assertIn("超时", result["error"])
        self.assertLess(result["duration_ms"], 15_000)

    def test_timeout_still_returns_partial_output(self):
        code = "import sys, time\nprint('before-sleep', flush=True)\ntime.sleep(30)\n"
        result = run_script("python", code, timeout=1.0)
        self.assertTrue(result["timed_out"])
        self.assertIn("before-sleep", result["stdout"])

    def test_output_is_truncated(self):
        result = run_script("python", "print('x' * 5000)\n", max_output=200)
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["truncated"])
        self.assertIn("截断", result["stdout"])
        self.assertLess(len(result["stdout"]), 400)
        self.assertNotIn("x" * 300, result["stdout"], "超限部分必须真的被丢掉")

    def test_temp_script_is_removed_after_run(self):
        run_script("python", "print('cleaned')\n")
        leftovers = list(self.workspace.glob("verify-*"))
        self.assertEqual(leftovers, [], f"临时脚本未回收：{leftovers}")

    def test_temp_script_is_removed_after_timeout(self):
        run_script("python", "import time\ntime.sleep(30)\n", timeout=1.0)
        self.assertEqual(list(self.workspace.glob("verify-*")), [])

    # ---------------------------------------------------------- Shell

    def test_shell_runs_when_available(self):
        if not sh_available():
            self.skipTest("本机未安装 sh / bash")
        result = run_script("sh", 'echo "shell-ok"\n')
        self.assertTrue(result["ok"], result)
        self.assertIn("shell-ok", result["stdout"])

    # ---------------------------------------------------------- PowerShell

    def test_powershell_runs_when_available(self):
        if not ps_available():
            self.skipTest("本机未安装 PowerShell")
        result = run_script("powershell", 'Write-Output "ps-ok"\n')
        self.assertTrue(result["ok"], result)
        self.assertIn("ps-ok", result["stdout"])

    def test_powershell_chinese_survives_encoding(self):
        """Windows 下 PowerShell 脚本若不带 BOM，中文会被读成乱码并导致解析失败。"""
        if not ps_available():
            self.skipTest("本机未安装 PowerShell")
        result = run_script(
            "powershell",
            'Write-Output ("PowerShell 版本：" + $PSVersionTable.PSVersion.ToString())\n',
        )
        self.assertTrue(result["ok"], result)
        self.assertIn("PowerShell 版本：", result["stdout"])

    def test_unavailable_interpreter_reports_stage(self):
        with mock.patch.object(script.shutil, "which", return_value=None):
            result = run_script("sh", "echo hi\n")
        self.assertFalse(result["ok"])
        self.assertEqual(result["stage"], "unavailable")
        self.assertIn("未找到", result["error"])
        self.assertFalse(self.workspace.exists(), "解释器都找不到就不该落盘")


# =============================================================== HTTP 接口


class ScriptHttpTest(unittest.TestCase):
    """走完整 HTTP 链路，运行时与设置都落在临时目录。"""

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
        self.runtime.bootstrap(
            config_path=CONFIG,
            ext_dir=SKILLS_DIR,
            profiles_dir=root / "profiles",
            settings_path=root / "settings.json",
            runs_path=root / "runs.json",
            users_path=root / "users.json",
        )
        self.client.cookies.clear()
        r = self.client.post(
            "/api/auth/login",
            json={"username": DEFAULT_ADMIN_USERNAME, "password": DEFAULT_ADMIN_PASSWORD},
            headers=self.ANON,
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.token = r.json()["token"]
        self.client.headers.update({"Authorization": f"Bearer {self.token}"})
        self.client.cookies.clear()

    def tearDown(self):
        self.client.headers.pop("Authorization", None)
        self.client.cookies.clear()
        self.tmp.cleanup()

    def _settings(self, payload):
        r = self.client.put("/api/settings", json=payload)
        self.assertEqual(r.status_code, 200, r.text)

    # ---------------------------------------------------------- 探测

    def test_runtimes_endpoint_exposes_limits(self):
        r = self.client.get("/api/scripts/runtimes")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["enabled"])
        self.assertFalse(body["admin_only"])
        self.assertEqual(body["limits"]["timeout_sec"], 15)
        self.assertEqual(
            [x["key"] for x in body["runtimes"]], ["python", "sh", "powershell"]
        )

    # ---------------------------------------------------------- 验证

    def test_validate_python_ok(self):
        r = self.client.post(
            "/api/scripts/validate",
            json={"language": "python", "code": "print('hi from http')\n"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["ok"], body)
        self.assertIn("hi from http", body["stdout"])
        self.assertEqual(body["language"], "python")

    def test_validate_reports_syntax_error_as_result(self):
        r = self.client.post(
            "/api/scripts/validate",
            json={"language": "python", "code": "def broken(:\n"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["stage"], "syntax")

    def test_validate_empty_code_is_422(self):
        r = self.client.post("/api/scripts/validate", json={"language": "python", "code": "  "})
        self.assertEqual(r.status_code, 422, r.text)
        self.assertIn("为空", r.json()["detail"])

    def test_validate_unknown_language_is_422(self):
        r = self.client.post("/api/scripts/validate", json={"language": "ruby", "code": "puts 1"})
        self.assertEqual(r.status_code, 422, r.text)
        self.assertIn("不支持的脚本语言", r.json()["detail"])

    def test_validate_oversize_is_422(self):
        self._settings({"script": {"max_script_kb": 1}})
        r = self.client.post(
            "/api/scripts/validate",
            json={"language": "python", "code": "x = 1\n" * 400},
        )
        self.assertEqual(r.status_code, 422, r.text)
        self.assertIn("超过上限", r.json()["detail"])

    def test_timeout_setting_is_honored(self):
        self._settings({"script": {"timeout_sec": 1}})
        r = self.client.post(
            "/api/scripts/validate",
            json={"language": "python", "code": "import time\ntime.sleep(30)\n"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["timed_out"])
        self.assertLess(body["duration_ms"], 15_000)

    # ---------------------------------------------------------- 准入

    def test_disabled_setting_blocks_everything(self):
        self._settings({"script": {"enabled": False}})
        for method, path, payload in (
            ("get", "/api/scripts/runtimes", None),
            ("post", "/api/scripts/validate", {"language": "python", "code": "print(1)"}),
        ):
            call = getattr(self.client, method)
            r = call(path, json=payload) if payload else call(path)
            self.assertEqual(r.status_code, 403, r.text)
            self.assertIn("脚本验证", r.json()["detail"])

    def test_admin_only_blocks_anonymous(self):
        self._settings({"script": {"admin_only": True}})
        r = self.client.post(
            "/api/scripts/validate",
            json={"language": "python", "code": "print(1)"},
            headers=self.ANON,
        )
        self.assertEqual(r.status_code, 401, r.text)

    def test_admin_only_still_allows_admin(self):
        self._settings({"script": {"admin_only": True}})
        r = self.client.post(
            "/api/scripts/validate",
            json={"language": "python", "code": "print('admin ok')"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

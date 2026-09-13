"""script 槽位插件：把 Python / Shell / PowerShell 脚本作为编排流水线的一个阶段。

与「脚本验证」工具不同，这里的脚本**接收上游产物作为 stdin**（可选），
并把 stdout 写回 ``text`` / ``clean_text``，从而嵌入到 loader → cleaner → splitter
等标准阶段之间做自定义处理。
"""

from __future__ import annotations

from ..core.context import CLEAN_TEXT, TEXT, PipelineContext
from ..core.script import detect_runtimes, run_script
from ..core.skill import Skill, skill


class ScriptSkillBase(Skill):
    """脚本执行技能的公共骨架。子类只需声明语言与元信息。"""

    slot = "script"
    #: 脚本语言 key，对应 :mod:`app.core.script` 里的 language key
    language: str = ""
    #: 产物：同时写入 text 和 clean_text，方便脚本阶段放在任意位置
    consumes = ()
    produces = (TEXT, CLEAN_TEXT)

    param_schema = {
        "code": {
            "type": "str",
            "default": "",
            "label": "脚本内容",
            "help": "会在独立子进程中执行，timeout 内未结束会被强制终止",
            "multiline": True,
        },
        "stdin_source": {
            "type": "str",
            "default": "text",
            "label": "标准输入来源",
            "choices": ["none", "text", "clean_text", "custom"],
            "help": "text=上游原始文本；clean_text=清洗后文本；custom=下方自定义内容；none=空",
        },
        "stdin": {
            "type": "str",
            "default": "",
            "label": "自定义 stdin",
            "help": "仅当「标准输入来源」选择 custom 时生效",
            "multiline": True,
        },
        "timeout": {
            "type": "int",
            "default": 15,
            "label": "超时（秒）",
            "min": 1,
            "max": 120,
        },
        "capture_stderr": {
            "type": "bool",
            "default": False,
            "label": "将 stderr 合并进产物",
        },
        "strip_output": {
            "type": "bool",
            "default": True,
            "label": "去除输出首尾空白",
        },
    }

    def configure(self, options: dict) -> None:
        self.code = str(options.get("code") or "").strip()
        if not self.code:
            raise ValueError("脚本内容不能为空")

        self.stdin_source = str(options.get("stdin_source") or "text").strip().lower()
        if self.stdin_source not in ("none", "text", "clean_text", "custom"):
            raise ValueError("stdin_source 必须是 none / text / clean_text / custom 之一")
        self.custom_stdin = str(options.get("stdin") or "")

        self.timeout = int(options.get("timeout", 15))
        if not 1 <= self.timeout <= 120:
            raise ValueError("timeout 必须在 1~120 秒之间")

        self.capture_stderr = bool(options.get("capture_stderr", False))
        self.strip_output = bool(options.get("strip_output", True))

    def _build_stdin(self, ctx: PipelineContext) -> str:
        if self.stdin_source == "none":
            return ""
        if self.stdin_source == "custom":
            return self.custom_stdin
        kind = TEXT if self.stdin_source == "text" else CLEAN_TEXT
        if not ctx.has(kind):
            raise ValueError(
                f"标准输入来源设为 '{self.stdin_source}'，但上游没有 '{kind}' 产物"
            )
        value = ctx.get(kind)
        # chunks/embeddings 等复杂对象不适合直接当 stdin
        if not isinstance(value, str):
            raise ValueError(f"'{kind}' 产物不是文本，无法作为脚本 stdin")
        return value

    def _write_output(self, ctx: PipelineContext, output: str) -> None:
        # Windows 子进程 stdout 可能是 CRLF，统一成 \n，避免下游切片/清洗出现 \r
        output = output.replace("\r\n", "\n").replace("\r", "\n")
        if self.strip_output:
            output = output.strip()
        ctx.put(TEXT, output, producer=self.name)
        ctx.put(CLEAN_TEXT, output, producer=self.name)

    def run(self, ctx: PipelineContext) -> None:
        stdin = self._build_stdin(ctx)
        result = run_script(self.language, self.code, stdin=stdin, timeout=float(self.timeout))

        if not result["ok"]:
            parts = [result.get("error") or f"脚本以退出码 {result['exit_code']} 结束"]
            if result.get("stderr"):
                parts.append(result["stderr"])
            raise RuntimeError("\n".join(parts))

        output = result["stdout"]
        if self.capture_stderr and result.get("stderr"):
            output = output + "\n" + result["stderr"] if output else result["stderr"]
        self._write_output(ctx, output)


@skill
class PythonScriptSkill(ScriptSkillBase):
    """Python 脚本阶段：用运行本服务的解释器执行，可直接 import app.core.*。"""

    name = "python_script"
    language = "python"
    description = "执行 Python 脚本，以上游文本为 stdin，stdout 写回 text/clean_text。"


@skill
class ShellScriptSkill(ScriptSkillBase):
    """Shell / Bash 脚本阶段。"""

    name = "shell_script"
    language = "sh"
    optional = True
    description = "执行 Shell / Bash 脚本，以上游文本为 stdin，stdout 写回 text/clean_text。"

    def configure(self, options: dict) -> None:
        if not _runtime_available("sh"):
            raise RuntimeError("本机未找到可用的 sh / bash 解释器，无法使用 shell_script")
        super().configure(options)


@skill
class PowerShellScriptSkill(ScriptSkillBase):
    """PowerShell 脚本阶段。"""

    name = "powershell_script"
    language = "powershell"
    optional = True
    description = "执行 PowerShell 脚本，以上游文本为 stdin，stdout 写回 text/clean_text。"

    def configure(self, options: dict) -> None:
        if not _runtime_available("powershell"):
            raise RuntimeError("本机未找到可用的 PowerShell 解释器，无法使用 powershell_script")
        super().configure(options)


def _runtime_available(key: str) -> bool:
    return any(item["key"] == key and item["available"] for item in detect_runtimes())

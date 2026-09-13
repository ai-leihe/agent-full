"""在线脚本验证：在受控条件下执行一小段 Python / Shell 代码，并原样回显结果。

边界先说清楚
------------
**这不是安全沙箱。** 它执行的就是调用者自己写的代码，能做的事情等同于把脚本
存成文件再手动运行——``shutil.rmtree('/')`` 一样删得掉东西。所以本模块不做
「能不能跑」的判断，只负责两件事：

1. **可控性**：超时强杀（连子孙进程）、脚本与输出体积上限、工作目录固定、
   临时文件必然回收、编码统一为 UTF-8。
2. **可诊断**：语法错误在起进程之前就地拦下并给出行列号；退出码 / stdout /
   stderr / 耗时全部原样回传，不做任何「帮你美化」的处理。

至于**准可执行**，交给平台层：``script.enabled`` 总开关、``script.admin_only``
仅管理员，再加上全局登录校验中间件。默认值按「本机开发工具」给，
对外提供服务前必须收紧。

为什么 Python 用 ``sys.executable``
-----------------------------------
跑脚本的解释器就是跑着本服务的那个，并且把项目根塞进子进程的 ``PYTHONPATH``。
这样 ``from app.core.skill import Skill, skill`` 这类平台导入能真正跑通，
「在线验证插件脚本」才有意义，而不是只能验证一段孤立的正则表达式。
"""

from __future__ import annotations

import functools
import locale
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .env import PROJECT_ROOT

#: 临时脚本落盘目录（``data/`` 已在 .gitignore 中，不会污染仓库）
WORKSPACE = PROJECT_ROOT / "data" / "script_workspace"

#: 兜底默认值；平台设置里的 ``script.*`` 优先，这里只保证模块可独立使用
DEFAULT_TIMEOUT = 15.0
DEFAULT_MAX_OUTPUT = 64 * 1024
DEFAULT_MAX_CODE = 64 * 1024

#: 输出被截断时追加的提示
TRUNCATED_NOTE = "\n…（输出已达上限，后续内容被截断）"


@dataclass(frozen=True)
class Language:
    """一种可验证的脚本语言及其解释器解析规则。"""

    key: str
    label: str
    #: 用于自动匹配本地文件后缀，也是临时文件的后缀
    extensions: tuple[str, ...]
    #: 在 PATH 上依次尝试的可执行文件名；python 用 ``sys.executable``，故留空
    candidates: tuple[str, ...] = ()
    hint: str = ""


LANGUAGES: tuple[Language, ...] = (
    Language(
        key="python",
        label="Python",
        extensions=(".py",),
        hint="用运行本服务的解释器执行，可直接 import app.core.* 验证插件脚本",
    ),
    Language(
        key="sh",
        label="Shell",
        extensions=(".sh", ".bash"),
        candidates=("bash", "sh"),
        hint="POSIX shell；Windows 下需已安装 Git Bash 或 WSL",
    ),
    Language(
        key="powershell",
        label="PowerShell",
        extensions=(".ps1",),
        candidates=("pwsh", "powershell"),
        hint="Windows PowerShell / PowerShell 7",
    ),
)

_BY_KEY: dict[str, Language] = {item.key: item for item in LANGUAGES}

#: 命中即提示「这是插件脚本」（子进程里注册不进本服务的注册表，故只做提示）
_PLUGIN_HINT_RE = re.compile(r"@skill\b|class\s+\w+\s*\(\s*Skill\s*\)")


# ---------------------------------------------------------------- 解释器


def language_of(key: str) -> Language | None:
    """按 key 取语言描述，未知返回 None。"""
    return _BY_KEY.get(str(key or "").strip().lower())


@functools.lru_cache(maxsize=16)
def _probe(candidate: str, kind: str) -> bool:
    """真跑一次 ``exit 0``：``which`` 只证明「PATH 上有这个文件」。

    典型反例就是 Windows 自带的 ``C:\\Windows\\System32\\bash.exe``——它只是 WSL 的
    入口，没装发行版时每次调用都返回 ``execvpe(/bin/bash) failed``。只探路径的话，
    界面会把 Shell 标成「可用」，用户却每次都失败，看起来像功能坏了。
    结果做缓存：探测要起进程，不能在每次 ``GET /runtimes`` 时都重来一遍。
    """
    args = (
        [candidate, "-c", "exit 0"]
        if kind == "sh"
        else [candidate, "-NoProfile", "-NonInteractive", "-Command", "exit 0"]
    )
    try:
        done = subprocess.run(
            args,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def _interpreter_spec(language: Language) -> tuple[list[str], str] | None:
    """返回 ``(命令前缀, 展示用路径)``；本机不可用则返回 None。

    非 Python 语言要求「PATH 上找得到」**且**「真的能执行」；
    Python 用正在跑本服务的那个解释器，它此刻正在运行，无需再探。
    """
    if language.key == "python":
        return [sys.executable], sys.executable
    for name in language.candidates:
        found = shutil.which(name)
        if found and _probe(found, language.key):
            return [found], found
    return None


def detect_runtimes() -> list[dict[str, Any]]:
    """探测本机可用的脚本解释器，供前端把不可用的语言标灰。"""
    items: list[dict[str, Any]] = []
    for language in LANGUAGES:
        found = _interpreter_spec(language)
        items.append(
            {
                "key": language.key,
                "label": language.label,
                "extensions": list(language.extensions),
                "available": found is not None,
                "command": found[1] if found else "",
                "hint": language.hint,
            }
        )
    return items


# ---------------------------------------------------------------- 子进程细节


def _argv(language: Language, prefix: list[str], script: Path) -> list[str]:
    if language.key == "python":
        # -u：stdout 不缓冲，脚本卡死被强杀时也能拿到已经写出的部分
        return [*prefix, "-u", str(script)]
    if language.key == "sh":
        return [*prefix, str(script)]
    return [
        *prefix,
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
    ]


def _child_env() -> dict[str, str]:
    """子进程环境：统一编码 + 让脚本能 import 平台的 app.core.*。"""
    env = dict(os.environ)
    # Windows 下 Python 默认按 GBK 写 stdout，脚本里 print 中文会直接抛 UnicodeEncodeError
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"  # 别在 data/ 里留下一堆 __pycache__
    root = str(PROJECT_ROOT)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{root}{os.pathsep}{existing}" if existing else root
    return env


def _decode(raw: bytes) -> str:
    """按 UTF-8 → 系统默认编码依次尝试，都失败就替换非法字节。

    回显本身绝不应该因为解码失败而炸掉——那会把「脚本有问题」误导成「平台有问题」。
    """
    for encoding in ("utf-8", locale.getpreferredencoding(False) or "utf-8"):
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit] + TRUNCATED_NOTE, True


def _terminate(proc: subprocess.Popen[bytes]) -> None:
    """强杀进程。Shell 脚本常再拉起子进程，只杀父进程会留下孤儿继续跑。"""
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                check=False,
                timeout=5,
            )
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:  # noqa: BLE001 - 已经是最后手段，失败也要往下走
        pass
    finally:
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


def _syntax_error(code: str) -> dict[str, Any] | None:
    """Python 先做一次纯静态编译：语法错误不必起进程，还能给出精确行列。"""
    try:
        compile(code, "<script>", "exec")
    except SyntaxError as exc:
        return {
            "message": f"SyntaxError: {exc.msg}",
            "line": exc.lineno or 0,
            "column": exc.offset or 0,
        }
    except ValueError as exc:  # 例如源码里含 \x00
        return {"message": f"ValueError: {exc}", "line": 0, "column": 0}
    return None


# ---------------------------------------------------------------- 执行


def run_script(
    language_key: str,
    code: str,
    *,
    stdin: str = "",
    timeout: float = DEFAULT_TIMEOUT,
    max_output: int = DEFAULT_MAX_OUTPUT,
    max_code: int = DEFAULT_MAX_CODE,
) -> dict[str, Any]:
    """执行一段脚本并返回完整回显。

    只抛 :class:`ValueError`（语言不支持 / 内容为空 / 超过体积上限）——
    那属于调用方输入问题，应当变成 4xx；脚本自身的失败一律落在返回值的
    ``ok=False`` 里，因为「验证不通过」本来就是一个正常结果。
    """
    language = language_of(language_key)
    if language is None:
        raise ValueError(
            f"不支持的脚本语言 '{language_key}'，可选：{sorted(_BY_KEY)}"
        )
    if not code.strip():
        raise ValueError("脚本内容为空")

    code_bytes = len(code.encode("utf-8"))
    if code_bytes > max_code:
        raise ValueError(
            f"脚本体积 {code_bytes} 字节，超过上限 {max_code} 字节"
            "（可在「系统设置 → 脚本验证」中调整）"
        )

    result: dict[str, Any] = {
        "ok": False,
        "language": language.key,
        "language_label": language.label,
        "command": "",
        "stage": "run",
        "exit_code": None,
        "stdout": "",
        "stderr": "",
        "duration_ms": 0.0,
        "timed_out": False,
        "truncated": False,
        "error": None,
        "syntax": None,
        "hints": [],
    }

    syntax = _syntax_error(code) if language.key == "python" else None
    if syntax is not None:
        result.update(
            stage="syntax",
            syntax=syntax,
            error=syntax["message"],
            hints=["语法错误在启动进程之前就被拦下，无需等超时"],
        )
        return result

    resolved = _interpreter_spec(language)
    if resolved is None:
        result.update(
            stage="unavailable",
            error=f"本机未找到 {language.label} 解释器（尝试过：{', '.join(language.candidates)}）",
            hints=[language.hint] if language.hint else [],
        )
        return result

    prefix, command = resolved
    result["command"] = command

    WORKSPACE.mkdir(parents=True, exist_ok=True)
    handle, raw_path = tempfile.mkstemp(
        prefix="verify-", suffix=language.extensions[0], dir=str(WORKSPACE)
    )
    script = Path(raw_path)
    try:
        with os.fdopen(handle, "wb") as stream:
            # Windows PowerShell 默认按系统 ANSI 编码读脚本，没 BOM 时中文会变成乱码并
            # 导致引号/括号解析失败；带 BOM 的 UTF-8 能让它正确识别为 UTF-8。
            encoding = "utf-8-sig" if language.key == "powershell" else "utf-8"
            stream.write(code.encode(encoding))

        try:
            proc = subprocess.Popen(
                _argv(language, prefix, script),
                # 相对路径都相对项目根，和手动执行脚本时的直觉一致
                cwd=str(PROJECT_ROOT),
                env=_child_env(),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                # POSIX 下自成进程组，超时才能整组强杀
                start_new_session=(os.name != "nt"),
            )
        except OSError as exc:
            result.update(
                stage="unavailable",
                error=f"无法启动 {language.label} 解释器：{exc}",
            )
            return result

        started = time.perf_counter()
        timed_out = False
        try:
            out_raw, err_raw = proc.communicate(
                input=stdin.encode("utf-8"), timeout=timeout
            )
        except subprocess.TimeoutExpired:
            timed_out = True
            _terminate(proc)
            # 进程已被杀掉，这次 communicate 会立刻返回，正好收下超时前已写出的输出
            out_raw, err_raw = proc.communicate()
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
    finally:
        script.unlink(missing_ok=True)

    stdout, out_cut = _truncate(_decode(out_raw), max_output)
    stderr, err_cut = _truncate(_decode(err_raw), max_output)
    exit_code = None if timed_out else proc.returncode
    ok = (not timed_out) and exit_code == 0

    result.update(
        ok=ok,
        stage="run",
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        duration_ms=duration_ms,
        timed_out=timed_out,
        truncated=out_cut or err_cut,
    )

    if timed_out:
        result["error"] = f"执行超时（{timeout:g} 秒），已被强制终止"
        result["hints"].append(
            "调大「系统设置 → 脚本验证 → 单次执行超时」，或把长任务拆成更小的片段分批验证"
        )
    elif not ok:
        result["error"] = f"脚本以退出码 {exit_code} 结束"
        if not stderr.strip():
            result["hints"].append(
                "stderr 为空：非 0 退出码不一定意味着报错，请检查脚本里是否有显式的 exit / 断言"
            )

    if ok and language.key == "python" and _PLUGIN_HINT_RE.search(code):
        result["hints"].append(
            "检测到 Skill 插件声明：把这段代码存成 ext_plugins/xxx.py，点「重新扫描插件目录」，"
            "即可在技能库里启停、并对它做沙盒试跑"
        )

    return result


__all__ = [
    "LANGUAGES",
    "Language",
    "WORKSPACE",
    "detect_runtimes",
    "language_of",
    "run_script",
]

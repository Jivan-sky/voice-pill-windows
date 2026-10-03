# -*- coding: utf-8 -*-
"""把本仓库里的 Codex 插件接到本机：建目录联接、铺 venv、写机器相关路径、装进 Codex。

为什么要这么一个脚本
--------------------
插件里有几处**只能是绝对路径**：Codex 会把插件整包拷进自己的 cache，相对路径在
那边解析不了（实测 `.mcp.json` 里写相对 command 直接报 os error 3）。涉及的是

    .mcp.json        command（venv 里的 python.exe）、args（mcp_server.py 的位置）
    hooks/hooks.json command（hook.cmd 的位置）

而这些路径**又必须不含 `&`**：实测路径里带 `&`（比如本仓库所在的
`D:\\Own_tools&skills\\...`）时，未加引号会被 cmd 当命令分隔符拆断、钩子报 Failed；
加了引号 Codex 只回一句 Completed，脚本根本没被执行——两种都是坏的。

所以插件在 Codex 眼里固定住在 `%USERPROFILE%\\plugins\\voice-pill`，这个目录是一个
**目录联接（junction）**，指向本仓库的 `plugins/voice-pill`：

    Codex ──> ~/plugins/voice-pill ──(junction)──> <仓库>/plugins/voice-pill

仓库是唯一源码；Codex 看到的路径里没有 `&`；个人市场清单沿用官方默认写法
（`./plugins/<名字>`），一个字都不用改。

用法
----
    python plugins/install.py            缺什么补什么，可以反复跑
    python plugins/install.py --check    只看现状，什么都不动

装完要**新开一个 Codex 线程**才会拾取新的技能与工具。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

PLUGIN_NAME = "voice-pill"
MARKETPLACE_NAME = "personal"
VENV_DIRNAME = "plugin-venv"

REPO_ROOT = Path(__file__).resolve().parents[1]

# 控制台默认是 GBK，装不上 ✅/❌ 这类字符。和 tools/ 下的自测脚本用同一招。
sys.path.insert(0, str(REPO_ROOT / "src"))
import console                      # noqa: E402
console.make_output_safe()

PLUGIN_DIR = REPO_ROOT / "plugins" / PLUGIN_NAME
LINK_PATH = Path.home() / "plugins" / PLUGIN_NAME
APP_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "VoicePill"
VENV_DIR = APP_DIR / VENV_DIRNAME
MARKETPLACE_PATH = Path.home() / ".agents" / "plugins" / "marketplace.json"

MARKETPLACE_ENTRY = {
    "name": PLUGIN_NAME,
    "source": {"source": "local", "path": "./plugins/%s" % PLUGIN_NAME},
    "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
    "category": "Productivity",
}


def say(ok: bool, label: str, detail: str = "") -> None:
    print("  %s %s%s" % ("OK  " if ok else "--  ", label,
                         ("  (%s)" % detail) if detail else ""))


def is_junction(path: Path) -> bool:
    """目录联接在 Python 里长得像目录；靠重解析点属性认出来。"""
    try:
        return bool(os.lstat(path).st_reparse_tag)
    except (OSError, AttributeError):
        return False


def link_target(path: Path):
    """读联接到哪儿去了。读不到返回 None。"""
    try:
        return Path(os.path.realpath(path)).resolve()
    except OSError:
        return None


def ensure_junction(check: bool) -> bool:
    want = PLUGIN_DIR.resolve()
    if LINK_PATH.exists():
        if is_junction(LINK_PATH) and link_target(LINK_PATH) == want:
            say(True, "目录联接已就位", str(LINK_PATH))
            return True
        say(False, "路径被占了，且不是指向本仓库的联接", str(LINK_PATH))
        print("      这里现在是个真目录。它可能是一份旧的插件副本。")
        print("      确认里面没有你在意的东西之后，把它移走再跑一次：")
        print("        move \"%s\" \"%s.bak\"" % (LINK_PATH, LINK_PATH))
        return False
    if check:
        say(False, "还没有目录联接", str(LINK_PATH))
        return False
    LINK_PATH.parent.mkdir(parents=True, exist_ok=True)
    import _winapi
    _winapi.CreateJunction(str(want), str(LINK_PATH))
    say(True, "建好目录联接", "%s -> %s" % (LINK_PATH, want))
    return True


def venv_python() -> Path:
    return VENV_DIR / "Scripts" / "python.exe"


def ensure_venv(check: bool) -> bool:
    py = venv_python()
    if py.is_file():
        if _has_mcp(py):
            say(True, "插件 venv 可用", str(py))
            return True
        if check:
            say(False, "venv 里没有 mcp 包", str(py))
            return False
        _pip_install(py)
        return _has_mcp(py)
    if check:
        say(False, "还没有插件 venv", str(VENV_DIR))
        return False
    VENV_DIR.parent.mkdir(parents=True, exist_ok=True)
    if shutil.which("uv"):
        _run(["uv", "venv", "--python", "3.11", str(VENV_DIR)])
    else:
        _run([sys.executable, "-m", "venv", str(VENV_DIR)])
    say(True, "建好 venv", str(VENV_DIR))
    _pip_install(py)
    return _has_mcp(py)


def _run(argv: list) -> int:
    print("      $ %s" % " ".join(argv))
    try:
        return subprocess.call(argv)
    except OSError as exc:
        print("      起不来：%s" % exc)
        return 1


def _has_mcp(py: Path) -> bool:
    try:
        return subprocess.call([str(py), "-c", "import mcp"],
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL) == 0
    except OSError:
        return False


def _pip_install(py: Path) -> None:
    """装 mcp。国内网络下拉不动 PyPI 时，自己给个镜像：
    uv pip install --python <venv> --index-url https://pypi.tuna.tsinghua.edu.cn/simple mcp
    """
    if shutil.which("uv"):
        _run(["uv", "pip", "install", "--python", str(py), "mcp"])
    else:
        _run([str(py), "-m", "pip", "install", "mcp"])


def render_files(check: bool) -> bool:
    """把两处机器相关的路径写进插件。内容对得上就一个字都不改（免得脏工作区）。"""
    mcp_json = {
        "mcpServers": {
            "voice_pill": {
                "command": _slash(venv_python()),
                "args": [_slash(LINK_PATH / "mcp_server.py")],
                "cwd": _slash(LINK_PATH),
                "env_vars": ["PATH", "USERPROFILE", "LOCALAPPDATA", "APPDATA",
                             "TEMP", "TMP", "SYSTEMROOT"],
                "startup_timeout_sec": 20,
                "tool_timeout_sec": 180,
            }
        }
    }
    # 三个钩子：
    #   SessionStart     —— Codex 一开就把驻留实例拉起来，并把它的命绑在
    #                       Codex 上（见 hook_session_start.py / src/anchor.py）
    #   UserPromptSubmit —— 你说的字**进**对话（说话 → 上下文）
    #   Stop             —— 助手的回复**出**声（回复 → 本机 TTS）
    # 都必须写绝对路径：Codex 从别的工作目录拉起钩子，相对路径解析不到。
    # SessionStart 超时给得宽：冷启动时要等引擎把控制面架起来（最多 3 秒），
    # 已经在跑时毫秒级返回。
    hooks_json = {
        "hooks": {
            "SessionStart": [
                {"hooks": [{"type": "command",
                            "command": str(LINK_PATH / "hook-session-start.cmd"),
                            "timeout": 20}]}
            ],
            "UserPromptSubmit": [
                {"hooks": [{"type": "command",
                            "command": str(LINK_PATH / "hook.cmd"),
                            "timeout": 10}]}
            ],
            "Stop": [
                {"hooks": [{"type": "command",
                            "command": str(LINK_PATH / "hook-stop.cmd"),
                            "timeout": 5}]}
            ],
        }
    }
    ok = True
    for path, payload in ((PLUGIN_DIR / ".mcp.json", mcp_json),
                          (PLUGIN_DIR / "hooks" / "hooks.json", hooks_json)):
        text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
        if path.is_file() and path.read_text(encoding="utf-8") == text:
            say(True, "路径已是最新", path.name)
            continue
        if check:
            say(False, "需要重写", str(path))
            ok = False
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        say(True, "写好", str(path))
    return ok


def ensure_marketplace(check: bool) -> bool:
    """个人市场清单里得有一条指到这个插件。已有的机器上早就有了。"""
    if MARKETPLACE_PATH.is_file():
        try:
            data = json.loads(MARKETPLACE_PATH.read_text(encoding="utf-8"))
        except ValueError:
            data = None
        entries = (data or {}).get("plugins") or []
        if any(e.get("name") == PLUGIN_NAME for e in entries):
            say(True, "市场清单里有这条", str(MARKETPLACE_PATH))
            return True
        if check or data is None:
            say(False, "市场清单里没有 %s 这条" % PLUGIN_NAME, str(MARKETPLACE_PATH))
            if check:
                return False
        data.setdefault("plugins", []).append(MARKETPLACE_ENTRY)
    else:
        if check:
            say(False, "还没有个人市场清单", str(MARKETPLACE_PATH))
            return False
        data = {"name": MARKETPLACE_NAME,
                "interface": {"displayName": "Personal"},
                "plugins": [MARKETPLACE_ENTRY]}
    MARKETPLACE_PATH.parent.mkdir(parents=True, exist_ok=True)
    MARKETPLACE_PATH.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    say(True, "市场清单已更新", str(MARKETPLACE_PATH))
    return True


def install_into_codex(check: bool) -> bool:
    if check:
        return True
    if not shutil.which("codex"):
        say(False, "PATH 里没有 codex，装不了", "自己跑：codex plugin add %s@%s"
            % (PLUGIN_NAME, MARKETPLACE_NAME))
        return False
    cachebuster = (Path.home() / ".codex" / "skills" / ".system" / "plugin-creator"
                   / "scripts" / "update_plugin_cachebuster.py")
    if cachebuster.is_file():
        _run([sys.executable, str(cachebuster), str(PLUGIN_DIR)])
    rc = _run(["codex", "plugin", "add", "%s@%s" % (PLUGIN_NAME, MARKETPLACE_NAME)])
    say(rc == 0, "codex plugin add", "退出码 %d" % rc)
    return rc == 0


def _slash(path: Path) -> str:
    return str(path).replace("\\", "/")


def main() -> int:
    ap = argparse.ArgumentParser(description="把本仓库的 Codex 插件接到本机")
    ap.add_argument("--check", action="store_true", help="只看现状，什么都不动")
    args = ap.parse_args()

    print("=" * 68)
    print("Voice Pill 插件安装%s" % ("（检查）" if args.check else ""))
    print("=" * 68)
    print("  仓库插件：%s" % PLUGIN_DIR)
    print("  Codex 侧：%s" % LINK_PATH)

    ok = ensure_junction(args.check)
    ok = ensure_venv(args.check) and ok
    ok = render_files(args.check) and ok
    ok = ensure_marketplace(args.check) and ok
    ok = install_into_codex(args.check) and ok

    print("-" * 68)
    if ok:
        print("✅ 就绪。新开一个 Codex 线程即可用。")
        return 0
    print("❌ 有没就位的地方，见上面标 -- 的行。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
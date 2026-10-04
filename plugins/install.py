# -*- coding: utf-8 -*-
"""把本仓库里的 Codex 插件接到本机：建目录联接、放置 exe、渲染配置、装进 Codex。

为什么要这么一个脚本
--------------------
插件里有几处**只能是绝对路径**：Codex 会把插件整包拷进自己的 cache，相对路径在
那边解析不了（实测 `.mcp.json` 里写相对 command 直接报 os error 3）。涉及的是

    .mcp.json        command / cwd（插件目录里的 voicepill.exe）
    hooks/hooks.json command（三个 hook-*.cmd 的位置）

而这些路径**又必须不含 `&`**：实测仓库路径里带 `&` 时，未加引号会被 cmd 当命令
分隔符拆断、钩子报 Failed；加了引号 Codex 只回一句 Completed，脚本根本没被执行
——两种都是坏的。所以本仓库路径里的 `&` 一个都不许出现在钩子命令里。

于是插件在 Codex 眼里固定住在 `%USERPROFILE%\\plugins\\voice-pill`，这个目录是一个
**目录联接（junction）**，指向本仓库的 `plugins/voice-pill`：

    Codex ──> ~/plugins/voice-pill ──(junction)──> <仓库>/plugins/voice-pill

仓库是唯一源码；Codex 看到的路径里没有 `&`；个人市场清单沿用官方默认写法
（`./plugins/<名字>`），一个字都不用改。

仓库里只留模板
--------------
`.mcp.json` 与 `hooks/hooks.json` 是**渲染产物**：里面是本机绝对路径，所以被
`.gitignore` 挡住、不进仓库。仓库里对应留 `.mcp.json.template` 与
`hooks/hooks.json.template`，占位符 `<PLUGIN_DIR>` 在安装时替换成上面的联接路径。
写产物之前先 `git check-ignore` 确认它真的被忽略，否则拒绝落盘。

引擎根
------
exe 按「环境变量 → 自解析 → `%LOCALAPPDATA%\\VoicePill\\engine.json`」三级找仓库根
（见 `go/voicepill/internal/engine`）。自解析看的是 exe 自己的真实路径，摆在联接
目录里时就能中；engine.json 是「exe 被挪走 / 经联接解析不出」时的兜底，由本脚本写。

用法
----
    python plugins/install.py            缺什么补什么，可以反复跑
    python plugins/install.py --check    只看现状：文件、市场清单，以及「Codex 装没装」（会调一次 codex CLI，约 0.4 秒）

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

REPO_ROOT = Path(__file__).resolve().parents[1]

# 控制台默认是 GBK，装不上 ✅/❌ 这类字符。和 tools/ 下的自测脚本用同一招。
sys.path.insert(0, str(REPO_ROOT / "src"))
import console                      # noqa: E402
console.make_output_safe()

PLUGIN_DIR = REPO_ROOT / "plugins" / PLUGIN_NAME
LINK_PATH = Path.home() / "plugins" / PLUGIN_NAME
APP_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "VoicePill"
MARKETPLACE_PATH = Path.home() / ".agents" / "plugins" / "marketplace.json"

# exe 的出处与去处。去处经目录联接就是 Codex 看到的 ~/plugins/voice-pill/voicepill.exe。
EXE_SRC = REPO_ROOT / "bin" / "voicepill.exe"
EXE_DST = PLUGIN_DIR / "voicepill.exe"
# exe 找仓库根的第三级兜底（前两级是环境变量与自解析）。
ENGINE_JSON = APP_DIR / "engine.json"

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


def place_exe(check: bool) -> bool:
    """把 bin\\voicepill.exe 摆到插件目录。经联接就是 Codex 看到的那一份。"""
    if not EXE_SRC.is_file():
        say(False, "还没有 exe", str(EXE_SRC))
        print("      先跑 tools\\build-plugin-exe.ps1 编一份。")
        return False
    if EXE_DST.is_file() and _same_bytes(EXE_SRC, EXE_DST):
        say(True, "exe 已就位", str(EXE_DST))
        return True
    if check:
        say(False, "需要放置 exe", str(EXE_DST))
        return False
    EXE_DST.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(EXE_SRC, EXE_DST)
    say(True, "放好 exe", str(EXE_DST))
    return True


def _same_bytes(a: Path, b: Path) -> bool:
    if a.stat().st_size != b.stat().st_size:
        return False
    return a.read_bytes() == b.read_bytes()


def engine_json(check: bool) -> bool:
    """写机器侧的引擎根纸条：{"root": "<仓库根>"}。跟 settings.json、bridge.key 同处。"""
    text = json.dumps({"root": str(REPO_ROOT)}, ensure_ascii=False) + "\n"
    if ENGINE_JSON.is_file() and ENGINE_JSON.read_text(encoding="utf-8") == text:
        say(True, "引擎根纸条已是最新", str(ENGINE_JSON))
        return True
    if check:
        say(False, "引擎根纸条需要重写", str(ENGINE_JSON))
        return False
    ENGINE_JSON.parent.mkdir(parents=True, exist_ok=True)
    ENGINE_JSON.write_text(text, encoding="utf-8")
    say(True, "写好引擎根纸条", str(ENGINE_JSON))
    return True


def _run(argv: list) -> int:
    print("      $ %s" % " ".join(argv))
    try:
        return subprocess.call(argv)
    except OSError as exc:
        print("      起不来：%s" % exc)
        return 1


def _is_ignored(path):
    """渲染产物必须被 .gitignore 挡住，否则机器路径会被 git add 回仓库。

    返回 True / False；问不到 git（没装、或不在仓库里）返回 None——那不是
    「没被忽略」而是「没法判断」，而那种情况下也没有 git add 的风险。
    `git check-ignore -q`：0=被忽略，1=没被忽略，其它=出错。
    """
    try:
        rc = subprocess.run(["git", "check-ignore", "-q", str(path)],
                            cwd=str(REPO_ROOT),
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL).returncode
    except OSError:
        return None
    if rc == 0:
        return True
    if rc == 1:
        return False
    return None


def _slash(path: Path) -> str:
    return str(path).replace("\\", "/")


def _json_native(path: Path) -> str:
    """往 JSON 字符串里塞本机路径时，反斜杠还得再转义一层（否则 \\U 是非法转义）。"""
    return str(path).replace("\\", "\\\\")


# 模板 -> 产物 -> 占位符替换成哪种形式。
#   .mcp.json    一直用正斜杠（Codex 侧历来如此）
#   hooks.json   一直用反斜杠（既有产物逐字如此；cmd 执行 .cmd 时也最保底）
# 两者都是**已在本机跑通的形态**，这里只是把当时的写法固化下来，不借机换风格。
PRODUCTS = (
    (PLUGIN_DIR / ".mcp.json.template", PLUGIN_DIR / ".mcp.json", _slash),
    (PLUGIN_DIR / "hooks" / "hooks.json.template",
     PLUGIN_DIR / "hooks" / "hooks.json", _json_native),
)


def render_files(check: bool) -> bool:
    """按模板渲染出机器相关的那两处路径。内容对得上就一个字都不改（免得脏工作区）。

    产物是**仓库里的文件**（经目录联接就是 Codex 读的那份），所以落盘之前先确认
    它被 .gitignore 挡住，免得哪天机器路径又被 git add 回仓库。
    """
    ok = True
    for template, path, render in PRODUCTS:
        if not template.is_file():
            say(False, "缺模板", str(template))
            ok = False
            continue
        text = template.read_text(encoding="utf-8").replace(
            "<PLUGIN_DIR>", render(LINK_PATH))
        ignored = _is_ignored(path)
        if ignored is False:
            say(False, "渲染产物没被 git 忽略，拒绝往仓库里写机器路径", str(path))
            print("      .gitignore 里应该有：%s" % path.relative_to(REPO_ROOT))
            ok = False
            continue
        if ignored is None:
            print("      问不到 git，「产物是否被忽略」这一道跳过（没装 git？）")
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


PLUGIN_SPEC = "%s@%s" % (PLUGIN_NAME, MARKETPLACE_NAME)


def codex_plugin_list() -> str:
    """问 Codex 要插件清单；问不到（没装 codex / 超时 / 退出码非 0）返回空串。

    「装没装上」只有 Codex 自己说了算。别去读 config.toml 推断——那只能说明
    「登记过」。2026-10-04 那次 Codex 升级就是这么栽的：文件一个不缺、config 里
    也有登记，插件其实没装，钩子一条没跑；而当时的 `--check` 会直接报「就绪」。
    """
    try:
        proc = subprocess.run(["codex", "plugin", "list"],
                              capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return proc.stdout if proc.returncode == 0 else ""


def plugin_state(text: str, spec: str = PLUGIN_SPEC) -> str:
    """从清单文本判这条插件装没装：installed / missing / unknown。

    每行是「插件 状态 [版本] 来源」，以空白分列、列数不定（没装的那行没有版本，
    状态是两个词 `not installed`）。只认「行首那一个 token 正好等于 spec」，
    所以市场名（`Marketplace `personal``）和来源路径里出现同样的字串都不会被误认。
    一张表头都没见到就返回 unknown——**输出格式变了要吵，不能当作没看见**。
    """
    saw_table = False
    for line in text.splitlines():
        parts = line.split()
        if not parts:
            continue
        if parts[0] == "PLUGIN":
            saw_table = True
            continue
        if not saw_table or parts[0] != spec:
            continue
        rest = " ".join(parts[1:]).lower()
        return "installed" if rest.startswith("installed") else "missing"
    return "missing" if saw_table else "unknown"


def install_into_codex(check: bool) -> bool:
    if not check:
        if not shutil.which("codex"):
            say(False, "PATH 里没有 codex，装不了", "自己跑：codex plugin add %s"
                % PLUGIN_SPEC)
            return False
        cachebuster = (Path.home() / ".codex" / "skills" / ".system" / "plugin-creator"
                       / "scripts" / "update_plugin_cachebuster.py")
        if cachebuster.is_file():
            _run([sys.executable, str(cachebuster), str(PLUGIN_DIR)])
        rc = _run(["codex", "plugin", "add", PLUGIN_SPEC])
        say(rc == 0, "codex plugin add", "退出码 %d" % rc)
        if rc != 0:
            return False
    # 装完（或 --check）都走这一条：问 Codex，不问自己。
    state = plugin_state(codex_plugin_list())
    if state == "installed":
        say(True, "Codex 已装上这条插件", PLUGIN_SPEC)
        return True
    if state == "missing":
        say(False, "Codex 里没有这条插件，钩子与工具都不会生效",
            "跑一次：.venv\\Scripts\\python.exe plugins\\install.py")
        return False
    say(False, "问不到 codex、或清单认不出来，无法确认装没装",
        "自己看一眼：codex plugin list")
    return False


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
    ok = place_exe(args.check) and ok
    ok = render_files(args.check) and ok
    ok = engine_json(args.check) and ok
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

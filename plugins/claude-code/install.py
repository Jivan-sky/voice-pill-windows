# -*- coding: utf-8 -*-
"""把本仓库里的 Claude Code 插件接到本机：放 exe、渲染人设副本、注册市场、装插件。

为什么单独一个脚本，不复用 `plugins/install.py`
----------------------------------------------
`plugins/install.py` 是 **Codex 专用**的，它的每一步都在解 Codex 特有的问题：
目录联接（Codex 会把插件整包拷进 cache，相对路径解析不了）、
`~/.agents/plugins/marketplace.json`（Codex 的个人市场清单）、
`codex plugin add`（Codex 的安装动作）。三处都是 Codex 专有，改不动也不用改。

CC 侧**不需要联接**：CC 的钩子命令是直接 spawn、不过 shell，路径占位符按元素替换
成纯字符串，仓库路径里的 `&` 到不了 shell 解析器。所以这里直接用
`${CLAUDE_PLUGIN_ROOT}`，一个绝对路径模板都不用渲染。

契约要求见 `docs/CONTRACT.md` §4。

安装时要做的两件事（`.gitignore` 挡住的两份产物）
------------------------------------------------
它们**必须在插件目录里**，因为宿主是把插件整包拿走的，指向插件目录之外的路径
活不到那边：

    voicepill.exe   从 `bin\\voicepill.exe` 拷过来（构建产物，不进仓库）
    persona.md      从 `plugins/voice-pill/persona.md` 拷过来（人设的源只有那一份，
                    这里是**安装时渲染的副本**——《ANC》里「双源手抄必然漂移」那条）

落盘之前先 `git check-ignore` 确认它们真被挡住，否则拒绝写。

装上 ≠ 生效
-----------
2026-10-04 Codex 侧栽过一次：文件一个不缺、`--check` 报就绪、钩子一条没跑。
所以这里判「装没装上」**只问 CC 自己**（`claude plugin list --json`），并且
**要求状态里有 `enabled: true`** —— 缺这个键就报 unknown，宁可吵，不放行。

用法
----
    .venv\\Scripts\\python.exe plugins\\claude-code\\install.py            缺什么补什么，可以反复跑
    .venv\\Scripts\\python.exe plugins\\claude-code\\install.py --check    只看现状，一个字都不动

装完要**新开一个 CC 会话**才会拾取新的钩子、技能与 MCP 工具。

注意：跑安装动作会真的动你的**全局** CC 配置（市场清单 + 插件登记），
并且装完之后这个会话的 `UserPromptSubmit` / `Stop` 钩子就会开始真的取待取队列、
真的让本机出声。要人点头再跑，别顺手跑。
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# 控制台默认是 GBK，装不上 ✅/❌ 这类字符。和 tools/ 下的自测脚本用同一招。
sys.path.insert(0, str(REPO_ROOT / "src"))
import console                      # noqa: E402
console.make_output_safe()

PLUGIN_NAME = "voice-pill"
# 市场名来自本仓库 `.claude-plugin/marketplace.json` 的 `name` 字段，与插件同名。
# 改那个文件时这里要跟着改，`_marketplace_json_name()` 会当场对出来。
MARKETPLACE_NAME = "voice-pill"
PLUGIN_SPEC = "%s@%s" % (PLUGIN_NAME, MARKETPLACE_NAME)

PLUGIN_DIR = REPO_ROOT / "plugins" / "claude-code"
MARKETPLACE_JSON = REPO_ROOT / ".claude-plugin" / "marketplace.json"

EXE_SRC = REPO_ROOT / "bin" / "voicepill.exe"
EXE_DST = PLUGIN_DIR / "voicepill.exe"
PERSONA_SRC = REPO_ROOT / "plugins" / "voice-pill" / "persona.md"
PERSONA_DST = PLUGIN_DIR / "persona.md"

# 问 CC 自己的超时。marketplace add 要读一遍市场源，给得比查询宽一点。
TIMEOUT_QUERY = 60
TIMEOUT_ACTION = 300


def say(ok: bool, label: str, detail: str = "") -> None:
    print("  %s %s%s" % ("OK  " if ok else "--  ", label,
                         ("  (%s)" % detail) if detail else ""))


def _same_bytes(a: Path, b: Path) -> bool:
    if a.stat().st_size != b.stat().st_size:
        return False
    return a.read_bytes() == b.read_bytes()


def _is_ignored(path: Path):
    """产物必须被 .gitignore 挡住，否则机器产物会被 git add 回仓库。

    返回 True / False；问不到 git（没装、或不在仓库里）返回 None——那不是
    「没被忽略」而是「没法判断」，而那种情况下也没有 git add 的风险。
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


def _copy(src: Path, dst: Path, check: bool, label: str) -> bool:
    """把 src 拷成 dst（gitignore 挡住的产物）。内容一样就一个字都不动。"""
    if not src.is_file():
        say(False, "缺源文件", str(src))
        return False
    if dst.is_file() and _same_bytes(src, dst):
        say(True, "%s 已就位" % label, str(dst))
        return True
    if check:
        say(False, "需要放/更新 %s" % label, str(dst))
        return False
    ignored = _is_ignored(dst)
    if ignored is False:
        say(False, "产物没被 git 忽略，拒绝往仓库里写", str(dst))
        print("      .gitignore 里应该有：%s" % dst.relative_to(REPO_ROOT))
        return False
    if ignored is None:
        print("      问不到 git，「产物是否被忽略」这一道跳过（没装 git？）")
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
    say(True, "放好 %s" % label, str(dst))
    return True


def place_exe(check: bool) -> bool:
    if not EXE_SRC.is_file():
        say(False, "还没有 exe", str(EXE_SRC))
        print("      先跑 tools\\build-plugin-exe.ps1 编一份。")
        return False
    return _copy(EXE_SRC, EXE_DST, check, "exe")


def render_persona(check: bool) -> bool:
    return _copy(PERSONA_SRC, PERSONA_DST, check, "persona.md")


def _claude_exe():
    """定位 `claude` 可执行文件。找不到返回 None。

    必须过 `shutil.which`：npm 装出来的 `claude` 在 Windows 上是**一个没有
    扩展名的 shell 脚本 + `claude.cmd` + `claude.ps1`** 三件套，直接拿 "claude"
    交给 `subprocess` 会 FileNotFoundError——CreateProcess 只自动补 `.exe`，
    不认 PATHEXT 里那一串。`shutil.which` 会按 PATHEXT 找到 `claude.CMD`。
    """
    return shutil.which("claude")


def _cmd_quote(token: str) -> str:
    """给 cmd.exe 用的一层引号。批处理里内层引号要写成两个。"""
    return '"%s"' % token.replace('"', '""')


def _run_claude(args: list, timeout: int):
    """跑一条 claude 命令，返回 (rc, stdout, stderr)；起不来返回 None。

    npm 的 shim 是 **批处理**（`claude.CMD`），批处理只能由 cmd.exe 跑；而
    cmd.exe 会把**没有引号的 `&`** 当命令分隔符。本仓库的路径偏偏就叫
    `Own_tools&skills`，所以直接 `subprocess.run([claude, "marketplace", "add",
    <仓库路径>])` 会被**劈成两半**，实测报的是 `Path does not exist: D:\\Own_tools`
    ——路径在 `&` 处断了，命令其实"跑成功了"，只是跑的是另一个路径。

    这是 Codex 侧早就踩过的同一个坑（`plugins/install.py` 的 docstring 里写着），
    那边靠目录联接绕开；CC 侧绕不开——`marketplace add` 的入参**必须是仓库路径**。
    所以这里自己拼命令行、每个 token 都加引号，再交给 `cmd.exe /c`。

    只对 `.cmd` / `.bat` 这么做；真 exe 直接 spawn，不进 shell。
    """
    exe = _claude_exe()
    if not exe:
        return None
    try:
        if exe.lower().endswith((".cmd", ".bat")):
            inner = " ".join(_cmd_quote(t) for t in [exe] + args)
            # 整串再包一层引号：这是 cmd /c 接引号开头程序的固定写法。
            proc = subprocess.run('cmd.exe /c "%s"' % inner,
                                  capture_output=True, text=True,
                                  encoding="utf-8", errors="replace",
                                  timeout=timeout)
        else:
            proc = subprocess.run([exe] + args,
                                  capture_output=True, text=True,
                                  encoding="utf-8", errors="replace",
                                  timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _parse_json_output(text: str):
    """从一条 `--json` 命令的输出里挖出那个 JSON。挖不到返回 None。

    **两种形状都要认**（实测，别只按一种写）：
      - `plugin list --json` / `marketplace list --json` 打的是**整份美化 JSON**；
      - `marketplace add --json` / `plugin install --json` 的约定是
        「机器可读的那**一行**在最后」，前面可能还有别的输出。

    所以先按整份解析，不行再从最后一行往前逐行试。都失败返回 None——
    **输出格式变了要吵**，不能当作「没有」。
    """
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        pass
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            return json.loads(line)
        except ValueError:
            continue
    return None


def _claude_json(args: list, timeout: int, quiet: bool = False):
    """跑一条 `claude ... --json`。命令没跑成、或输出认不出来，返回 None。

    **失败要把原因打出来。** 第一版这里默默返回 None，结果 `marketplace add`
    失败时只报一句「没成功」，把真正的错因（路径被 `&` 劈了）藏掉了。
    """
    got = _run_claude(args, timeout)
    if got is None:
        return None
    rc, out, err = got
    parsed = _parse_json_output(out)
    if rc != 0 or parsed is None:
        if not quiet:
            reason = parsed.get("message") if isinstance(parsed, dict) else ""
            if not reason:
                lines = (err or out).strip().splitlines()
                reason = lines[0].strip() if lines else "(没有输出)"
            say(False, "`claude %s` 没跑成" % " ".join(args[:3]),
                "rc=%d  %s" % (rc, reason))
        return None
    return parsed


def _marketplace_json_name():
    """读本仓库市场清单里的 `name`，用来和脚本里的常量对账。读不到返回 None。"""
    try:
        data = json.loads(MARKETPLACE_JSON.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data.get("name")


def ensure_marketplace(check: bool) -> bool:
    """本仓库得先被登记成一个市场，插件才有地方可装。"""
    if not MARKETPLACE_JSON.is_file():
        say(False, "仓库里没有市场清单", str(MARKETPLACE_JSON))
        return False

    declared = _marketplace_json_name()
    if declared != MARKETPLACE_NAME:
        say(False, "市场名对不上",
            "仓库写的是 %r，脚本里是 %r" % (declared, MARKETPLACE_NAME))
        print("      `.claude-plugin/marketplace.json` 的 name 与本脚本的")
        print("      MARKETPLACE_NAME 必须一致，否则下面那一步会装到一个不存在的市场。")
        return False

    listed = _claude_json(["plugin", "marketplace", "list", "--json"], TIMEOUT_QUERY)
    if listed is None:
        say(False, "问不到 claude 的市场清单", "自己看一眼：claude plugin marketplace list")
        return False
    if any((e or {}).get("name") == MARKETPLACE_NAME for e in listed):
        say(True, "市场已登记", MARKETPLACE_NAME)
        return True

    if check:
        say(False, "市场还没登记", MARKETPLACE_NAME)
        return False

    print("      $ claude plugin marketplace add %s --json" % REPO_ROOT)
    got = _claude_json(["plugin", "marketplace", "add", str(REPO_ROOT), "--json"],
                       TIMEOUT_ACTION)
    if got is None:
        say(False, "marketplace add 没成功", "自己看一眼：claude plugin marketplace add")
        return False
    say(True, "登记好市场", MARKETPLACE_NAME)
    return True


def plugin_state(text_state):
    """从 `claude plugin list --json` 的结果判这条插件什么状态。

    返回 installed / disabled / missing / unknown。

    为什么关键在 `enabled`：**装上和生效是两回事**，钩子只在启用时才跑。
    条目在、但状态里没有 `enabled` 这个键 → unknown，不放行——
    2026-10-04 那个坑就是「文件一个不缺、`--check` 说就绪、钩子一条没跑」。
    """
    if text_state is None:
        return "unknown"
    for entry in text_state:
        if not isinstance(entry, dict) or entry.get("id") != PLUGIN_SPEC:
            continue
        enabled = entry.get("enabled")
        if enabled is True:
            return "installed"
        if enabled is False:
            return "disabled"
        return "unknown"
    return "missing"


def install_into_claude_code(check: bool) -> bool:
    if not check:
        print("      $ claude plugin install %s -y --json" % PLUGIN_SPEC)
        got = _claude_json(["plugin", "install", PLUGIN_SPEC, "-y", "--json"],
                           TIMEOUT_ACTION)
        if got is None:
            say(False, "plugin install 没成功",
                "自己看一眼：claude plugin install %s" % PLUGIN_SPEC)
            return False
    state = plugin_state(_claude_json(["plugin", "list", "--json"], TIMEOUT_QUERY))
    if state == "installed":
        say(True, "CC 已装上并启用这条插件", PLUGIN_SPEC)
        return True
    if state == "disabled":
        say(False, "插件装上了但是停用状态，钩子与工具都不会生效",
            "自己跑：claude plugin enable %s" % PLUGIN_SPEC)
        return False
    if state == "missing":
        say(False, "CC 里没有这条插件，钩子与工具都不会生效",
            "跑一次：.venv\\Scripts\\python.exe plugins\\claude-code\\install.py")
        return False
    say(False, "问不到 claude、或清单认不出来，无法确认装没装",
        "自己看一眼：claude plugin list")
    return False


def install_path():
    """装机之后宿主实际读的那份插件目录。查不到返回 None。"""
    listed = _claude_json(["plugin", "list", "--json"], TIMEOUT_QUERY)
    if listed is None:
        return None
    for entry in listed:
        if isinstance(entry, dict) and entry.get("id") == PLUGIN_SPEC:
            where = entry.get("installPath")
            return Path(where) if where else None
    return None


def verify_at_install_path(check: bool) -> bool:
    """装完之后，去宿主真正读的那份目录里点名：exe 和 persona.md 在不在。

    不能在仓库里点名就收工——宿主拿到的是哪一份，只有它自己说了算。CC 对本地
    路径的市场可能是**原地引用**、也可能拷贝一份；两种都行，但两份产物必须
    出现在最终那一份里。缺了就在那儿补上（那是宿主自己的目录，不是仓库）。
    """
    where = install_path()
    if where is None:
        say(False, "问不到安装目录，没法确认产物到位没",
            "自己看一眼：claude plugin list --json")
        return False
    missing = [p for p in (EXE_DST.name, PERSONA_DST.name)
               if not (where / p).is_file()]
    if not missing:
        say(True, "产物在宿主那一份里点过名", str(where))
        return True
    if check:
        say(False, "宿主那一份缺 %s" % " / ".join(missing), str(where))
        return False
    where.mkdir(parents=True, exist_ok=True)
    for name in missing:
        shutil.copyfile(PLUGIN_DIR / name, where / name)
    still = [p for p in (EXE_DST.name, PERSONA_DST.name)
             if not (where / p).is_file()]
    say(not still, "补齐宿主那一份的产物", str(where))
    return not still


def main() -> int:
    ap = argparse.ArgumentParser(description="把本仓库的 Claude Code 插件接到本机")
    ap.add_argument("--check", action="store_true", help="只看现状，什么都不动")
    args = ap.parse_args()

    print("=" * 68)
    print("Voice Pill —— Claude Code 侧安装%s" % ("（检查）" if args.check else ""))
    print("=" * 68)
    print("  仓库插件：%s" % PLUGIN_DIR)
    print("  市场清单：%s" % MARKETPLACE_JSON)

    exe = _claude_exe()
    if not exe:
        say(False, "PATH 里没有 claude，装不了也查不了")
        print("      装完 CC 之后重开一个终端再跑本脚本。")
        print("-" * 68)
        print("❌ 有没就位的地方，见上面标 -- 的行。")
        return 1
    print("  claude  ：%s" % exe)

    ok = place_exe(args.check)
    ok = render_persona(args.check) and ok
    ok = ensure_marketplace(args.check) and ok
    ok = install_into_claude_code(args.check) and ok
    # 没装上就没有「宿主那一份」可点名，那一项只在装上之后才有意义。
    if ok:
        ok = verify_at_install_path(args.check)

    print("-" * 68)
    if ok:
        print("✅ 就绪。新开一个 CC 会话即可用。")
        return 0
    print("❌ 有没就位的地方，见上面标 -- 的行。")
    return 1


if __name__ == "__main__":
    sys.exit(main())

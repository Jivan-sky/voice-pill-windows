# -*- coding: utf-8 -*-
"""Claude Code 侧安装自测：钉住两条纪律。

为什么要有它
------------
**一、装上 ≠ 生效。** 2026-10-04 Codex 侧栽过一次：文件一个不缺、`--check` 报
就绪、钩子一条没跑——因为「Codex 到底装没装」那一步被跳过了。这里的对应判据是
`claude plugin list --json` 里的 `enabled`：条目在、但状态里**没有** `enabled`
这个键，要判 unknown、不放行。用假清单钉边界，再用真机钉「`--check` 的退出码
必须和 CC 的说法一致」。

**二、入参里那个 `&` 会被 cmd.exe 劈开。** 本仓库路径就叫 `Own_tools&skills`，
而 `claude` 是 npm 装的批处理 `claude.CMD`——批处理只能由 cmd.exe 跑，cmd.exe 把
没加引号的 `&` 当命令分隔符。实测报的是 `Path does not exist: D:\\Own_tools`：
命令"跑成功了"，只是跑的是另一个路径，**错得很安静**。这里用一个真 `.cmd` 替身
把两头都钉住：不加引号时路径**确实**会被劈（证明这把尺子能证伪），经
`_run_claude()` 走一遍则原样到达。

用法：
    .venv\\Scripts\\python.exe tools\\claude-code-install-selftest.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))
import console                     # noqa: E402

console.make_output_safe()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTALL_PY = os.path.join(ROOT, "plugins", "claude-code", "install.py")

SPEC = "voice-pill@voice-pill"
# 故意带 `&`：这是那个坑的触发条件。路径是编的，不指向任何真目录——
# 尺子只认「会被劈开」这个形状，跟谁家盘符无关。
AMP_PATH = r"C:\tools\a&b\voice-pill"


def load_install():
    spec = importlib.util.spec_from_file_location("vp_cc_install", INSTALL_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Checker:
    def __init__(self) -> None:
        self.failed = 0

    def __call__(self, label: str, ok: bool, detail: str = "") -> bool:
        print("  %s %s%s" % ("✅" if ok else "❌", label,
                             "" if ok else ("  ← %s" % detail if detail else "")))
        if not ok:
            self.failed += 1
        return ok


def entry(enabled, name=SPEC):
    """造一条 `claude plugin list --json` 的条目。enabled 传 None = 没有这个键。"""
    out = {"id": name, "version": "0.1.0", "scope": "user",
           "installPath": r"C:\Users\x\.claude\plugins\cache\voice-pill"}
    if enabled is not None:
        out["enabled"] = enabled
    return out


def check_plugin_state(ck, M) -> None:
    """判「装没装上」：只认 enabled，缺这个键就不放行"""
    ck("enabled=true → installed", M.plugin_state([entry(True)]) == "installed")
    ck("enabled=false → disabled", M.plugin_state([entry(False)]) == "disabled")
    ck("条目在但没有 enabled 键 → unknown（钉住「装上≠生效」）",
       M.plugin_state([entry(None)]) == "unknown", M.plugin_state([entry(None)]))
    ck("清单里没有这条 → missing",
       M.plugin_state([entry(True, "别的插件@别处")]) == "missing")
    ck("空清单 → missing", M.plugin_state([]) == "missing")
    ck("问不到（None）→ unknown", M.plugin_state(None) == "unknown")
    ck("同前缀的别的插件不算",
       M.plugin_state([entry(True, "voice-pill-extra@voice-pill")]) == "missing")
    ck("同插件在别的市场不算",
       M.plugin_state([entry(True, "voice-pill@别的市场")]) == "missing")
    ck("非序列的成员被跳过，不炸",
       M.plugin_state(["字符串", None, entry(True)]) == "installed")


def check_parse_json(ck, M) -> None:
    """两种输出形状都要认"""
    pretty = '[\n  {\n    "name": "a"\n  }\n]'
    ck("整份美化 JSON", M._parse_json_output(pretty) == [{"name": "a"}])
    ck("前面有噪声、真结果在最后一行",
       M._parse_json_output("废话一行\n{\"outcome\":\"ok\"}") == {"outcome": "ok"})
    ck("尾随空行不影响",
       M._parse_json_output("{\"a\":1}\n\n") == {"a": 1})
    ck("纯垃圾 → None", M._parse_json_output("这不是 JSON") is None)
    ck("空 → None", M._parse_json_output("") is None)
    ck("只有空行 → None", M._parse_json_output("\n \n") is None)


def check_cmd_quote(ck, M) -> None:
    """给 cmd.exe 的引号"""
    ck("带 & 的 token 会被引号包住", M._cmd_quote(AMP_PATH) == '"%s"' % AMP_PATH)
    ck("内层引号写成两个",
       M._cmd_quote('a"b') == '"a""b"')


def make_fake_claude(tmp):
    """造一个真 `.cmd` 替身：把收到的参数原样打成一行 JSON。

    必须是真 `.cmd`——`_run_claude` 对 `.cmd` / `.bat` 才走 cmd.exe 那条路，
    换成 .exe 就绕开了被测的那段代码。
    """
    echo = os.path.join(tmp, "echo_args.py")
    with open(echo, "w", encoding="utf-8", newline="") as fh:
        fh.write("import json, sys\n"
                 "print(json.dumps(sys.argv[1:], ensure_ascii=False))\n")
    fake = os.path.join(tmp, "fake-claude.cmd")
    with open(fake, "w", encoding="utf-8", newline="") as fh:
        fh.write('@echo off\n"%s" "%s" %%*\n' % (sys.executable, echo))
    return fake


def check_ampersand(ck, M, tmp) -> None:
    """那个 `&`：不加引号会被劈开，走 _run_claude 则原样到达"""
    fake = make_fake_claude(tmp)
    args = ["plugin", "marketplace", "add", AMP_PATH, "--json"]

    # 反面：手工拼一条不加引号的行——必须**看得见**它被劈开。
    # 这一半是尺子的自证：没有它，下面那句"原样到达"可能只是因为路径里
    # 本来就没有会被劈的东西。
    broken_line = 'cmd.exe /c "%s" %s' % (fake, " ".join(args))
    proc = subprocess.run(broken_line, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=60)
    got = M._parse_json_output(proc.stdout)
    ck("反面：不加引号时路径确实被劈开（尺子能证伪）",
       isinstance(got, list) and AMP_PATH not in got
       and AMP_PATH.split("&")[0] in got,
       str(got))

    # 正面：经 _run_claude 走一遍，每个参数都原样到达。
    orig = M._claude_exe
    M._claude_exe = lambda: fake
    try:
        got = M._run_claude(args, 60)
    finally:
        M._claude_exe = orig
    ck("_run_claude 真的走了 cmd 那条路（替身被跑起来）",
       got is not None and got[0] == 0, str(got)[:120])
    arrived = M._parse_json_output(got[1]) if got else None
    ck("经 _run_claude：路径带着 & 原样到达", arrived == args, str(arrived))
    ck("经 _run_claude：没多出、没少掉参数",
       isinstance(arrived, list) and len(arrived) == len(args), str(arrived))


def check_claude_json_reports_failure(ck, M, tmp) -> None:
    """失败要吵：认不出来的输出必须返回 None，不能当成一个空结果"""
    fake = os.path.join(tmp, "noisy.cmd")
    # 正文用纯 ASCII：`cmd.exe` 按 GBK 读批处理，塞中文进去会让它把自己的
    # 乱码当成"命令没找到"，报出来的东西跟被测的判断无关。
    with open(fake, "w", encoding="utf-8", newline="") as fh:
        fh.write("@echo off\necho not-json-at-all\nexit /b 1\n")
    orig = M._claude_exe
    M._claude_exe = lambda: fake
    try:
        got = M._claude_json(["plugin", "list", "--json"], 60)
    finally:
        M._claude_exe = orig
    ck("退出码非 0 且输出认不出来 → None", got is None, str(got))


def check_live_consistency(ck, M) -> None:
    """真机一把：`--check` 的退出码必须和 CC 自己说的状态一致。

    这是本文件的正题——**装没装上，只问 CC**。它说 installed，`--check` 就不许
    报红；它说不是 installed，`--check` 就不许报绿（放行）。
    """
    if not M._claude_exe():
        ck("真机一致性：PATH 里没有 claude", False, "装完 CC 再跑")
        return
    listed = M._claude_json(["plugin", "list", "--json"], 60, quiet=True)
    if listed is None:
        ck("真机一致性：问不到 claude 的插件清单", False)
        return
    state = M.plugin_state(listed)
    proc = subprocess.run([sys.executable, INSTALL_PY, "--check"],
                          capture_output=True, text=True,
                          encoding="utf-8", errors="replace",
                          timeout=300, cwd=ROOT)
    code = proc.returncode
    ck("CC 说 %s → --check 的退出码对得上" % state,
       (code == 0) == (state == "installed"),
       "state=%s  exit=%d" % (state, code))


def main() -> int:
    ck = Checker()
    M = load_install()
    tmp = tempfile.mkdtemp(prefix="vp-cc-install-")
    try:
        print("=== Claude Code 侧安装自测 ===")
        steps = (
            ("判「装没装上」", check_plugin_state, (M,)),
            ("认 --json 的两种形状", check_parse_json, (M,)),
            ("cmd 引号", check_cmd_quote, (M,)),
            ("路径里的 & ", check_ampersand, (M, tmp)),
            ("失败要吵", check_claude_json_reports_failure, (M, tmp)),
            ("真机一致性", check_live_consistency, (M,)),
        )
        for label, fn, extra in steps:
            print("\n[%s]" % label)
            fn(ck, *extra)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("\n%s（%d 项失败）"
          % ("✅ 全部通过" if not ck.failed else "❌ 有失败", ck.failed))
    return 1 if ck.failed else 0


if __name__ == "__main__":
    sys.exit(main())

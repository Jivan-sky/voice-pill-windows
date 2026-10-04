# -*- coding: utf-8 -*-
"""插件安装自测：钉住「装没装上要问 Codex，不许盲报就绪」这条纪律。

为什么要有它
------------
2026-10-04 现场：Codex 桌面端从 26.924 升到 26.930，voice-pill 插件在应用侧
**没了**——`plugin.log` 一整天一条没写、钩子一次没跑，可按 Fn 那套全靠它。

而当时的 `plugins/install.py --check` 会报「✅ 就绪」：它查目录联接、查 exe、
查渲染产物、查市场清单，**唯独跳过了「Codex 到底装没装」这一步**（那一步在
`--check` 分支里直接 `return True`）。所有文件都对，装的记录没了——最容易被
漏掉的一层，恰好它不查。

于是这里的判据只有一条：**装没装上，问 Codex 自己**（`codex plugin list`），
问不到就报「无法确认」，绝不放行。这个自测把两头都钉住：解析函数的边界（假
文本），以及 `--check` 的退出码与 Codex 的说法必须一致（真机一把）。

用法：
    .venv\\Scripts\\python.exe tools\\install-selftest.py
"""
from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))
import console                     # noqa: E402

console.make_output_safe()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTALL_PY = os.path.join(ROOT, "plugins", "install.py")
CONFIG_TOML = os.path.join(os.path.expanduser("~"), ".codex", "config.toml")
RENDERED = [os.path.join(ROOT, "plugins", "voice-pill", ".mcp.json"),
            os.path.join(ROOT, "plugins", "voice-pill", "hooks", "hooks.json")]

SPEC = "voice-pill@personal"
HEADER = "PLUGIN               STATUS              VERSION                     SOURCE"


def load_install():
    spec = importlib.util.spec_from_file_location("vp_install", INSTALL_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Checker:
    def __init__(self) -> None:
        self.failed = 0
        self.skipped = 0

    def __call__(self, label: str, ok: bool, detail: str = "") -> bool:
        print("  %s %s%s" % ("✅" if ok else "❌", label,
                             "" if ok else ("  ← %s" % detail if detail else "")))
        if not ok:
            self.failed += 1
        return ok

    def skip(self, label: str, why: str) -> None:
        print("  ⏭  %s（跳过：%s）" % (label, why))
        self.skipped += 1


def fake_marketplace(*lines: str) -> str:
    """照 `codex plugin list` 的真实形状拼一份清单：市场标题 + 路径 + 空行 + 表。"""
    return "\n".join(["Marketplace `personal`",
                      "C:\\Users\\x\\.agents\\plugins\\marketplace.json",
                      "",
                      HEADER] + list(lines)) + "\n"


def main() -> int:
    ck = Checker()
    print("=== 插件安装自测（装上没装上，问 Codex）===")
    mod = load_install()

    # ---- A 解析函数的边界：全是假文本，不碰机器 ----
    cases = [
        ("installed 行 → installed",
         fake_marketplace(SPEC + "  installed, enabled  0.1.0+codex.x  C:\\Users\\x\\plugins\\voice-pill"),
         "installed"),
        ("not installed 行 → missing（状态是两个词）",
         fake_marketplace(SPEC + "  not installed                     C:\\Users\\x\\plugins\\voice-pill"),
         "missing"),
        ("表里只有别的插件 → missing",
         fake_marketplace("codex-app-tools@openai-bundled  not installed   C:\\x\\plugins\\codex-app-tools"),
         "missing"),
        ("空串 → unknown",
         "",
         "unknown"),
        ("有内容但没有表头 → unknown（输出格式变了要吵）",
         "Marketplace `personal`\nC:\\Users\\x\\marketplace.json\n",
         "unknown"),
        ("市场标题里出现同样的字串不算数",
         "Marketplace `personal`\n" + SPEC + "\n",
         "unknown"),
        ("来源路径里含 voice-pill、行首却是别的插件 → missing",
         fake_marketplace("other@personal  installed, enabled  v  C:\\Users\\x\\plugins\\voice-pill"),
         "missing"),
        ("名字只是前缀相同 → missing",
         fake_marketplace("voice-pill-pro@personal  installed, enabled  v  C:\\x"),
         "missing"),
        ("状态大小写与多余空白 → installed",
         fake_marketplace(SPEC + "   INSTALLED,   ENABLED   v   C:\\x"),
         "installed"),
        ("installed, disabled → disabled（装了不等于生效）",
         fake_marketplace(SPEC + "  installed, disabled  0.1.0  C:\\x"),
         "disabled"),
        ("只有 installed、没有 enabled/disabled → unknown（说不清就不放行）",
         fake_marketplace(SPEC + "  installed  C:\\x"),
         "unknown"),
    ]
    for label, text, want in cases:
        got = mod.plugin_state(text)
        ck(label, got == want, "得到 %r，期望 %r" % (got, want))

    # ---- B 接口口径：问不到 Codex 就不许放行 ----
    original = mod.codex_plugin_list
    try:
        mod.codex_plugin_list = lambda: ""
        ck("问不到 codex → install_into_codex(check) 为 False",
           mod.install_into_codex(True) is False)
    finally:
        mod.codex_plugin_list = original
    try:
        mod.codex_plugin_list = lambda: fake_marketplace(SPEC + "  not installed  C:\\x")
        ck("Codex 说没装 → install_into_codex(check) 为 False",
           mod.install_into_codex(True) is False)
    finally:
        mod.codex_plugin_list = original
    try:
        mod.codex_plugin_list = lambda: fake_marketplace(SPEC + "  installed, disabled  v  C:\\x")
        ck("Codex 说停用了 → install_into_codex(check) 为 False",
           mod.install_into_codex(True) is False)
    finally:
        mod.codex_plugin_list = original
    try:
        mod.codex_plugin_list = lambda: fake_marketplace(SPEC + "  installed, enabled  v  C:\\x")
        ck("Codex 说装了 → install_into_codex(check) 为 True",
           mod.install_into_codex(True) is True)
    finally:
        mod.codex_plugin_list = original

    # ---- C 真机一把：退出码必须与 Codex 的说法一致 ----
    listing = mod.codex_plugin_list()
    state = mod.plugin_state(listing)
    if not listing:
        ck.skip("真机 `--check` 一致性", "问不到 codex（PATH 里没有？）")
    else:
        ck("真机清单认得出来（installed / missing / disabled）",
           state in ("installed", "missing", "disabled"),
           "得到 %r" % state)

        before = {}
        for path in RENDERED:
            if os.path.isfile(path):
                with open(path, "rb") as fh:
                    before[path] = fh.read()
        proc = subprocess.run([sys.executable, INSTALL_PY, "--check"],
                              capture_output=True, text=True,
                              encoding="utf-8", errors="replace", cwd=ROOT)
        said_ready = proc.returncode == 0
        ck("`--check` 的退出码与 Codex 的说法一致",
           said_ready == (state == "installed"),
           "退出码 %d，而清单说 %r" % (proc.returncode, state))
        ck("`--check` 报就绪 ⟺ 清单说 installed（2026-10-04 那个盲报的判据）",
           ("✅ 就绪" in proc.stdout) == (state == "installed"))
        after = {}
        for path in before:
            with open(path, "rb") as fh:
                after[path] = fh.read()
        ck("`--check` 一个字节都不写（可反复跑）", before == after)

        # 两个独立来源互证：config.toml 的登记 vs Codex 的说法。
        if os.path.isfile(CONFIG_TOML):
            with open(CONFIG_TOML, "r", encoding="utf-8", errors="replace") as fh:
                toml = fh.read()
            registered = bool(re.search(r'^\[plugins\."%s"\]' % re.escape(SPEC),
                                        toml, re.M))
            ck("config.toml 的登记与 Codex 的说法一致",
               registered == (state == "installed"),
               "登记 %s，清单 %r" % (registered, state))
        else:
            ck.skip("config.toml 互证", "没有 ~/.codex/config.toml")

    print("\n%s（%d 项失败%s）" % ("✅ 全部通过" if not ck.failed else "❌ 有失败",
                                 ck.failed,
                                 "，%d 项跳过" % ck.skipped if ck.skipped else ""))
    return 1 if ck.failed else 0


if __name__ == "__main__":
    sys.exit(main())

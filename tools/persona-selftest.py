# -*- coding: utf-8 -*-
"""persona 规格自测：ANC 七段式 + 「单一真相源」这条纪律，能用机器查的都用机器查。

为什么要有它
------------
ANC（`HA7CH/ai-native-company` `SPEC.md` §4.1 / §4.2）把两件事写成硬规矩：

* persona 必须是**七段式**（身份 / 职责边界 / 数据来源 / 诚实条款 / 风格 /
  收资料 SOP / 动态事实引用）——缺一段就是漏掉一类边界。
* **persona 只准有一个出处**。「双源手抄是头号架构债，渲染必须是唯一上线
  路径，手改 config 视为事故。」

这两条都是"没人看着就会慢慢烂掉"的那类规矩：改的人不会觉得自己在违规。
所以钉成尺子。

用法：
    .venv\\Scripts\\python.exe tools\\persona-selftest.py
"""
from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))
import console                     # noqa: E402

console.make_output_safe()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN = os.path.join(ROOT, "plugins", "voice-pill")
PERSONA = os.path.join(PLUGIN, "persona.md")
SKILL = os.path.join(PLUGIN, "skills", "voice-pill", "SKILL.md")

# ANC SPEC §4.1 的七段，顺序即 schema 顺序。
SECTIONS = ["身份", "职责边界", "数据来源", "诚实条款", "风格",
            "收资料 SOP", "动态事实引用"]


class Checker:
    def __init__(self) -> None:
        self.failed = 0

    def __call__(self, label: str, ok: bool, detail: str = "") -> bool:
        print("  %s %s%s" % ("✅" if ok else "❌", label,
                             "" if ok else ("  ← %s" % detail if detail else "")))
        if not ok:
            self.failed += 1
        return ok


def read(path: str) -> str:
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return fh.read()


def body_lines(text: str) -> list:
    """只留正文：去掉标题行与引用块，免得拿"格式"当"复述"。"""
    out = []
    for raw in text.split("\n"):
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(">"):
            continue
        out.append(re.sub(r"\s+", "", line))
    return out


def main() -> int:
    ck = Checker()
    print("=== persona 规格自测（ANC 七段式 + 单一真相源）===")

    ok = ck("persona.md 存在", os.path.isfile(PERSONA), PERSONA)
    ck("SKILL.md 存在", os.path.isfile(SKILL), SKILL)
    if not ok:
        print("\n❌ 有失败（%d 项）" % ck.failed)
        return 1

    persona = read(PERSONA)
    skill = read(SKILL)

    heads = re.findall(r"^##\s+\d+\.\s*(.+?)\s*$", persona, re.M)
    ck("七段齐全（ANC SPEC §4.1）", heads == SECTIONS,
       "实际 %r" % (heads,))
    ck("段序与 schema 一致（顺序也是规格的一部分）", heads == SECTIONS)

    # 诚实条款必须真的写了东西，不能是空壳标题
    m = re.search(r"^##\s+4\.\s*诚实条款.*?$(.*?)^##\s+5\.",
                  persona, re.M | re.S)
    ck("诚实条款非空且有实质内容",
       bool(m) and len(re.sub(r"\s+", "", m.group(1))) >= 40,
       "长度不足" if m else "没找到第 4 节")

    ck("SKILL.md 指向 persona.md（引用而非复述）", "persona.md" in skill)
    ck("SKILL.md 说明了单一真相源的来处", "ANC" in skill)

    shared = set(body_lines(persona)) & set(body_lines(skill))
    ck("SKILL.md 没有复述 persona 正文（无 >=15 字的重合行）",
       not [x for x in shared if len(x) >= 15],
       "重合：%r" % ([x for x in shared if len(x) >= 15][:2],))

    # ANC §4.1 第 7 条：易变事实一律引用。写死绝对路径/版本号就是违规。
    # 注意别把 https:// 里的 "s:/" 当成盘符：要求前面不是字母或斜杠。
    abs_paths = re.findall(r"(?<![A-Za-z0-9/])([A-Za-z]):[\\/]", persona)
    versions = re.findall(r"\b\d+\.\d+\.\d+\b", persona)
    ck("persona 里没有写死绝对路径", not abs_paths, "实际 %r" % (abs_paths,))
    ck("persona 里没有写死版本号", not versions, "实际 %r" % (versions,))
    ck("persona 声明了「以代码为准」的裁决口径",
       "以代码为准" in persona or "为准" in persona)

    for label, path in (("persona.md", PERSONA), ("SKILL.md", SKILL)):
        with open(path, "rb") as fh:
            b = fh.read()
        ck("%s 是 LF、无 BOM" % label,
           b.count(b"\r\n") == 0 and b[:3] != b"\xef\xbb\xbf")

    link = os.path.join(os.path.expanduser("~"), "plugins", "voice-pill",
                        "persona.md")
    if os.path.exists(os.path.dirname(link)):
        ck("经 ~/plugins/voice-pill 目录联接能看到 persona.md",
           os.path.isfile(link), link)

    print("\n%s（%d 项失败）" % ("✅ 全部通过" if not ck.failed else "❌ 有失败",
                              ck.failed))
    return 1 if ck.failed else 0


if __name__ == "__main__":
    sys.exit(main())
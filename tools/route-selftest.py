# -*- coding: utf-8 -*-
"""落点路由自测 —— 落点只认显式口令、关键词只出建议、同文本同结果。

纯函数，不碰窗口、麦克风、网络，也不落任何盘：
    .venv\\Scripts\\python.exe tools\\route-selftest.py
"""
from __future__ import annotations

import io
import os
import shutil
import sys
import tempfile
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))
import console                     # noqa: E402

console.make_output_safe()

import capture                     # noqa: E402

INBOX = r"D:\vault\00_Inbox"
PROJ = r"D:\vault\04_Projects"
KW = {"计划": PROJ, "决策": PROJ}


class Checker:
    def __init__(self) -> None:
        self.failed = 0

    def __call__(self, label: str, ok: bool, detail: str = "") -> bool:
        print("  %s %s%s" % ("✅" if ok else "❌", label,
                             ("  ← %s" % detail) if detail and not ok else ""))
        if not ok:
            self.failed += 1
        return ok


def check_default(ck) -> None:
    """没有显式口令时，落点必须还是今天那个默认目录"""
    ck("无 routes 也落默认",
       capture.route("明天跟 A 确认接口", None, INBOX) ==
       (INBOX, "无口令命中，落默认捕获区", ""))
    ck("空 routes 也落默认",
       capture.route("明天跟 A 确认接口", {}, INBOX)[0] == INBOX)
    ck("空文本不崩、仍回默认",
       capture.route("   ", {}, INBOX) == (INBOX, "空文本，落默认捕获区", ""))
    ck("默认目录没配时回空串",
       capture.route("随便一句", {}, "")[0] == "")


def check_explicit(ck) -> None:
    """显式口令是唯一能改落点的东西"""
    routes = {"记一下存项目": PROJ}
    ck("口令命中改落点",
       capture.route("记一下存项目 接口定稿了", routes, INBOX)[0] == PROJ)
    ck("理由写明是哪个口令",
       "记一下存项目" in capture.route("记一下存项目 接口定稿了", routes, INBOX)[1])
    ck("口令带前导标点也命中",
       capture.route("，记一下存项目 接口定稿了", routes, INBOX)[0] == PROJ)
    ck("口令不在句首不算命中",
       capture.route("我刚刚记一下存项目 接口定稿了", routes, INBOX)[0] == INBOX)
    ck("没有配的口令不改落点",
       capture.route("记一下 接口定稿了", routes, INBOX)[0] == INBOX)


def check_longest_wins(ck) -> None:
    """更具体的口令压过更笼统的，且与配置顺序无关"""
    a = {"记一下": INBOX, "记一下存项目": PROJ}
    b = {"记一下存项目": PROJ, "记一下": INBOX}
    ck("最长者胜（笼统在前）", capture.route("记一下存项目 x", a, INBOX)[0] == PROJ)
    ck("最长者胜（具体在前）", capture.route("记一下存项目 x", b, INBOX)[0] == PROJ)
    ck("只命中笼统的仍落笼统",
       capture.route("记一下 x", a, INBOX)[0] == INBOX)


def check_keyword_is_advisory_only(ck) -> None:
    """关键词只出建议，绝不动落点 —— 两条真实口语句的反例"""
    # 落点已由显式口令指定时不再出建议：人已经指过路，再提示一个被压过的
    # 候选只是噪音。建议的语义是「你只说了内容、没指定落点，这是线索」。
    dest, reason, sug = capture.route("记一下 明天的计划", {"记一下": INBOX}, INBOX, KW)
    ck("「计划」命中不改落点", dest == INBOX)
    ck("显式口令命中时不出建议", sug == "")
    ck("理由写的是口令而非关键词", "口令" in reason and "不生效" not in reason)

    # 同一句话去掉口令，才轮到关键词出建议（落点仍不动）
    d2, r2, s2 = capture.route("明天的计划", {"记一下": INBOX}, INBOX, KW)
    ck("无口令时「计划」才出建议", s2 == PROJ and d2 == INBOX)
    ck("无口令时理由说明建议不生效", "不生效" in r2)
    # 理由和 route 写在同一篇 frontmatter 里，两边都只能是目录名，不能一边
    # 收短名、一边漏机器路径（Codex 在 2723fe2b 上抓到的正是这条）。
    ck("建议理由里不留机器路径", PROJ not in r2 and "04_Projects" in r2, r2)
    ck("建议理由里没有盘符", ":" not in r2, r2)

    dest2, r3, sug2 = capture.route("把刚才的决策过程记一下", {}, INBOX, KW)
    ck("「决策」命中也不改落点", dest2 == INBOX)
    ck("「决策」出建议", sug2 == PROJ)
    ck("「决策」的理由也不带机器路径", PROJ not in r3 and "04_Projects" in r3, r3)

    ck("没配关键词时不产建议",
       capture.route("随便一句", {}, INBOX)[2] == "")
    ck("关键词没命中时不产建议",
       capture.route("明天跟 A 确认接口", {}, INBOX, KW)[2] == "")


def check_replayable(ck) -> None:
    """同一条文本重放 → 同落点 + 同理由（issue #1 的验收判据）"""
    routes = {"记一下": INBOX, "记一下存项目": PROJ}
    text = "记一下存项目 明天跟 A 确认接口"
    first = capture.route(text, routes, INBOX, KW)
    again = capture.route(text, routes, INBOX, KW)
    ck("两次调用逐字相同", first == again, "%r vs %r" % (first, again))
    ck("输入没被改动（纯函数）", text == "记一下存项目 明天跟 A 确认接口")
    ck("返回三元组", isinstance(first, tuple) and len(first) == 3)


def check_dest_label(ck) -> None:
    """落点路径收成笔记里那个名字，机器路径不进 frontmatter"""
    ck("取末段", capture.dest_label(r"D:\vault\00_Inbox") == "00_Inbox")
    ck("末尾带斜杠也认", capture.dest_label("D:/vault/00_Inbox/") == "00_Inbox")
    ck("空串回空串", capture.dest_label("") == "")


def check_route_in_frontmatter(ck, tmp) -> None:
    """落点与理由落进 frontmatter；不给 route_meta 时输出与今天逐字节相同"""
    now = datetime(2026, 10, 5, 1, 2, 3)
    today = capture.compose("明天跟 A 确认接口", now)

    ck("不给 route_meta 时与今天一致",
       today == capture.compose("明天跟 A 确认接口", now, None))
    ck("今天那份没有 route/reason 键",
       "route:" not in today and "reason:" not in today)
    ck("今天那份仍是 type/inbox", "  - type/inbox\n" in today)

    meta = capture.route("记一下存项目 接口定稿", {"记一下存项目": PROJ}, INBOX)
    got = capture.compose("接口定稿", now, (meta[0], meta[1]))
    ck("route 行是目录名、不是机器路径", 'route: "04_Projects"\n' in got)
    ck("reason 行就是 route 给的那句",
       'reason: "%s"\n' % meta[1] in got)
    ck("route 行在 tags 之后", got.index("type/inbox") < got.index("route:"))
    ck("两行都在 frontmatter 块内",
       got.index("route:") < got.index("\n---\n") and
       got.index("reason:") < got.index("\n---\n"))
    ck("正文没被这两行碰到", "接口定稿\n" in got.split("\n---\n")[1])
    ck("理由里有引号会被转义",
       'reason: "a\\"b"\n' in capture.compose("x", now, (INBOX, 'a"b')))
    # 双引号标量里的裸换行会被 YAML 折成空格——不报错，只是理由被悄悄改写。
    nl = capture.compose("x", now, (INBOX, "上句\n下句"))
    ck("理由里的换行转义成 \\n", 'reason: "上句\\n下句"\n' in nl, repr(nl))
    ck("理由里的换行没漏进 frontmatter", nl.count("\n---\n") == 1, repr(nl))
    ck("落点为空时不写 route 行",
       "route:" not in capture.compose("x", now, ("", "空文本，落默认捕获区")))
    ck("落点为空时 reason 仍在",
       'reason: "空文本，落默认捕获区"\n' in capture.compose("x", now, ("", "空文本，落默认捕获区")))

    path = capture.write(tmp, "接口定稿", now, (meta[0], meta[1]))
    raw = io.open(path, encoding="utf-8", newline="").read()
    ck("落盘内容与 compose 逐字相同", raw == got)
    ck("落盘是纯 LF、无 BOM",
       "\r" not in raw and not raw.startswith("\ufeff"))


def main() -> int:
    ck = Checker()
    tmp = tempfile.mkdtemp(prefix="vp-route-")
    try:
        print("=== 落点路由自测 ===")
        for fn, args in ((check_default, ()), (check_explicit, ()),
                         (check_longest_wins, ()),
                         (check_keyword_is_advisory_only, ()),
                         (check_replayable, ()), (check_dest_label, ()),
                         (check_route_in_frontmatter, (tmp,))):
            print("\n[%s]" % fn.__doc__.strip().splitlines()[0])
            fn(ck, *args)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("\n%s（%d 项失败）"
          % ("✅ 全部通过" if not ck.failed else "❌ 有失败", ck.failed))
    return 1 if ck.failed else 0


if __name__ == "__main__":
    sys.exit(main())

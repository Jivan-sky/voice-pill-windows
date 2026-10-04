# -*- coding: utf-8 -*-
"""控制面 `capture` 动词自测 —— 分发、幂等、坏输入、落点接没接上。

为什么需要它
------------
`capture` 是控制面上**唯一会往用户文件系统写东西**的动词，而 `main.py` 的
分发有个静默陷阱：`_bridge_command` 里没被 if 拦住的命令会一路落到最后那行
`return self._bridge_status()`——**不报错，回一份状态快照**。加动词时漏一次
分支，调用方会以为落库成功、拿到的却是一个 `status`。没有任何东西会红。

本脚本就是那个「红」：它按调用方的样子调 `_bridge_command("capture", ...)`，
断言回来的是 capture 的形状、不是 status 的形状。

不需要窗口、麦克风、网络、命名管道，只碰临时目录：
    .venv\\Scripts\\python.exe tools\\bridge-capture-selftest.py

刻意**不**调 `VoicePill.__init__`：那会去开热键、建 HUD、连引擎。这里只把
用到的那几个字段挂上去（settings / 索引目录），量的是动词这一层。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))
import console                     # noqa: E402

console.make_output_safe()

import bridge                      # noqa: E402
import main as app_main            # noqa: E402


class Checker:
    def __init__(self) -> None:
        self.failed = 0

    def __call__(self, label: str, ok: bool, detail: str = "") -> bool:
        print("  %s %s%s" % ("✅" if ok else "❌", label,
                             ("  ← %s" % detail) if detail and not ok else ""))
        if not ok:
            self.failed += 1
        return ok


def _engine(tmp: str, routes=None, capture_dir=None):
    """一台只有 capture 这条路需要的零件的假引擎。"""
    pill = app_main.VoicePill.__new__(app_main.VoicePill)
    pill.settings = SimpleNamespace(
        capture_enabled=True,
        capture_dir=capture_dir if capture_dir is not None
        else os.path.join(tmp, "inbox"),
        capture_prefixes=["记一下"],
        capture_routes=routes or {},
        capture_keywords={},
    )
    pill._capture_index_dir = lambda: os.path.join(tmp, "index")
    return pill


def check_in_commands(ck) -> None:
    """真闸门与 Go 侧清单：capture 必须在，且两处不能只有一处有"""
    ck("bridge.COMMANDS 里有 capture", "capture" in bridge.COMMANDS,
       repr(bridge.COMMANDS))

    client_go = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))),
        "go", "voicepill", "internal", "bridge", "client.go")
    try:
        with open(client_go, "r", encoding="utf-8") as fh:
            src = fh.read()
    except OSError as exc:
        ck("Go 侧清单读得到", False, repr(exc))
        return
    ck("Go 侧 Commands 里有 capture", '"capture"' in src.split("var Commands")[1][:400])
    ck("Go 侧 commandTimeouts 里有 capture",
       '"capture"' in src.split("var commandTimeouts")[1][:600])


def check_dispatch(ck, tmp) -> None:
    """分发：capture 走自己的分支，不落到静默的 status"""
    pill = _engine(tmp, routes={"记一下存项目": os.path.join(tmp, "proj")})
    got = pill._bridge_command("capture",
                               {"text": "记一下存项目：接口定了", "text_id": "d-1"})
    ck("回的是 capture 的形状（有 duplicate 键）",
       isinstance(got, dict) and "duplicate" in got, repr(got))
    # 静默陷阱的样子：没接分支时会回 _bridge_status()，那份里有 phase/pid
    ck("没有落到静默的 status（不该有 phase/pid 键）",
       isinstance(got, dict) and "phase" not in got and "pid" not in got, repr(got))
    ck("落到了口令指定的目录（route 接线生效）",
       os.path.dirname(got.get("path", "")) == os.path.join(tmp, "proj"),
       repr(got))
    ck("response.dest 是目录名、不是机器路径",
       got.get("dest") == "proj", repr(got.get("dest")))
    ck("reason 跟着落点一起回", bool(got.get("reason")), repr(got.get("reason")))
    ck("落盘文件真的在", os.path.isfile(got.get("path", "")), repr(got.get("path")))


def check_idempotent(ck, tmp) -> None:
    """同一个 text_id 重投：只落一篇，第二次指回第一次那一篇"""
    pill = _engine(tmp)
    first = pill._bridge_command("capture", {"text": "一句话", "text_id": "i-1"})
    second = pill._bridge_command("capture", {"text": "一句话", "text_id": "i-1"})
    ck("第一次不是重复", first.get("duplicate") is False, repr(first))
    ck("第二次认成重复", second.get("duplicate") is True, repr(second))
    ck("第二次指回第一次那一篇", second.get("path") == first.get("path"),
       "%r vs %r" % (first.get("path"), second.get("path")))
    where = os.path.join(tmp, "inbox")
    notes = sorted(f for f in os.listdir(where) if f.endswith(".md"))
    ck("落盘只有一篇（重投没写出第二篇）", len(notes) == 1, repr(notes))

    # 不同 text_id、同样的文本 → 必须各落一篇：同一个人今天把同一句话说两遍
    # 是合法的，拿内容兜底会把第二遍当重复吃掉（契约 §3 规则 4）。
    third = pill._bridge_command("capture", {"text": "一句话", "text_id": "i-2"})
    ck("换 text_id 就是新的一篇", third.get("duplicate") is False, repr(third))
    ck("两篇都在", len(os.listdir(where)) == 2, repr(os.listdir(where)))


def check_bad_input(ck, tmp) -> None:
    """坏输入：该报的报、该跳的跳，不许静默成功"""
    pill = _engine(tmp)

    for label, args in (("缺 text_id", {"text": "x"}),
                        ("text_id 是空串", {"text": "x", "text_id": ""}),
                        ("text_id 是空白", {"text": "x", "text_id": "   "}),
                        ("text_id 不是字符串", {"text": "x", "text_id": 3})):
        try:
            pill._bridge_command("capture", args)
            ck("%s → 报错" % label, False, "没有抛异常")
        except ValueError:
            ck("%s → 报错" % label, True)
        except Exception as exc:                      # noqa: BLE001
            ck("%s → 报错" % label, False, repr(exc))

    where = os.path.join(tmp, "inbox")
    before = len(os.listdir(where)) if os.path.isdir(where) else 0
    got = pill._bridge_command("capture", {"text": "   ", "text_id": "b-1"})
    ck("空文本 → accepted:false + skipped",
       got.get("accepted") is False and bool(got.get("skipped")), repr(got))
    after = len(os.listdir(where)) if os.path.isdir(where) else 0
    ck("空文本不落盘", before == after, "%d → %d" % (before, after))

    empty = _engine(tmp, capture_dir="")
    try:
        empty._bridge_command("capture", {"text": "x", "text_id": "b-2"})
        ck("没配 capture_dir → 报错（不许悄悄落到别处）", False, "没有抛异常")
    except OSError:
        ck("没配 capture_dir → 报错（不许悄悄落到别处）", True)
    except Exception as exc:                          # noqa: BLE001
        ck("没配 capture_dir → 报错（不许悄悄落到别处）", False, repr(exc))


def check_failed_write_releases(ck, tmp) -> None:
    """落盘失败要放掉认领 —— 否则这个 text_id 废了、文字真丢了"""
    pill = _engine(tmp)
    import capture as capture_mod
    real = capture_mod.write

    def boom(*_a, **_k):
        raise OSError("盘满了（这是自测故意造的）")

    capture_mod.write = boom
    try:
        pill._bridge_command("capture", {"text": "会失败的这一篇", "text_id": "f-1"})
        ck("落盘失败要抛出去", False, "没有抛异常")
    except OSError:
        ck("落盘失败要抛出去", True)
    except Exception as exc:                          # noqa: BLE001
        ck("落盘失败要抛出去", False, repr(exc))
    finally:
        capture_mod.write = real

    got = pill._bridge_command("capture", {"text": "会失败的这一篇", "text_id": "f-1"})
    ck("放掉认领之后重投能落下去", got.get("accepted") is True, repr(got))
    ck("重投落出来的不是「重复」", got.get("duplicate") is False, repr(got))


def main() -> int:
    ck = Checker()
    tmp = tempfile.mkdtemp(prefix="vp-capture-verb-")
    try:
        print("=== 控制面 capture 动词自测 ===")
        for fn, args in ((check_in_commands, ()), (check_dispatch, (tmp,)),
                         (check_idempotent, (tmp,)), (check_bad_input, (tmp,)),
                         (check_failed_write_releases, (tmp,))):
            print("\n[%s]" % fn.__doc__.strip().splitlines()[0])
            fn(ck, *args)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("\n%s（%d 项失败）"
          % ("✅ 全部通过" if not ck.failed else "❌ 有失败", ck.failed))
    return 1 if ck.failed else 0


if __name__ == "__main__":
    sys.exit(main())

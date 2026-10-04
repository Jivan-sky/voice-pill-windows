# -*- coding: utf-8 -*-
"""`take` 的 `seq` 自测：打真队列，验「按条数跳号」这条契约（§1 规则 4、§4.1）。

为什么非要有这把尺子
--------------------
`seq` 的全部价值在于**算得出「你错过了 N 条」**。算错的方向有两种，而且
都不会报错、只会给个看着合理的数：按调用次数递增（永远只差 1）、或者把
过期丢弃也占号（把「没送到」记成一次投递）。两种都得在这里钉死。

另外钉一个跨重启的事实：`seq` 只在内存里，重启归零——契约 §1 明确写了
宿主该把回落当重启处理，所以这里必须真的复现一次回落，别让那句话变成
没人验过的承诺。

不碰麦克风、不装热键钩子、不开控制面管道、不显示 HUD：只构造 VoicePill
对象，然后手工往队列里塞。

用法：
    .venv\\Scripts\\python.exe tools\\take-seq-selftest.py
"""
from __future__ import annotations

import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))
import console                     # noqa: E402

console.make_output_safe()

import config                      # noqa: E402
import main as app_main            # noqa: E402


class Checker:
    def __init__(self) -> None:
        self.failed = 0

    def __call__(self, label: str, ok: bool, detail: str = "") -> bool:
        print("  %s %s%s" % ("✅" if ok else "❌", label,
                             "" if ok else ("  ← %s" % detail if detail else "")))
        if not ok:
            self.failed += 1
        return ok


def make_pill() -> "app_main.VoicePill":
    """造一个不带副作用的外壳：无 HUD、无钩子、无管道、无麦克风。"""
    return app_main.VoicePill(config.Settings(hud_enabled=False, auto_paste=True))


def put(pill, *texts: str, age: float = 0.0) -> None:
    """往待取队列里塞字。`age` 是「这条是多久以前说的」，用来造过期件。"""
    stamp = time.time() - age
    with pill._lock:
        for text in texts:
            pill._pending.append((stamp, text))


def take(pill) -> dict:
    """走控制面那一条路，拿到的就是宿主真正会看见的形状。"""
    return pill._bridge_command("take", {})


def missed(got: dict, last_seq: int) -> int:
    """契约 §1 规则 4 给的宿主算法，原样抄一遍。

    抄而不是引用，是故意的：宿主在别的语言里（Go），这份算法得能被独立
    实现。这里验的是「照契约算出来的数对不对」。
    """
    return got["seq"] - got["count"] - last_seq


# ---------- 各项检查 ----------

def check_increments_by_count(ck: Checker) -> None:
    """按条数递增，不按调用次数。"""
    pill = make_pill()
    put(pill, "甲", "乙")
    first = take(pill)
    ck("首次取走 2 条 → count=2", first["count"] == 2, repr(first))
    ck("seq 按条数走：2（不是 1）", first["seq"] == 2, repr(first))

    empty = take(pill)
    ck("空队列不是错误（count=0）", empty["count"] == 0, repr(empty))
    ck("空队列 seq 不动（仍是 2）", empty["seq"] == 2, repr(empty))

    put(pill, "丙")
    ck("再取 1 条 → seq=3", take(pill)["seq"] == 3)


def check_expired_do_not_burn_numbers(ck: Checker) -> None:
    """过期件不占号：它们谁也没送到，占号会把丢弃报成投递。"""
    pill = make_pill()
    ttl = pill._pending_ttl_seconds()
    ck("保鲜期是个正数（不然这项没意义）", ttl > 0, "ttl=%r" % (ttl,))
    put(pill, "很久以前说的", age=ttl + 60)
    put(pill, "刚说的")
    got = take(pill)
    ck("过期的既不返回也不留队", got["texts"] == ["刚说的"], repr(got))
    ck("seq 只 +1（过期那条不占号）", got["seq"] == 1, repr(got))


def check_gap_is_computable(ck: Checker) -> None:
    """契约要的那个效果：后到的宿主算得出自己错过了几条。"""
    pill = make_pill()
    put(pill, "一")
    a_first = take(pill)
    last_seq = a_first["seq"]
    ck("甲第一次拿到 1 条、last_seq=1",
       a_first["count"] == 1 and last_seq == 1, repr(a_first))

    # 乙在这中间把后说的两条都取走了。
    put(pill, "二", "三")
    b = take(pill)
    ck("乙取走 2 条 → seq=3", b["count"] == 2 and b["seq"] == 3, repr(b))

    # 甲下一轮再问，队列已经空了。
    a_second = take(pill)
    ck("甲拿到空结果", a_second["count"] == 0, repr(a_second))
    ck("甲算得出「错过了 2 条」", missed(a_second, last_seq) == 2,
       "missed=%d（got=%r last_seq=%d）" % (missed(a_second, last_seq),
                                            a_second, last_seq))

    # 反向：没有别人插手时不能误报。
    put(pill, "四")
    a_third = take(pill)
    ck("自己取的这一轮不误报错过",
       missed(a_third, a_second["seq"]) == 0,
       "missed=%d（got=%r）" % (missed(a_third, a_second["seq"]), a_third))


def check_seq_shared_across_both_hosts(ck: Checker) -> None:
    """两个宿主拿到的是同一条号轴，不是各记各的。"""
    pill = make_pill()
    put(pill, "给甲")
    a = take(pill)
    put(pill, "给乙")
    b = take(pill)
    ck("乙的号接在甲后面（2 而不是 1）", b["seq"] == 2, "a=%r b=%r" % (a, b))


def check_restart_resets_and_contract_says_so(ck: Checker) -> None:
    """重启归零是**已知例外**，不是缺陷——契约 §1 明确写了宿主怎么处理。"""
    old = make_pill()
    put(old, "甲", "乙")
    took = take(old)
    ck("重启前 seq=2", took["seq"] == 2, repr(took))

    new = make_pill()
    fresh = take(new)
    ck("重启后 seq 归零（内存里，不落盘）", fresh["seq"] == 0, repr(fresh))
    ck("回落是真的会发生的：0 < 2（所以契约那句不是空话）",
       fresh["seq"] < took["seq"], "%r < %r" % (fresh["seq"], took["seq"]))


def check_concurrent_takers_do_not_reuse_numbers(ck: Checker) -> None:
    """两个宿主同时来问：不重不漏，号也不重合在同一批字上。"""
    pill = make_pill()
    total = 8
    put(pill, *["第 %d 条" % i for i in range(total)])

    results = []
    lock = threading.Lock()

    def worker() -> None:
        got = take(pill)
        with lock:
            results.append(got)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    delivered = sum(r["count"] for r in results)
    ck("4 个并发取走，条数不重不漏（合起来 8 条）", delivered == total,
       "合起来 %d 条：%r" % (delivered, [r["count"] for r in results]))
    ck("最终 seq == 投进去的条数", take(pill)["seq"] == total)
    ck("每个非空响应都满足 seq >= count（号没被提前读走）",
       all(r["seq"] >= r["count"] for r in results),
       repr([(r["count"], r["seq"]) for r in results]))


def check_shape(ck: Checker) -> None:
    """形状层面的两条：多出来的字段不会挤掉原有的、也不冒充别的动词。"""
    pill = make_pill()
    put(pill, "甲")
    got = take(pill)
    ck("take 的响应带 seq", isinstance(got.get("seq"), int), repr(got))
    ck("原有的 texts/count 还在", got.get("texts") == ["甲"] and got["count"] == 1,
       repr(got))
    ck("没有混进 status 的形状", "phase" not in got and "pid" not in got, repr(got))

    drained = pill._drain_pending()
    ck("_drain_pending 回的是 (文字, 序号) 二元组",
       isinstance(drained, tuple) and len(drained) == 2
       and isinstance(drained[0], list) and isinstance(drained[1], int),
       repr(drained))

    ck("status 那边不受影响（还是只报 pending 计数）",
       "pending" in pill._bridge_status())


def main() -> int:
    print("take seq 自测")
    ck = Checker()
    check_increments_by_count(ck)
    check_expired_do_not_burn_numbers(ck)
    check_gap_is_computable(ck)
    check_seq_shared_across_both_hosts(ck)
    check_restart_resets_and_contract_says_so(ck)
    check_concurrent_takers_do_not_reuse_numbers(ck)
    check_shape(ck)
    print("\n%s" % ("全部通过" if not ck.failed else "失败 %d 项" % ck.failed))
    return 1 if ck.failed else 0


if __name__ == "__main__":
    sys.exit(main())

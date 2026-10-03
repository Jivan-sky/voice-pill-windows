# -*- coding: utf-8 -*-
"""交付护栏自测 —— 原版 `tests/DeliverySessionTests.swift` 的等价物。

查的是"按 Fn 没反应"这一类故障的根：一次交付卡住之后，状态还回不回得来。
前四条照搬上游，后两条是本移植版特有的——迟到的看门狗（确定性断言）与
锁序。看门狗跑在自己的线程上，而调用方的回调会去拿调用方自己的锁；顺序
反了就是死锁，且只在超时那一刻才出现，平时测不到。

不需要窗口、麦克风、网络：
    .venv\\Scripts\\python.exe tools\\delivery-selftest.py
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

import delivery                    # noqa: E402

# 死锁的话整个脚本会卡住：宁可报失败也不要挂在那儿。
START = time.monotonic()
LIMIT_SECONDS = 20.0


def _hard_stop() -> None:
    if time.monotonic() - START > LIMIT_SECONDS:
        print("❌ 超时 %.0f 秒未结束——疑似死锁（见 delivery.py 顶部锁序）"
              % LIMIT_SECONDS)
        sys.stdout.flush()
        os._exit(1)


class Checker:
    def __init__(self) -> None:
        self.failed = 0

    def __call__(self, label: str, ok: bool, detail: str = "") -> bool:
        print("  %s %s%s" % ("✅" if ok else "❌", label,
                             "" if ok else ("  ← %s" % detail if detail else "")))
        if not ok:
            self.failed += 1
        return ok


def check_early_return_unlocks(ck: Checker) -> None:
    """提前返回（这里是抛异常）之后，看门狗必须已经解除。"""
    fired = []
    s = delivery.DeliverySession(0.05, lambda: fired.append("timeout"))
    gen = None
    try:
        with s.run() as gen:
            raise RuntimeError("模拟粘贴路径上的意外异常")
    except RuntimeError:
        pass
    time.sleep(0.15)               # 超过 timeout，若没解除就会误触发
    ck("提前异常后不再触发看门狗", fired == [], "实际 %r" % (fired,))
    ck("提前异常后本代仍算数（由调用方决定怎么收尾）", s.is_current(gen))


def check_success_never_reports(ck: Checker) -> None:
    """正常走完的交付不能被误报成失败。"""
    fired = []
    s = delivery.DeliverySession(0.05, lambda: fired.append("timeout"))
    with s.run():
        pass
    time.sleep(0.15)
    ck("成功路径不误报", fired == [], "实际 %r" % (fired,))


def check_stall_fires_once(ck: Checker) -> None:
    """停滞到点：作废一次，且只作废一次。"""
    fired = []
    done = threading.Event()

    def on_interrupt() -> None:
        fired.append("timeout")
        done.set()

    s = delivery.DeliverySession(0.05, on_interrupt)
    with s.run() as gen:
        done.wait(2.0)             # 待在交付里，等看门狗来作废
        ck("停滞到点后本代被作废", not s.is_current(gen))
    time.sleep(0.15)
    ck("停滞只作废一次", fired == ["timeout"], "实际 %r" % (fired,))


def check_stale_body_sees_itself_stale(ck: Checker) -> None:
    """看门狗作废之后，仍在跑的那次交付必须能看出自己已经过期。

    真实后果：粘贴阻塞 8 秒以上，看门狗解锁、用户又按了 Fn 说下一句；此时
    旧交付返回，若不自查就会去删新会话的 WAV、清它的队列。
    """
    s = delivery.DeliverySession(0.05, lambda: None)
    observed = []
    new_started = threading.Event()
    release_old = threading.Event()

    def old_delivery() -> None:
        with s.run() as gen:
            new_started.wait(2.0)
            observed.append(s.is_current(gen))
            release_old.set()

    worker = threading.Thread(target=old_delivery, daemon=True)
    worker.start()
    time.sleep(0.2)                # 让旧代先超时
    with s.run() as gen2:
        new_started.set()
        release_old.wait(2.0)
        ck("新会话在当前代里", s.is_current(gen2))
    worker.join(2.0)
    ck("旧交付自查为过期", observed == [False], "实际 %r" % (observed,))


def check_late_watchdog_is_noop(ck: Checker) -> None:
    """迟到的看门狗必须是无操作（确定性断言，不靠时序）。

    每个代际各有自己的定时器，`_fire(gen)` 先比对代际。这里直接把旧代的
    定时器回调手工调一次，模拟"定时器排队晚了、新会话已开始"的情形。
    """
    fired = []
    s = delivery.DeliverySession(0, lambda: fired.append("timeout"))  # 不自动武装
    with s.run() as gen1:
        pass
    with s.run() as gen2:
        pass
    s._fire(gen1)                  # 旧代迟到
    ck("旧代迟到不触发回调", fired == [], "实际 %r" % (fired,))
    ck("新会话不受影响", s.is_current(gen2))
    s._fire(gen2)                  # 本代仍有效 → 应该触发
    ck("本代仍然有效时正常触发", fired == ["timeout"], "实际 %r" % (fired,))


def check_lock_order(ck: Checker) -> None:
    """看门狗回调里读 generation() 不能死锁。

    真实调用方（VoicePill._reset）会持自己的锁读状态，所以这里故意在回调
    里再取一次本模块的锁——顺序写反了就会卡死，最后由 _hard_stop 报出来。
    """
    seen = []

    def on_interrupt() -> None:
        seen.append(s.generation)   # 回调里再锁一次本模块
        seen.append(s.is_current(0))

    s = delivery.DeliverySession(0.05, on_interrupt)
    with s.run():
        time.sleep(0.25)
    ck("回调里能安全再读状态（无死锁）", len(seen) == 2, "实际 %r" % (seen,))


def main() -> int:
    stopper = threading.Timer(2.0, _hard_stop)
    stopper.daemon = True
    stopper.start()

    ck = Checker()
    print("=== 交付护栏自测（原版 DeliverySessionTests 等价物）===")
    for fn in (check_early_return_unlocks, check_success_never_reports,
               check_stall_fires_once, check_stale_body_sees_itself_stale,
               check_late_watchdog_is_noop, check_lock_order):
        print("\n[%s]" % fn.__doc__.strip().splitlines()[0])
        fn(ck)
    stopper.cancel()
    print("\n%s（%d 项失败）" % ("✅ 全部通过" if not ck.failed else "❌ 有失败",
                              ck.failed))
    return 1 if ck.failed else 0


if __name__ == "__main__":
    sys.exit(main())
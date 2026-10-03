# -*- coding: utf-8 -*-
"""交付护栏的**故障注入**自测：直接打真状态机，不靠真麦克风。

和 delivery-selftest.py 的分工
------------------------------
那边量的是护栏原语（代际、看门狗、锁序），是这个模块自己的尺子。
这里量的是**接进 VoicePill 之后到底管不管用**：把粘贴换成"卡死不返回"，
看那条 Fn 准入闸门会不会自己打开；再模拟用户看到解锁后立刻说下一句，
看迟到的旧交付会不会把新会话砸了。

这两件事正是原版 0.2.3 的实际故障（应用忙着、没有转写子进程、按 Fn 没
反应，见上游 docs/VERIFY-0.2.4.md），而它们**没有任何可见症状**——所以
必须有尺子，不能靠"我按着还好"。

不碰麦克风、不装热键钩子、不开控制面管道、不显示 HUD：
只构造 VoicePill 对象，然后手工摆状态、注入故障。

用法：
    .venv\\Scripts\\python.exe tools\\delivery-fault-selftest.py
"""
from __future__ import annotations

import io
import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))
import console                     # noqa: E402

console.make_output_safe()

import config                      # noqa: E402
import main as app_main            # noqa: E402
import paste as paste_mod          # noqa: E402


class Checker:
    def __init__(self) -> None:
        self.failed = 0

    def __call__(self, label: str, ok: bool, detail: str = "") -> bool:
        print("  %s %s%s" % ("✅" if ok else "❌", label,
                             "" if ok else ("  ← %s" % detail if detail else "")))
        if not ok:
            self.failed += 1
        return ok


def wait_until(pred, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return bool(pred())


def make_pill() -> "app_main.VoicePill":
    """造一个不带副作用的外壳：无 HUD、无钩子、无管道、无麦克风。"""
    return app_main.VoicePill(config.Settings(hud_enabled=False, auto_paste=True))


def enter_transcribing(pill) -> None:
    with pill._lock:
        pill._set_phase(app_main.Phase.TRANSCRIBING)


def check_timeout_is_upstream_value(ck: Checker) -> None:
    """护栏时长必须和原版一致——不然就是换了个拍脑袋的数。"""
    ck("停滞上限 = 8 秒（同上游 DeliverySession.swift）",
       app_main.DELIVERY_TIMEOUT_SECONDS == 8.0,
       "实际 %r" % (app_main.DELIVERY_TIMEOUT_SECONDS,))


def check_stuck_paste_unlocks_and_spares_new_session(ck: Checker) -> None:
    """粘贴卡死 → 自动解锁；随后开的新会话不能被旧交付砸掉。

    这是本修复的核心场景，也是上游那个故障的完整重演。
    """
    # 先缩短常量、再造对象：DeliverySession 在 __init__ 里读它。
    # 顺序写反过一次，结果是"护栏没触发"——测试自己先把结论写死了。
    real_timeout = app_main.DELIVERY_TIMEOUT_SECONDS
    app_main.DELIVERY_TIMEOUT_SECONDS = 0.4
    pill = make_pill()
    hang = threading.Event()
    real_paste = paste_mod.paste
    paste_mod.paste = lambda text, hwnd: hang.wait(30)

    buf = io.StringIO()
    real_stderr, sys.stderr = sys.stderr, buf
    new_wav = os.path.join(tempfile.gettempdir(), "voicepill-fault-new.wav")
    worker = None
    try:
        enter_transcribing(pill)
        worker = threading.Thread(target=pill.on_complete,
                                  args=("卡住的那句话", None), daemon=True)
        worker.start()

        ck("粘贴卡死时护栏把状态收回了 IDLE",
           wait_until(lambda: pill.phase is app_main.Phase.IDLE, 5.0),
           "实际 %s" % pill.phase)

        # 用户看到解锁，立刻又说了一句（新会话）
        with open(new_wav, "wb") as fh:
            fh.write(b"RIFF-new-session")
        with pill._lock:
            pill._set_phase(app_main.Phase.RECORDING)
        pill._wav_path = new_wav
        pill._transcript = "新会话正在说的话"

        hang.set()                                # 放行那个卡住的粘贴
        worker.join(5.0)

        ck("新会话的录音没被旧交付删掉", os.path.isfile(new_wav))
        ck("新会话的状态没被旧交付复位",
           pill.phase is app_main.Phase.RECORDING, "实际 %s" % pill.phase)
        ck("新会话的话没被旧交付覆盖",
           pill._transcript == "新会话正在说的话",
           "实际 %r" % pill._transcript)
        text = buf.getvalue()
        ck("护栏在日志里留了痕", "[护栏]" in text)
        ck("旧交付自认迟到并放弃收尾", "[迟到]" in text)
        ck("卡住的那句话仍然进了待取队列（不丢用户的话）",
           pill._pending_count() == 1, "实际 %d" % pill._pending_count())
    finally:
        sys.stderr = real_stderr
        paste_mod.paste = real_paste
        app_main.DELIVERY_TIMEOUT_SECONDS = real_timeout
        if worker is not None:
            hang.set()
            worker.join(2.0)
        if os.path.exists(new_wav):
            os.remove(new_wav)


def check_unexpected_paste_exception_still_unlocks(ck: Checker) -> None:
    """非 PasteError 的异常也必须解锁——上游 0.2.3 正是让它穿透了出去。

    以前这里只捕 PasteError；换成 OSError（剪贴板被别的程序占着）就一路
    抛穿 on_complete，`_reset()` 轮不到执行，Fn 静默失效。
    """
    pill = make_pill()
    real_paste = paste_mod.paste
    paste_mod.paste = lambda text, hwnd: (_ for _ in ()).throw(
        OSError("模拟剪贴板被独占"))

    buf = io.StringIO()
    real_stderr, sys.stderr = sys.stderr, buf
    try:
        enter_transcribing(pill)
        pill.on_complete("异常路径的一句话", None)
        ck("非预期异常之后状态回到 IDLE",
           pill.phase is app_main.Phase.IDLE, "实际 %s" % pill.phase)
        ck("文字仍进了待取队列", pill._pending_count() == 1,
           "实际 %d" % pill._pending_count())
        text = buf.getvalue()
        ck("异常被记进日志（不是静默吞掉）", "[粘贴失败] 非预期异常" in text)
        ck("日志里有原始异常类型", "OSError" in text)
    finally:
        sys.stderr = real_stderr
        paste_mod.paste = real_paste


def check_normal_delivery_is_untouched(ck: Checker) -> None:
    """正常交付不能被护栏改味：照旧入队、照旧收尾。"""
    pill = make_pill()
    real_paste = paste_mod.paste
    pasted = []
    paste_mod.paste = lambda text, hwnd: pasted.append(text)

    buf = io.StringIO()
    real_stderr, sys.stderr = sys.stderr, buf
    try:
        enter_transcribing(pill)
        pill.on_complete("正常的一句话", None)
        ck("粘贴被调用了一次", pasted == ["正常的一句话"], "实际 %r" % (pasted,))
        ck("状态回到 IDLE", pill.phase is app_main.Phase.IDLE)
        ck("文字进了待取队列", pill._pending_count() == 1)
        ck("没有误报停滞", "[护栏]" not in buf.getvalue())
    finally:
        sys.stderr = real_stderr
        paste_mod.paste = real_paste


def main() -> int:
    ck = Checker()
    print("=== 交付护栏故障注入自测（真状态机）===")
    for fn in (check_timeout_is_upstream_value,
               check_stuck_paste_unlocks_and_spares_new_session,
               check_unexpected_paste_exception_still_unlocks,
               check_normal_delivery_is_untouched):
        print("\n[%s]" % fn.__doc__.strip().splitlines()[0])
        fn(ck)
    print("\n%s（%d 项失败）" % ("✅ 全部通过" if not ck.failed else "❌ 有失败",
                              ck.failed))
    return 1 if ck.failed else 0


if __name__ == "__main__":
    sys.exit(main())
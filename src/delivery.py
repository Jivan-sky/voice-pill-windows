# -*- coding: utf-8 -*-
"""交付阶段的停滞护栏与代际令牌 —— 原版 0.2.4 `DeliverySession.swift` 的等价物。

为什么要有它
------------
原版 0.2.3 有过一次真实故障，作者在 `docs/VERIFY-0.2.4.md` 里记着：

    应用正忙着、也没有转写子进程、按 Fn 完全没反应。

根因是粘贴路径上有一个**提前 return**，把状态留在了 `inserting`（忙）。而
Fn 的准入条件恰恰是"空闲才受理"（`VoicePill.swift` 的状态机），于是热键被
自己永久挡在门外。症状是"它有时候不灵"，只有重启才好——这类故障没有可见
报错，最难查。

0.2.4 的修法是三条，本模块照搬：

1. **提前返回也要解锁**：用上下文管理器保证收尾一定执行（`run()`），调用方
   再在 `finally`/`except` 里把状态收回 IDLE。
2. **停滞看门狗**：到点（默认 8 秒，同上游）作废这一次交付，强制解锁一次。
3. **代际令牌**：解锁之后用户可能立刻又开了一次录音，**迟到的旧回调绝不能
   改新会话的状态**——否则它会把新会话的 WAV 路径删掉、把它的队列清空。
   所以每个破坏性步骤前都要 `is_current(gen)`。

锁序（重要）
------------
`_lock` 只保护本模块的两个字段，**绝不在持锁时回调外部函数**：`_fire()` 先在
锁内作废本代、释放锁，然后才叫 `on_interrupt`。否则会与调用方形成环——看门狗
线程持 delivery 锁等调用方的锁，而调用方正持自己的锁读 delivery 状态。

与 UI、麦克风、Windows API 都无关，所以可以在没有窗口的进程里直接跑测试：
    .venv\\Scripts\\python.exe tools\\delivery-selftest.py
"""
from __future__ import annotations

import contextlib
import threading
from typing import Callable, Iterator, Optional


class DeliverySession:
    """一次"转写收尾 → 粘贴"交付的代际令牌与停滞看门狗。

    用法（照原版的 `DeliverySession.run`）：

        with self._delivery.run() as gen:
            ...
            if not self._delivery.is_current(gen):
                return          # 看门狗已作废本次交付，别再动状态
            paste(...)
        # 出了 with，看门狗一定已经解除

    `on_interrupt` 在看门狗到点、且本代仍然有效时被调用恰好一次，跑在
    看门狗线程上。调用方在它里面把状态收回 IDLE（VoicePill._reset）。
    """

    def __init__(self, timeout: float, on_interrupt: Callable[[], None]) -> None:
        self._timeout = float(timeout)
        self._on_interrupt = on_interrupt
        self._lock = threading.Lock()
        self._generation = 0
        self._timer: Optional[threading.Timer] = None

    # ---------- 对外 ----------

    @property
    def generation(self) -> int:
        """当前代际。只读快照，不持锁拿调用方的锁（见模块顶部锁序）。"""
        with self._lock:
            return self._generation

    def is_current(self, gen: int) -> bool:
        """这一次交付还算数吗。看门狗到点后立刻变假。"""
        with self._lock:
            return gen == self._generation

    @contextlib.contextmanager
    def run(self) -> Iterator[int]:
        """占一代并武装看门狗；退出时一定解除。"""
        gen = self._begin()
        try:
            yield gen
        finally:
            self._disarm()

    # ---------- 内部 ----------

    def _begin(self) -> int:
        with self._lock:
            self._disarm_locked()
            self._generation += 1
            gen = self._generation
            if self._timeout > 0:
                timer = threading.Timer(self._timeout, self._fire, args=(gen,))
                timer.daemon = True
                self._timer = timer
                timer.start()
        return gen

    def _disarm(self) -> None:
        with self._lock:
            self._disarm_locked()

    def _disarm_locked(self) -> None:
        """调用方必须已持锁。"""
        timer, self._timer = self._timer, None
        if timer is not None:
            timer.cancel()

    def _fire(self, gen: int) -> None:
        with self._lock:
            if gen != self._generation:
                return                    # 这一代早已收尾，看门狗是迟到的
            self._generation += 1         # 作废本代：is_current(gen) 立刻变假
            self._disarm_locked()
        # 出了锁再回调——调用方会去拿它自己的锁，不能在这儿交叉持有
        self._on_interrupt()
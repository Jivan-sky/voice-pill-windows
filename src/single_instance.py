# -*- coding: utf-8 -*-
"""单实例互斥 + 优雅退出信号。

为什么需要
----------
本工具抢的是三样**全机唯一**的东西：全局键盘钩子、麦克风、前台窗口的
Ctrl+V。两个实例同时驻留时，一次按住 Fn 会被两个进程各自完整地走一遍——
各自采音（WASAPI 共享模式两个进程都收得到）、各自转写、各自粘贴，
**同一句话粘两遍**。这不是推演：实测同秒启动的两个实例会写同一个
`rec-<秒>.log`，且两边都采到了声音。

所以「同一时刻只允许一个会录音的实例」是硬约束。用命名互斥体
（`CreateMutexW`）来守——互斥体由内核维护，进程无论怎么死（崩溃、被任务
管理器结束、断电）都会自动释放，不会像锁文件那样留下要手工清理的残留。

顺带解决「怎么让正在驻留的实例干净退出」
------------------------------------
驻留实例没有窗口，也没有控制台可以按 Ctrl+C。要升级、要改代码、要排查，
总得有个正常收摊的入口：`--stop` 置一个命名事件，驻留实例的主循环看到就
走完整退出流程（拔钩子、关悬浮条、写日志尾），而不是被任务管理器砍掉。
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

MUTEX_NAME = r"Local\VoicePill.Windows.Singleton"
QUIT_EVENT_NAME = r"Local\VoicePill.Windows.Quit"
# 看门狗自己的互斥体（见 supervise.py）。名字放在这里而不是 supervise 里：
# 主进程的 `--check` 也要问"现在有没有人守着"，两边必须用同一个名字，
# 而 single_instance 是本工程里唯一管这些内核对象的地方。
SUPERVISOR_MUTEX_NAME = r"Local\VoicePill.Windows.Supervisor"

ERROR_ALREADY_EXISTS = 183
SYNCHRONIZE = 0x00100000        # OpenMutexW 要的访问权：够"看一眼"就行，不含修改
WAIT_OBJECT_0 = 0x00000000

kernel32.CreateMutexW.restype = wintypes.HANDLE
kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL,
                                  wintypes.LPCWSTR]
kernel32.OpenMutexW.restype = wintypes.HANDLE
kernel32.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateEventW.restype = wintypes.HANDLE
kernel32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL,
                                  wintypes.BOOL, wintypes.LPCWSTR]
kernel32.SetEvent.argtypes = [wintypes.HANDLE]
kernel32.SetEvent.restype = wintypes.BOOL
kernel32.ReleaseMutex.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
kernel32.WaitForSingleObject.restype = wintypes.DWORD


class InstanceLock:
    """命名互斥体包装。`acquire()` 返回 False 表示已有实例在跑。"""

    def __init__(self, name: str = MUTEX_NAME) -> None:
        self._name = name
        self._handle = None

    def acquire(self) -> bool:
        """拿锁。**非阻塞**：已经有实例就立刻返回 False，不等待。"""
        ctypes.set_last_error(0)
        handle = kernel32.CreateMutexW(None, True, self._name)
        err = ctypes.get_last_error()
        if not handle:
            raise OSError(err, "CreateMutexW 失败")
        if err == ERROR_ALREADY_EXISTS:
            # 内核把已有互斥体的句柄给了我们，但所有权不在我们手上。
            # 不关掉它，就等于我们替它多留了一个引用。
            kernel32.CloseHandle(handle)
            return False
        self._handle = handle
        return True

    @property
    def held(self) -> bool:
        return self._handle is not None

    def exists(self) -> bool:
        """这把锁此刻**有没有人在拿**（别人也算）。纯探测：只 OpenMutex，不抢所有权。

        为什么不能用 held 判：held 说的是「我自己拿到没拿到」，问的是自己；这里要问
        的是「别人正在服务吗」。

        为什么不能用 acquire() 判：那把没人在的时候会**把锁拿过来**——探一下就顺手
        占住，真正该驻留的那只反而会以「已有实例」退出去。看门狗每轮都要探一次
        （见 supervise.py 的 resident_alive），必须挑不产生副作用的那条路。

        名字下没有任何对象时返回 False（对象随最后一个句柄关闭而销毁，所以「没人拿」
        和「对象不在」是同一件事）。
        """
        handle = kernel32.OpenMutexW(SYNCHRONIZE, False, self._name)
        if not handle:
            return False
        kernel32.CloseHandle(handle)
        return True

    def release(self) -> None:
        if self._handle is None:
            return
        kernel32.ReleaseMutex(self._handle)
        kernel32.CloseHandle(self._handle)
        self._handle = None


def open_quit_event() -> int:
    """驻留实例进入主循环前调用一次，拿到要监视的事件句柄。

    不需要 Reset：事件对象随最后一个句柄关闭而销毁，所以新起的实例拿到的
    必定是全新的、未置位的对象。唯一还握着旧句柄的情形是 `/--stop` 正在
    执行，而那种情况下本来就该退出。
    """
    handle = kernel32.CreateEventW(None, True, False, QUIT_EVENT_NAME)
    if not handle:
        raise OSError(ctypes.get_last_error(), "CreateEventW 失败")
    return handle


def close_handle(handle: int) -> None:
    if handle:
        kernel32.CloseHandle(handle)


def quit_requested(handle: int) -> bool:
    """非阻塞地看一眼事件有没有被置位。主循环每 0.5 秒问一次。"""
    return kernel32.WaitForSingleObject(handle, 0) == WAIT_OBJECT_0


def request_quit() -> bool:
    """`--stop` 用。返回是否真的置上了位。"""
    handle = kernel32.CreateEventW(None, True, False, QUIT_EVENT_NAME)
    if not handle:
        return False
    ok = bool(kernel32.SetEvent(handle))
    kernel32.CloseHandle(handle)
    return ok

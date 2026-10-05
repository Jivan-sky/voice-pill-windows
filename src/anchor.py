# -*- coding: utf-8 -*-
"""锚点：把驻留进程的命，绑在 Codex 上。

为什么需要它
------------
用户要的形态是「开着 Codex，按 Fn 就有反应；关了 Codex，它自己收摊」。前
一半由 SessionStart 钩子拉起（插件侧入口 voicepill.exe session-start），
后一半在这里：钩子顺手把自己「在谁的子孙里」查出来落成一张小纸条，看门狗
（supervise.py）照着纸条盯人，人一没就走收摊流程。

为什么不是「盯父进程」了事
--------------------------
钩子是 `hook-session-start.cmd` 拉起来的，直接父进程是 cmd.exe，再往上才是
Codex。必须沿父链往上走，取**最外层的**那一个 Codex 进程：

    python → cmd.exe → codex.exe(本次会话的运行时) → ChatGPT.exe(桌面壳)

最外层才是「Codex 关没关」的判据。里面那个 codex.exe 只活在一次会话里，
盯它会在会话切换时误关能力——而能力要的是常驻。

为什么落纸条（anchor.json），而不是启动时用参数把 PID 传进去
------------------------------------------------------------
因为「开机自启」这条路（autostart.py 的启动文件夹快捷方式）**已经在登录时
就把看门狗拉起来了**，那会儿 Codex 还没开。看门狗必须能先活着、等 Codex
起来之后再「后挂」上去。参数是一次性的，纸条可以改，所以用纸条。

纸条靠不住怎么办（读不到 / 是上一个 Codex 留下的 / PID 被复用）
--------------------------------------------------------------
一律当作「暂时没有锚点」**照常守着**。能力本身比绑生命周期重要，绑不上只是
退回到「常驻到天荒地老」的老行为。绝不因为纸条读不出来就不干活。

PID 复用
--------
PID 会被系统回收再利用，几小时后「同一个号」很可能已经是别的进程。所以
认人不认号：`OpenProcess` 拿到的句柄会**钉住内核里那个进程对象**，编号再被
复用也影响不到它。开句柄时顺手核对一次映像路径，防止一上来就认错人。
判活用 `WaitForSingleObject` 挂上去，不轮询、不吃 CPU。
"""
from __future__ import annotations

import ctypes
import json
import os
import time
from ctypes import wintypes

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
ntdll = ctypes.WinDLL("ntdll")

import config                        # noqa: E402  （只为 app_dir()）

# 认得出来的「Codex」映像名。codex.exe 是本次会话的运行时，ChatGPT.exe 是它
# 上面的桌面壳（实测父链：codex.exe 的父就是 ChatGPT.exe）。
CODEX_IMAGE_NAMES = ("codex.exe", "chatgpt.exe")

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SYNCHRONIZE = 0x00100000
WAIT_TIMEOUT = 0x00000102
STILL_ACTIVE = 259

MAX_HOPS = 16                        # 父链最多往上走几层，防止异常链把循环卡住

kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
kernel32.WaitForSingleObject.restype = wintypes.DWORD
kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
    ctypes.POINTER(wintypes.DWORD)]
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
# 句柄是 64 位的：不声明 argtypes，ctypes 会按 32 位 c_int 传，高 32 位被截断。
ntdll.NtQueryInformationProcess.argtypes = [
    wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.ULONG,
    ctypes.POINTER(wintypes.ULONG)]
ntdll.NtQueryInformationProcess.restype = ctypes.c_long


class _ProcessBasicInformation(ctypes.Structure):
    """NtQueryInformationProcess(ProcessBasicInformation) 要的结构。

    只关心最后那个父 PID，但 ctypes 必须把前面的字段都摆出来才能对齐。
    """

    _fields_ = [("Reserved1", wintypes.DWORD),
                ("PebBaseAddress", ctypes.c_void_p),
                ("Reserved2", ctypes.c_void_p * 2),
                ("UniqueProcessId", ctypes.c_void_p),
                ("InheritedFromUniqueProcessId", ctypes.c_void_p)]


def process_image(pid: int) -> str:
    """进程的完整映像路径。拿不到（不存在 / 是系统进程）返回空串。"""
    if pid <= 0:
        return ""
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buf,
                                                   ctypes.byref(size)):
            return ""
        return buf.value
    finally:
        kernel32.CloseHandle(handle)


def process_parent(pid: int) -> int:
    """父进程 PID。拿不到返回 0。"""
    if pid <= 0:
        return 0
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return 0
    try:
        info = _ProcessBasicInformation()
        written = wintypes.ULONG()
        status = ntdll.NtQueryInformationProcess(
            handle, 0, ctypes.byref(info), ctypes.sizeof(info),
            ctypes.byref(written))
        if status != 0:
            return 0
        return int(info.InheritedFromUniqueProcessId or 0)
    finally:
        kernel32.CloseHandle(handle)


def ancestors(start_pid: int = None, hops: int = MAX_HOPS) -> list:
    """从 start_pid（默认自己）往上，返回 [(pid, 映像路径), ...]，近的在前。"""
    pid = os.getpid() if start_pid is None else int(start_pid)
    out = []
    seen = set()
    for _ in range(hops):
        if pid <= 0 or pid in seen:
            break
        seen.add(pid)
        parent = process_parent(pid)
        if parent <= 0:
            break
        out.append((parent, process_image(parent)))
        pid = parent
    return out


def is_codex_image(path: str) -> bool:
    """映像路径是不是 Codex 家族（按文件名比，不管装在哪）。"""
    return os.path.basename(path or "").lower() in CODEX_IMAGE_NAMES


def find_anchor(start_pid: int = None):
    """找「最外层的 Codex 进程」，返回 (pid, 映像路径)；找不到返回 None。"""
    matches = [(pid, image) for pid, image in ancestors(start_pid)
               if is_codex_image(image)]
    return matches[-1] if matches else None


class Anchor:
    """一个进程的句柄。拿着它，PID 被复用也认不错人。"""

    def __init__(self) -> None:
        self._handle = None
        self._pid = 0
        self._image = ""

    @property
    def pid(self) -> int:
        return self._pid

    @property
    def attached(self) -> bool:
        return self._handle is not None

    def attach(self, pid: int, image: str = "") -> bool:
        """认下这个进程。映像路径对不上（多半是 PID 被复用了）就拒收。"""
        self.detach()
        if pid <= 0:
            return False
        handle = kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, False, pid)
        if not handle:
            return False
        actual = process_image(pid)
        if not actual or (image and actual.lower() != image.lower()):
            kernel32.CloseHandle(handle)
            return False
        self._handle = handle
        self._pid = pid
        self._image = actual
        return True

    def alive(self) -> bool:
        """非阻塞看一眼还在不在。进程对象在它终止的那一刻被置位。"""
        if self._handle is None:
            return False
        return kernel32.WaitForSingleObject(self._handle, 0) == WAIT_TIMEOUT

    def detach(self) -> None:
        if self._handle is not None:
            kernel32.CloseHandle(self._handle)
        self._handle = None
        self._pid = 0
        self._image = ""


# ---------- 纸条（钩子写给看门狗的那张小抄）----------

def note_path() -> str:
    """纸条位置。跟 settings.json 放一起（同一个 app_dir）。"""
    return os.path.join(config.app_dir(), "anchor.json")


def write_note(pid: int, image: str) -> bool:
    """把锚点写下来。内容没变就一个字节都不动（免得白刷 mtime）。"""
    if pid <= 0:
        return False
    image = image or ""
    path = note_path()
    try:
        if read_note() == (int(pid), image):
            return True
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"pid": int(pid), "image": image, "at": time.time()}, fh)
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def read_note():
    """读纸条。读不到 / 格式不对返回 None（调用方要当「没有锚点」处理）。"""
    try:
        with open(note_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            return None
        pid = int(data.get("pid") or 0)
        image = str(data.get("image") or "")
    except (OSError, ValueError, TypeError, AttributeError):
        return None
    return (pid, image) if pid > 0 else None

# ---------- 停用纸条（看门狗写给哨兵的那张）----------

def stop_note_path() -> str:
    """停用纸条位置。跟 anchor.json 放一起（同一个 app_dir）。"""
    return os.path.join(config.app_dir(), "stopped.json")


def write_stop_note(pid: int) -> bool:
    """看门狗**自己决定**收摊时留一张纸。

    只在一个地方调：看门狗看到子进程**正常退出（code 0）**——那来自 `--stop`
    或 Ctrl+C，是用户明确要停。别的下场（崩了、被外力打死、连崩到放弃、认出
    "已经有一只驻留在跑"）一律不写：留错纸会让哨兵从此不敢补拉，比不留危险得多
    （2026-10-06 实测踩过）。所以「这张纸在不在」正好把「用户的决定」和「崩了」
    分开，哨兵补拉之前先看它（见 go/.../engine.StoppedByUser）。

    pid 记的是当时绑着的那个 Codex；没绑就写 0。哨兵那边判的是**两张纸的先后**，
    不是 pid 相等（pid 会被复用），所以这里只做记录。
    """
    path = stop_note_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"pid": int(pid or 0), "at": time.time()}, fh)
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def read_stop_note():
    """读停用纸条，返回 (pid, at)；读不到 / 格式不对返回 None。"""
    try:
        with open(stop_note_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            return None
        return (int(data.get("pid") or 0), float(data.get("at") or 0.0))
    except (OSError, ValueError, TypeError):
        return None

def status_line() -> str:
    """给 `main.py --check` 用的一句话：现在绑的是谁、还作不作数。"""
    note = read_note()
    if note is None:
        return "无（没绑 Codex，会一直常驻）"
    pid, image = note
    actual = process_image(pid)
    name = os.path.basename(image or actual or "?")
    if not actual:
        return "⚠️  纸条是 pid=%d（%s），现在不在了——上次 Codex 留下的" % (pid, name)
    if image and actual.lower() != image.lower():
        return "⚠️  pid=%d 现在是 %s，不是 %s（PID 被复用了）" % (
            pid, os.path.basename(actual), name)
    return "✅ Codex pid=%d（%s）" % (pid, name)

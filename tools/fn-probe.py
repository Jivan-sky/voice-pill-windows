# -*- coding: utf-8 -*-
"""
Fn 键探针 —— 检测 Windows 能否在 OS 层看到 Fn 键。

背景：Fn 由键盘固件/EC 处理，多数机器不向 OS 上报。本脚本用两条独立通道同时监听：

  通道 1   Raw Input (WM_INPUT)          HID 原始上报。厂商自定义的 Fn 报告最可能从这里冒出来。
  通道 2   WH_KEYBOARD_LL 低级钩子        常规扫描码路径。

用法：
    python fn-probe.py [监听秒数，默认 30]

操作：脚本跑起来后依次按
    Fn（单独按，多按几次）
    Fn + F1 ~ F12
    Fn + 方向键
    Fn + Esc（有些机器这是 Fn Lock 开关）
    A、B 两个普通字母键（对照组，证明探针本身在工作）

结束时打印去重汇总。

无需管理员权限。
"""
import ctypes
import os
import sys
import time
from ctypes import wintypes

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))
import console                     # noqa: E402

console.make_output_safe()         # 管道下打印 ⚠️/❌ 会 GBK 崩，见 console.py

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT,
                             wintypes.WPARAM, wintypes.LPARAM)

# ---------- 常量 ----------
WM_INPUT = 0x00FF
WM_KEYDOWN, WM_KEYUP = 0x0100, 0x0101
WM_SYSKEYDOWN, WM_SYSKEYUP = 0x0104, 0x0105
WM_DESTROY = 0x0002

RID_INPUT = 0x10000003
RIDEV_INPUTSINK = 0x00000100
RIDEV_PAGEONLY = 0x00000020

RIM_TYPEMOUSE, RIM_TYPEKEYBOARD, RIM_TYPEHID = 0, 1, 2

RI_KEY_MAKE, RI_KEY_BREAK, RI_KEY_E0, RI_KEY_E1 = 0x00, 0x01, 0x02, 0x04

WH_KEYBOARD_LL = 13
LLKHF_EXTENDED = 0x01
LLKHF_INJECTED = 0x10

HWND_MESSAGE = wintypes.HWND(-3)

# ---------- 运行开关（由命令行设置）----------
# 默认两条通道都只记「按下」——噪声小、够定位 Fn 的扫描码。
# 但要不要给 Fn 做「按住说话」，取决于它有没有**松开**事件，
# 所以需要一组能看到 KEYUP 的开关。
SHOW_KEYUP = False        # 记「松开」事件
ONLY_SCAN = None          # 只记这个扫描码（int），None = 全记
MARK_DOWN, MARK_UP = "[按下]", "[松开]"


# ---------- 结构体 ----------
class RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [("usUsagePage", wintypes.USHORT),
                ("usUsage", wintypes.USHORT),
                ("dwFlags", wintypes.DWORD),
                ("hwndTarget", wintypes.HWND)]


class RAWINPUTHEADER(ctypes.Structure):
    _fields_ = [("dwType", wintypes.DWORD),
                ("dwSize", wintypes.DWORD),
                ("hDevice", wintypes.HANDLE),
                ("wParam", wintypes.WPARAM)]


class RAWKEYBOARD(ctypes.Structure):
    _fields_ = [("MakeCode", wintypes.USHORT),
                ("Flags", wintypes.USHORT),
                ("Reserved", wintypes.USHORT),
                ("VKey", wintypes.USHORT),
                ("Message", wintypes.UINT),
                ("ExtraInformation", wintypes.ULONG)]


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT),
                ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE),
                ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR)]


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class MSG(ctypes.Structure):
    _fields_ = [("hwnd", wintypes.HWND),
                ("message", wintypes.UINT),
                ("wParam", wintypes.WPARAM),
                ("lParam", wintypes.LPARAM),
                ("time", wintypes.DWORD),
                ("pt", POINT)]


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wintypes.DWORD),
                ("scanCode", wintypes.DWORD),
                ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_void_p)]


# ---------- 函数签名（64 位下必须显式声明，否则指针被截断）----------
user32.DefWindowProcW.restype = LRESULT
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                  wintypes.WPARAM, wintypes.LPARAM]
user32.CreateWindowExW.restype = wintypes.HWND
user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR,
                                   wintypes.LPCWSTR, wintypes.DWORD,
                                   ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int,
                                   wintypes.HWND, wintypes.HMENU,
                                   wintypes.HINSTANCE, wintypes.LPVOID]
user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
user32.RegisterRawInputDevices.argtypes = [ctypes.POINTER(RAWINPUTDEVICE),
                                           wintypes.UINT, wintypes.UINT]
user32.GetRawInputData.argtypes = [wintypes.HANDLE, wintypes.UINT,
                                   wintypes.LPVOID,
                                   ctypes.POINTER(wintypes.UINT),
                                   wintypes.UINT]
user32.GetRawInputData.restype = wintypes.UINT
user32.SetWindowsHookExW.restype = wintypes.HHOOK
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, wintypes.LPVOID,
                                     wintypes.HINSTANCE, wintypes.DWORD]
user32.CallNextHookEx.restype = LRESULT
user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int,
                                  wintypes.WPARAM, wintypes.LPARAM]
user32.PeekMessageW.argtypes = [ctypes.POINTER(MSG), wintypes.HWND,
                                wintypes.UINT, wintypes.UINT, wintypes.UINT]
user32.PeekMessageW.restype = wintypes.BOOL
user32.TranslateMessage.argtypes = [ctypes.POINTER(MSG)]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(MSG)]
user32.DispatchMessageW.restype = LRESULT
kernel32.GetModuleHandleW.restype = wintypes.HMODULE
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]

# ---------- 采集 ----------
events = []          # (时刻, 通道, 描述)
t0 = time.time()


def stamp():
    return "%.2fs" % (time.time() - t0)


def record(channel, desc):
    events.append((stamp(), channel, desc))


def vk_name(vk):
    """把部分虚拟键码翻译成人话，方便看对照组的 A/B 键。"""
    if 0x41 <= vk <= 0x5A:
        return chr(vk)
    if 0x30 <= vk <= 0x39:
        return chr(vk)
    if 0x70 <= vk <= 0x87:
        return "F%d" % (vk - 0x6F)
    return {0x1B: "Esc", 0x20: "Space", 0x25: "Left", 0x26: "Up",
            0x27: "Right", 0x28: "Down", 0x00: "(none)"}.get(vk, "")


def describe_raw(lparam):
    """解析 WM_INPUT，返回一行描述；无法解析返回 None。"""
    size = wintypes.UINT(0)
    user32.GetRawInputData(wintypes.HANDLE(lparam), RID_INPUT, None,
                           ctypes.byref(size), ctypes.sizeof(RAWINPUTHEADER))
    if size.value == 0:
        return None
    buf = ctypes.create_string_buffer(size.value)
    got = user32.GetRawInputData(wintypes.HANDLE(lparam), RID_INPUT, buf,
                                 ctypes.byref(size), ctypes.sizeof(RAWINPUTHEADER))
    if got == 0xFFFFFFFF:
        return None

    hdr = ctypes.cast(buf, ctypes.POINTER(RAWINPUTHEADER)).contents
    base = ctypes.sizeof(RAWINPUTHEADER)

    if hdr.dwType == RIM_TYPEKEYBOARD:
        kb = ctypes.cast(ctypes.byref(buf, base),
                         ctypes.POINTER(RAWKEYBOARD)).contents
        if kb.Message not in (WM_KEYDOWN, WM_KEYUP, WM_SYSKEYDOWN, WM_SYSKEYUP):
            return None
        is_up = kb.Message in (WM_KEYUP, WM_SYSKEYUP)
        if is_up and not SHOW_KEYUP:
            return None                      # 默认只报按下，减少噪声
        if ONLY_SCAN is not None and kb.MakeCode != ONLY_SCAN:
            return None
        ext = "E0" if (kb.Flags & RI_KEY_E0) else "  "
        e1 = "E1" if (kb.Flags & RI_KEY_E1) else "  "
        name = vk_name(kb.VKey)
        return ("[RAW 键盘] %s scancode=0x%02X %s%s vkCode=0x%02X %-5s msg=0x%04X"
                % (MARK_UP if is_up else MARK_DOWN, kb.MakeCode, ext, e1,
                   kb.VKey, name, kb.Message))

    if hdr.dwType == RIM_TYPEHID:
        # HID 设备：前 8 字节是 dwSizeHid + dwCount，其后是原始报文
        if size.value < base + 8:
            return None
        raw_hdr = ctypes.cast(ctypes.byref(buf, base),
                              ctypes.POINTER(wintypes.DWORD * 2)).contents
        size_hid, count = raw_hdr[0], raw_hdr[1]
        start = base + 8
        end = min(start + size_hid * count, size.value)
        payload = bytes(buf[start:end])
        if not payload:
            return None
        return "[RAW HID ] hDevice=%d  sizeHid=%d  report=%s" % (
            int(hdr.hDevice or 0), size_hid, payload.hex(" ").upper())

    return None


hook_proc_ref = None      # 防止回调被 GC


def make_hook_proc():
    def proc(nCode, wParam, lParam):
        down = wParam in (WM_KEYDOWN, WM_SYSKEYDOWN)
        up = wParam in (WM_KEYUP, WM_SYSKEYUP)
        if nCode == 0 and (down or (up and SHOW_KEYUP)):
            kb = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            if (not (kb.flags & LLKHF_INJECTED)
                    and (ONLY_SCAN is None or kb.scanCode == ONLY_SCAN)):
                ext = "E0" if (kb.flags & LLKHF_EXTENDED) else "  "
                name = vk_name(kb.vkCode)
                record("HOOK", "[HOOK     ] %s scancode=0x%02X %s vkCode=0x%02X %-5s"
                       % (MARK_UP if up else MARK_DOWN, kb.scanCode, ext,
                          kb.vkCode, name))
        return user32.CallNextHookEx(None, nCode, wParam, lParam)
    return KBDLLHOOKSTRUCT, proc


wndproc_ref = None


def make_wndproc():
    def proc(hwnd, msg, wparam, lparam):
        if msg == WM_INPUT:
            line = describe_raw(lparam)
            if line:
                record("RAW", line)
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)
    return proc


def main():
    global wndproc_ref, hook_proc_ref

    global SHOW_KEYUP, ONLY_SCAN

    duration = 30
    rest = sys.argv[1:]
    skip = False
    for i, a in enumerate(rest):
        if skip:
            skip = False
            continue
        if a == "--keyup":
            SHOW_KEYUP = True
        elif a == "--only" and i + 1 < len(rest):
            try:
                ONLY_SCAN = int(rest[i + 1], 0)      # 支持 0x63 / 99
            except ValueError:
                pass
            skip = True          # 别把 "63" 这种纯数字参数又当成时长
        elif a.isdigit():
            duration = int(a)

    if SHOW_KEYUP:
        print("（已开启「松开」事件记录%s）"
              % ("" if ONLY_SCAN is None
                 else "，只过滤扫描码 0x%02X" % ONLY_SCAN))

    hinst = kernel32.GetModuleHandleW(None)
    cls_name = "VoicePillFnProbe"

    wndproc_ref = WNDPROC(make_wndproc())
    wc = WNDCLASSW()
    wc.lpfnWndProc = wndproc_ref
    wc.hInstance = hinst
    wc.lpszClassName = cls_name
    if not user32.RegisterClassW(ctypes.byref(wc)):
        err = ctypes.get_last_error()
        # 类已注册（重复运行）不算致命
        if err not in (0, 1410):
            print("RegisterClassW 失败, err=%d" % err)
            return 1

    # 普通隐藏窗口。不用 message-only 窗口——它不在输入树里，收不到 WM_INPUT。
    hwnd = user32.CreateWindowExW(0, cls_name, "fn-probe", 0,
                                  0, 0, 0, 0, None, None, hinst, None)
    if not hwnd:
        print("CreateWindowExW 失败, err=%d" % ctypes.get_last_error())
        return 1

    devs = (RAWINPUTDEVICE * 2)(
        RAWINPUTDEVICE(0x01, 0x00, RIDEV_INPUTSINK | RIDEV_PAGEONLY, hwnd),  # 通用桌面页
        RAWINPUTDEVICE(0x0C, 0x00, RIDEV_INPUTSINK | RIDEV_PAGEONLY, hwnd),  # 消费类控制页
    )
    if not user32.RegisterRawInputDevices(devs, 2, ctypes.sizeof(RAWINPUTDEVICE)):
        print("RegisterRawInputDevices 失败, err=%d  —— 继续，仅用钩子通道"
              % ctypes.get_last_error())

    hook_cb = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM,
                                 wintypes.LPARAM)(make_hook_proc()[1])
    hook_proc_ref = hook_cb
    hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, hook_cb, hinst, 0)
    if not hook:
        print("SetWindowsHookExW 失败, err=%d  —— 继续，仅用 Raw Input 通道"
              % ctypes.get_last_error())

    print("=" * 72)
    print("Fn 探针运行中，监听 %d 秒。" % duration)
    print("请依次按：")
    print("  1) Fn 单独按 5 次以上")
    print("  2) Fn + F1 / F2 / F3")
    print("  3) Fn + 方向键")
    print("  4) Fn + Esc")
    print("  5) 普通字母 A 和 B 各按一次（对照组，证明探针在工作）")
    print("=" * 72)
    print()

    deadline = time.time() + duration
    msg = MSG()
    shown = 0                      # 已经打印过的事件条数
    while time.time() < deadline:
        # PeekMessage 轮询，避免 GetMessage 阻塞导致超时失效
        if user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):  # PM_REMOVE
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
            while shown < len(events):
                ts, ch, line = events[shown]
                print("%-7s %-5s %s" % (ts, ch, line))
                shown += 1
        else:
            time.sleep(0.01)

    print()
    print("=" * 72)
    print("汇总：共捕获 %d 条事件" % len(events))
    print("=" * 72)

    seen_hook = set()
    seen_raw = set()
    for _, ch, line in events:
        if ch == "HOOK":
            seen_hook.add(line)
        elif ch == "RAW":
            seen_raw.add(line)

    print("\n--- HOOK 通道（低级键盘钩子）去重 ---")
    if seen_hook:
        for line in sorted(seen_hook):
            print("  " + line)
    else:
        print("  （无）—— 说明钩子没收到任何按键，可能是权限或被拦截")

    print("\n--- RAW 通道（HID 原始上报）去重 ---")
    if seen_raw:
        for line in sorted(seen_raw):
            print("  " + line)
    else:
        print("  （无）")

    print("\n--- 判读 ---")
    has_letter = any(("0x41" in l or "0x42" in l or " A " in l or " B " in l)
                     for _, _, l in events)
    if not has_letter:
        print("  ⚠️  连对照组的 A/B 键都没抓到 → 探针本身没工作，结果不可信。")
        print("      可能原因：脚本被安全软件拦截，或需要在真实桌面会话下运行。")
    else:
        print("  ✅ 对照组 A/B 键已抓到 → 探针工作正常。")
        suspicious = [l for _, _, l in events
                      if "0x00 " in l or "vkCode=0x00" in l or "HID" in l]
        if suspicious:
            print("  ⚠️  出现非标准项（下方），重点看这些是不是 Fn：")
            for l in sorted(set(suspicious)):
                print("       " + l)
        else:
            print("  ❌ 未发现任何非标准上报 → 本机 Fn 键**不上报给 OS**。")
            print("      结论：无法在 Windows 应用层直接绑 Fn，需走固件级方案。")
            print("      （ThinkPad 可在 BIOS / Lenovo Vantage 改 Fn 行为，见移植方案）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

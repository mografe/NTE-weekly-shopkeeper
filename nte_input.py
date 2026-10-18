"""向 NTE 窗口发送鼠标/键盘输入。

安全约定：先确保游戏窗口真的拿到前台焦点，确认后再发点击。
否则 SendInput 会把点击送给当前前台的那个窗口（可能就是别的软件），这种错误很难察觉。
"""
import ctypes
import time
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# 让本进程 DPI 感知，坐标才和抓帧一致（3840x2160）
try:
    ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)
except Exception:                                            # noqa: BLE001
    pass

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000
MOUSEEVENTF_WHEEL = 0x0800


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG))]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG))]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
                ("wParamH", wintypes.WORD)]


class _U(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _U)]


user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = wintypes.UINT
user32.GetForegroundWindow.restype = wintypes.HWND
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.SetForegroundWindow.restype = wintypes.BOOL
user32.BringWindowToTop.argtypes = [wintypes.HWND]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
kernel32.GetCurrentThreadId.restype = wintypes.DWORD
user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]


def get_foreground():
    return user32.GetForegroundWindow()


def ensure_foreground(hwnd):
    """把游戏窗口切到前台，返回是否成功。失败时调用方绝对不能继续点击。"""
    if get_foreground() == hwnd:
        return True

    # 常规做法：ShowWindow 唤起 + SetForegroundWindow
    user32.ShowWindow(hwnd, 9)          # SW_RESTORE
    user32.BringWindowToTop(hwnd)
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.15)
    if get_foreground() == hwnd:
        return True

    # 前台锁：临时挂到当前前台窗口的输入队列上再抢
    fg = get_foreground()
    tid_fg = user32.GetWindowThreadProcessId(fg, None)
    tid_me = kernel32.GetCurrentThreadId()
    attached = False
    try:
        if tid_fg and user32.AttachThreadInput(tid_me, tid_fg, True):
            attached = True
        user32.SetForegroundWindow(hwnd)
        user32.BringWindowToTop(hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(tid_me, tid_fg, False)
    time.sleep(0.25)
    return get_foreground() == hwnd


def _send(*inputs):
    n = len(inputs)
    arr = (INPUT * n)(*inputs)
    sent = user32.SendInput(n, arr, ctypes.sizeof(INPUT))
    return sent == n


def _mouse_event(flags, dx=0, dy=0):
    inp = INPUT()
    inp.type = INPUT_MOUSE
    inp.u.mi = MOUSEINPUT(dx, dy, 0, flags, 0, None)
    return inp


def _key_event(vk, up=False):
    inp = INPUT()
    inp.type = INPUT_KEYBOARD
    inp.u.ki = KEYBDINPUT(vk, 0, KEYEVENTF_KEYUP if up else 0, 0, None)
    return inp


def virtual_screen_size():
    """返回 (x, y, w, h) 虚拟屏幕，用于绝对坐标归一化。"""
    x = user32.GetSystemMetrics(76)
    y = user32.GetSystemMetrics(77)
    w = user32.GetSystemMetrics(78)
    h = user32.GetSystemMetrics(79)
    return x, y, w, h


def move_to(screen_x, screen_y):
    vx, vy, vw, vh = virtual_screen_size()
    nx = int(round((screen_x - vx) * 65535 / max(vw - 1, 1)))
    ny = int(round((screen_y - vy) * 65535 / max(vh - 1, 1)))
    return _send(_mouse_event(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE |
                              MOUSEEVENTF_VIRTUALDESK, nx, ny))


def click(screen_x, screen_y, hwnd=None, settle=0.06, hold=0.05):
    """在屏幕坐标处点击一次。给了 hwnd 就先确认它是前台，抢不到前台就不点。"""
    if hwnd is not None and not ensure_foreground(hwnd):
        raise RuntimeError("无法把游戏窗口切到前台，已放弃点击（避免误点其它窗口）")
    move_to(screen_x, screen_y)
    time.sleep(settle)
    ok1 = _send(_mouse_event(MOUSEEVENTF_LEFTDOWN))
    time.sleep(hold)
    ok2 = _send(_mouse_event(MOUSEEVENTF_LEFTUP))
    return ok1 and ok2


def press_key(vk, hwnd=None):
    if hwnd is not None and not ensure_foreground(hwnd):
        raise RuntimeError("无法把游戏窗口切到前台，已放弃按键")
    ok1 = _send(_key_event(vk))
    time.sleep(0.04)
    ok2 = _send(_key_event(vk, up=True))
    return ok1 and ok2


def wheel(delta, hwnd=None, settle=0.05):
    """滚轮：delta>0 向上滚。用在关卡列表被滚动时滚回顶部。"""
    if hwnd is not None and not ensure_foreground(hwnd):
        return False
    inp = INPUT()
    inp.type = INPUT_MOUSE
    inp.u.mi = MOUSEINPUT(0, 0, delta & 0xFFFFFFFF, MOUSEEVENTF_WHEEL, 0, None)
    ok = _send(inp)
    time.sleep(settle)
    return ok


VK_ESCAPE = 0x1B
VK_SPACE = 0x20
VK_F = 0x46

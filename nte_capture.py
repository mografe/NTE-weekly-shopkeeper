"""NTE (异环) 窗口定位与抓帧模块。

用法:
    python nte_capture.py                 # 抓一帧存 frame.png
    python nte_capture.py --out a.png
    python nte_capture.py --list          # 只列出窗口

作为模块:
    from nte_capture import find_game_window, grab
    hwnd = find_game_window()
    img = grab(hwnd)          # numpy BGR (H, W, 3)
"""
import argparse
import ctypes
import sys
from ctypes import wintypes

import numpy as np

TITLE_KEY = "异环"

# 必须在任何窗口/DC 调用之前设成 per-monitor DPI aware。
# 否则进程被 DPI 虚拟化：GetClientRect 返回逻辑像素（4K@150% 下是 2560x1440），
# 而游戏真实客户区是 3840x2160，抓出来的帧只有左上角一块。
try:
    # PROCESS_PER_MONITOR_DPI_AWARE = 2 (Windows 8.1+)
    ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.WinDLL("user32").SetProcessDPIAware()
    except Exception:
        pass

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

SRCCOPY = 0x00CC0020
DIB_RGB_COLORS = 0
BI_RGB = 0

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


class RECT(ctypes.Structure):
    _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                ("right", wintypes.LONG), ("bottom", wintypes.LONG)]


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
user32.EnumWindows.restype = wintypes.BOOL
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(POINT)]
user32.GetDC.argtypes = [wintypes.HWND]
user32.GetDC.restype = wintypes.HDC
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.GetForegroundWindow.restype = wintypes.HWND
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.BitBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                         ctypes.c_int, wintypes.HDC, ctypes.c_int, ctypes.c_int,
                         wintypes.DWORD]
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wintypes.HDC]
gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT,
                            wintypes.UINT, ctypes.c_void_p,
                            ctypes.POINTER(BITMAPINFO), wintypes.UINT]
gdi32.StretchBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int,
                             ctypes.c_int, ctypes.c_int, wintypes.HDC,
                             ctypes.c_int, ctypes.c_int, ctypes.c_int,
                             ctypes.c_int, wintypes.DWORD]
gdi32.SetStretchBltMode.argtypes = [wintypes.HDC, ctypes.c_int]
gdi32.SetBrushOrgEx.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int,
                                ctypes.c_void_p]

HALFTONE = 4
COLORONCOLOR = 3


def list_windows(key=None):
    """返回 [(hwnd, title), ...]，key 为标题子串过滤。"""
    found = []

    def _cb(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            n = user32.GetWindowTextLengthW(hwnd)
            if n > 0:
                buf = ctypes.create_unicode_buffer(n + 2)
                user32.GetWindowTextW(hwnd, buf, n + 2)
                if key is None or key in buf.value:
                    found.append((hwnd, buf.value))
        return True

    user32.EnumWindows(WNDENUMPROC(_cb), 0)
    return found


def find_game_window(key=TITLE_KEY):
    """按标题子串找游戏主窗口句柄，找不到返回 None。"""
    wins = list_windows(key)
    if not wins:
        return None
    # 多个候选时取面积最大的
    best, best_area = None, -1
    for hwnd, _title in wins:
        r = RECT()
        if user32.GetWindowRect(hwnd, ctypes.byref(r)):
            area = (r.right - r.left) * (r.bottom - r.top)
            if area > best_area:
                best, best_area = hwnd, area
    return best


def client_rect(hwnd):
    """返回客户区 (x, y, w, h)，x/y 为屏幕坐标。"""
    cr = RECT()
    user32.GetClientRect(hwnd, ctypes.byref(cr))
    pt = POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(pt))
    return pt.x, pt.y, cr.right, cr.bottom


def is_foreground(hwnd):
    return user32.GetForegroundWindow() == hwnd


def grab(hwnd):
    """抓取窗口客户区，返回 numpy BGR 数组 (H, W, 3)。"""
    x, y, w, h = client_rect(hwnd)
    return grab_screen_rect(x, y, w, h)


def grab_region(hwnd, rx, ry, rw, rh):
    """只抓客户区内的一块区域 (rx, ry, rw, rh)，用于高频轮询。

    整屏 3840x2160 一次约 90ms，只抓需求气泡那一小片能降到 10ms 以内，
    轮询反应速度差一个数量级。
    """
    x, y, _w, _h = client_rect(hwnd)
    return grab_screen_rect(x + rx, y + ry, rw, rh)


def grab_region_scaled(hwnd, rx, ry, rw, rh, scale=0.5, mode=None):
    """抓客户区内一块区域并直接降采样（用 StretchBlt，省掉抓全尺寸的开销）。

    覆盖整个客座区（3840x1030）时 1:1 抓取要 ~116ms，0.5 倍只要 ~25ms，
    对「需求有时限」的场景差别很大。返回 (图像, (实际缩放比x, 实际缩放比y))。
    """
    x, y, _w, _h = client_rect(hwnd)
    dw = max(1, int(round(rw * scale)))
    dh = max(1, int(round(rh * scale)))
    if mode is None:
        mode = HALFTONE

    screen_dc = user32.GetDC(None)
    if not screen_dc:
        raise RuntimeError("GetDC 失败")
    mem_dc = gdi32.CreateCompatibleDC(screen_dc)
    bmp = gdi32.CreateCompatibleBitmap(screen_dc, dw, dh)
    old = gdi32.SelectObject(mem_dc, bmp)
    try:
        gdi32.SetStretchBltMode(mem_dc, mode)
        # HALFTONE 模式要求先设置刷子原点，否则画面会有偏移
        gdi32.SetBrushOrgEx(mem_dc, 0, 0, None)
        ok = gdi32.StretchBlt(mem_dc, 0, 0, dw, dh, screen_dc,
                              x + rx, y + ry, rw, rh, SRCCOPY)
        if not ok:
            raise RuntimeError(f"StretchBlt 失败, err={ctypes.get_last_error()}")

        bi = BITMAPINFO()
        bi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bi.bmiHeader.biWidth = dw
        bi.bmiHeader.biHeight = -dh
        bi.bmiHeader.biPlanes = 1
        bi.bmiHeader.biBitCount = 32
        bi.bmiHeader.biCompression = BI_RGB

        buf = ctypes.create_string_buffer(dw * dh * 4)
        got = gdi32.GetDIBits(mem_dc, bmp, 0, dh, buf, ctypes.byref(bi),
                              DIB_RGB_COLORS)
        if got != dh:
            raise RuntimeError(f"GetDIBits 行数异常: {got}/{dh}")
        arr = np.frombuffer(buf, dtype=np.uint8).reshape(dh, dw, 4)
        return np.ascontiguousarray(arr[:, :, :3]), (dw / float(rw), dh / float(rh))
    finally:
        gdi32.SelectObject(mem_dc, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(None, screen_dc)


def grab_screen_rect(x, y, w, h):
    """从屏幕指定矩形抓图，返回 BGR 数组。"""
    if w <= 0 or h <= 0:
        raise RuntimeError(f"抓取尺寸异常: {w}x{h}")

    screen_dc = user32.GetDC(None)
    if not screen_dc:
        raise RuntimeError("GetDC 失败")
    mem_dc = gdi32.CreateCompatibleDC(screen_dc)
    bmp = gdi32.CreateCompatibleBitmap(screen_dc, w, h)
    old = gdi32.SelectObject(mem_dc, bmp)
    try:
        ok = gdi32.BitBlt(mem_dc, 0, 0, w, h, screen_dc, x, y, SRCCOPY)
        if not ok:
            raise RuntimeError(f"BitBlt 失败, err={ctypes.get_last_error()}")

        bi = BITMAPINFO()
        bi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bi.bmiHeader.biWidth = w
        bi.bmiHeader.biHeight = -h  # 负数 = 自上而下
        bi.bmiHeader.biPlanes = 1
        bi.bmiHeader.biBitCount = 32
        bi.bmiHeader.biCompression = BI_RGB

        buf = ctypes.create_string_buffer(w * h * 4)
        got = gdi32.GetDIBits(mem_dc, bmp, 0, h, buf, ctypes.byref(bi), DIB_RGB_COLORS)
        if got != h:
            raise RuntimeError(f"GetDIBits 行数异常: {got}/{h}")
        arr = np.frombuffer(buf, dtype=np.uint8).reshape(h, w, 4)
        return np.ascontiguousarray(arr[:, :, :3])  # BGRA -> BGR
    finally:
        gdi32.SelectObject(mem_dc, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(None, screen_dc)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="frame.png")
    ap.add_argument("--list", action="store_true", help="只列出窗口")
    args = ap.parse_args()

    if args.list:
        for hwnd, title in list_windows():
            print(f"{hwnd:>10}  {title}")
        return 0

    hwnd = find_game_window()
    if hwnd is None:
        print(f"未找到标题含「{TITLE_KEY}」的窗口，NTE 可能没在运行", file=sys.stderr)
        return 1
    print(f"hwnd={hwnd} client={client_rect(hwnd)} foreground={is_foreground(hwnd)}")

    import cv2
    img = grab(hwnd)
    cv2.imwrite(args.out, img)
    print(f"saved {args.out}  shape={img.shape}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

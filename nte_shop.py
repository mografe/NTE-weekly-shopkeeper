"""NTE「店长特供」营业小游戏的识别。

统一在 3840x2160 的规范空间里工作：任何输入帧先等比缩放到这个尺寸，
于是模板与坐标常量只有一套，不受实际分辨率/缩放影响。

流程对应：
  准备页 --点「开始营业」--> 加载 --> 小游戏
  小游戏里：等需求气泡出现 -> 点右下角杯子 -> 点台面上的番茄汁 -> 自动交付
"""
import os

import cv2
import numpy as np

CAN = (3840, 2160)
ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")

# ---- 规范空间坐标 (x, y)，均由参考截图实测 ----
CUP_POINT = (3600, 1470)        # 台面右侧的三只透明玻璃杯（取上方那只的中心）
TOMATO_POINT = (3399, 1297)     # 台面右侧的番茄汁利乐包（红白纸盒）

# ---- 结算页（挑战成功）----
COLLECT_POINT = (2325, 1676)    # 「领取」
EXIT_POINT = (1523, 1676)       # 「退出」
SETTLE_BTN_ROW = (1200, 1600, 2650, 1740)   # 两个白色胶囊按钮所在横条

# ---- 区域 (x0, y0, x1, y1) ----
REVENUE_BOX = (3260, 195, 3520, 265)   # 右上面板「营业额 0 / 100」
TIMER_BOX = (1690, 100, 2260, 210)     # 顶部计时 1分59秒
DEMAND_SEARCH = (0, 150, 3840, 1180)   # 需求气泡可能出现在整个客座区（台面以上）


def client_scale(hwnd):
    """返回规范空间 -> 客户区像素的缩放比 (kx, ky)。

    所有坐标常量都以 3840x2160 为基准，游戏换分辨率/窗口化后，
    这里算出的比例负责把它们映射到真实窗口上。
    """
    nte_capture_mod = __import__("nte_capture")
    _x, _y, cw, ch = nte_capture_mod.client_rect(hwnd)
    return cw / float(CAN[0]), ch / float(CAN[1])


def window_info(hwnd):
    """探测游戏窗口的位置与分辨率，供启动时打印/界面显示。"""
    nte_capture_mod = __import__("nte_capture")
    x, y, cw, ch = nte_capture_mod.client_rect(hwnd)
    kx, ky = client_scale(hwnd)
    aspect = cw / float(ch) if ch else 0
    return {"x": x, "y": y, "w": cw, "h": ch,
            "kx": round(kx, 4), "ky": round(ky, 4),
            "aspect": round(aspect, 3),
            "is_16x9": abs(aspect - 16 / 9.0) < 0.02}


def canon_box_to_client(box, hwnd):
    """把规范空间的 (x0,y0,x1,y1) 映射成客户区像素的 (x, y, w, h)。"""
    kx, ky = client_scale(hwnd)
    x0, y0, x1, y1 = box
    return (int(round(x0 * kx)), int(round(y0 * ky)),
            int(round((x1 - x0) * kx)), int(round((y1 - y0) * ky)))


def canon(img):
    """把任意尺寸的帧缩放到规范 3840x2160。"""
    if img.shape[1] == CAN[0] and img.shape[0] == CAN[1]:
        return img
    interp = cv2.INTER_AREA if img.shape[1] > CAN[0] else cv2.INTER_CUBIC
    return cv2.resize(img, CAN, interpolation=interp)


def crop(img, box):
    x0, y0, x1, y1 = box
    return img[y0:y1, x0:x1]


def load(name):
    p = os.path.join(ASSETS, name)
    if not os.path.exists(p):
        raise FileNotFoundError(f"缺少模板 {p}，先跑 build_shop_templates.py")
    return cv2.imread(p, cv2.IMREAD_GRAYSCALE)


def match_best(img, tpl, box=None, scales=(1.0,)):
    """在区域内做多尺度灰度归一化互相关，返回 (score, cx, cy)。"""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    if box is None:
        ox, oy, search = 0, 0, gray
    else:
        x0, y0, x1, y1 = box
        ox, oy, search = x0, y0, gray[y0:y1, x0:x1]

    best = (0.0, 0, 0)
    for s in scales:
        t = tpl if s == 1.0 else cv2.resize(
            tpl, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
        th, tw = t.shape[:2]
        if search.shape[0] < th or search.shape[1] < tw:
            continue
        res = cv2.matchTemplate(search, t, cv2.TM_CCOEFF_NORMED)
        _min, maxv, _ml, maxloc = cv2.minMaxLoc(res)
        if maxv > best[0]:
            best = (float(maxv), ox + maxloc[0] + tw // 2, oy + maxloc[1] + th // 2)
    return best


def detect_demand(img):
    """检测客人需求气泡（番茄汁）。返回 (score, cx, cy)。"""
    tpl = load("shop_demand_tomato.png")
    return match_best(img, tpl, box=DEMAND_SEARCH, scales=(1.0,))


_DEMAND_TPL = None
_DEMAND_TPL_S = {}       # 按缩放比缓存缩小的模板
SCAN_SCALE = 0.5         # 扫描时用半分辨率：3840x1030 -> 1920x515
# 需求气泡的大小会随客人距离浮动（实测同一关内幅度约 15%），单尺度会漏检，
# 所以按多个尺度各匹配一遍取最高分。
DEMAND_SCALES = (1.0, 1.08, 1.15)


def _tpl_scaled(sx, sy):
    key = (round(sx, 4), round(sy, 4))
    if key not in _DEMAND_TPL_S:
        th, tw = _DEMAND_TPL.shape[:2]
        _DEMAND_TPL_S[key] = cv2.resize(
            _DEMAND_TPL, (max(1, int(round(tw * sx))), max(1, int(round(th * sy)))),
            interpolation=cv2.INTER_AREA)
    return _DEMAND_TPL_S[key]



def demand_probe(hwnd, scale=None):
    """高频轮询专用：抓整个客座区并降采样后单尺度匹配。返回 (score, cx, cy)。

    需求气泡可能出现在整幅上半屏（客座区任意座位），所以搜索区放宽到
    3840x1030；直接 1:1 抓要 116ms，用 StretchBlt 半分辨率抓只要 ~35ms，
    再在 1920x515 上匹配，总耗时 ~40ms。
    """
    global _DEMAND_TPL
    nte_capture_mod = __import__("nte_capture")
    if _DEMAND_TPL is None:
        _DEMAND_TPL = load("shop_demand_tomato.png")
    s = SCAN_SCALE if scale is None else scale
    x0, y0, x1, y1 = DEMAND_SEARCH

    # 规范坐标 -> 真实客户区像素（换分辨率 / 窗口化都靠这一步）
    bx, by, bw, bh = canon_box_to_client(DEMAND_SEARCH, hwnd)
    reg, (ax, ay) = nte_capture_mod.grab_region_scaled(
        hwnd, bx, by, bw, bh, scale=s)

    kx, ky = client_scale(hwnd)
    gray = cv2.cvtColor(reg, cv2.COLOR_BGR2GRAY)
    best_score, best_cx, best_cy = 0.0, 0, 0
    for ts in DEMAND_SCALES:
        # 模板要按「规范像素 -> 扫描图像素」的比例缩放，ts 是气泡大小浮动
        tpl = _tpl_scaled(ax * kx * ts, ay * ky * ts)
        th, tw = tpl.shape[:2]
        if gray.shape[0] < th or gray.shape[1] < tw:
            continue
        res = cv2.matchTemplate(gray, tpl, cv2.TM_CCOEFF_NORMED)
        _mn, mx, _ml, loc = cv2.minMaxLoc(res)
        if mx > best_score:
            best_score = float(mx)
            # 匹配位置换算回规范坐标
            best_cx = int(x0 + (loc[0] + tw / 2.0) / (ax * kx))
            best_cy = int(y0 + (loc[1] + th / 2.0) / (ay * ky))
    return best_score, best_cx, best_cy


def detect_cup(img):
    """台面右侧的三只玻璃杯。"""
    tpl = load("shop_cup.png")
    return match_best(img, tpl, box=(3300, 1340, 3840, 1800), scales=(1.0,))


def detect_tomato(img):
    """番茄汁利乐包（客人需求一定是番茄汁，所以只需认这一个）。"""
    tpl = load("shop_tomato.png")
    return match_best(img, tpl, box=(3250, 1100, 3560, 1420), scales=(1.0,))


def settle_probe(hwnd):
    """快速判断是否处于结算页：底部并排两个白色胶囊按钮。

    只抓那一条 1450x140 的窄带（~5ms），所以可以直接放进轮询。
    """
    nte_capture_mod = __import__("nte_capture")
    bx, by, bw, bh = canon_box_to_client(SETTLE_BTN_ROW, hwnd)
    reg = nte_capture_mod.grab_region(hwnd, bx, by, bw, bh)
    hsv = cv2.cvtColor(reg, cv2.COLOR_BGR2HSV)
    m = cv2.inRange(hsv, (0, 0, 190), (180, 45, 255))
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((15, 15), np.uint8))
    cnts, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    big = [cv2.boundingRect(c) for c in cnts if cv2.contourArea(c) > 20000]
    return len(big) >= 2


def in_minigame(img):
    """判断是否处于营业小游戏：顶部有一条高饱和的绿色进度条。"""
    hsv = cv2.cvtColor(crop(img, (800, 0, 3000, 200)), cv2.COLOR_BGR2HSV)
    green = cv2.inRange(hsv, (35, 120, 120), (85, 255, 255))
    frac = green.sum() / 255.0 / green.size
    return frac > 0.02, float(frac)


def read_revenue(img, workdir=None):
    """OCR 读「营业额 X / Y」，返回 (cur, total) 或 None。"""
    import nte_vision as V
    region = crop(img, REVENUE_BOX)
    region = cv2.resize(region, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
    path = os.path.join(workdir or ASSETS, "_revenue_ocr.png")
    cv2.imwrite(path, region)
    import re
    try:
        res = V.ocr_image(path, scale=1)
    except Exception:                                    # noqa: BLE001
        return None
    finally:
        if os.path.exists(path):
            os.remove(path)
    text = res.get("text", "").replace(" ", "")
    m = re.search(r"(\d+)/(\d+)", text)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))

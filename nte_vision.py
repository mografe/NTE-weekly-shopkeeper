"""NTE (异环) 界面识别基础能力：区域定义、高亮行定位、文字掩码比对、模板匹配、OCR。

坐标一律以 3840x2160 为参考分辨率定义，运行时按实际帧尺寸等比缩放，
所以游戏换分辨率后只需重新跑一次 build_templates.py。
"""
import json
import os
import subprocess
import sys

import cv2
import numpy as np

REF_W, REF_H = 3840, 2160

# ---- 参考分辨率下的区域常量 (x0, y0, x1, y1) ----
# 左侧关卡列表栏（含粉色高亮边框）
LEVEL_COL = (0, 280, 720, 2100)
# 右侧「辅助雇员」面板
EMP_PANEL = (2000, 1300, 3500, 1900)
# 右上角「都市活力」数字
VITALITY_BOX = (3190, 58, 3440, 142)

HERE = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.join(HERE, "assets")
OCR_PS1 = os.path.join(HERE, "ocr.ps1")


def sx(x, shape):
    return int(round(x * shape[1] / REF_W))


def sy(y, shape):
    return int(round(y * shape[0] / REF_H))


def region(box, shape):
    x0, y0, x1, y1 = box
    return sx(x0, shape), sy(y0, shape), sx(x1, shape), sy(y1, shape)


# ---------------------------------------------------------------- 高亮行

def find_highlight_row(img):
    """用粉色边框定位当前选中的关卡行，返回 (x, y, w, h) 或 None。

    选中行有一圈洋红色描边，列表里只有它是这个颜色，因此比对比对文字更稳。
    """
    h, w = img.shape[:2]
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, (155, 120, 120), (179, 255, 255))
    x0, y0, x1, y1 = region(LEVEL_COL, img.shape)
    keep = np.zeros_like(mask)
    keep[y0:y1, x0:x1] = 255
    mask = cv2.bitwise_and(mask, keep)

    n, _lab, stats, _c = cv2.connectedComponentsWithStats(mask, 8)
    best = None
    for s in stats[1:]:
        if s[4] < 5000:
            continue
        x, y, w, h = int(s[0]), int(s[1]), int(s[2]), int(s[3])
        # 关键：选中行是「紫色底 + 洋红描边」，而鼠标悬停/焦点行只有洋红描边、
        # 底色仍是灰的。只认描边会把悬停行误判成选中行（实测导致同一次运行里
        # 先后判断出 1.0 和 0.03 两种结果），所以这里必须再验一下填充色。
        patch = img[max(y + 8, 0):y + h - 8, max(x + 8, 0):x + 60]
        if patch.size == 0:
            continue
        b, g, r = (float(v) for v in patch.reshape(-1, 3).mean(axis=0))
        is_purple = (b > 140) and (b - g > 35)
        if not is_purple:
            continue
        if best is None or s[4] > best[4]:
            best = s
    if best is None:
        return None
    return int(best[0]), int(best[1]), int(best[2]), int(best[3])


# ---------------------------------------------------------------- 文字掩码

def white_text_mask(img):
    """提取白色描边文字的二值掩码（排除金色星星等饱和色块）。"""
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    v, s = hsv[:, :, 2], hsv[:, :, 1]
    return ((v > 200) & (s < 45)).astype(np.uint8) * 255


def row_text_mask(img, box):
    """取高亮行内的白色文字掩码，返回 (mask, tight_box)。"""
    x, y, w, h = box
    roi = img[y:y + h, x:x + w]
    m = white_text_mask(roi)
    cols = np.where(m.sum(0) > 2)[0]
    rows = np.where(m.sum(1) > 2)[0]
    if len(cols) == 0 or len(rows) == 0:
        return m, None
    tight = (int(cols.min()), int(rows.min()),
             int(cols.max() - cols.min() + 1), int(rows.max() - rows.min() + 1))
    return m, tight


def mask_similarity(a, b):
    """两个文字掩码的相似度：先各自裁到文字紧包围盒，再归一到同一尺寸后取相关系数。

    返回 (corr, aspect_ratio_a, aspect_ratio_b)。
    """
    def tight(m):
        cols = np.where(m.sum(0) > 2)[0]
        rows = np.where(m.sum(1) > 2)[0]
        if len(cols) == 0 or len(rows) == 0:
            return None
        return m[rows.min():rows.max() + 1, cols.min():cols.max() + 1]

    ta, tb = tight(a), tight(b)
    if ta is None or tb is None:
        return 0.0, 0.0, 0.0
    ar_a = ta.shape[1] / max(ta.shape[0], 1)
    ar_b = tb.shape[1] / max(tb.shape[0], 1)
    size = (320, 80)
    ra = cv2.resize(ta, size, interpolation=cv2.INTER_AREA).astype(np.float32)
    rb = cv2.resize(tb, size, interpolation=cv2.INTER_AREA).astype(np.float32)
    ra -= ra.mean(); rb -= rb.mean()
    denom = np.sqrt((ra ** 2).sum() * (rb ** 2).sum())
    if denom < 1e-6:
        return 0.0, ar_a, ar_b
    return float((ra * rb).sum() / denom), ar_a, ar_b


# ---------------------------------------------------------------- 模板匹配

# 右侧大标题「1-1 新品练习」所在区域（规范 3840x2160）
LEVEL_TITLE_BOX = (768, 282, 1495, 428)
LEVEL_TITLE_TH = 0.80

_title_cache = {}


def level_title_score(img, name="level_title_1_1.png"):
    """右侧大标题与 1-1 的相似度。

    这是判断"当前选中的是哪一关"最可靠的依据——它和左侧列表滚到哪儿无关。
    实测 1-1 得 0.97~1.00，1-3 只有 0.50，结算页 0.09。
    """
    if name not in _title_cache:
        p = os.path.join(ASSETS, name)
        if not os.path.exists(p):
            return 0.0
        _title_cache[name] = cv2.imread(p, cv2.IMREAD_GRAYSCALE)
    tpl = _title_cache[name]
    x0, y0, x1, y1 = LEVEL_TITLE_BOX
    reg = img[y0:y1, x0:x1]
    if reg.shape[0] < tpl.shape[0] or reg.shape[1] < tpl.shape[1]:
        return 0.0
    gray = cv2.cvtColor(reg, cv2.COLOR_BGR2GRAY)
    res = cv2.matchTemplate(gray, tpl, cv2.TM_CCOEFF_NORMED)
    return float(cv2.minMaxLoc(res)[1])


def find_level_rows(img):
    """用白色文字行带定位左侧列表里所有可见的关卡行，返回行框列表。

    不依赖固定坐标，所以列表被滚动过也能找到。
    """
    x0, y0, x1, y1 = region(LEVEL_COL, img.shape)
    col = img[y0:y1, x0:x1]
    m = white_text_mask(col)
    prof = m.sum(1) / 255.0
    on = prof > 15
    bands, s = [], None
    for i, v in enumerate(on):
        if v and s is None:
            s = i
        elif not v and s is not None:
            if i - s >= 35:          # 关卡行文字高 ~46px，标题行只有 ~22px
                bands.append((s, i))
            s = None
    if s is not None and len(on) - s >= 35:
        bands.append((s, len(on)))
    rows = []
    for (a, _b) in bands:
        box = (x0 + 42, y0 + a - 40, 604, 224)
        bx, by, bw, bh = box
        patch = img[max(by + 30, 0):by + 90, max(bx + 20, 0):bx + 120]
        if patch.size == 0:
            continue
        # 章节标题是近黑色面板（均值 ~30），关卡行是灰/紫底（~110 以上），
        # 不排除的话会把章节标题误当成关卡行去点，反而把章节收起。
        if float(patch.mean()) < 70:
            continue
        # 红色「未解锁」横幅也会带白色文字，它不可点击，必须排除：
        # 红色横幅 R 远大于 G/B，而关卡行是灰底(R≈G≈B)或紫底(B>G)。
        b_, g_, r_ = (float(v) for v in patch.reshape(-1, 3).mean(axis=0))
        if (r_ - g_) > 45 and (r_ - b_) > 45:
            continue
        rows.append(box)
    return rows


def find_11_row(img, tpl_path=None):
    """在可见的关卡行里找出 1-1，返回 (相似度, 行框)。"""
    p = tpl_path or os.path.join(ASSETS, "row_mask_1_1.png")
    tpl = cv2.imread(p, cv2.IMREAD_GRAYSCALE)
    best = (0.0, None)
    for box in find_level_rows(img):
        m, _t = row_text_mask(img, box)
        corr = mask_similarity(m, tpl)[0]
        if corr > best[0]:
            best = (corr, box)
    return best


def match_template(img, tpl, box=None, thresh=0.0):
    """在指定区域内做灰度归一化互相关匹配，返回 [(score, cx, cy, w, h), ...] 按分数降序。"""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    th, tw = tpl.shape[:2]
    if box is None:
        ox, oy = 0, 0
        search = gray
    else:
        x0, y0, x1, y1 = box
        ox, oy = x0, y0
        search = gray[y0:y1, x0:x1]
    if search.shape[0] < th or search.shape[1] < tw:
        return []
    res = cv2.matchTemplate(search, tpl, cv2.TM_CCOEFF_NORMED)
    # 取局部极大值，避免同一目标重复出现
    dil = cv2.dilate(res, np.ones((31, 31), np.uint8))
    peaks = (res >= dil - 1e-6) & (res >= thresh)
    ys, xs = np.where(peaks)
    out = [(float(res[y, x]), ox + int(x) + tw // 2, oy + int(y) + th // 2, tw, th)
           for y, x in zip(ys, xs)]
    out.sort(key=lambda t: -t[0])
    return out


# ---------------------------------------------------------------- OCR

def ocr_image(path, scale=2, lang="zh-Hans-CN"):
    """调用 Windows 内置 OCR，返回 {"text":..., "lines":[...]}。"""
    cmd = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
           "-File", OCR_PS1, "-Path", path, "-Scale", str(scale), "-Lang", lang]
    # CREATE_NO_WINDOW：否则每次 OCR 都会在屏幕上闪一个 PowerShell 黑窗，
    # 还能抢走游戏焦点导致后续点击被吞。
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", creationflags=flags)
    if p.returncode != 0:
        raise RuntimeError(f"OCR 失败: {p.stderr.strip() or p.stdout.strip()}")
    txt = (p.stdout or "").strip().splitlines()
    if not txt:
        raise RuntimeError("OCR 无输出")
    return json.loads(txt[-1])


def parse_pair(raw):
    """从 OCR 文本里抠出 a/b。

    实测同一个数字在不同放大倍率下，OCR 对斜杠的识别会飘（「190/700」被读成
    「19 □ 700」），所以这里把常见误认的字符归一化，并用宽松正则兜底。
    """
    import re
    t = (raw or "").replace(" ", "")
    t = (t.replace("O", "0").replace("o", "0").replace("〇", "0")
          .replace("／", "/").replace("l", "1").replace("I", "1"))
    m = re.search(r"(\d+)\s*[/|\\]\s*(\d+)", t)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.search(r"(\d+)\D+(\d+)", t)
    if m:
        return int(m.group(1)), int(m.group(2))
    return None, None


def read_vitality(img, debug_dir=None):
    """读右上角「都市活力」，返回 (num, den, raw_text) 或 None。

    单个倍率会偶发读错（斜杠丢字），所以按多个倍率依次尝试，
    只要有一个能解析出 a/b 就返回。
    """
    import tempfile
    x0, y0, x1, y1 = region(VITALITY_BOX, img.shape)
    crop = img[y0:y1, x0:x1]
    path = os.path.join(debug_dir or tempfile.gettempdir(), "nte_vitality.png")
    cv2.imwrite(path, crop)

    for scale in (4, 2, 3, 5):
        try:
            res = ocr_image(path, scale=scale)
        except Exception:                                # noqa: BLE001
            continue
        raw = res.get("text", "")
        num, den = parse_pair(raw)
        if num is not None and den:
            return num, den, raw
    return None

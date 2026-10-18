"""识别能力的交叉验证：证明这些检查真的能「分辨」，而不只是拿模板自比自。

三组实验：
  A. 用 1-1 行文字模板去比对左侧列表里其它关卡行 -> 相似度必须明显更低
  B. 用「安魂曲」头像模板去匹配「达芙蒂尔」的槽位 -> 分数必须明显更低
  C. 用同一套 OCR 流程去读另一个数字（右上角累计获得 9/33）-> 应能正确读出
"""
import json
import os
import sys

import cv2
import numpy as np

import nte_vision as V

# 中文版 Windows 的控制台是 GBK，OCR 结果里可能带替换字符（U+FFFD），
# 直接 print 会抛 UnicodeEncodeError。这里改成"编不出来就替换"，不中断输出。
try:
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:                                        # noqa: BLE001
    pass


def find_rows_by_stars(img):
    """利用每行都有 3 颗金色星星这一特征定位所有关卡行，返回 [(box, y_center)]。"""
    x0, y0, x1, y1 = V.region(V.LEVEL_COL, img.shape)
    col = img[y0:y1, x0:x1]
    hsv = cv2.cvtColor(col, cv2.COLOR_BGR2HSV)
    gold = cv2.inRange(hsv, (18, 140, 170), (36, 255, 255))
    gold = cv2.morphologyEx(gold, cv2.MORPH_CLOSE, np.ones((21, 21), np.uint8))
    _n, _l, stats, _c = cv2.connectedComponentsWithStats(gold, 8)
    star_ys = sorted([s[1] + s[3] // 2 for s in stats[1:] if s[4] > 3000])
    # 把相邻的星星 y 聚成一行
    groups = []
    for y in star_ys:
        if groups and y - groups[-1][-1] < 60:
            groups[-1].append(y)
        else:
            groups.append([y])
    rows = []
    for g in groups:
        yc = int(np.mean(g)) + y0
        # 卡片比星星高：星星中心约在卡片下部，向上抬 90px 取左上角
        box = (x0 + 40, yc - 130, 604, 224)
        rows.append((box, yc))
    return rows


def load_frame():
    """不给参数时直接抓当前游戏画面（需停在准备页）。"""
    if len(sys.argv) > 1:
        img = cv2.imread(sys.argv[1])
        if img is None:
            print(f"读不到 {sys.argv[1]}", file=sys.stderr)
            return None, sys.argv[1]
        return img, sys.argv[1]
    import nte_capture
    import nte_input
    import nte_shop
    hwnd = nte_capture.find_game_window()
    if hwnd is None:
        print("没找到「异环」窗口", file=sys.stderr)
        return None, "live"
    # 抓屏只能抓到屏幕上真实的像素，游戏必须在前台且不被遮挡。
    # 别的窗口（浏览器、聊天软件）会抢焦点，所以抓到图后要验证一下，
    # 确认抓到的是准备页而不是别的程序。
    import time
    import nte_vision
    img = None
    for attempt in range(5):
        nte_input.ensure_foreground(hwnd)
        time.sleep(0.35)
        img = nte_shop.canon(nte_capture.grab(hwnd))
        if (nte_vision.level_title_score(img) > 0.5
                or nte_vision.find_highlight_row(img) is not None):
            return img, "实时抓屏"
        time.sleep(0.5)
    print("警告: 抓到的画面看起来不是准备页——可能游戏不在前台或被遮挡",
          file=sys.stderr)
    return img, "实时抓屏(未确认)"


def main():
    img, frame = load_frame()
    if img is None:
        return 2
    print(f"frame: {frame} {img.shape[1]}x{img.shape[0]}\n")

    tpl_mask = cv2.imread(os.path.join(V.ASSETS, "row_mask_1_1.png"), cv2.IMREAD_GRAYSCALE)
    hl = V.find_highlight_row(img)

    # ---------------- A ----------------
    print("A. 1-1 文字模板 vs 各关卡行")
    rows = find_rows_by_stars(img)
    for box, yc in rows:
        mask, tight = V.row_text_mask(img, box)
        corr, ar_c, ar_t = V.mask_similarity(mask, tpl_mask)
        is_hl = hl is not None and abs(box[1] - hl[1]) < 130
        tag = "<== 当前选中(1-1)" if is_hl else ""
        print(f"   y={box[1]:5d}  corr={corr:+.3f}  文字宽高比={ar_c:5.2f}  {tag}")

    # ---------------- B ----------------
    print("\nB. 头像模板交叉匹配（在雇员面板内）")
    panel = V.region(V.EMP_PANEL, img.shape)
    emps = ["emp_0.png", "emp_1.png"]
    labels = ["安魂曲", "达芙蒂尔"]
    scores = np.zeros((2, 2))
    for i, f in enumerate(emps):
        tpl = cv2.imread(os.path.join(V.ASSETS, f), cv2.IMREAD_GRAYSCALE)
        # 分别在两个槽位附近取最高分
        for j in range(2):
            sx = 2063 if j == 0 else 2483
            sub = (sx - 60, 1300, sx + 420, 1830)
            hs = V.match_template(img, tpl, box=sub, thresh=0.2)
            scores[i, j] = hs[0][0] if hs else 0.0
    hdr = "模板/槽位"
    print(f"   {hdr:<12}{'槽位1':>10}{'槽位2':>10}")
    for i, name in enumerate(labels):
        print(f"   {name:<10}{scores[i, 0]:>10.3f}{scores[i, 1]:>10.3f}")
    print(f"   对角线均值(应为己方) = {(scores[0,0]+scores[1,1])/2:.3f}")
    print(f"   非对角均值(应为他人) = {(scores[0,1]+scores[1,0])/2:.3f}")

    # ---------------- C ----------------
    print("\nC. 同一 OCR 流程读另一个数字（右上角累计获得 9/33）")
    badge = img[368:452, 3488:3652]
    badge = cv2.resize(badge, None, fx=4, fy=4, interpolation=cv2.INTER_CUBIC)
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_badge.png")
    cv2.imwrite(p, badge)
    import re
    try:
        res = V.ocr_image(p, scale=1)
        raw = res.get("text", "")
        m = re.search(r"(\d+)\s*/\s*(\d+)", raw.replace(" ", ""))
        print(f"   原文: {raw!r}")
        print(f"   解析: {m.group(1)}/{m.group(2)}" if m else "   解析: 失败")
    except Exception as ex:                        # noqa: BLE001
        print(f"   OCR 失败: {ex}")
    os.remove(p)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

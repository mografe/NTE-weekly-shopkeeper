"""从当前游戏画面里提取识别模板，生成 assets/。

前提：画面停在「店长特供」关卡选择页，且 1-1 处于选中状态、辅助雇员已就位。
只需要在首次配置或游戏换分辨率/改 UI 后跑一次。

用法:
    python build_templates.py                 # 自动抓当前画面
    python build_templates.py --frame a.png   # 用已有截图
    python build_templates.py --names 安魂曲 达芙蒂尔
"""
import argparse
import json
import os
import sys

import cv2
import numpy as np

import nte_capture
import nte_vision as V


def find_employee_slots(img):
    """在辅助雇员面板内找深色头像块，返回按 x 排序的 [(x, y, w, h), ...]。"""
    x0, y0, x1, y1 = V.region(V.EMP_PANEL, img.shape)
    panel = img[y0:y1, x0:x1]
    gray = cv2.cvtColor(panel, cv2.COLOR_BGR2GRAY)
    dark = (gray < 90).astype(np.uint8) * 255
    dark = cv2.morphologyEx(dark, cv2.MORPH_CLOSE, np.ones((15, 15), np.uint8))
    _n, _lab, stats, _c = cv2.connectedComponentsWithStats(dark, 8)
    boxes = []
    min_area = int(0.02 * panel.shape[0] * panel.shape[1])
    for s in stats[1:]:
        if s[4] < min_area:
            continue
        w, h = int(s[2]), int(s[3])
        if w < 100 or h < 100:
            continue
        boxes.append((x0 + int(s[0]), y0 + int(s[1]), w, h))
    boxes.sort(key=lambda b: b[0])
    return boxes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--frame", help="用已有截图，不传则实时抓帧")
    ap.add_argument("--names", nargs="*", default=["安魂曲", "达芙蒂尔"],
                    help="按从左到右顺序指定雇员名")
    args = ap.parse_args()

    if args.frame:
        img = cv2.imread(args.frame)
        if img is None:
            print(f"读不到图片: {args.frame}", file=sys.stderr)
            return 1
        print(f"frame: {args.frame} {img.shape[1]}x{img.shape[0]}")
    else:
        hwnd = nte_capture.find_game_window()
        if hwnd is None:
            print("没找到「异环」窗口", file=sys.stderr)
            return 1
        img = nte_capture.grab(hwnd)
        print(f"frame: live {img.shape[1]}x{img.shape[0]}")

    os.makedirs(V.ASSETS, exist_ok=True)
    meta = {"ref_size": [int(img.shape[1]), int(img.shape[0])], "employees": []}

    # --- 1) 选中行文字掩码 ---
    row = V.find_highlight_row(img)
    if row is None:
        print("没找到粉色高亮行，请确认 1-1 处于选中状态", file=sys.stderr)
        return 1
    mask, tight = V.row_text_mask(img, row)
    if tight is None:
        print("高亮行里没提取到白色文字", file=sys.stderr)
        return 1
    cv2.imwrite(os.path.join(V.ASSETS, "row_mask_1_1.png"), mask)
    meta["highlight_row"] = {"x": row[0], "y": row[1], "w": row[2], "h": row[3],
                             "text_tight": list(tight)}
    print(f"高亮行 bbox={row}  文字紧包围盒={tight}")

    # --- 2) 雇员头像 ---
    slots = find_employee_slots(img)
    if len(slots) < len(args.names):
        print(f"只找到 {len(slots)} 个头像槽位，期望 {len(args.names)} 个", file=sys.stderr)
        return 1
    for i, name in enumerate(args.names):
        x, y, w, h = slots[i]
        # 去掉圆角边缘，往里收一圈，避免匹配时吃到背景
        pad_x, pad_y = int(w * 0.06), int(h * 0.06)
        crop = img[y + pad_y:y + h - pad_y, x + pad_x:x + w - pad_x]
        fn = f"emp_{i}.png"
        cv2.imwrite(os.path.join(V.ASSETS, fn), crop)
        meta["employees"].append({"file": fn, "name": name, "slot": i,
                                  "box": [x, y, w, h]})
        print(f"雇员 {i}: {name}  slot={x},{y} {w}x{h}  -> {fn}")

    with open(os.path.join(V.ASSETS, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    print(f"\n已写入 {V.ASSETS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

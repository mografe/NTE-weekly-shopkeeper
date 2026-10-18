"""NTE 关卡循环的「进入条件」校验。

判定三件事：
  1. 当前选中的是 1-1 新品练习（左侧高亮行 + 行内文字与 1-1 模板比对）
  2. 辅助雇员同时包含 安魂曲 与 达芙蒂尔（头像模板匹配）
  3. 右上角都市活力不为 0（OCR 数字）

用法:
    python nte_check.py                 # 实时抓帧校验
    python nte_check.py --frame a.png   # 对已有截图校验
    python nte_check.py --calibrate     # 打印所有候选行的相似度，用于调阈值
    python nte_check.py --annotate out.png
"""
import argparse
import json
import os
import sys

import cv2
import numpy as np

import nte_capture
import nte_vision as V

# 阈值
TH_LEVEL_CORR = 0.80      # 1-1 行文字掩码相关系数
TH_EMP_SCORE = 0.70       # 雇员头像匹配分数


def load_meta():
    p = os.path.join(V.ASSETS, "meta.json")
    if not os.path.exists(p):
        raise SystemExit(f"缺少 {p}，先跑一次 build_templates.py")
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def list_candidate_rows(img):
    """把左侧列表里所有行都切出来（用于标定/调试），返回 [(box, mask), ...]。"""
    x0, y0, x1, y1 = V.region(V.LEVEL_COL, img.shape)
    col = img[y0:y1, x0:x1]
    # 行与行之间有明显分界：用列方向灰度投影找卡片块
    gray = cv2.cvtColor(col, cv2.COLOR_BGR2GRAY)
    # 行卡片在灰色背景上有一定对比度，用边缘密度定位
    edges = cv2.Canny(gray, 40, 120)
    prof = edges.sum(1) / 255.0
    th = prof.mean() + prof.std()
    inside = prof > th
    rows, start = [], None
    for i, v in enumerate(inside):
        if v and start is None:
            start = i
        elif not v and start is not None:
            if i - start > 60:
                rows.append((start, i))
            start = None
    if start is not None and len(inside) - start > 60:
        rows.append((start, len(inside)))
    out = []
    hl = V.find_highlight_row(img)
    hl_y = hl[1] - y0 if hl else None
    for (a, b) in rows:
        h = b - a
        if h > 300 or h < 150:      # 关卡行高度大致固定
            continue
        box = (x0 + 40, y0 + a, 604, h)
        mask, tight = V.row_text_mask(img, box)
        is_hl = hl_y is not None and abs((y0 + a) - (y0 + hl_y)) < 30
        out.append((box, mask, is_hl))
    return out


def check(img, meta, require_vitality=True):
    res = {"ok": False, "level": {}, "employees": {}, "vitality": {}, "problems": []}

    # ---------- 1) 选中的是不是 1-1 ----------
    # 主依据是右侧大标题：它和左侧列表滚到哪儿无关（列表被滚走时高亮行根本不可见）。
    title = V.level_title_score(img)
    corr = 0.0
    row = V.find_highlight_row(img)
    if row is not None:
        mask, _tight = V.row_text_mask(img, row)
        tpl = cv2.imread(os.path.join(V.ASSETS, "row_mask_1_1.png"),
                         cv2.IMREAD_GRAYSCALE)
        corr = V.mask_similarity(mask, tpl)[0]
    ok = (title >= V.LEVEL_TITLE_TH) or (corr >= TH_LEVEL_CORR)
    res["level"] = {"ok": ok, "corr": round(corr, 3), "title": round(title, 3),
                    "box": list(row) if row is not None else None}
    if not ok:
        res["problems"].append(
            f"选中的不是 1-1（标题 {title:.2f} / 行 {corr:.2f}）")

    # ---------- 2) 辅助雇员 ----------
    panel = V.region(V.EMP_PANEL, img.shape)
    emp_ok, detail = True, []
    for e in meta["employees"]:
        tpl = cv2.imread(os.path.join(V.ASSETS, e["file"]), cv2.IMREAD_GRAYSCALE)
        hits = V.match_template(img, tpl, box=panel, thresh=TH_EMP_SCORE)
        best = hits[0] if hits else None
        found = best is not None and best[0] >= TH_EMP_SCORE
        detail.append({"name": e["name"], "found": found,
                       "score": round(best[0], 3) if best else 0.0,
                       "at": [best[1], best[2]] if best else None})
        if not found:
            emp_ok = False
            res["problems"].append(f"辅助雇员缺少 {e['name']}")
    res["employees"] = {"ok": emp_ok, "detail": detail}

    # ---------- 3) 都市活力 ----------
    try:
        v = V.read_vitality(img)
    except Exception as ex:                                    # noqa: BLE001
        v = None
        res["problems"].append(f"活力 OCR 失败: {ex}")
    if v is None:
        if not any("活力" in p for p in res["problems"]):
            res["problems"].append("没能读出都市活力数值")
        res["vitality"] = {"ok": False}
    else:
        num, den, raw = v
        res["vitality"] = {"ok": num > 0, "value": num, "max": den, "raw": raw}
        if num <= 0:
            res["problems"].append("都市活力为 0，需要补充")

    vital_ok = bool(res["vitality"].get("ok"))
    if not require_vitality:
        # 指定轮数模式：活力只用于记录，不阻塞
        res["problems"] = [p for p in res["problems"] if "活力" not in p]
        vital_ok = True
    res["ok"] = bool(res["level"].get("ok")) and res["employees"]["ok"] and vital_ok
    return res


def annotate(img, res, path):
    out = img.copy()
    if res["level"].get("box"):
        x, y, w, h = res["level"]["box"]
        color = (0, 255, 0) if res["level"]["ok"] else (0, 0, 255)
        cv2.rectangle(out, (x, y), (x + w, y + h), color, 4)
        cv2.putText(out, f"1-1? corr={res['level'].get('corr')}", (x, y - 14),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.1, color, 3)
    for d in res["employees"].get("detail", []):
        if d["at"]:
            cx, cy = d["at"]
            color = (0, 255, 0) if d["found"] else (0, 0, 255)
            cv2.circle(out, (cx, cy), 190, color, 4)
            cv2.putText(out, f"{d['name']} {d['score']:.2f}", (cx - 150, cy - 200),
                        cv2.FONT_HERSHEY_SIMPLEX, 1.1, color, 3)
    v = res["vitality"]
    if v.get("value") is not None:
        x0, y0, x1, y1 = V.region(V.VITALITY_BOX, img.shape)
        cv2.rectangle(out, (x0 - 10, y0 - 10), (x1 + 10, y1 + 10), (0, 255, 0), 4)
        cv2.putText(out, f"{v['value']}/{v['max']}", (x0 - 10, y1 + 50),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 255, 0), 3)
    cv2.imwrite(path, out)
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--frame")
    ap.add_argument("--calibrate", action="store_true")
    ap.add_argument("--annotate")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if args.frame:
        img = cv2.imread(args.frame)
        if img is None:
            print(f"读不到图片: {args.frame}", file=sys.stderr)
            return 2
    else:
        hwnd = nte_capture.find_game_window()
        if hwnd is None:
            print("没找到「异环」窗口，游戏可能没在运行", file=sys.stderr)
            return 2
        img = nte_capture.grab(hwnd)

    meta = load_meta()

    if args.calibrate:
        tpl = cv2.imread(os.path.join(V.ASSETS, "row_mask_1_1.png"), cv2.IMREAD_GRAYSCALE)
        print("左侧列表各行与 1-1 模板的相似度：")
        for box, mask, is_hl in list_candidate_rows(img):
            corr, ar_c, ar_t = V.mask_similarity(mask, tpl)
            flag = "  <== 当前选中" if is_hl else ""
            print(f"  box={box}  corr={corr:.3f}  aspect={ar_c:.2f}/{ar_t:.2f}{flag}")
        return 0

    res = check(img, meta)

    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        print("=" * 62)
        lv = res["level"]
        print(f"[1] 选中关卡 = 1-1 ?     {'通过' if lv.get('ok') else '不通过'}"
              f"   (相似度 {lv.get('corr')})")
        em = res["employees"]
        print(f"[2] 辅助雇员齐备 ?      {'通过' if em.get('ok') else '不通过'}")
        for d in em.get("detail", []):
            print(f"      {d['name']:<6} {'命中' if d['found'] else '缺失'}"
                  f"  分数 {d['score']:.3f}  位置 {d['at']}")
        vt = res["vitality"]
        if "value" in vt:
            print(f"[3] 都市活力 > 0 ?       {'通过' if vt['ok'] else '不通过'}"
                  f"   {vt['value']}/{vt['max']}")
        else:
            print(f"[3] 都市活力 > 0 ?       不通过   (未读出)")
        print("=" * 62)
        print(f"总体: {'可以开始刷关' if res['ok'] else '不满足进入条件'}")
        if res["problems"]:
            for p in res["problems"]:
                print(f"  ! {p}")

    if args.annotate:
        p = annotate(img, res, args.annotate)
        print(f"标注图已保存: {p}")

    return 0 if res["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

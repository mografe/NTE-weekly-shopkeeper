"""NTE 店长特供 1-1 完整刷关流程（必须管理员运行）。

为什么要管理员：游戏 HTGame 以 High 完整性级别运行，普通权限脚本发出的模拟
鼠标/键盘事件会被 Windows UIPI 静默丢弃——事件能进系统输入流，但永远送不到游戏。

流程：
  准备页 -> 若没选中 1-1 则点选 1-1 -> 校验雇员/活力 -> 点开始营业
  -> 等加载进小游戏 -> 等需求气泡(番茄汁) -> 点杯子 -> 点番茄汁 -> 自动交付
  -> 重复 N 次 -> ESC 退出 -> 回到准备页

经验规则：游戏会吞掉「传送光标后的第一次点击」，所以每个动作都用
「点击 -> 验证 -> 重试」的循环，而不是假设一次点击必成功。

用法:
  python nte_farm.py                 # 服务 2 位客人
  python nte_farm.py --serve 2
  python nte_farm.py --dry-run       # 只识别不点击
  python nte_farm.py --no-select     # 不自动切关卡，只校验
"""
import argparse
import ctypes
import os
import sys
import time
from ctypes import wintypes

import cv2
import numpy as np

import nte_capture
import nte_input as I
import nte_check
import nte_shop as S
import nte_vision as V

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(HERE, "farm_log.txt")
SHOT_DIR = os.path.join(HERE, "shots")
STOP_FILE = os.path.join(HERE, "_stop.flag")   # 控制页面用它让脚本优雅退出


def stop_requested():
    return os.path.exists(STOP_FILE)

# ---- 规范 3840x2160 空间里的坐标 ----
START_POINT = (3436, 2012)          # 开始营业
ROW_CENTER_X = 344
# 卡片行高 224，相邻行间距 234（由 1-1 顶部 680、1-3 顶部 1148 实测推得）
ROW_TOP_11 = 680
ROW_PITCH = 234
ROW_Y = {f"1-{i}": ROW_TOP_11 + (i - 1) * ROW_PITCH + 112 for i in range(7)}

DEMAND_TH = 0.65
CHAPTER_HEADER = (344, 351)   # 左侧第一章标题「目标是咖啡大师」的中心（规范坐标）
MIN_VITALITY = 1           # 活力到 0 才停；不足一轮消耗也能开，游戏会按拥有量折算奖励
LEVEL_TH = 0.60          # 原生 4K 下 1-1 得 1.00、其它关 0.03~0.15；低分辨率重采样会降到 ~0.73

user32 = ctypes.WinDLL("user32", use_last_error=True)

# 用 pythonw.exe 跑时没有控制台，sys.stdout 是 None，print 会炸；这里兜住。
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = sys.stdout


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    try:
        print(line, flush=True)
    except Exception:                                    # noqa: BLE001
        pass
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:                                    # noqa: BLE001
        pass


def shot(img, name):
    """存一张缩略截图。用 imencode+tofile 是因为 cv2.imwrite 不支持中文路径。"""
    os.makedirs(SHOT_DIR, exist_ok=True)
    p = os.path.join(SHOT_DIR, name + ".png")
    try:
        ok, buf = cv2.imencode(".png", cv2.resize(img, (1280, 720)))
        if ok:
            buf.tofile(p)
    except Exception:                                    # noqa: BLE001
        pass
    return p


def grab(hwnd):
    return S.canon(nte_capture.grab(hwnd))


def to_screen(pt, hwnd):
    cx, cy, cw, ch = nte_capture.client_rect(hwnd)
    return int(cx + pt[0] * cw / S.CAN[0]), int(cy + pt[1] * ch / S.CAN[1])


def click_raw(sx, sy, hold=0.06, settle=0.15):
    """把光标移过去、停一下、再点一次。"""
    user32.SetCursorPos(sx, sy)
    time.sleep(settle)
    d = I._send(I._mouse_event(I.MOUSEEVENTF_LEFTDOWN))
    time.sleep(hold)
    u = I._send(I._mouse_event(I.MOUSEEVENTF_LEFTUP))
    return d and u


def click_point(hwnd, pt, label="", hold=0.07):
    """移动光标 -> 确认游戏在前台 -> 点击。

    每次点击前都必须重抢前台：否则一旦有别的窗口（比如控制台）拿到焦点，
    第一次点击会被当成「激活窗口」而吞掉，游戏收不到。
    """
    if not ensure_fg(hwnd):
        log(f"    !! 抢前台失败，跳过点击 {label}")
        return False
    sx, sy = to_screen(pt, hwnd)
    ok = click_raw(sx, sy, hold=hold)
    log(f"    点击 {label} ({sx},{sy}) -> {'OK' if ok else 'FAIL'}")
    return ok


def region_diff(a, b, box):
    """两个规范帧在指定区域内的平均绝对差，用来判断点击后画面有没有变化。"""
    x0, y0, x1, y1 = box
    ra = a[y0:y1, x0:x1].astype(np.int16)
    rb = b[y0:y1, x0:x1].astype(np.int16)
    return float(np.abs(ra - rb).mean())


CUP_REGION = (2300, 1400, 2700, 1760)
TOMATO_REGION = (3250, 1120, 3540, 1400)


def ensure_fg(hwnd):
    if I.get_foreground() == hwnd:
        return True
    return I.ensure_foreground(hwnd)


def level_corr(img):
    """当前高亮行与 1-1 模板的相似度。"""
    row = V.find_highlight_row(img)
    if row is None:
        return None, None
    mask, _t = V.row_text_mask(img, row)
    tpl = cv2.imread(os.path.join(V.ASSETS, "row_mask_1_1.png"), cv2.IMREAD_GRAYSCALE)
    corr, _a, _b = V.mask_similarity(mask, tpl)
    return corr, row


def wait_for(fn, timeout, interval=0.45, desc=""):
    t0 = time.time()
    while time.time() - t0 < timeout:
        r = fn()
        if r is not None:
            log(f"  ✓ {desc} ({time.time()-t0:.1f}s)")
            return r
        time.sleep(interval)
    log(f"  ✗ {desc} 超时({timeout:.0f}s)")
    return None


def scroll_list(hwnd, notches, up=True):
    """在关卡列表上滚动。实测游戏里一个滚轮档位约 1/4 行，所以要少量多次地滚。"""
    try:
        sx, sy = to_screen((ROW_CENTER_X, 1200), hwnd)
        I.ensure_foreground(hwnd)
        user32.SetCursorPos(sx, sy)
        time.sleep(0.10)
        for _ in range(notches):
            I.wheel(120 if up else -120, hwnd=hwnd)
            time.sleep(0.04)
        time.sleep(0.30)
        return True
    except Exception as ex:                              # noqa: BLE001
        log(f"  滚动列表失败: {ex}")
        return False


def toggle_first_chapter(hwnd):
    """点第一章标题「目标是咖啡大师」展开/收起该章节。

    章节被收起时，1-0~1-6 这些行是整块隐藏的，滚多少都找不到 1-1。
    """
    log("   点击章节标题「目标是咖啡大师」（展开/收起）")
    click_point(hwnd, CHAPTER_HEADER, "章节标题")
    time.sleep(1.0)


def select_level_11(hwnd, max_try=10):
    """确保当前选中的是 1-1，返回标题相似度。

    依次尝试：标题已对 -> 行已可见就点 -> 向上滚 -> 展开章节 -> 继续滚。
    章节收起、列表被滚走这两种情况都能自己恢复，全程不会因为"不是 1-1"退出。
    """
    tpl_path = os.path.join(V.ASSETS, "row_mask_1_1.png")

    for attempt in range(1, max_try + 1):
        img = grab(hwnd)
        ts = V.level_title_score(img)
        if ts >= V.LEVEL_TITLE_TH:
            log(f"标题确认当前就是 1-1（{ts:.2f}）")
            return ts

        corr, row = V.find_11_row(img, tpl_path)
        if row is not None and corr >= LEVEL_TH:
            cx, cy = row[0] + row[2] // 2, row[1] + row[3] // 2
            log(f"-- 第 {attempt} 次: 1-1 行已可见 {row}，点击中心 ({cx},{cy}) --")
            click_point(hwnd, (cx, cy), "1-1 行(按内容定位)")
            time.sleep(0.9)
            continue

        log(f"-- 第 {attempt} 次: 标题={ts:.2f}，1-1 不可见(corr={corr:.2f}) --")
        if attempt in (3, 7):
            # 章节很可能是收起的，试着展开（跑到第二次时说明展开没用，再切回去）
            toggle_first_chapter(hwnd)
        scroll_list(hwnd, 6, up=True)

    img = grab(hwnd)
    return V.level_title_score(img)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--serve", type=int, default=2)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-select", action="store_true")
    ap.add_argument("--rounds", type=int, default=1, help="连续刷几轮")
    ap.add_argument("--until-empty", action="store_true", help="刷到活力不足为止")
    args = ap.parse_args()

    try:
        os.remove(LOG_PATH)
    except OSError:
        pass

    log("=" * 58)
    log(f"启动 serve={args.serve} dry_run={args.dry_run}")

    hwnd = nte_capture.find_game_window()
    if hwnd is None:
        log("找不到「异环」窗口")
        return 2
    win = S.window_info(hwnd)
    log(f"hwnd={hwnd} 窗口位置=({win['x']},{win['y']}) 客户区={win['w']}x{win['h']} "
        f"缩放=({win['kx']},{win['ky']}) 宽高比={win['aspect']}")
    if not win["is_16x9"]:
        log(f"  !! 画面宽高比 {win['aspect']} 不是 16:9，UI 定位可能偏移，"
            f"建议用 build_shop_templates.py 在该分辨率下重建模板")

    if not ensure_fg(hwnd):
        log("抢前台失败，中止")
        return 2
    log("已切到前台")

    # ---------- 0. 判断当前状态 ----------
    img = grab(hwnd)
    in_game, _ = S.in_minigame(img)

    if in_game:
        log("已经在小游戏里，直接开始服务（不再切关卡/不按开始）")
        got = img
        shot(got, "03_ingame")
    else:
        shot(img, "01_prep")

        # ---------- 1+2. 选中 1-1 并校验 ----------
        # 原则：只要还没选中 1-1 就一直点，不因为"不是 1-1"退出。
        meta = nte_check.load_meta()
        res = None
        for attempt in range(1, 7):
            if not args.no_select:
                select_level_11(hwnd)
            time.sleep(0.4)
            img = grab(hwnd)
            # 指定轮数模式不把活力当门槛（0 也照跑）；刷空模式才校验
            res = nte_check.check(img, meta, require_vitality=args.until_empty)
            log(f"[校验 {attempt}] 1-1={res['level'].get('ok')} "
                f"corr={res['level'].get('corr')} "
                f"雇员={res['employees'].get('ok')} "
                f"活力={res['vitality'].get('value')}/{res['vitality'].get('max')}")
            if res["ok"]:
                shot(img, "02_level_11")
                break
            if res["level"].get("ok") is False:
                log("  还没选中 1-1，继续点")
                time.sleep(0.7)
                continue
            # 关卡对了，是雇员/活力不满足——这种重试也没用
            log("条件不满足: " + "; ".join(res["problems"]))
            return 3
        else:
            log("连续 6 次都没能选中 1-1，中止")
            shot(grab(hwnd), "02_select_failed")
            return 3

        if args.until_empty:
            v = res["vitality"].get("value") or 0
            log(f"刷空模式：当前活力 {v}，只要大于 0 就继续开新的一轮")
            if v < MIN_VITALITY:   # 只有真正为 0 才停，剩 1 点也继续刷
                log(f"活力已耗尽（{v}），刷空结束")
                return 3

        if args.dry_run:
            log("dry-run，不点击。结束")
            return 0

        # ---------- 3. 开始营业 ----------
        log("-- 点击开始营业 --")

        def entered():
            im = grab(hwnd)
            ok, _f = S.in_minigame(im)
            return im if ok else None

        got = None
        for attempt in range(1, 7):
            click_point(hwnd, START_POINT, "开始营业")
            time.sleep(1.2)
            got = entered()
            if got is not None:
                log(f"  已进入小游戏（第 {attempt} 次点击后）")
                break
            ensure_fg(hwnd)
        if got is None:
            log("多次点击仍未进入小游戏")
            shot(grab(hwnd), "03_start_failed")
            return 4

    shot(got, "03_ingame")
    log(f"  营业额初始={S.read_revenue(got)}")

    # ---------- 4. 服务客人 ----------
    # 客人需求有时限，所以这里只抓「需求搜索区」这一小块做单尺度匹配（~20ms），
    # 不再每轮整屏抓图 + OCR。
    served = 0
    last_probe = 0.0

    for i in range(1, args.serve + 1):
        if stop_requested():
            log("收到停止指令，留在小游戏里退出")
            break
        log(f"-- 等待第 {i} 位客人 --")

        def demand_ready():
            nonlocal last_probe
            t0 = time.time()
            sc, cx, cy = S.demand_probe(hwnd)
            last_probe = (time.time() - t0) * 1000
            return (sc, cx, cy) if sc >= DEMAND_TH else None

        hit = wait_for(demand_ready, 40.0, 0.05, "需求气泡出现")
        if hit is None:
            shot(grab(hwnd), f"04_demand_timeout_{i}")
            break
        sc, cx, cy = hit
        log(f"  需求命中 score={sc:.3f} 位置=({cx},{cy}) "
            f"单次探测 {last_probe:.0f}ms")
        shot(grab(hwnd), f"04_demand_{i}")

        def cleared():
            sc2, _x, _y = S.demand_probe(hwnd)
            return True if sc2 < DEMAND_TH else None

        ok = False
        for attempt in range(1, 4):
            log(f"  交付尝试 {attempt}")
            click_point(hwnd, S.CUP_POINT, "玻璃杯")
            time.sleep(0.30)
            click_point(hwnd, S.TOMATO_POINT, "番茄汁")
            t_srv = time.time()
            if wait_for(cleared, 2.5, 0.05, "需求消失(交付成功)") is not None:
                ok = True
                served += 1
                log(f"  第 {i} 位交付耗时 {time.time()-t_srv:.2f}s")
                time.sleep(0.4)
                break
        if not ok:
            shot(grab(hwnd), f"05_serve_failed_{i}")
            log("  交付失败，停止流程")
            break

    # ---------- 5. ESC 退出 -> 结算页 ----------
    log(f"-- 已服务 {served}/{args.serve}，按 ESC 退出到结算页 --")
    I.press_key(I.VK_ESCAPE)
    time.sleep(0.8)

    def settle_seen():
        return True if S.settle_probe(hwnd) else None

    if wait_for(settle_seen, 10.0, 0.25, "结算页(挑战成功)出现") is None:
        log("  没等到结算页，再按一次 ESC")
        I.press_key(I.VK_ESCAPE)
        time.sleep(1.5)
        shot(grab(hwnd), "06_no_settlement")
    else:
        shot(grab(hwnd), "06_settlement")
        # ---------- 6. 点「领取」 ----------
        log("-- 点击「领取」 --")
        for attempt in range(1, 5):
            click_point(hwnd, S.COLLECT_POINT, "领取")
            time.sleep(0.9)
            if not S.settle_probe(hwnd):
                log(f"  领取成功（第 {attempt} 次点击后）")
                break
            ensure_fg(hwnd)
        else:
            log("  多次点击领取都没离开结算页")
            shot(grab(hwnd), "06_collect_failed")
            return 6

    # ---------- 7. 回到准备页 ----------
    def back_prep():
        im = grab(hwnd)
        ing, _f = S.in_minigame(im)
        if ing or S.settle_probe(hwnd):
            return None
        return im if V.find_highlight_row(im) is not None else None

    final = wait_for(back_prep, 15.0, 0.6, "回到准备页")
    if final is None:
        shot(grab(hwnd), "07_back_failed")
        log("没能确认回到准备页")
        return 5

    shot(final, "07_back_prep")
    r2 = nte_check.check(final, nte_check.load_meta(), require_vitality=False)
    log(f"回页校验: 活力={r2['vitality'].get('value')}/"
        f"{r2['vitality'].get('max')} 当前选中与1-1相似度={r2['level'].get('corr')}")

    # 回到准备页后游戏常常停在别的关卡，这里立刻切回 1-1，
    # 保证下一轮无论如何都是从 1-1 开始。
    corr = select_level_11(hwnd)
    log(f"已重新选中 1-1，相似度={corr}")
    log("结束")
    return 0


if __name__ == "__main__":
    until_empty = "--until-empty" in sys.argv
    rounds = 1
    if "--rounds" in sys.argv:
        try:
            rounds = int(sys.argv[sys.argv.index("--rounds") + 1])
        except (ValueError, IndexError):
            rounds = 1
    if until_empty:
        rounds = 999          # 上限只是防死循环，实际由活力决定

    try:
        os.remove(STOP_FILE)
    except OSError:
        pass

    rc = 0
    done = 0
    for r in range(1, rounds + 1):
        if stop_requested():
            log("收到停止指令，结束")
            rc = 0
            break
        log(f"########## 第 {r} 轮 ##########")
        try:
            rc = main()
        except Exception as ex:                          # noqa: BLE001
            log(f"异常: {type(ex).__name__}: {ex}")
            import traceback
            log(traceback.format_exc())
            rc = 1
        if rc == 3 and until_empty:
            log("活力不足或进入条件不满足，刷空结束")
            rc = 0
            break
        if rc != 0:
            log(f"第 {r} 轮失败 rc={rc}，停止")
            break
        done += 1
        if r < rounds:
            time.sleep(1.5)
    log(f"共完成 {done} 轮，退出码 {rc}")
    raise SystemExit(rc)

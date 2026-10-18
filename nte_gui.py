"""NTE 自动刷关 · 控制面板

- 点「开始」后窗口自动隐藏，脚本在后台跑；跑完自动弹回来。
- 隐藏期间按 F9 可随时停止（游戏里按也有效，系统级热键）。
- 必须管理员权限：游戏 HTGame 是 High 完整性进程，普通权限的模拟点击会被
  Windows UIPI 静默丢弃。本程序启动时会自己请求提权。
"""
import ctypes
import os
import subprocess
import sys
import threading
import time
import tkinter as tk
from ctypes import wintypes
from tkinter import messagebox, ttk

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(HERE, "farm_log.txt")
STOP_FILE = os.path.join(HERE, "_stop.flag")
FARM_SCRIPT = os.path.join(HERE, "nte_farm.py")


def hklog(msg):
    """热键相关日志。面板没有控制台，只能落盘，出问题时好查。"""
    try:
        with open(HOTKEY_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
    except Exception:                                    # noqa: BLE001
        pass

CREATE_NO_WINDOW = 0x08000000
HELP_TEXT = """【刷关要求】这三条缺一不可，脚本每轮开始前都会校验

1. 关卡必须是「1-1 新品练习」
   脚本会自动点选。列表被滚动过、章节被收起过，它都会自己滚回顶部、
   展开章节，再按 1-1 那一行的实际位置点它。

2. 辅助雇员必须同时包含「安魂曲」和「达芙蒂尔」
   少一个脚本就会停下并提示，不会硬刷。
   改法：准备页底部「替换雇员」。

3. 都市活力（右上角 X/700）—— 按运行方式区分

   · 跑指定轮数：不校验活力，0 也照跑
   · 一直刷到活力用完：活力为 0 时停下

   活力只影响结算奖励多少，不影响能否开局。


【每轮流程】

开始营业 → 等客人 → 识别番茄汁需求 → 点右下角玻璃杯 → 点番茄汁利乐包
→ 游戏自动交付 → 再来一位 → ESC → 结算页点「领取」→ 回到准备页
→ 重新选中 1-1（等下一轮）


【热键】

F9   优雅停止：跑完当前这一轮（含领取、回到 1-1）再退出
F7   立即中断：直接杀掉脚本进程。游戏可能停在关卡中间，
     需要自己按一下 ESC 回到准备页，或者等它自然结束。


【常见问题】

· 点「开始」后面板消失：这是正常的，跑完会自动弹回来。
  期间按 F7/F9 也能让它提前回来。

· 运行期间不要遮挡游戏窗口、不要切走：
  DirectX 画面只能从屏幕表面抓取，被别的窗口盖住就识别不到。
  脚本有超时保护，不会乱点，但这一轮会白跑。

· 必须管理员权限：
  游戏进程以「高完整性级别（管理员）」运行，普通权限脚本发出的模拟
  点击会被 Windows 的 UIPI 机制静默丢弃——点了完全没反应。
  面板会自己请求提权。

· 换了分辨率还能用吗：
  16:9 的任意分辨率都自适应（已实测 3840/2560/1920/1600）。
  若改成 21:9 等非 16:9，需要在该分辨率下重新生成模板：
      python build_templates.py
      python build_shop_templates.py

· 提示「选中的不是 1-1」：
  现在会自己点回 1-1；如果连续 6 次都失败才会停，届时把
  farm_log.txt 发出来看现场。

· 日志在哪：
  刷关日志  farm_log.txt
  热键日志  hotkey_log.txt
"""
WM_HOTKEY = 0x0312
HOTKEY_LOG = os.path.join(HERE, 'hotkey_log.txt')
VK_F9 = 0x78
VK_F7 = 0x76
MOD_NOREPEAT = 0x4000

user32 = ctypes.WinDLL("user32", use_last_error=True)

# 4K 屏常见 150%/200% 缩放。不声明 DPI 感知的话，Windows 会把整个窗口
# 位图放大，文字会发虚；声明后按真实像素排版，再按缩放比放大控件尺寸。
try:
    ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)   # per-monitor aware
except Exception:                                        # noqa: BLE001
    try:
        user32.SetProcessDPIAware()
    except Exception:                                    # noqa: BLE001
        pass


def dpi_scale(root):
    """当前窗口所在显示器的缩放比（1.0 = 100%，1.5 = 150%）。"""
    try:
        hwnd = user32.GetParent(root.winfo_id()) or root.winfo_id()
        dpi = user32.GetDpiForWindow(hwnd)
        return (dpi / 96.0) if dpi else 1.0
    except Exception:                                    # noqa: BLE001
        return 1.0


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:                                    # noqa: BLE001
        return False


def relaunch_as_admin():
    params = " ".join(f'"{a}"' for a in sys.argv)
    ctypes.windll.shell32.ShellExecuteW(
        None, "runas", sys.executable, params, HERE, 1)


def pythonw_path():
    p = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return p if os.path.exists(p) else sys.executable


class App:
    def __init__(self, root):
        self.root = root
        self.proc = None
        self.running = False
        self.stop_event = threading.Event()
        self.kill_event = threading.Event()
        self.stop_deadline = None

        root.title("NTE 自动刷关")
        root.configure(bg="#1e2128")

        s = dpi_scale(root)
        root.tk.call("tk", "scaling", s)
        root.geometry(f"{int(520 * s)}x{int(520 * s)}")
        root.minsize(int(460 * s), int(430 * s))

        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TFrame", background="#1e2128")
        style.configure("TLabel", background="#1e2128", foreground="#e8eaed",
                        font=("Microsoft YaHei UI", 10))
        style.configure("Hdr.TLabel", font=("Microsoft YaHei UI", 16, "bold"),
                        foreground="#ffd166")
        style.configure("Sub.TLabel", foreground="#8b93a1",
                        font=("Microsoft YaHei UI", 9))
        style.configure("Start.TButton", font=("Microsoft YaHei UI", 11, "bold"),
                        padding=(10, 8))
        style.configure("Stop.TButton", font=("Microsoft YaHei UI", 11),
                        padding=(10, 8))
        style.configure("TCheckbutton", background="#1e2128",
                        foreground="#e8eaed", font=("Microsoft YaHei UI", 10))

        wrap = ttk.Frame(root, padding=18)
        wrap.pack(fill="both", expand=True)

        ttk.Label(wrap, text="NTE · 店长特供自动刷关", style="Hdr.TLabel").pack(anchor="w")
        self.admin_lbl = ttk.Label(
            wrap, text="管理员权限：已获取" if is_admin() else "管理员权限：未获取（点击开始会请求）",
            style="Sub.TLabel")
        self.admin_lbl.pack(anchor="w", pady=(2, 2))

        self.game_lbl = ttk.Label(wrap, text="游戏窗口：检测中…", style="Sub.TLabel")
        self.game_lbl.pack(anchor="w", pady=(0, 12))

        # ---- 运行方式 ----
        box = ttk.Frame(wrap)
        box.pack(fill="x", pady=(0, 12))

        self.mode = tk.StringVar(value="rounds")
        r1 = ttk.Frame(box)
        r1.pack(fill="x", pady=3)
        ttk.Radiobutton(r1, text="跑指定轮数", variable=self.mode, value="rounds",
                        command=self.refresh_mode).pack(side="left")
        self.spin = ttk.Spinbox(r1, from_=1, to=200, width=6,
                                font=("Microsoft YaHei UI", 10))
        self.spin.set(5)
        self.spin.pack(side="left", padx=8)
        ttk.Label(r1, text="轮（每轮服务 2 位客人）", style="Sub.TLabel").pack(side="left")

        r2 = ttk.Frame(box)
        r2.pack(fill="x", pady=3)
        ttk.Radiobutton(r2, text="一直刷到活力用完（到 0）", variable=self.mode,
                        value="empty", command=self.refresh_mode).pack(side="left")

        # ---- 状态 ----
        self.status = ttk.Label(wrap, text="状态：待机", font=("Microsoft YaHei UI", 11, "bold"),
                                foreground="#7ee787")
        self.status.pack(anchor="w", pady=(4, 8))

        # ---- 按钮 ----
        btns = ttk.Frame(wrap)
        btns.pack(fill="x", pady=(0, 12))
        self.start_btn = ttk.Button(btns, text="开始刷关", style="Start.TButton",
                                    command=self.start)
        self.start_btn.pack(side="left", expand=True, fill="x", padx=(0, 6))
        self.stop_btn = ttk.Button(btns, text="停止", style="Stop.TButton",
                                   command=self.request_stop, state="disabled")
        self.stop_btn.pack(side="left", expand=True, fill="x", padx=(6, 0))

        # ---- 日志 ----
        ttk.Label(wrap, text="运行日志", style="Sub.TLabel").pack(anchor="w")
        self.log = tk.Text(wrap, height=12, bg="#15171c", fg="#c9d1d9",
                           insertbackground="#c9d1d9", relief="flat", wrap="word",
                           font=("Consolas", 9))
        self.log.pack(fill="both", expand=True, pady=(4, 0))
        self.log.configure(state="disabled")

        self.tip = ttk.Label(wrap, text="运行中窗口隐藏：F9 优雅停止（跑完本轮）· F7 立即中断",
                             style="Sub.TLabel")
        self.tip.pack(anchor="w", pady=(8, 0))

        self.help_btn = ttk.Button(wrap, text="? 刷关要求与帮助", command=self.show_help)
        self.help_btn.pack(anchor="w", pady=(6, 0))

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.start_hotkey()
        self.refresh_mode()
        self.tail_log(force=True)
        self.refresh_window_info()

    # ------------------------------------------------ 帮助
    def show_help(self):
        win = tk.Toplevel(self.root)
        win.title("刷关要求与帮助")
        win.configure(bg="#1e2128")
        s = dpi_scale(self.root)
        w, h = int(640 * s), int(620 * s)
        win.geometry(f"{w}x{h}")
        win.transient(self.root)

        frame = ttk.Frame(win, padding=12)
        frame.pack(fill="both", expand=True)
        txt = tk.Text(frame, bg="#15171c", fg="#c9d1d9", relief="flat",
                      wrap="word", font=("Microsoft YaHei UI", 10),
                      padx=10, pady=8)
        sb = ttk.Scrollbar(frame, command=txt.yview)
        txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        txt.pack(side="left", fill="both", expand=True)
        txt.insert("1.0", HELP_TEXT)
        txt.configure(state="disabled")

        ttk.Button(win, text="知道了", command=win.destroy).pack(pady=(0, 10))
        win.lift()
        win.focus_force()

    # ------------------------------------------------ 窗口探测
    def refresh_window_info(self):
        """每次回到面板都重新探测，游戏改了分辨率/换显示器后能自动跟上。"""
        try:
            import nte_capture
            import nte_shop
            hwnd = nte_capture.find_game_window()
            if hwnd is None:
                self.game_lbl.configure(text="游戏窗口：未检测到「异环」")
                self.game_hwnd = None
            else:
                self.game_hwnd = hwnd
                info = nte_shop.window_info(hwnd)
                warn = "" if info["is_16x9"] else "  ⚠ 非 16:9"
                self.game_lbl.configure(
                    text=f"游戏窗口：{info['w']}×{info['h']}  "
                         f"位置 ({info['x']}, {info['y']}){warn}")
        except Exception:                                # noqa: BLE001
            self.game_lbl.configure(text="游戏窗口：探测失败")
        if not self.running:
            self.root.after(2000, self.refresh_window_info)

    # ------------------------------------------------ UI
    def refresh_mode(self):
        state = "normal" if self.mode.get() == "rounds" else "disabled"
        try:
            self.spin.configure(state=state)
        except tk.TclError:
            pass

    def set_status(self, text, color="#7ee787"):
        self.status.configure(text=f"状态：{text}", foreground=color)

    def tail_log(self, force=False):
        try:
            if os.path.exists(LOG_PATH):
                with open(LOG_PATH, encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()[-200:]
                txt = "".join(lines)
                self.log.configure(state="normal")
                self.log.delete("1.0", "end")
                self.log.insert("end", txt)
                self.log.see("end")
                self.log.configure(state="disabled")
        except Exception:                                # noqa: BLE001
            pass
        if self.running:
            self.root.after(800, self.tail_log)

    # ------------------------------------------------ 开始 / 停止
    def start(self):
        if self.running:
            return
        if not is_admin():
            if messagebox.askyesno("需要管理员权限",
                                   "模拟点击需要管理员权限，是否现在提权？"):
                relaunch_as_admin()
            return

        args = [pythonw_path(), FARM_SCRIPT, "--serve", "2"]
        if self.mode.get() == "empty":
            args.append("--until-empty")
            label = "刷空活力"
        else:
            try:
                n = int(self.spin.get())
            except ValueError:
                messagebox.showerror("轮数不对", "请输入 1~200 之间的整数")
                return
            n = max(1, min(200, n))
            args += ["--rounds", str(n)]
            label = f"{n} 轮"

        try:
            os.remove(STOP_FILE)
        except OSError:
            pass
        try:
            os.remove(LOG_PATH)
        except OSError:
            pass

        try:
            self.proc = subprocess.Popen(
                args, cwd=HERE, creationflags=CREATE_NO_WINDOW,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL)
        except Exception as ex:                          # noqa: BLE001
            messagebox.showerror("启动失败", str(ex))
            return

        self.running = True
        self.stop_deadline = None
        self.stop_event.clear()
        self.set_status(f"运行中（{label}）", "#ffd166")
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.tail_log(force=True)
        # 让状态刷新一下再隐藏，避免"点了没反应"的错觉
        self.root.after(400, self.hide_window)

    def hide_window(self):
        self.root.withdraw()
        self.root.after(500, self.tick)

    def tick(self):
        if not self.running:
            return
        if self.proc is not None and self.proc.poll() is not None:
            self.finish()
            return
        if self.stop_deadline is not None and time.time() > self.stop_deadline:
            self.hard_kill()
            return
        self.root.after(500, self.tick)

    def request_stop(self):
        try:
            open(STOP_FILE, "w").close()
        except OSError:
            pass
        if not self.running:
            # 不在面板启动的任务也有可能正在跑（外部启动），停止标记同样有效
            self.set_status("已发出优雅停止信号（当前轮结束后退出）", "#ffa657")
            return
        self.set_status("正在停止（当前轮结束后退出）", "#ffa657")
        self.stop_btn.configure(state="disabled")
        if self.stop_deadline is None:
            self.stop_deadline = time.time() + 45   # 兜底：最多再等 45 秒
        self.root.deiconify()
        self.tail_log(force=True)

    def hard_kill(self):
        """立即中断：杀掉所有跑着 nte_farm.py 的进程。

        不能只杀 self.proc——如果脚本是从命令行/别处启动的，面板手里没有句柄，
        只杀自己的子进程会出现"按了 F7 没反应"。
        """
        try:
            open(STOP_FILE, "w").close()
        except OSError:
            pass

        killed = 0
        # 先杀自己的子进程
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.kill()
                killed += 1
            except Exception:                            # noqa: BLE001
                pass
        # 再按命令行匹配，把其它来源启动的刷关进程一并清掉
        try:
            ps = ("Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe'\" "
                  "| Where-Object { $_.CommandLine -like '*nte_farm.py*' } "
                  "| ForEach-Object { Stop-Process -Id $_.ProcessId -Force; "
                  "Write-Output $_.ProcessId }")
            r = subprocess.run(
                ["powershell.exe", "-NoProfile", "-Command", ps],
                capture_output=True, text=True, creationflags=CREATE_NO_WINDOW,
                timeout=15)
            killed += len([x for x in (r.stdout or "").split() if x.strip().isdigit()])
        except Exception as ex:                          # noqa: BLE001
            hklog(f"F7 清理残留进程失败: {ex}")

        hklog(f"F7 强制中断，清理进程数={killed}")
        if self.running:
            self.set_status(f"已强制中断（清理 {killed} 个进程）", "#ff7b72")
            self.finish(killed=True)
        else:
            msg = (f"已清理 {killed} 个残留刷关进程" if killed
                   else "没有正在运行的刷关进程")
            self.set_status(msg, "#ffa657" if killed else "#7ee787")
            self.root.deiconify()

    def finish(self, killed=False):
        self.running = False
        self.stop_deadline = None
        try:
            if os.path.exists(STOP_FILE):
                os.remove(STOP_FILE)
        except OSError:
            pass
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.after(300, lambda: self.root.attributes("-topmost", False))
        self.tail_log(force=True)
        self.refresh_window_info()

        rounds = 0
        reason = None
        try:
            with open(LOG_PATH, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            for ln in lines:
                if ln.startswith("##########"):
                    rounds += 1
            # 找出真正的失败原因，别把出错也显示成"已完成"
            for ln in reversed(lines[-60:]):
                s = ln.strip()
                if any(k in s for k in ("失败", "异常", "条件不满足", "超时", "!!")):
                    reason = s
                    break
        except OSError:
            pass

        rc = self.proc.returncode if self.proc is not None else None
        if killed:
            self.set_status(f"已停止（完成 {max(rounds - 1, 0)} 轮）", "#ffa657")
        elif rc not in (0, None):
            msg = "；".join(x for x in (f"第 {rounds} 轮中断", reason) if x)
            self.set_status(msg, "#ff7b72")
        elif reason:
            msg = "；".join(x for x in (f"完成 {max(rounds - 1, 0)} 轮后停止", reason) if x)
            self.set_status(msg, "#ffa657")
        else:
            self.set_status(f"已完成（{rounds} 轮）", "#7ee787")

    # ------------------------------------------------ 全局热键 F9
    def start_hotkey(self):
        """F9 优雅停止（跑完当前轮）；F7 立即中断（直接杀掉脚本进程）。"""
        def worker():
            id_soft, id_hard = 1, 2
            ok1 = user32.RegisterHotKey(None, id_soft, MOD_NOREPEAT, VK_F9)
            ok2 = user32.RegisterHotKey(None, id_hard, MOD_NOREPEAT, VK_F7)
            hklog(f"注册热键: F9={bool(ok1)} F7={bool(ok2)}  err={ctypes.get_last_error()}")
            if not (ok1 or ok2):
                hklog("热键全部注册失败，F7/F9 不可用")
                return
            try:
                msg = wintypes.MSG()
                while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                    if msg.message == WM_HOTKEY:
                        hklog(f"收到热键 id={msg.wParam}")
                        if msg.wParam == id_hard:
                            self.kill_event.set()
                        else:
                            self.stop_event.set()
            finally:
                user32.UnregisterHotKey(None, id_soft)
                user32.UnregisterHotKey(None, id_hard)

        threading.Thread(target=worker, daemon=True).start()

        def watch():
            # 注意：这里不做 self.running 判断。刷关进程可能是从别处（命令行、
            # 上一版脚本）启动的，面板手里没有子进程句柄；如果按 running 过滤，
            # 就会出现"按 F7 完全没反应"。
            if self.kill_event.is_set():
                self.kill_event.clear()
                self.hard_kill()
            if self.stop_event.is_set():
                self.stop_event.clear()
                self.request_stop()
            self.root.after(150, watch)

        watch()

    def on_close(self):
        if self.running:
            if not messagebox.askyesno("仍在运行",
                                       "刷关还在进行，确定要停止并退出吗？"):
                return
            self.request_stop()
            time.sleep(0.5)
            self.hard_kill()
        self.root.destroy()


def focus_existing():
    """已经有面板在跑时，把那个窗口拉到前台——否则用户双击会"没反应"。"""
    try:
        u = ctypes.WinDLL("user32", use_last_error=True)
        u.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
        u.FindWindowW.restype = wintypes.HWND
        hwnd = u.FindWindowW(None, "NTE 自动刷关")
        if not hwnd:
            return False
        u.ShowWindow(hwnd, 9)          # SW_RESTORE
        u.SetForegroundWindow(hwnd)
        u.BringWindowToTop(hwnd)
        return True
    except Exception:                                    # noqa: BLE001
        return False


def already_running():
    """单实例保护：避免重复双击开出多个面板，互相抢着跑。"""
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW(None, False, "NTE_AutoFarm_GUI_SingleInstance")
        return ctypes.get_last_error() == 183   # ERROR_ALREADY_EXISTS
    except Exception:                                    # noqa: BLE001
        return False


def main():
    if not is_admin():
        relaunch_as_admin()
        return
    if already_running():
        focus_existing()
        return
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()

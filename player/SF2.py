"""SF2 — 玩家端連線啟動器（純連線，無任何伺服器檔）。

放在客端根目錄（與 Binaries\\Win32\\sf2.exe 同一層）。用整合包內的
python\\pythonw.exe 執行，不需要玩家另外裝 Python。
只做一件事：用正確參數把客端指向主機 IP 啟動，並提醒關掉會害 XIGNCODE
報錯的背景程式。
"""
import ctypes
import json
import os
import subprocess
import sys
import tkinter as tk
from tkinter import filedialog, messagebox

try:  # keep the window crisp on scaled displays
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    try: ctypes.windll.user32.SetProcessDPIAware()
    except Exception: pass

BASE = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))
CONFIG = os.path.join(BASE, "玩家設定.json")

# 主機 IP 預設值：發包前把這裡改成你的 VPS / VPN IP，玩家就不用自己填。
DEFAULT_IP = "127.0.0.1"
LANG = "TWN"
PORTS = {"server": 28080, "clan": 28081, "messenger": 28082}

BG = "#0e1217"; CARD = "#171d26"; LINE = "#28313d"; INK = "#e8edf3"; DIM = "#8a97a8"
GREEN = "#2fb85f"; GREEN2 = "#36d06c"; ACCENT = "#ff8a3d"


def load_cfg():
    try:
        with open(CONFIG, encoding="utf-8") as f: return json.load(f)
    except Exception:
        return {}


def save_cfg(c):
    try:
        with open(CONFIG, "w", encoding="utf-8") as f: json.dump(c, f, ensure_ascii=False, indent=2)
    except Exception: pass


def find_client(root):
    for p in (os.path.join(root, "Binaries", "Win32", "sf2.exe"),
              os.path.join(root, "sf2.exe")):
        if os.path.isfile(p): return p
    return None


class Launcher(tk.Tk):
    def __init__(self):
        super().__init__()
        self.cfg = load_cfg()
        self.title("SF2 連線啟動器")
        self.configure(bg=BG)
        self.resizable(False, False)
        w, h = 520, 400
        self.geometry("%dx%d+%d+%d" % (w, h, (self.winfo_screenwidth() - w) // 2,
                                       (self.winfo_screenheight() - h) // 2))
        self._build()

    def _build(self):
        tk.Frame(self, bg=ACCENT, height=4).pack(fill="x")
        wrap = tk.Frame(self, bg=BG); wrap.pack(fill="both", expand=True, padx=26, pady=22)

        tk.Label(wrap, text="SF2", bg=BG, fg=INK,
                 font=("Microsoft JhengHei UI", 24, "bold")).pack(anchor="w")
        tk.Label(wrap, text="SPECIAL FORCE 2 · 台服連線", bg=BG, fg=ACCENT,
                 font=("Consolas", 10, "bold")).pack(anchor="w", pady=(0, 16))

        card = tk.Frame(wrap, bg=CARD, highlightbackground=LINE, highlightthickness=1)
        card.pack(fill="x")
        inner = tk.Frame(card, bg=CARD); inner.pack(fill="x", padx=16, pady=14)

        tk.Label(inner, text="主機 IP", bg=CARD, fg=DIM,
                 font=("Microsoft JhengHei UI", 10)).grid(row=0, column=0, sticky="w")
        self.ip = tk.Entry(inner, bg="#0e141b", fg=INK, insertbackground=INK, bd=0,
                           relief="flat", font=("Consolas", 13))
        self.ip.insert(0, self.cfg.get("ip", DEFAULT_IP))
        self.ip.grid(row=1, column=0, columnspan=2, sticky="we", ipady=6, pady=(3, 0))
        inner.columnconfigure(0, weight=1)

        self.clidir = self.cfg.get("client_dir", BASE)
        self.status = tk.Label(wrap, text="", bg=BG, fg=DIM, font=("Microsoft JhengHei UI", 9),
                               wraplength=468, justify="left")
        self.status.pack(anchor="w", pady=(12, 0))

        tips = ("進入前請關掉：遠端軟體(AnyDesk/TeamViewer/向日葵)、模擬器、"
                "加速器/VPN 以外的程式、OBS/Discord 覆蓋層——否則 XIGNCODE 會擋。")
        tk.Label(wrap, text=tips, bg=BG, fg=DIM, font=("Microsoft JhengHei UI", 9),
                 wraplength=468, justify="left").pack(anchor="w", pady=(10, 0))

        row = tk.Frame(wrap, bg=BG); row.pack(fill="x", side="bottom")
        self.start = tk.Button(row, text="進入遊戲", command=self.play, bg=GREEN, fg="white",
                               activebackground=GREEN2, activeforeground="white", bd=0, relief="flat",
                               font=("Microsoft JhengHei UI", 15, "bold"), cursor="hand2")
        self.start.pack(side="left", fill="x", expand=True, ipady=10)
        tk.Button(row, text="定位客戶端", command=self.pick, bg=CARD, fg=INK,
                  activebackground=LINE, bd=0, relief="flat", font=("Microsoft JhengHei UI", 10),
                  cursor="hand2").pack(side="left", padx=(8, 0), ipady=10, ipadx=10)

        self._refresh()

    def _refresh(self):
        exe = find_client(self.clidir)
        if exe:
            self.status.config(text="客戶端：" + os.path.dirname(exe), fg=DIM)
            self.start.config(state="normal", bg=GREEN, text="進入遊戲")
        else:
            self.status.config(text="找不到 sf2.exe，請點「定位客戶端」選客端資料夾（含 Binaries\\Win32）。", fg=ACCENT)
            self.start.config(state="disabled", bg="#2a3a30", text="請先定位客戶端")

    def pick(self):
        folder = filedialog.askdirectory(title="選擇客端資料夾（含 Binaries\\Win32\\sf2.exe）",
                                         initialdir=self.clidir or "C:\\")
        if not folder: return
        if not find_client(folder):
            messagebox.showerror("目錄無效", "這個資料夾找不到 sf2.exe。", parent=self); return
        self.clidir = folder; self.cfg["client_dir"] = folder; save_cfg(self.cfg); self._refresh()

    def play(self):
        exe = find_client(self.clidir)
        if not exe:
            messagebox.showinfo("定位客戶端", "請先點「定位客戶端」。", parent=self); return
        ip = self.ip.get().strip() or DEFAULT_IP
        self.cfg["ip"] = ip; save_cfg(self.cfg)
        args = [exe, "SFFrontEnd_new",
                "-ServerIP=%s" % ip, "-ServerPort=%d" % PORTS["server"],
                "-ClanServerIP=%s" % ip, "-ClanServerPort=%d" % PORTS["clan"],
                "-MessengerServerIP=%s" % ip, "-MessengerServerPort=%d" % PORTS["messenger"],
                "-GameStartFrom=0", "-Language=%s" % LANG,
                "-NoSplash", "-nomoviestartup", "-windowed", "-ResX=1280", "-ResY=720"]
        try:
            flags = 0x00000008 if os.name == "nt" else 0   # DETACHED_PROCESS
            subprocess.Popen(args, cwd=os.path.dirname(exe), creationflags=flags, close_fds=True)
        except Exception as e:
            messagebox.showerror("啟動失敗", str(e), parent=self); return
        self.start.config(text="遊戲已啟動", bg="#2a6b43")
        self.status.config(text="已連線到 %s，遊戲視窗開啟中…（若卡住請看上方 XIGNCODE 提醒）" % ip, fg=DIM)


if __name__ == "__main__":
    Launcher().mainloop()

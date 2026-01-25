import os
import sys
import json
import time
import queue
import threading
import subprocess
import ctypes
from ctypes import wintypes
import tempfile
from io import BytesIO

import tkinter as tk
from tkinter import ttk, messagebox

from pynput import mouse
import pystray
from PIL import Image, ImageDraw

def resource_path(relative_name):
    """
    Works for both:
    - normal .py run
    - PyInstaller --onefile (uses sys._MEIPASS)
    """
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, relative_name)



# ----------------------------
# Win32 SendInput (keyboard + mouse wheel)
# ----------------------------
user32 = ctypes.WinDLL("user32", use_last_error=True)

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x01000
WHEEL_DELTA = 120

KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004


VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12  # ALT
VK_RETURN = 0x0D

# Fix for Python 3.12 missing wintypes.ULONG_PTR
ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong
# ---- missing wintypes on some Python 3.12 builds ----
LRESULT = ctypes.c_ssize_t  # pointer-sized signed result



class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class INPUT_UNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("union", INPUT_UNION)]


def _check_bool(result, func, args):
    if not result:
        raise ctypes.WinError(ctypes.get_last_error())
    return args


user32.SendInput.errcheck = _check_bool
user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)


def send_key(vk, is_up=False):
    flags = KEYEVENTF_KEYUP if is_up else 0
    inp = INPUT(
        type=INPUT_KEYBOARD,
        union=INPUT_UNION(ki=KEYBDINPUT(wVk=vk, wScan=0, dwFlags=flags, time=0, dwExtraInfo=0)),
    )
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


def send_mouse_wheel(vertical_steps):
    data = ctypes.c_int(vertical_steps * WHEEL_DELTA).value & 0xFFFFFFFF
    inp = INPUT(
        type=INPUT_MOUSE,
        union=INPUT_UNION(
            mi=MOUSEINPUT(dx=0, dy=0, mouseData=data, dwFlags=MOUSEEVENTF_WHEEL, time=0, dwExtraInfo=0)
        ),
    )
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


def send_horizontal_wheel(horizontal_steps):
    data = ctypes.c_int(horizontal_steps * WHEEL_DELTA).value & 0xFFFFFFFF
    inp = INPUT(
        type=INPUT_MOUSE,
        union=INPUT_UNION(
            mi=MOUSEINPUT(dx=0, dy=0, mouseData=data, dwFlags=MOUSEEVENTF_HWHEEL, time=0, dwExtraInfo=0)
        ),
    )
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


def vk_from_token(tok):
    t = tok.strip().lower()
    if t in ("shift",):
        return VK_SHIFT
    if t in ("ctrl", "control"):
        return VK_CONTROL
    if t in ("alt",):
        return VK_MENU
    if t in ("enter", "return"):
        return VK_RETURN

    if len(t) == 1 and t.upper().isalnum():
        return ord(t.upper())

    if t.startswith("f") and t[1:].isdigit():
        n = int(t[1:])
        if 1 <= n <= 24:
            return 0x70 + (n - 1)

    raise ValueError(f"Unknown key token: {tok}")


def press_combo(combo_str):
    parts = [p.strip() for p in combo_str.split("+") if p.strip()]
    vks = [vk_from_token(p) for p in parts]
    for vk in vks:
        send_key(vk, is_up=False)
    for vk in reversed(vks):
        send_key(vk, is_up=True)


def send_text_unicode(text):
    # Types into the currently focused window (browser/Excel/etc.)
    # No PowerShell, no SendKeys popups.
    if not text:
        return

    for ch in text:
        # Normalize newline
        if ch == "\n":
            ch = "\r"

        code = ord(ch)

        down = INPUT(
            type=INPUT_KEYBOARD,
            union=INPUT_UNION(
                ki=KEYBDINPUT(
                    wVk=0,
                    wScan=code,
                    dwFlags=KEYEVENTF_UNICODE,
                    time=0,
                    dwExtraInfo=0,
                )
            ),
        )
        up = INPUT(
            type=INPUT_KEYBOARD,
            union=INPUT_UNION(
                ki=KEYBDINPUT(
                    wVk=0,
                    wScan=code,
                    dwFlags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP,
                    time=0,
                    dwExtraInfo=0,
                )
            ),
        )

        user32.SendInput(1, ctypes.byref(down), ctypes.sizeof(INPUT))
        user32.SendInput(1, ctypes.byref(up), ctypes.sizeof(INPUT))


# ----------------------------
# Win32 Low-Level Mouse Hook (block XBUTTON back/forward)
# ----------------------------
WH_MOUSE_LL = 14
HC_ACTION = 0

WM_XBUTTONDOWN = 0x020B
WM_XBUTTONUP = 0x020C

XBUTTON1 = 0x0001
XBUTTON2 = 0x0002

user32.SetWindowsHookExW.argtypes = (wintypes.INT, wintypes.HANDLE, wintypes.HINSTANCE, wintypes.DWORD)
user32.SetWindowsHookExW.restype = wintypes.HANDLE

# ---- missing wintypes on some Python 3.12 builds ----
LRESULT = ctypes.c_ssize_t  # pointer-sized signed
WPARAM = wintypes.WPARAM if hasattr(wintypes, "WPARAM") else ULONG_PTR
LPARAM = wintypes.LPARAM if hasattr(wintypes, "LPARAM") else ctypes.c_ssize_t

user32.CallNextHookEx.argtypes = (wintypes.HANDLE, wintypes.INT, WPARAM, LPARAM)
user32.CallNextHookEx.restype = LRESULT

user32.UnhookWindowsHookEx.argtypes = (wintypes.HANDLE,)
user32.UnhookWindowsHookEx.restype = wintypes.BOOL

user32.GetMessageW.argtypes = (ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT)
user32.GetMessageW.restype = wintypes.BOOL

user32.TranslateMessage.argtypes = (ctypes.POINTER(wintypes.MSG),)
user32.TranslateMessage.restype = wintypes.BOOL

user32.DispatchMessageW.argtypes = (ctypes.POINTER(wintypes.MSG),)
user32.DispatchMessageW.restype = LRESULT

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.GetModuleHandleW.argtypes = (wintypes.LPCWSTR,)
kernel32.GetModuleHandleW.restype = wintypes.HMODULE


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt", POINT),
        ("mouseData", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


LowLevelMouseProc = ctypes.WINFUNCTYPE(LRESULT, wintypes.INT, WPARAM, LPARAM)


class XButtonBlocker:
    """
    Blocks XBUTTON1/XBUTTON2 from reaching OS (prevents Back/Forward),
    while still letting the app run its macros + detect those buttons.
    """
    def __init__(self, app):
        self.app = app
        self.hook = None
        self.thread = None
        self.stop_flag = False
        self._proc_ref = None  # keep callback alive

    def start(self):
        if self.thread:
            return
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_flag = True
        try:
            if self.hook:
                user32.UnhookWindowsHookEx(self.hook)
                self.hook = None
        except Exception:
            pass

    def _run(self):
        @LowLevelMouseProc
        def proc(nCode, wParam, lParam):
            if nCode == HC_ACTION and not self.stop_flag:
                if wParam in (WM_XBUTTONDOWN, WM_XBUTTONUP):
                    info = ctypes.cast(lParam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                    xb = (info.mouseData >> 16) & 0xFFFF  # HIWORD: XBUTTON1/2

                    if xb == XBUTTON1:
                        name = "xbutton1"
                    elif xb == XBUTTON2:
                        name = "xbutton2"
                    else:
                        name = None

                    if name:
                        if wParam == WM_XBUTTONDOWN:
                            self.app.event_q.put(("detect", name))
                            self.app.engine.on_press(name)
                        else:
                            self.app.engine.on_release(name)

                        if self.app.cfg.get("enabled", True):
                            return 1  # block OS Back/Forward

            return user32.CallNextHookEx(self.hook, nCode, wParam, lParam)

        self._proc_ref = proc
        hmod = kernel32.GetModuleHandleW(None)
        self.hook = user32.SetWindowsHookExW(WH_MOUSE_LL, self._proc_ref, hmod, 0)
        if not self.hook:
            return

        msg = wintypes.MSG()
        while not self.stop_flag and user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))


# ----------------------------
# Config
# ----------------------------
DEFAULT_CONFIG = {
    "enabled": True,
    "repeat_interval_ms": 16,
    "scroll_steps_per_tick": 1,
    "excel_web_mode": "shift_wheel",  # shift_wheel (recommended) or hwheel
    "bindings": {
        "xbutton1": {"mode": "hold", "action": "hscroll_left", "param": ""},
        "xbutton2": {"mode": "hold", "action": "hscroll_right", "param": ""},
    },
}

ACTION_OPTIONS = [
    "none",
    "hscroll_left",
    "hscroll_right",
    "hscroll_left_hwheel",
    "hscroll_right_hwheel",
    "keys",
    "text",
    "run",
]

MODE_OPTIONS = ["press", "hold"]


def load_config(path):
    if not os.path.exists(path):
        return DEFAULT_CONFIG.copy()
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)

        merged = DEFAULT_CONFIG.copy()
        merged.update({k: cfg.get(k, merged[k]) for k in merged.keys() if k != "bindings"})

        # Keep old behavior:
        # - Always keep defaults for xbutton1/xbutton2
        # - Only include other buttons if user explicitly created them before by detection/edit
        saved_bindings = cfg.get("bindings", {}) or {}
        merged_bindings = {}

        # Always include the 2 defaults (and let saved values override)
        for k in ("xbutton1", "xbutton2"):
            merged_bindings[k] = saved_bindings.get(k, DEFAULT_CONFIG["bindings"][k]).copy()

        # Only include other bindings if they exist AND are not "accidentally persisted as defaults"
        # (i.e., user created them earlier by detecting and then saving)
        for k, v in saved_bindings.items():
            if k in ("xbutton1", "xbutton2"):
                continue
            # Only keep if it looks like a real mapping object
            if isinstance(v, dict) and {"mode", "action", "param"}.issubset(set(v.keys())):
                merged_bindings[k] = v.copy()

        merged["bindings"] = merged_bindings
        return merged
    except Exception:
        return DEFAULT_CONFIG.copy()


def save_config(path, cfg):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


# ----------------------------
# Macro engine
# ----------------------------
class MacroEngine:
    def __init__(self, cfg):
        self.cfg = cfg
        self.enabled = bool(cfg.get("enabled", True))
        self.held = {}
        self.lock = threading.Lock()
        self.stop_flag = False

    def set_enabled(self, enabled):
        with self.lock:
            self.enabled = bool(enabled)
            if not self.enabled:
                self.held.clear()

    def update_config(self, cfg):
        with self.lock:
            self.cfg = cfg

    def _do_scroll_action(self, action):
        steps = int(self.cfg.get("scroll_steps_per_tick", 1))
        mode = (self.cfg.get("excel_web_mode") or "shift_wheel").lower()

        if action in ("hscroll_left_hwheel", "hscroll_right_hwheel"):
            if action == "hscroll_left_hwheel":
                send_horizontal_wheel(-steps)
            else:
                send_horizontal_wheel(+steps)
            return

        if mode == "shift_wheel":
            send_key(VK_SHIFT, is_up=False)
            try:
                if action == "hscroll_left":
                    send_mouse_wheel(+steps)
                else:
                    send_mouse_wheel(-steps)
            finally:
                send_key(VK_SHIFT, is_up=True)
        else:
            if action == "hscroll_left":
                send_horizontal_wheel(-steps)
            else:
                send_horizontal_wheel(+steps)

    def run_once(self, action, param):
        if action == "none":
            return
        if action.startswith("hscroll_"):
            self._do_scroll_action(action)
        elif action == "keys":
            if param.strip():
                press_combo(param.strip())
        elif action == "text":
            if param:
                send_text_unicode(param)
        elif action == "run":
            if param.strip():
                subprocess.Popen(param, shell=True)

    def start_hold(self, btn_name):
        with self.lock:
            if not self.enabled or self.stop_flag:
                return
            bind = self.cfg.get("bindings", {}).get(btn_name)
            if not bind:
                return
            if bind.get("mode") != "hold":
                return
            if self.held.get(btn_name):
                return
            self.held[btn_name] = True

        interval = max(8, int(self.cfg.get("repeat_interval_ms", 16))) / 1000.0

        def loop():
            while True:
                with self.lock:
                    if self.stop_flag or not self.enabled or not self.held.get(btn_name, False):
                        break
                    bind2 = self.cfg.get("bindings", {}).get(btn_name, {})
                    action2 = bind2.get("action", "none")
                    param2 = bind2.get("param", "")
                self.run_once(action2, param2)
                time.sleep(interval)

        threading.Thread(target=loop, daemon=True).start()

    def on_press(self, btn_name):
        with self.lock:
            if not self.enabled or self.stop_flag:
                return
            bind = self.cfg.get("bindings", {}).get(btn_name)
            if not bind:
                return
            mode = bind.get("mode", "press")
            action = bind.get("action", "none")
            param = bind.get("param", "")

        if mode == "press":
            self.run_once(action, param)
        else:
            self.start_hold(btn_name)

    def on_release(self, btn_name):
        with self.lock:
            bind = self.cfg.get("bindings", {}).get(btn_name)
            if not bind:
                return
            if bind.get("mode") == "hold":
                self.held[btn_name] = False

    def shutdown(self):
        with self.lock:
            self.stop_flag = True
            self.held.clear()


# ----------------------------
# GUI + tray app
# ----------------------------
BUTTON_NAME_MAP = {
    mouse.Button.left: "left",
    mouse.Button.right: "right",
    mouse.Button.middle: "middle",
    mouse.Button.x1: "xbutton1",
    mouse.Button.x2: "xbutton2",
}


def center_window(win, width=None, height=None, relative_to=None):
    win.update_idletasks()

    if width is None:
        width = win.winfo_reqwidth()
    if height is None:
        height = win.winfo_reqheight()

    if relative_to is None:
        sw = win.winfo_screenwidth()
        sh = win.winfo_screenheight()
        x = (sw - width) // 2
        y = (sh - height) // 2
    else:
        relative_to.update_idletasks()
        px = relative_to.winfo_rootx()
        py = relative_to.winfo_rooty()
        pw = relative_to.winfo_width()
        ph = relative_to.winfo_height()
        x = px + (pw - width) // 2
        y = py + (ph - height) // 2

    win.geometry(f"{width}x{height}+{x}+{y}")

# Icon functions
def _icon_png_path():
    return resource_path("app_icon.png")

def load_icon_pil(size=64):
    """
    Loads app_icon.png as a PIL image for tray usage.
    """
    p = _icon_png_path()
    im = Image.open(p).convert("RGBA")
    if size and im.size != (size, size):
        im = im.resize((size, size), Image.LANCZOS)
    return im


def ensure_icon_ico():
    """
    Creates a .ico from app_icon.png for best Windows taskbar/title icon compatibility.
    Returns the ico path.
    """
    p = _icon_png_path()
    im = Image.open(p).convert("RGBA")

    # Save ico in temp (safe; no admin needed)
    ico_path = os.path.join(tempfile.gettempdir(), "mousemacro_app_icon.ico")
    im.save(
        ico_path,
        format="ICO",
        sizes=[(256, 256), (128, 128), (64, 64), (32, 32), (16, 16)],
    )
    return ico_path


class App:
    def __init__(self):
        self.cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mouse_macros.json")
        self.cfg = load_config(self.cfg_path)

        self.event_q = queue.Queue()
        self.engine = MacroEngine(self.cfg)

        self.root = tk.Tk()
        self.root.title("Mouse Macro Manager")
        # --- Set Title bar + Taskbar icon ---
        try:
            # Titlebar icon (png) - keep reference to avoid GC
            self._tk_icon = tk.PhotoImage(file=_icon_png_path())
            self.root.iconphoto(True, self._tk_icon)
        except Exception:
            pass

        try:
            # Taskbar icon (ico) - best on Windows
            self._ico_path = ensure_icon_ico()
            self.root.iconbitmap(self._ico_path)
        except Exception:
            pass

        # X button closes the app (NOT hide-to-tray)
        self.root.protocol("WM_DELETE_WINDOW", self.quit_app)

        self._build_ui()

        # Center and size like the shared script
        self.root.update_idletasks()
        center_window(self.root, width=760, height=self._compute_compact_height())

        # Start the XBUTTON blocker hook (prevents Back/Forward)
        self.xblocker = XButtonBlocker(self)
        self.xblocker.start()

        self.listener = mouse.Listener(on_click=self._on_click)
        self.listener.start()

        self.tray = None
        self._start_tray()

        self._poll_events()

    def _compute_compact_height(self):
        self.root.update_idletasks()
        h = self.root.winfo_reqheight() + 6
        return max(360, min(520, h))

    def _autosize_tree_height(self):
        rows = len(self.cfg.get("bindings", {}))
        h = max(8, min(12, rows if rows > 0 else 8))
        self.tree.configure(height=h)

    def _popup_fit_to_parent(self, dlg, preferred_w, preferred_h):
        self.root.update_idletasks()
        dlg.update_idletasks()

        pw = max(1, self.root.winfo_width())
        ph = max(1, self.root.winfo_height())

        # Parent-based max bounds (keep popups inside)
        max_w = max(420, pw - 40)
        max_h = max(260, ph - 40)

        # Dialog minimum required size (prevents cropping)
        req_w = dlg.winfo_reqwidth()
        req_h = dlg.winfo_reqheight()

        # Ensure we never go under required size
        w = max(req_w, min(max_w, preferred_w))
        h = max(req_h, min(max_h, preferred_h))

        center_window(dlg, width=w, height=h, relative_to=self.root)

    def _build_ui(self):
        top = ttk.Frame(self.root, padding=10)
        top.pack(fill="x")

        self.enabled_var = tk.BooleanVar(value=bool(self.cfg.get("enabled", True)))
        ttk.Checkbutton(top, text="Enabled", variable=self.enabled_var, command=self.on_toggle_enabled).pack(side="left")

        ttk.Label(top, text="  Excel Web mode:").pack(side="left")
        self.mode_var = tk.StringVar(value=self.cfg.get("excel_web_mode", "shift_wheel"))
        mode_dd = ttk.Combobox(top, textvariable=self.mode_var, values=["shift_wheel", "hwheel"], state="readonly", width=12)
        mode_dd.pack(side="left", padx=(4, 14))
        mode_dd.bind("<<ComboboxSelected>>", lambda e: self.on_change_settings())

        ttk.Label(top, text="Repeat ms:").pack(side="left")
        self.repeat_var = tk.StringVar(value=str(self.cfg.get("repeat_interval_ms", 16)))
        ttk.Entry(top, textvariable=self.repeat_var, width=6).pack(side="left", padx=(4, 10))

        ttk.Label(top, text="Steps/tick:").pack(side="left")
        self.steps_var = tk.StringVar(value=str(self.cfg.get("scroll_steps_per_tick", 1)))
        ttk.Entry(top, textvariable=self.steps_var, width=6).pack(side="left", padx=(4, 10))

        ttk.Button(top, text="Apply", command=self.on_change_settings).pack(side="left")
        ttk.Button(top, text="Save", command=self.on_save).pack(side="left", padx=(8, 0))
        help_btn = ttk.Button(top, text="ℹ", width=3, command=self.on_help)
        help_btn.pack(side="right")

        mid = ttk.Frame(self.root, padding=(10, 0, 10, 10))
        mid.pack(fill="both", expand=True)

        ttk.Label(mid, text="Press any mouse button to detect it. Then assign an action.").pack(anchor="w", pady=(6, 6))

        tv_wrap = ttk.Frame(mid)
        tv_wrap.pack(fill="both", expand=True)

        self.tree = ttk.Treeview(tv_wrap, columns=("button", "mode", "action", "param"), show="headings")
        self.tree.heading("button", text="Button")
        self.tree.heading("mode", text="Mode")
        self.tree.heading("action", text="Action")
        self.tree.heading("param", text="Param (keys/text/command)")

        self.tree.column("button", width=120, anchor="w")
        self.tree.column("mode", width=90, anchor="w")
        self.tree.column("action", width=170, anchor="w")
        self.tree.column("param", width=330, anchor="w")

        vsb = ttk.Scrollbar(tv_wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)

        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        tv_wrap.grid_rowconfigure(0, weight=1)
        tv_wrap.grid_columnconfigure(0, weight=1)

        btns = ttk.Frame(mid)
        btns.pack(fill="x", pady=(8, 0))

        ttk.Button(btns, text="Edit selected", command=self.on_edit_selected).pack(side="left")
        ttk.Button(btns, text="Reset selected", command=self.on_reset_selected).pack(side="left", padx=8)
        ttk.Button(btns, text="Hide to tray", command=self.hide_window).pack(side="right")

        self._refresh_table()
        self._autosize_tree_height()

    def _refresh_table(self):
        self.tree.delete(*self.tree.get_children())
        bindings = self.cfg.get("bindings", {})
        for btn_name in sorted(bindings.keys()):
            b = bindings[btn_name]
            self.tree.insert("", "end", values=(btn_name, b.get("mode", "press"), b.get("action", "none"), b.get("param", "")))
        self._autosize_tree_height()

    def _ensure_button_exists(self, btn_name):
        if "bindings" not in self.cfg:
            self.cfg["bindings"] = {}
        if btn_name not in self.cfg["bindings"]:
            self.cfg["bindings"][btn_name] = {"mode": "press", "action": "none", "param": ""}

    def _on_click(self, x, y, button, pressed):
        # XBUTTON1/XBUTTON2 are handled by the Win32 hook (to block Back/Forward)
        if button in (mouse.Button.x1, mouse.Button.x2):
            return

        name = BUTTON_NAME_MAP.get(button)
        if not name:
            return

        if pressed:
            self.event_q.put(("detect", name))

        if pressed:
            self.engine.on_press(name)
        else:
            self.engine.on_release(name)

    def _poll_events(self):
        resized = False
        try:
            while True:
                ev = self.event_q.get_nowait()
                if ev[0] == "detect":
                    btn_name = ev[1]
                    if btn_name not in self.cfg.get("bindings", {}):
                        self._ensure_button_exists(btn_name)
                        self._refresh_table()
                        resized = True
        except queue.Empty:
            pass

        if resized:
            self.root.update_idletasks()
            center_window(self.root, width=760, height=self._compute_compact_height())

        self.root.after(50, self._poll_events)

    def _get_selected_button(self):
        sel = self.tree.selection()
        if not sel:
            return None
        vals = self.tree.item(sel[0], "values")
        return vals[0] if vals else None

    def on_toggle_enabled(self):
        self.cfg["enabled"] = bool(self.enabled_var.get())
        self.engine.set_enabled(self.cfg["enabled"])
        self._update_tray_title()

    def on_change_settings(self):
        try:
            repeat_ms = int(self.repeat_var.get().strip())
            steps = int(self.steps_var.get().strip())
            if repeat_ms < 8 or repeat_ms > 200:
                raise ValueError("Repeat ms should be between 8 and 200.")
            if steps < 1 or steps > 10:
                raise ValueError("Steps/tick should be between 1 and 10.")

            self.cfg["repeat_interval_ms"] = repeat_ms
            self.cfg["scroll_steps_per_tick"] = steps
            self.cfg["excel_web_mode"] = self.mode_var.get().strip()

            self.engine.update_config(self.cfg)
        except Exception as e:
            messagebox.showerror("Invalid settings", str(e))

    def on_save(self):
        self.on_change_settings()
        save_config(self.cfg_path, self.cfg)
        messagebox.showinfo("Saved", f"Saved to:\n{self.cfg_path}")

    def on_reset_selected(self):
        btn_name = self._get_selected_button()
        if not btn_name:
            return
        self.cfg["bindings"][btn_name] = {"mode": "press", "action": "none", "param": ""}
        self.engine.update_config(self.cfg)
        self._refresh_table()
        self.root.update_idletasks()
        center_window(self.root, width=760, height=self._compute_compact_height())

    def on_help(self):
        dlg = tk.Toplevel(self.root)
        dlg.title("Help")
        dlg.transient(self.root)
        dlg.grab_set()
        dlg.resizable(False, False)

        container = ttk.Frame(dlg, padding=12)
        container.pack(fill="both", expand=True)

        ttk.Label(container, text="How to use", font=("Segoe UI", 12, "bold")).pack(anchor="w", pady=(0, 8))

        body_wrap = ttk.Frame(container)
        body_wrap.pack(fill="both", expand=True)

        txt = tk.Text(body_wrap, wrap="word", height=9, borderwidth=1, relief="solid")
        vsb = ttk.Scrollbar(body_wrap, orient="vertical", command=txt.yview)
        txt.configure(yscrollcommand=vsb.set)

        txt.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        body_wrap.grid_rowconfigure(0, weight=1)
        body_wrap.grid_columnconfigure(0, weight=1)

        help_text = (
            "Basics\n"
            "• Run the script. A tray icon will appear.\n"
            "• Press any mouse button once. If it’s new, it will be added to the list.\n"
            "• Select a row → click “Edit selected”.\n\n"
            "Modes\n"
            "• press: runs once on click\n"
            "• hold: repeats while the button is held (best for scrolling)\n\n"
            "Actions\n"
            "• hscroll_left / hscroll_right: Excel Web-friendly horizontal scroll\n"
            "• keys: send a key combo (example: ctrl+shift+t)\n"
            "• text: type text using SendKeys\n"
            "• run: run a command (example: notepad.exe)\n\n"
            "Excel Web tips\n"
            "• Click inside the Excel grid first (so the sheet has focus).\n"
            "• If scrolling is slow: increase Steps/tick to 2–3.\n"
            "• If scrolling is too fast: increase Repeat (ms).\n\n"
            "Tray\n"
            "• Use “Hide to tray” to keep it running in the tray.\n"
            "• If you click X, the app will exit.\n"
        )
        txt.insert("1.0", help_text)
        txt.configure(state="disabled")

        btnrow = ttk.Frame(container)
        btnrow.pack(fill="x", pady=(10, 0))
        ttk.Button(btnrow, text="Close", command=dlg.destroy).pack(side="right")

        dlg.update_idletasks()
        self._popup_fit_to_parent(dlg, preferred_w=720, preferred_h=260)
        dlg.focus_force()

    def on_edit_selected(self):
        btn_name = self._get_selected_button()
        if not btn_name:
            return

        b = self.cfg.get("bindings", {}).get(btn_name, {"mode": "press", "action": "none", "param": ""})
        mode = b.get("mode", "press")
        action = b.get("action", "none")
        param = b.get("param", "")

        dlg = tk.Toplevel(self.root)
        dlg.title(f"Edit: {btn_name}")
        dlg.transient(self.root)
        dlg.grab_set()
        dlg.resizable(False, False)

        frm = ttk.Frame(dlg, padding=(10, 10, 10, 8))
        frm.pack(fill="both", expand=True)

        ttk.Label(frm, text=f"Button: {btn_name}").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))

        ttk.Label(frm, text="Mode:").grid(row=1, column=0, sticky="w", padx=(0, 10), pady=2)
        mode_var = tk.StringVar(value=mode)
        ttk.Combobox(frm, textvariable=mode_var, values=MODE_OPTIONS, state="readonly", width=14) \
            .grid(row=1, column=1, sticky="ew", pady=2)

        ttk.Label(frm, text="Action:").grid(row=2, column=0, sticky="w", padx=(0, 10), pady=2)
        action_var = tk.StringVar(value=action)
        ttk.Combobox(frm, textvariable=action_var, values=ACTION_OPTIONS, state="readonly", width=28) \
            .grid(row=2, column=1, sticky="ew", pady=2)

        ttk.Label(frm, text="Param:").grid(row=3, column=0, sticky="w", padx=(0, 10), pady=2)
        param_var = tk.StringVar(value=param)
        ttk.Entry(frm, textvariable=param_var, width=42) \
            .grid(row=3, column=1, sticky="ew", pady=2)

        examples = (
            "  keys:  ctrl+shift+t\n"
            "  run:   notepad.exe\n"
            "  text:  hello world\n"
            "  scroll: hscroll_left / hscroll_right"
        )
        ex_box = ttk.LabelFrame(frm, text="Examples", padding=(8, 6))
        ex_box.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(6, 6))
        ttk.Label(ex_box, text=examples, justify="left").pack(anchor="w")

        btnrow = ttk.Frame(frm)
        btnrow.grid(row=5, column=0, columnspan=2, sticky="e", pady=(2, 0))

        def apply_and_close():
            self._ensure_button_exists(btn_name)
            self.cfg["bindings"][btn_name] = {
                "mode": mode_var.get(),
                "action": action_var.get(),
                "param": param_var.get(),
            }
            self.engine.update_config(self.cfg)
            self._refresh_table()
            dlg.destroy()

        ttk.Button(btnrow, text="Cancel", command=dlg.destroy).pack(side="right")
        ttk.Button(btnrow, text="Apply", command=apply_and_close).pack(side="right", padx=(0, 8))

        frm.grid_columnconfigure(1, weight=1)

        dlg.update_idletasks()
        self._popup_fit_to_parent(dlg, preferred_w=560, preferred_h=250)
        dlg.focus_force()

    # ----------------------------
    # Hide vs Quit behavior
    # ----------------------------
    def hide_window(self):
        self.root.withdraw()

    def show_window(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def quit_app(self):
        """
        Called by window X and tray Quit.
        Fully exits the application.
        """
        try:
            if hasattr(self, "xblocker") and self.xblocker:
                self.xblocker.stop()
        except Exception:
            pass

        try:
            self.engine.shutdown()
            try:
                self.listener.stop()
            except Exception:
                pass
        except Exception:
            pass

        try:
            if self.tray:
                self.tray.stop()
        except Exception:
            pass

        try:
            self.root.destroy()
        except Exception:
            try:
                self.root.quit()
            except Exception:
                pass

    # ----------------------------
    # Tray
    # ----------------------------
    def _tray_image(self):
        # Use the exact same icon as app_icon.png
        return load_icon_pil(size=64)

    def _start_tray(self):
        def on_show(icon, item):
            self.show_window()

        def on_hide(icon, item):
            self.hide_window()

        def on_toggle(icon, item):
            self.enabled_var.set(not self.enabled_var.get())
            self.on_toggle_enabled()

        def on_quit(icon, item):
            self.quit_app()

        menu = pystray.Menu(
            pystray.MenuItem("Show", on_show),
            pystray.MenuItem("Hide", on_hide),
            pystray.MenuItem("Enable/Disable", on_toggle),
            pystray.MenuItem("Quit", on_quit),
        )

        title = self._tray_title()
        self.tray = pystray.Icon("MouseMacro", self._tray_image(), title, menu)
        threading.Thread(target=self.tray.run, daemon=True).start()

    def _tray_title(self):
        return f"Mouse Macros ({'Enabled' if self.cfg.get('enabled', True) else 'Disabled'})"

    def _update_tray_title(self):
        try:
            if self.tray:
                self.tray.title = self._tray_title()
        except Exception:
            pass

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    App().run()

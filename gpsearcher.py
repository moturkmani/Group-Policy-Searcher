import os
import sys
import subprocess
import traceback


def log(msg):
    print(f"[INFO]  {msg}", flush=True)


def warn(msg):
    print(f"[WARN]  {msg}", file=sys.stderr, flush=True)


def fail(msg):
    print(f"[ERROR] {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------
# Early environment checks - these print clearly in PowerShell/CMD
# ---------------------------------------------------------------
if sys.platform != "win32":
    fail("This tool only runs on Windows (it needs the 'winreg' module).")
    sys.exit(1)

try:
    import winreg
    import xml.etree.ElementTree as ET
    import tkinter as tk
    from tkinter import ttk, messagebox
except Exception:
    fail("Could not import a required module:")
    traceback.print_exc()
    sys.exit(1)

# The real namespace used by Windows .admx / .adml files
ADMX_NS = {"admx": "http://schemas.microsoft.com/GroupPolicy/2006/07/PolicyDefinitions"}

# Where Regedit remembers the last key it had open (we use this to "jump" to a key)
REGEDIT_APPLET_KEY = r"Software\Microsoft\Windows\CurrentVersion\Applets\Regedit"

# Always use the 64-bit registry view so policies land where Windows reads them
WOW64 = getattr(winreg, "KEY_WOW64_64KEY", 0)

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# ---------------------------------------------------------------
# Window icons: registry.ico = title bar (top-left), icon.png = taskbar / Alt+Tab.
# Looked for next to this script first, then in an "assets" folder.
# ---------------------------------------------------------------
ICO_NAME = "registry.ico"
PNG_NAME = "icon.png"
APP_ID = "GPOExplorer.GroupPolicyTool.1"  # makes Windows show OUR icon, not python.exe's


def find_resource(name):
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
    for folder in (base, os.path.join(base, "assets"), os.getcwd()):
        path = os.path.join(folder, name)
        if os.path.isfile(path):
            return path
    return None


def set_titlebar_icon(root, ico_path):
    """Give the window's small (title bar) icon a different image than the taskbar one."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    user32.GetParent.argtypes = [wintypes.HWND]
    user32.GetParent.restype = wintypes.HWND
    user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
                                  ctypes.c_int, ctypes.c_int, wintypes.UINT]
    user32.LoadImageW.restype = wintypes.HANDLE
    user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.SendMessageW.restype = ctypes.c_void_p

    IMAGE_ICON, LR_LOADFROMFILE = 1, 0x0010
    WM_SETICON, ICON_SMALL = 0x0080, 0
    cx, cy = user32.GetSystemMetrics(49), user32.GetSystemMetrics(50)  # small-icon size

    hicon = user32.LoadImageW(None, ico_path, IMAGE_ICON, cx, cy, LR_LOADFROMFILE)
    if not hicon:
        raise OSError("Windows could not load the .ico file")
    hwnd = user32.GetParent(root.winfo_id())
    if not hwnd:
        raise OSError("Could not find the window handle")
    user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, hicon)

# ---------------------------------------------------------------
# Colour palette
# ---------------------------------------------------------------
C = {
    "bg": "#eef1f6",
    "header": "#1e293b",
    "header_sub": "#cbd5e1",
    "card": "#ffffff",
    "panel": "#f8fafc",
    "border": "#d5dbe5",
    "text": "#1f2937",
    "muted": "#6b7280",
    "link": "#1d4ed8",
    "link_bg": "#eff6ff",
    "row_alt": "#f5f7fb",
    "select": "#cfe0ff",
    "on_bg": "#bbf7d0", "on_hover": "#86efac", "on_fg": "#14532d",
    "off_bg": "#fecaca", "off_hover": "#fca5a5", "off_fg": "#7f1d1d",
    "neu_bg": "#dbeafe", "neu_hover": "#bfdbfe", "neu_fg": "#1e3a8a",
    "dis_bg": "#e5e7eb", "dis_fg": "#9ca3af",
    "chip_bg": "#eef2ff", "chip_fg": "#3730a3",
    "none_bg": "#f1f5f9", "none_fg": "#475569",
    "warn_bg": "#fef3c7", "warn_fg": "#92400e",
}

STATUS_COLORS = {"info": "#e2e8f0", "ok": "#86efac", "warn": "#fcd34d", "error": "#fca5a5"}


def is_admin():
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


class FlatButton(tk.Button):
    """A flat coloured button with a hover effect and a greyed-out disabled look."""

    def __init__(self, parent, text, bg, hover, fg, command, **kw):
        font = kw.pop("font", ("Segoe UI", 10, "bold"))
        padx = kw.pop("padx", 16)
        pady = kw.pop("pady", 8)
        super().__init__(parent, text=text, command=command, bg=bg, fg=fg,
                         activebackground=hover, activeforeground=fg,
                         relief="flat", bd=0, cursor="hand2", font=font,
                         padx=padx, pady=pady, highlightthickness=0, **kw)
        self._bg, self._hover, self._fg = bg, hover, fg
        self._enabled = True
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)

    def _on_enter(self, _e):
        if self._enabled:
            self.config(bg=self._hover)

    def _on_leave(self, _e):
        if self._enabled:
            self.config(bg=self._bg)

    def set_enabled(self, enabled):
        self._enabled = enabled
        if enabled:
            self.config(state="normal", bg=self._bg, fg=self._fg, cursor="hand2")
        else:
            self.config(state="disabled", bg=C["dis_bg"],
                        disabledforeground=C["dis_fg"], cursor="arrow")


class SafeGPOApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Windows 10/11 GPO Explorer")

        self.policy_database = []
        self.current_matches = []
        self.selected_policy = None
        self._search_job = None

        self.setup_window()
        self.apply_icons()

        log("Building window...")
        self.create_widgets()

        log("Loading built-in fallback policies...")
        self.load_fallback_database()

        try:
            self.load_system_policies()
        except Exception:
            warn("System policy scan failed. Continuing with fallback policies only.")
            traceback.print_exc()

        self.finalize_database()
        self.perform_search()
        log(f"Ready. {len(self.policy_database)} policies loaded.")

        self.admin = is_admin()
        if self.admin:
            self.admin_label.config(text="\u25CF Administrator", fg=STATUS_COLORS["ok"])
        else:
            self.admin_label.config(text="\u25CF Not elevated - Turn ON/OFF will fail for HKLM policies",
                                    fg=STATUS_COLORS["warn"])
            warn("Not running as Administrator. Searching works, but "
                 "Turn ON/OFF will fail for HKLM policies.")

        self.set_status(f"Ready - {len(self.policy_database):,} policies loaded. "
                        "Type in the search box to begin.", "info")
        self.root.after(250, self._place_sash)

    # ------------------------------------------------------------------
    # Window sizing: opens maximized; "restore down" gives a window that is
    # 75% of the screen (25% smaller), centered on the display.
    # ------------------------------------------------------------------
    def setup_window(self):
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        w, h = int(sw * 0.75), int(sh * 0.75)
        x, y = (sw - w) // 2, (sh - h) // 2
        self.root.geometry(f"{w}x{h}+{x}+{y}")
        self.root.minsize(760, 480)
        self.root.update_idletasks()
        try:
            self.root.state("zoomed")
        except tk.TclError:
            pass

    def apply_icons(self):
        ico = find_resource(ICO_NAME)
        png = find_resource(PNG_NAME)
        if not ico:
            warn(f"{ICO_NAME} not found (put it next to this script or in an 'assets' folder).")
        if not png:
            warn(f"{PNG_NAME} not found (put it next to this script or in an 'assets' folder).")

        self._icon_img = None  # keep a reference so Tk doesn't garbage-collect the image
        if png:
            try:
                img = tk.PhotoImage(file=png)
                # Windows icons top out around 256px, so shrink very large images
                biggest = max(img.width(), img.height())
                if biggest > 256 and biggest // 256 >= 2:
                    img = img.subsample(biggest // 256)
                self.root.iconphoto(True, img)  # taskbar + Alt+Tab icon
                self._icon_img = img
                log(f"Taskbar icon loaded: {png}")
            except Exception as e:
                warn(f"Could not load {PNG_NAME}: {type(e).__name__}: {e}")

        if ico:
            try:
                if self._icon_img is None:
                    self.root.iconbitmap(ico)  # no PNG available: use the .ico for both
                else:
                    set_titlebar_icon(self.root, ico)  # title bar (top-left) icon
                log(f"Title bar icon loaded: {ico}")
            except Exception as e:
                warn(f"Could not apply {ICO_NAME}: {type(e).__name__}: {e}")

    def _place_sash(self):
        try:
            total = self.paned.winfo_width()
            if total > 200:
                self.paned.sash_place(0, int(total * 0.38), 1)
        except tk.TclError:
            pass

    # ------------------------------------------------------------------
    # Loading policies
    # ------------------------------------------------------------------
    def load_system_policies(self):
        admx_dir = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "PolicyDefinitions")
        adml_dir = os.path.join(admx_dir, "en-US")

        if not os.path.isdir(admx_dir):
            warn(f"Policy folder not found: {admx_dir}")
            return
        if not os.path.isdir(adml_dir):
            warn(f"Language folder not found: {adml_dir}")
            return

        log(f"Scanning {admx_dir} ...")
        seen_names = {p["name"] for p in self.policy_database}
        files_ok = 0
        files_failed = 0
        files_no_adml = 0
        added = 0

        def clean_ref(ref):
            # "$(string.SomeId)" -> "SomeId"
            if ref and ref.startswith("$(string.") and ref.endswith(")"):
                return ref[len("$(string."):-1]
            return ref

        for file_name in sorted(os.listdir(admx_dir)):
            if not file_name.lower().endswith(".admx"):
                continue

            admx_path = os.path.join(admx_dir, file_name)
            adml_path = os.path.join(adml_dir, file_name[:-5] + ".adml")

            if not os.path.exists(adml_path):
                files_no_adml += 1
                continue

            try:
                adml_root = ET.parse(adml_path).getroot()
                string_table = {}
                for string_node in adml_root.findall(".//admx:string", ADMX_NS):
                    s_id = string_node.get("id")
                    if s_id:
                        string_table[s_id] = string_node.text or ""

                admx_root = ET.parse(admx_path).getroot()
                category = file_name[:-5].upper()

                for policy_node in admx_root.findall(".//admx:policy", ADMX_NS):
                    friendly_name = string_table.get(
                        clean_ref(policy_node.get("displayName")), policy_node.get("name"))
                    description = string_table.get(
                        clean_ref(policy_node.get("explainText")), "No description available.")

                    reg_key = policy_node.get("key", "")
                    reg_value = policy_node.get("valueName", "")
                    class_type = policy_node.get("class", "Both")

                    if not reg_key or not friendly_name or friendly_name in seen_names:
                        continue

                    if class_type == "User":
                        hive, hive_text = winreg.HKEY_CURRENT_USER, "HKCU"
                    else:
                        hive, hive_text = winreg.HKEY_LOCAL_MACHINE, "HKLM"

                    seen_names.add(friendly_name)
                    added += 1
                    self.policy_database.append({
                        "name": friendly_name,
                        "category": category,
                        "description": (description or "").strip(),
                        "reg_hive": hive,
                        "hive_text": hive_text,
                        "reg_path": reg_key,
                        "reg_value": reg_value,  # may be "" for policies with sub-settings
                    })
                files_ok += 1

            except Exception as e:
                files_failed += 1
                if files_failed <= 5:
                    warn(f"Could not parse {file_name}: {type(e).__name__}: {e}")

        log(f"Scan done: {files_ok} files read, {files_failed} failed, "
            f"{files_no_adml} skipped (no English .adml), {added} policies added.")
        if added == 0:
            warn("No system policies were found. Only the built-in fallback list is available.")

    def load_fallback_database(self):
        self.policy_database.extend([
            {
                "name": "Disable Windows Consumer Features",
                "category": "CLOUDCONTENT",
                "description": "Turns off personalized recommendations and promotional app installations from Microsoft on your start menu.",
                "reg_hive": winreg.HKEY_LOCAL_MACHINE, "hive_text": "HKLM",
                "reg_path": r"SOFTWARE\Policies\Microsoft\Windows\CloudContent",
                "reg_value": "DisableWindowsConsumerFeatures",
            },
            {
                "name": "Prevent Windows Update Reboots",
                "category": "WINDOWSUPDATE",
                "description": "Forces Windows Update to wait for a manual restart if a user is logged into the system.",
                "reg_hive": winreg.HKEY_LOCAL_MACHINE, "hive_text": "HKLM",
                "reg_path": r"SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU",
                "reg_value": "NoAutoRebootWithLoggedOnUsers",
            },
        ])

    def finalize_database(self):
        """Pre-compute a lowercase search string per policy and sort by category/name."""
        for p in self.policy_database:
            p["blob"] = (f"{p['name']} {p['category']} {p['description']} "
                         f"{p['reg_path']} {p['reg_value']}").lower()
        self.policy_database.sort(key=lambda p: (p["category"], p["name"].lower()))

    # ------------------------------------------------------------------
    # Building the window
    # ------------------------------------------------------------------
    def create_widgets(self):
        root = self.root
        root.configure(bg=C["bg"])

        # ---- Header ----
        header = tk.Frame(root, bg=C["header"])
        header.pack(side="top", fill="x")
        tk.Label(header, text="Windows 10/11 GPO Explorer", bg=C["header"], fg="white",
                 font=("Segoe UI", 17, "bold")).pack(anchor="w", padx=20, pady=(12, 0))
        tk.Label(header, text="Search, inspect and toggle Group Policy settings",
                 bg=C["header"], fg=C["header_sub"],
                 font=("Segoe UI", 10)).pack(anchor="w", padx=20, pady=(0, 12))

        # ---- Status bar (packed before the body so it stays at the bottom) ----
        status_bar = tk.Frame(root, bg=C["header"])
        status_bar.pack(side="bottom", fill="x")
        self.status_msg = tk.Label(status_bar, text="Starting...", bg=C["header"],
                                   fg=STATUS_COLORS["info"], font=("Segoe UI", 9), anchor="w")
        self.status_msg.pack(side="left", padx=14, pady=6)
        self.admin_label = tk.Label(status_bar, text="", bg=C["header"],
                                    fg=STATUS_COLORS["info"], font=("Segoe UI", 9, "bold"))
        self.admin_label.pack(side="right", padx=14, pady=6)

        # ---- Search bar ----
        bar = tk.Frame(root, bg=C["bg"])
        bar.pack(side="top", fill="x", padx=16, pady=(12, 6))

        tk.Label(bar, text="Search", bg=C["bg"], fg=C["text"],
                 font=("Segoe UI", 10, "bold")).pack(side="left")

        entry_border = tk.Frame(bar, bg=C["border"], padx=1, pady=1)
        entry_border.pack(side="left", fill="x", expand=True, padx=(10, 6))
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", self.schedule_search)
        self.search_entry = tk.Entry(entry_border, textvariable=self.search_var,
                                     font=("Segoe UI", 12), relief="flat", bd=0,
                                     bg="white", fg=C["text"], insertbackground=C["text"])
        self.search_entry.pack(fill="x", ipady=6, padx=1, pady=1)
        self.search_entry.focus()

        FlatButton(bar, "\u2715  Clear", C["neu_bg"], C["neu_hover"], C["neu_fg"],
                   self.clear_search, padx=12, pady=6,
                   font=("Segoe UI", 9, "bold")).pack(side="left", padx=(0, 10))

        self.scope_var = tk.StringVar(value="All policies")
        scope = ttk.Combobox(bar, textvariable=self.scope_var, state="readonly", width=18,
                             font=("Segoe UI", 10),
                             values=["All policies", "Computer (HKLM)", "User (HKCU)"])
        scope.pack(side="left", padx=(0, 12))
        scope.bind("<<ComboboxSelected>>", lambda _e: self.perform_search())

        self.count_label = tk.Label(bar, text="", bg=C["bg"], fg=C["muted"],
                                    font=("Segoe UI", 10), width=16, anchor="e")
        self.count_label.pack(side="left")

        # ---- Body: list on the left, details card on the right ----
        self.paned = tk.PanedWindow(root, orient="horizontal", bg=C["bg"], bd=0,
                                    sashwidth=10, sashrelief="flat")
        self.paned.pack(side="top", fill="both", expand=True, padx=16, pady=(4, 12))

        # Left: policy list
        left = tk.Frame(self.paned, bg=C["border"], padx=1, pady=1)
        list_inner = tk.Frame(left, bg="white")
        list_inner.pack(fill="both", expand=True)
        self.policy_listbox = tk.Listbox(
            list_inner, selectmode="single", font=("Segoe UI", 10), activestyle="none",
            bd=0, highlightthickness=0, bg="white", fg=C["text"],
            selectbackground=C["select"], selectforeground=C["text"], exportselection=False)
        scrollbar = ttk.Scrollbar(list_inner, orient="vertical", command=self.policy_listbox.yview)
        scrollbar.pack(side="right", fill="y")
        self.policy_listbox.pack(side="left", fill="both", expand=True)
        self.policy_listbox.config(yscrollcommand=scrollbar.set)
        self.policy_listbox.bind("<<ListboxSelect>>", self.on_policy_select)
        self.paned.add(left, minsize=300, width=480, stretch="always")

        # Right: details card
        right = tk.Frame(self.paned, bg=C["border"], padx=1, pady=1)
        card = tk.Frame(right, bg=C["card"])
        card.pack(fill="both", expand=True)
        self.body = tk.Frame(card, bg=C["card"], padx=22, pady=18)
        self.body.pack(fill="both", expand=True)
        self.body.bind("<Configure>", self._on_body_resize)
        self.paned.add(right, minsize=420, stretch="always")

        self.title_label = tk.Label(self.body, text="Select a policy to see its details",
                                    bg=C["card"], fg=C["text"], font=("Segoe UI", 15, "bold"),
                                    justify="left", anchor="w", wraplength=520)
        self.title_label.pack(side="top", fill="x")

        self.hint_label = tk.Label(
            self.body, bg=C["card"], fg=C["muted"], font=("Segoe UI", 10),
            justify="left", anchor="nw", wraplength=520,
            text="Use the search box to find a setting, then click it in the list.\n\n"
                 "\u2022  Green button = turn the setting ON (writes 1)\n"
                 "\u2022  Red button = turn the setting OFF (writes 0)\n"
                 "\u2022  Click the blue registry path to jump straight to it in Regedit\n"
                 "\u2022  Ctrl+F focuses the search box, Esc clears it")
        self.hint_label.pack(side="top", fill="x", pady=(14, 0))

        # Everything below is hidden until a policy is selected
        self.detail_group = tk.Frame(self.body, bg=C["card"])

        meta_row = tk.Frame(self.detail_group, bg=C["card"])
        meta_row.pack(side="top", fill="x", pady=(10, 8))
        self.chip_category = tk.Label(meta_row, text="", bg=C["chip_bg"], fg=C["chip_fg"],
                                      font=("Segoe UI", 9, "bold"), padx=10, pady=3)
        self.chip_category.pack(side="left", padx=(0, 6))
        self.chip_scope = tk.Label(meta_row, text="", bg=C["chip_bg"], fg=C["chip_fg"],
                                   font=("Segoe UI", 9, "bold"), padx=10, pady=3)
        self.chip_scope.pack(side="left", padx=(0, 6))
        self.badge = tk.Label(meta_row, text="", bg=C["none_bg"], fg=C["none_fg"],
                              font=("Segoe UI", 9, "bold"), padx=10, pady=3)
        self.badge.pack(side="right")

        tk.Label(self.detail_group, text="DESCRIPTION", bg=C["card"], fg=C["muted"],
                 font=("Segoe UI", 8, "bold"), anchor="w").pack(side="top", fill="x")

        # Buttons (packed from the bottom up so the description gets the leftover space)
        btn_row = tk.Frame(self.detail_group, bg=C["card"])
        btn_row.pack(side="bottom", fill="x", pady=(14, 0))
        self.btn_enable = FlatButton(btn_row, "\u2714   Turn ON", C["on_bg"], C["on_hover"],
                                     C["on_fg"], lambda: self.set_policy_state(1),
                                     padx=26, pady=10, font=("Segoe UI", 11, "bold"))
        self.btn_enable.pack(side="left", padx=(0, 12))
        self.btn_disable = FlatButton(btn_row, "\u2716   Turn OFF", C["off_bg"], C["off_hover"],
                                      C["off_fg"], lambda: self.set_policy_state(0),
                                      padx=26, pady=10, font=("Segoe UI", 11, "bold"))
        self.btn_disable.pack(side="left")

        # Registry location
        reg_group = tk.Frame(self.detail_group, bg=C["card"])
        reg_group.pack(side="bottom", fill="x", pady=(14, 0))
        tk.Label(reg_group, text="REGISTRY LOCATION  (click the path to open it in Regedit)",
                 bg=C["card"], fg=C["muted"], font=("Segoe UI", 8, "bold"),
                 anchor="w").pack(side="top", fill="x")
        self.path_link = tk.Label(reg_group, text="", bg=C["link_bg"], fg=C["link"],
                                  font=("Consolas", 10, "underline"), cursor="hand2",
                                  justify="left", anchor="w", padx=10, pady=8, wraplength=520)
        self.path_link.pack(side="top", fill="x", pady=(4, 4))
        self.path_link.bind("<Button-1>", self.open_in_regedit)
        self.path_link.bind("<Enter>", lambda _e: self.path_link.config(bg=C["select"]))
        self.path_link.bind("<Leave>", lambda _e: self.path_link.config(bg=C["link_bg"]))
        self.value_label = tk.Label(reg_group, text="", bg=C["card"], fg=C["muted"],
                                    font=("Segoe UI", 9), anchor="w", justify="left",
                                    wraplength=520)
        self.value_label.pack(side="top", fill="x")
        link_btns = tk.Frame(reg_group, bg=C["card"])
        link_btns.pack(side="top", fill="x", pady=(8, 0))
        FlatButton(link_btns, "Open in Regedit", C["neu_bg"], C["neu_hover"], C["neu_fg"],
                   self.open_in_regedit, padx=12, pady=5,
                   font=("Segoe UI", 9, "bold")).pack(side="left", padx=(0, 8))
        FlatButton(link_btns, "Copy path", C["neu_bg"], C["neu_hover"], C["neu_fg"],
                   self.copy_path, padx=12, pady=5,
                   font=("Segoe UI", 9, "bold")).pack(side="left")

        # Description text (takes the remaining space)
        desc_frame = tk.Frame(self.detail_group, bg=C["border"], padx=1, pady=1)
        desc_frame.pack(side="top", fill="both", expand=True, pady=(4, 0))
        desc_inner = tk.Frame(desc_frame, bg=C["panel"])
        desc_inner.pack(fill="both", expand=True)
        desc_scroll = ttk.Scrollbar(desc_inner, orient="vertical")
        desc_scroll.pack(side="right", fill="y")
        self.desc_text = tk.Text(desc_inner, wrap="word", bd=0, relief="flat", bg=C["panel"],
                                 fg=C["text"], font=("Segoe UI", 10), padx=10, pady=8,
                                 height=6, highlightthickness=0,
                                 yscrollcommand=desc_scroll.set)
        self.desc_text.pack(side="left", fill="both", expand=True)
        desc_scroll.config(command=self.desc_text.yview)
        self.desc_text.config(state="disabled")

        self.btn_enable.set_enabled(False)
        self.btn_disable.set_enabled(False)

        # Keyboard shortcuts
        root.bind("<Control-f>", lambda _e: (self.search_entry.focus_set(),
                                             self.search_entry.select_range(0, "end")))
        root.bind("<Escape>", lambda _e: self.clear_search())

    def _on_body_resize(self, event):
        wrap = max(200, event.width - 50)
        for label in (self.title_label, self.hint_label, self.path_link, self.value_label):
            label.config(wraplength=wrap)

    # ------------------------------------------------------------------
    # Status helpers
    # ------------------------------------------------------------------
    def set_status(self, msg, kind="info"):
        self.status_msg.config(text=msg, fg=STATUS_COLORS.get(kind, STATUS_COLORS["info"]))

    # ------------------------------------------------------------------
    # Searching
    # ------------------------------------------------------------------
    def schedule_search(self, *_args):
        if self._search_job is not None:
            try:
                self.root.after_cancel(self._search_job)
            except Exception:
                pass
        self._search_job = self.root.after(150, self.perform_search)

    def clear_search(self):
        self.search_var.set("")
        self.search_entry.focus_set()

    def perform_search(self):
        self._search_job = None
        terms = self.search_var.get().strip().lower().split()
        scope = self.scope_var.get()
        hive_filter = None
        if scope.startswith("Computer"):
            hive_filter = "HKLM"
        elif scope.startswith("User"):
            hive_filter = "HKCU"

        self.policy_listbox.delete(0, tk.END)
        self.current_matches = []
        labels = []

        for p in self.policy_database:
            if hive_filter and p["hive_text"] != hive_filter:
                continue
            blob = p["blob"]
            if terms and not all(t in blob for t in terms):
                continue
            labels.append(f"  [{p['category']}]  {p['name']}")
            self.current_matches.append(p)

        if labels:
            self.policy_listbox.insert(tk.END, *labels)
            for i in range(1, len(labels), 2):
                self.policy_listbox.itemconfig(i, bg=C["row_alt"])

        self.count_label.config(
            text=f"{len(self.current_matches):,} of {len(self.policy_database):,}")

    # ------------------------------------------------------------------
    # Selecting a policy
    # ------------------------------------------------------------------
    def on_policy_select(self, _event):
        selection = self.policy_listbox.curselection()
        if not selection:
            return
        index = selection[0]  # curselection() returns a tuple, not an int
        if index >= len(self.current_matches):
            return
        self.selected_policy = self.current_matches[index]
        p = self.selected_policy

        self.hint_label.pack_forget()
        if not self.detail_group.winfo_ismapped():
            self.detail_group.pack(side="top", fill="both", expand=True)

        self.title_label.config(text=p["name"])
        self.chip_category.config(text=p["category"])
        self.chip_scope.config(
            text="Computer policy (HKLM)" if p["hive_text"] == "HKLM" else "User policy (HKCU)")

        self.desc_text.config(state="normal")
        self.desc_text.delete("1.0", tk.END)
        self.desc_text.insert(tk.END, p["description"])
        self.desc_text.config(state="disabled")
        self.desc_text.yview_moveto(0)

        self.path_link.config(text=self.display_path(p))
        if p["reg_value"]:
            self.value_label.config(text=f"Value name: {p['reg_value']}   (type: REG_DWORD)")
        else:
            self.value_label.config(
                text="No single value - this policy uses sub-settings and can't be toggled here.")

        can_toggle = bool(p["reg_value"])
        self.btn_enable.set_enabled(can_toggle)
        self.btn_disable.set_enabled(can_toggle)
        self.refresh_badge()

    # ------------------------------------------------------------------
    # Registry helpers
    # ------------------------------------------------------------------
    @staticmethod
    def display_path(p):
        return f"{p['hive_text']}\\{p['reg_path']}"

    @staticmethod
    def regedit_path(p):
        hive_name = "HKEY_LOCAL_MACHINE" if p["hive_text"] == "HKLM" else "HKEY_CURRENT_USER"
        return f"Computer\\{hive_name}\\{p['reg_path']}"

    def key_exists(self, p):
        try:
            with winreg.OpenKey(p["reg_hive"], p["reg_path"], 0, winreg.KEY_READ | WOW64):
                return True
        except OSError:
            return False

    def read_policy_value(self, p):
        """Returns ('ok', value), ('missing', None) or ('error', message)."""
        try:
            with winreg.OpenKey(p["reg_hive"], p["reg_path"], 0, winreg.KEY_READ | WOW64) as key:
                value, _type = winreg.QueryValueEx(key, p["reg_value"])
                return "ok", value
        except FileNotFoundError:
            return "missing", None
        except OSError as e:
            return "error", str(e)

    def refresh_badge(self):
        p = self.selected_policy
        if not p:
            return
        if not p["reg_value"]:
            self.badge.config(text="\u25CF No single value", bg=C["none_bg"], fg=C["none_fg"])
            return
        state, value = self.read_policy_value(p)
        if state == "missing":
            self.badge.config(text="\u25CF Not configured", bg=C["none_bg"], fg=C["none_fg"])
        elif state == "error":
            self.badge.config(text="\u25CF Can't read value", bg=C["warn_bg"], fg=C["warn_fg"])
        elif isinstance(value, int) and value == 1:
            self.badge.config(text="\u25CF Currently ON (1)", bg=C["on_bg"], fg=C["on_fg"])
        elif isinstance(value, int) and value == 0:
            self.badge.config(text="\u25CF Currently OFF (0)", bg=C["off_bg"], fg=C["off_fg"])
        else:
            self.badge.config(text=f"\u25CF Value = {value}", bg=C["warn_bg"], fg=C["warn_fg"])

    # ------------------------------------------------------------------
    # Turning a policy ON / OFF
    # ------------------------------------------------------------------
    def set_policy_state(self, val_to_write):
        if not self.selected_policy:
            return
        p = self.selected_policy
        if not p["reg_value"]:
            messagebox.showwarning("Not supported",
                                   "This policy has no single registry value to toggle.")
            return
        try:
            with winreg.CreateKeyEx(p["reg_hive"], p["reg_path"], 0,
                                    winreg.KEY_SET_VALUE | WOW64) as key:
                winreg.SetValueEx(key, p["reg_value"], 0, winreg.REG_DWORD, val_to_write)
            log(f"Wrote {self.display_path(p)}\\{p['reg_value']} = {val_to_write}")
            self.refresh_badge()
            word = "ON" if val_to_write == 1 else "OFF"
            self.set_status(f"Turned {word} (value = {val_to_write}):  {p['name']}", "ok")
        except PermissionError:
            fail("Access denied writing to the registry. Run PowerShell as Administrator.")
            self.set_status("Access denied - run this script as Administrator.", "error")
            messagebox.showerror("Privilege Error",
                                 "Access Denied. You must run this script as an Administrator.")
        except Exception as e:
            fail(f"Registry write failed: {type(e).__name__}: {e}")
            traceback.print_exc()
            self.set_status(f"Registry write failed: {e}", "error")
            messagebox.showerror("Error", str(e))

    # ------------------------------------------------------------------
    # Click-the-path: open Regedit at the selected key
    # ------------------------------------------------------------------
    def copy_path(self):
        if not self.selected_policy:
            return
        text = self.display_path(self.selected_policy)
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.set_status(f"Copied to clipboard:  {text}", "ok")

    @staticmethod
    def regedit_running():
        try:
            out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq regedit.exe", "/NH"],
                                 capture_output=True, text=True, timeout=8,
                                 creationflags=NO_WINDOW).stdout
            return "regedit.exe" in out.lower()
        except Exception:
            return False

    def open_in_regedit(self, _event=None):
        p = self.selected_policy
        if not p:
            return
        try:
            # Regedit reopens at whatever key is stored in "LastKey"
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, REGEDIT_APPLET_KEY, 0,
                                    winreg.KEY_SET_VALUE) as key:
                winreg.SetValueEx(key, "LastKey", 0, winreg.REG_SZ, self.regedit_path(p))
        except Exception as e:
            fail(f"Could not prepare Regedit: {type(e).__name__}: {e}")
            traceback.print_exc()
            self.set_status(f"Could not prepare Regedit: {e}", "error")
            messagebox.showerror("Error", f"Could not prepare Regedit:\n{e}")
            return

        if self.regedit_running():
            if not messagebox.askyesno(
                    "Regedit is already open",
                    "Regedit is already running. To jump to this key it has to be "
                    "reopened (Regedit saves changes instantly, so nothing is lost).\n\n"
                    "Close it and reopen at this key?"):
                self.set_status("Regedit is already open - close it, then click the path again.",
                                "warn")
                return
            result = subprocess.run(["taskkill", "/IM", "regedit.exe", "/F"],
                                    capture_output=True, creationflags=NO_WINDOW)
            if result.returncode != 0:
                messagebox.showwarning(
                    "Could not close Regedit",
                    "Regedit couldn't be closed automatically (it may be running elevated).\n"
                    "Please close it yourself, then click the path again.")
                return
            self.root.after(700, lambda: self._launch_regedit(p))
        else:
            self._launch_regedit(p)

    def _launch_regedit(self, p):
        exists = self.key_exists(p)
        try:
            os.startfile("regedit.exe")  # prompts for UAC if this script isn't elevated
        except OSError as e:
            fail(f"Could not launch Regedit: {e}")
            self.set_status("Regedit did not open (cancelled or blocked).", "warn")
            return
        log(f"Opened Regedit at {self.regedit_path(p)}")
        if exists:
            self.set_status(f"Opened Regedit at  {self.display_path(p)}", "ok")
        else:
            self.set_status("That key doesn't exist yet (the policy has never been set). "
                            "Regedit opens as far down the path as it exists.", "warn")


def main():
    # Crisp text on high-DPI displays (must be set before the window is created)
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    # Tell Windows this is its own app so the taskbar shows our icon instead of Python's
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except Exception:
        pass

    try:
        root = tk.Tk()
    except Exception:
        fail("Could not start Tkinter (is Python installed with tcl/tk?):")
        traceback.print_exc()
        sys.exit(1)

    # Send any error that happens inside a button/search callback to the console too
    def tk_exception_handler(exc_type, exc_value, exc_tb):
        fail(f"Error inside the app: {exc_type.__name__}: {exc_value}")
        traceback.print_exception(exc_type, exc_value, exc_tb)
        try:
            messagebox.showerror("Unexpected error",
                                 f"{exc_type.__name__}: {exc_value}\n\nSee the console for details.")
        except Exception:
            pass

    root.report_callback_exception = tk_exception_handler

    try:
        SafeGPOApp(root)
        root.mainloop()
    except Exception:
        fail("The app crashed during startup:")
        traceback.print_exc()
        try:
            root.destroy()
        except Exception:
            pass
        sys.exit(1)


if __name__ == "__main__":
    main()
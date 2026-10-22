"""Main window: account, folder, one Start button, and plain progress.

Login and the less common save options open in their own small windows so the
main screen stays short. Download work runs in a separate worker process.
"""

from dataclasses import asdict, replace
import gc
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from tkinter import filedialog, ttk
import webbrowser

from . import __version__, dialogs, theme
from .core import JobOptions, _normal_account
from .login import (clear_active_session, clear_session, has_session, list_accounts, normalize_identity,
                    remove_account, save_auth_token, save_authenticated_session, select_account)
from .settings import load_settings, settings_path
from .review import CONTROL_NAME, ReviewControl
from .storage import StoragePaths, missing_media_help, resolve_storage, saved_media_missing


def _file_label(event) -> str:
    """A short, single-line file name for the work log."""
    name = Path(str(event.get("path") or "")).name or str(event.get("filename") or "")
    name = re.sub(r"[\x00-\x1f\x7f]", "", name)
    return name or "파일 " + str(event.get("tweet_id") or "미확인")


AUTH_MODES = {
    "인증 토큰 (권장)": "managed",
    "cookies.txt 파일": "file",
}
FOLDER_MODES = {"구분 없이 저장": "flat", "작성자별 폴더": "author", "저장일별 폴더": "date"}
FOLDER_CHOICES = {
    "구분 없이 저장": ("한 폴더에 모두 저장", "게시물ID_1.jpg"),
    "작성자별 폴더": ("작성자 아이디별 폴더로 나누기", "작성자\\게시물ID_1.jpg"),
    "저장일별 폴더": ("저장한 날짜별 폴더로 나누기", "2026-10-05\\게시물ID_1.jpg"),
}
SCAN_MODES = {"새 Like만 빠르게 확인 (권장)": "quick", "전체 목록 다시 확인": "full"}
STATUS_TEXT = {"stopped": "중단됨", "limited": "시험 저장으로 끝남", "error": "실패"}
GIF_FORMATS = {"GIF로 바꿔 저장": "gif", "원본 MP4 그대로 저장": "mp4"}
GIF_FORMAT_HINTS = {
    "gif": "어디서나 움직이는 .gif입니다. 색이 256개로 줄고 MP4보다 몇 배 클 수 있습니다.",
    "mp4": "X의 원본 MP4를 그대로 저장합니다. 작고 화질이 좋습니다.",
}
GIF_SIZES = {"원본 크기": "original", "최대 720px · 초당 15장 (용량 절약)": "720",
             "최대 480px · 초당 12장 (가장 작게)": "480"}
SCAN_HINTS = {
    "quick": "지난번 이후 새로 누른 Like만 빠르게 받습니다. 평소에 쓰세요.",
    "full": "오래된 Like까지 처음부터 다시 확인합니다. 오래 걸립니다.",
}
TOKEN_STEPS = ("1. 크롬·엣지에서 x.com에 로그인합니다.\n"
               "2. F12 → Application(애플리케이션) → Cookies → https://x.com\n"
               "3. auth_token의 Value를 복사합니다.\n"
               "4. 아래 칸에 붙여 넣고 ‘연결 저장’을 누릅니다.")


def worker_command(request: Path, events: Path, stop: Path) -> list[str]:
    if getattr(sys, "frozen", False):
        prefix = [sys.executable]
    else:
        prefix = [sys.executable, str(Path(__file__).resolve().parents[1] / "entry.py")]
    return prefix + ["--worker", str(request), str(events), str(stop)]


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title(f"러브박스 (LuvBox) {__version__}")
        width = min(1440, max(760, root.winfo_screenwidth() - 80))
        height = min(1320, max(360, root.winfo_screenheight() - 100))
        x = max(0, (root.winfo_screenwidth() - width) // 2)
        # Leave room for the title bar and taskbar when opening a tall window.
        y = max(0, (root.winfo_screenheight() - height - 80) // 2)
        root.geometry(f"{width}x{height}+{x}+{y}")
        root.minsize(min(780, width), min(620, height))
        # On Windows iconbitmap creates the native wrapper. Request geometry
        # first so a withdrawn window does not retain Tk's default 200x200 size.
        from .app_icon import install
        install(root)
        root.configure(bg=theme.BG)
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.events: queue.Queue = queue.Queue()
        self._poll_timer = None
        self._dashboard_layout_timer = None
        self._setup_collapsed = False
        self._dashboard_compact_height = None
        self._dashboard_base_minimum = (min(780, width), min(620, height))
        self._dashboard_screen_limit = (max(width, root.winfo_screenwidth() - 80), height)
        self._polling = False
        self._ui_closed = False
        self._deferred_event = None
        self._poll_logs = None
        self._poll_counts_dirty = False
        self.running = False
        self.relocating = False
        self._storage_action = None
        self._storage_plan = None
        self._storage_fresh = False
        self.stopping = False
        self.closing = False
        self.stop_path: Path | None = None
        self.process: subprocess.Popen | None = None
        self.saved = 0
        self.skipped = 0
        self.size_skipped = 0
        self.review_skipped = 0
        self.review_never = 0
        self.review_window = None
        self._review_candidate = None
        self._review_context = None
        self._review_auto_from_toggle = False
        self.history_window = None
        self.connect_window = None
        self.login_cancel = threading.Event()
        self.login_busy = False
        self.options_window = None
        self.help_window = None
        self.schedule_window = None
        self.schedule_busy = False
        self.schedule_info = None
        self.history_refresh_pending = False
        self.received_final = False
        self.final_status = None
        self.inputs: list[tk.Widget] = []
        settings = load_settings()
        self.appearance = tk.StringVar(root, value="dark" if settings.get("appearance") == "dark" else "light")
        self.theme_button_text = tk.StringVar(root)
        self.record_locations = {}
        locations = settings.get("record_locations", {})
        if isinstance(locations, dict):
            for ident, directory in locations.items():
                try:
                    if isinstance(directory, str) and directory.strip():
                        self.record_locations[_normal_account(ident)] = directory
                except (ValueError, TypeError, AttributeError):
                    pass
        # Seed the new per-account map from preferences saved by 1.0.0.
        try:
            previous_account = _normal_account(str(settings.get("account", "")))
            previous_output = settings.get("output_dir")
            if "record_locations" not in settings and isinstance(previous_output, str) and previous_output.strip():
                self.record_locations.setdefault(previous_account, previous_output)
        except ValueError:
            pass
        self.account = tk.StringVar(value=str(settings.get("account", "")))
        self._connected_accounts = list_accounts()
        self._active_identity = next((row for row in self._connected_accounts if row.get("active")), None)
        if self._active_identity is not None and settings.get("auth_mode", "managed") != "file":
            self.account.set(self._active_identity["handle"])
        self.folder = tk.StringVar(value=str(settings.get("output_dir", Path.home() / "Pictures" / "LuvBox")))
        if (settings.get("auth_mode", "managed") != "file" and self._active_identity
                and self._active_identity["handle"] in self.record_locations):
            self.folder.set(self.record_locations[self._active_identity["handle"]])
        mode = settings.get("auth_mode", "managed")
        self.auth = tk.StringVar(value=next((k for k, v in AUTH_MODES.items() if v == mode), next(iter(AUTH_MODES))))
        self.cookies = tk.StringVar()
        self.token = tk.StringVar()
        layout = settings.get("folder_mode", "flat")
        self.folder_mode = tk.StringVar(value=next((k for k, v in FOLDER_MODES.items() if v == layout), "구분 없이 저장"))
        self.separate_accounts = tk.BooleanVar(value=settings.get("separate_accounts", False) is True)
        self.destination_preview = tk.StringVar()
        # Shown only when files go below the chosen folder (author/date/account).
        self.destination_text = tk.StringVar()
        self.include_videos = tk.BooleanVar(value=settings.get("include_videos", True) is not False)
        self.include_gifs = tk.BooleanVar(value=settings.get("include_gifs", True) is not False)
        self.max_video_mb = tk.StringVar(value=str(settings.get("max_video_mb", 200)))
        gif_format = settings.get("gif_format", "gif")
        self.gif_format = tk.StringVar(value=next((k for k, v in GIF_FORMATS.items() if v == gif_format), next(iter(GIF_FORMATS))))
        gif_size = settings.get("gif_size", "original")
        self.gif_size = tk.StringVar(value=next((k for k, v in GIF_SIZES.items() if v == gif_size), next(iter(GIF_SIZES))))
        self.detailed_progress = tk.BooleanVar(value=settings.get("detailed_progress", False) is True)
        self.confirm_each = tk.BooleanVar(root, value=settings.get("confirm_each", False) is True)
        self.review_control = ReviewControl(self.confirm_each.get())
        self.review_summary = tk.StringVar(root)
        try:
            from .scheduled import normal_time
            schedule_time = normal_time(settings.get("schedule_time", "09:00"))
        except ValueError:
            schedule_time = "09:00"
        self.schedule_time = tk.StringVar(value=schedule_time)
        self.schedule_prompted = str(settings.get("schedule_prompted", ""))
        self.schedule_text = tk.StringVar(value="자동 저장을 아직 설정하지 않았습니다.")
        self.schedule_state = tk.StringVar()
        self.schedule_last = tk.StringVar()
        self.progress_summary = tk.StringVar(value="")
        self.progress_details = tk.StringVar()
        # A short test is opt-in for one run and is never loaded from settings.
        self.limited = tk.BooleanVar(value=False)
        scan = settings.get("scan_mode", "quick")
        self.scan_mode = tk.StringVar(value=next((k for k, v in SCAN_MODES.items() if v == scan), next(iter(SCAN_MODES))))
        self.status = tk.StringVar(value="준비되었습니다")
        self.status_detail = tk.StringVar(value="아이디와 폴더를 확인하고 ‘저장 시작’을 누르세요.")
        self.counts = tk.StringVar()
        self.saved_text, self.skipped_text, self.size_text = tk.StringVar(), tk.StringVar(), tk.StringVar()
        self.login_text = tk.StringVar()
        self.option_summary = tk.StringVar()
        self.scheduled_option_summary = tk.StringVar()
        theme.apply(self.root, mode=self.appearance.get())
        self._update_theme_caption()
        self._build()
        self._update_counts()
        self.auth.trace_add("write", lambda *_: self._auth_changed())
        self.cookies.trace_add("write", lambda *_: self._auth_changed())
        for variable in (self.account, self.folder, self.folder_mode, self.separate_accounts):
            variable.trace_add("write", lambda *_: self._preview_location())
        for variable in (self.folder_mode, self.separate_accounts, self.include_videos, self.include_gifs,
                         self.max_video_mb, self.scan_mode, self.limited, self.gif_format, self.gif_size, self.confirm_each):
            variable.trace_add("write", lambda *_: self._update_option_summary())
        self.confirm_each.trace_add("write", lambda *_: self._review_mode_changed())
        self._auth_changed()
        self._preview_location()
        self._update_option_summary()
        # Keep the work controls reachable even when larger system fonts need
        # more room. The middle view remains scrollable on a short screen.
        self.root.bind("<Configure>", self._schedule_dashboard_layout, add="+")
        self._schedule_dashboard_layout()
        self._poll_timer = self.root.after(120, self._poll)
        self.root.bind("<Destroy>", self._release_ui, add="+")
        # The installed app checks its daily task and the last scheduled run.
        # (Running from source, this happens when the schedule window opens.)
        if getattr(sys, "frozen", False):
            self.root.after(1500, self._startup_schedule_check)

    def _release_ui(self, event):
        if event.widget is self.root:
            self._ui_closed = True
            if self._poll_timer is not None:
                self.root.after_cancel(self._poll_timer)
                self._poll_timer = None
            if self._dashboard_layout_timer is not None:
                self.root.after_cancel(self._dashboard_layout_timer)
                self._dashboard_layout_timer = None
            self._deferred_event = None
            self._poll_logs = None
            self.login_cancel.set()
            self._destroy_review()
            theme.release_variables(self)
            self.inputs.clear()

    # ------------------------------------------------------------------ layout
    def _build(self):
        self.workspace = ttk.Frame(self.root)
        self.workspace.pack(fill="both", expand=True)
        self.workspace.columnconfigure(0, weight=1)
        self.workspace.rowconfigure(1, weight=1)

        # Keep navigation in one fixed header at every supported width. Moving
        # a sidebar into the page at a breakpoint made a 1px resize shift the
        # entire form by hundreds of pixels.
        self.sidebar = ttk.Frame(self.workspace, style="Sidebar.TFrame", padding=(24, 12))
        self.sidebar.grid(row=0, column=0, sticky="ew")
        self.sidebar.columnconfigure(0, weight=1)
        brand = ttk.Frame(self.sidebar, style="Sidebar.TFrame")
        brand.grid(row=0, column=0, sticky="w")
        try:
            from PIL import Image, ImageTk
            from .app_icon import asset_directory
            with Image.open(asset_directory() / "luvbox.png") as picture:
                self._brand_icon = ImageTk.PhotoImage(picture.resize((34, 34), Image.Resampling.LANCZOS),
                                                      master=self.root)
            ttk.Label(brand, image=self._brand_icon, style="Sidebar.TLabel").pack(side="left", padx=(0, 10))
        except (OSError, tk.TclError):
            self._brand_icon = None
        ttk.Label(brand, text="러브박스", style="SidebarTitle.TLabel").pack(side="left")
        self.setup_toggle_button = ttk.Button(self.sidebar, text="계정 · 폴더 접기", command=self._toggle_setup,
                                              style="Ghost.TButton")
        self.setup_toggle_button.grid(row=0, column=1, sticky="e", padx=(12, 8))
        self.theme_button = ttk.Button(self.sidebar, textvariable=self.theme_button_text,
                                       command=self.toggle_theme, style="Ghost.TButton")
        self.theme_button.grid(row=0, column=2, sticky="e")
        navigation = ttk.Frame(self.sidebar, style="Sidebar.TFrame")
        navigation.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(12, 0))
        navigation.columnconfigure((0, 2, 4, 6), weight=1, uniform="navigation")
        navigation.columnconfigure((1, 3, 5), minsize=8)
        for column, (caption, command) in enumerate((
                ("기록 · 통계", self.show_history), ("저장 설정", self.show_options),
                ("자동 저장", self.show_schedule), ("사용 방법", self.help))):
            button = ttk.Button(navigation, text=caption, command=command, style="Ghost.TButton")
            button.grid(row=0, column=column * 2, sticky="nsew")
            if caption == "저장 설정":
                self.options_button = button
        self.compact_navigation = navigation

        self.content_view = theme.ScrollableContent(self.workspace, padding=(24, 16, 24, 8))
        self.content_view.grid(row=1, column=0, sticky="nsew")
        shell = self.content_view.content

        form = self.setup_card = theme.card(shell, fill="x", pady=(0, 8))
        form.columnconfigure(1, weight=1)
        ttk.Label(form, text="X 계정", style="CardMuted.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 12))
        account = self.account_entry = ttk.Combobox(form, textvariable=self.account, width=1, font=theme.BODY,
                                                   values=[row["handle"] for row in self._connected_accounts])
        account.grid(row=0, column=1, sticky="ew")
        account.bind("<<ComboboxSelected>>", self._choose_account)
        self.inputs.append(account)
        connection = ttk.Frame(form, style="Plain.TFrame")
        connection.grid(row=0, column=2, sticky="ew", padx=(16, 0))
        self.login_label = ttk.Label(connection, textvariable=self.login_text,
                                                style="Disconnected.TLabel", wraplength=400, anchor="w")
        self.login_label.pack(side="left", padx=(0, 8))
        self.login_button = ttk.Button(connection, text="로그인 연결", command=self.show_connect, style="Ghost.TButton")
        self.login_button.pack(side="right")
        self.inputs.append(self.login_button)
        ttk.Label(form, text="저장 폴더", style="CardMuted.TLabel").grid(row=1, column=0, sticky="w", padx=(0, 12), pady=(12, 0))
        folder_row = ttk.Frame(form, style="Plain.TFrame")
        folder_row.grid(row=1, column=1, columnspan=2, sticky="ew", pady=(12, 0))
        folder_row.columnconfigure(0, weight=1)
        folder = self.folder_entry = ttk.Entry(folder_row, textvariable=self.folder, width=1, font=theme.BODY)
        folder.grid(row=0, column=0, sticky="ew")
        folder.bind("<Return>", lambda _: self._ensure_storage())
        browse = ttk.Button(folder_row, text="찾아보기", command=self.choose_folder, style="Ghost.TButton", width=8)
        browse.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        ttk.Button(folder_row, text="열기", command=self.open_folder, style="Ghost.TButton", width=8).grid(row=0, column=2, sticky="nsew", padx=(8, 0))
        self.inputs.extend((folder, browse))
        self.preview_label = self._wrapping_label(form, textvariable=self.destination_text,
                                                  style="CardMuted.TLabel", wraplength=700)
        self.preview_label.grid(row=2, column=1, columnspan=2, sticky="ew", pady=(8, 0))
        summary = ttk.Frame(form, style="Plain.TFrame")
        summary.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        summary.columnconfigure(0, weight=1)
        self.option_summary_label = self._wrapping_label(summary, textvariable=self.option_summary,
                                                         style="CardMuted.TLabel", wraplength=600)
        self.option_summary_label.grid(row=0, column=0, sticky="ew")
        self.inputs.append(self.options_button)

        hero = self.work_card = theme.card(self.workspace)
        hero.grid(row=2, column=0, sticky="ew", padx=24, pady=(0, 16))
        head = ttk.Frame(hero, style="Plain.TFrame")
        head.pack(fill="x", pady=(0, 12))
        ttk.Label(head, text="이번 저장", style="CardEyebrow.TLabel").pack(side="left")
        self.work_actions = ttk.Frame(head, style="Plain.TFrame")
        self.work_actions.pack(side="right")
        self.work_actions.columnconfigure(0, weight=1, uniform="work-action")
        self.work_actions.columnconfigure(1, minsize=8)
        self.work_actions.columnconfigure(2, weight=1, uniform="work-action")
        self.start_button = ttk.Button(self.work_actions, text="저장 시작", style="DialogPrimary.TButton", command=self.start)
        self.start_button.grid(row=0, column=0, sticky="nsew")
        self.stop_button = ttk.Button(self.work_actions, text="중단", command=self.stop, state="disabled")
        self.stop_button.grid(row=0, column=2, sticky="nsew")
        self.review_toggle = ttk.Checkbutton(hero, text="저장 전에 하나씩 확인", variable=self.confirm_each,
                                            style="Plain.TCheckbutton")
        self.review_toggle.pack(anchor="w", pady=(0, 6))
        self._wrapping_label(hero, textvariable=self.status, style="CardStatus.TLabel", wraplength=800).pack(fill="x")
        tiles = ttk.Frame(hero, style="Plain.TFrame")
        tiles.pack(fill="x", pady=(8, 12))
        for column, (variable, caption) in enumerate(((self.saved_text, "새로 저장"), (self.skipped_text, "이미 저장된 파일"),
                                                       (self.size_text, "용량 초과"))):
            metric = ttk.Frame(tiles, style="Plain.TFrame")
            metric.grid(row=0, column=column, sticky="ew", padx=(0 if column == 0 else 20, 0))
            ttk.Label(metric, text=caption, style="CardMuted.TLabel").pack(side="left", padx=(0, 8))
            ttk.Label(metric, textvariable=variable, style="CardBold.TLabel").pack(side="left")
            tiles.columnconfigure(column, weight=1, uniform="metric")
        self.progress = theme.Progressbar(hero, mode="determinate", value=0)
        self.progress.pack(fill="x", pady=(0, 6))
        self.progress_heading = ttk.Frame(hero, style="Plain.TFrame")
        self.progress_heading.pack(fill="x")
        self.progress_heading.columnconfigure(0, weight=1)
        self.progress_line = tk.StringVar(self.root)
        self.progress_detail_text = tk.StringVar(self.root)
        def summarize_progress(*_):
            text = " ".join(self.progress_summary.get().split())
            self.progress_line.set(text if len(text) <= 76 else text[:73] + "…")
            self.progress_detail_text.set("\n".join(filter(None, (self.progress_summary.get(), self.progress_details.get()))))
        self.progress_summary.trace_add("write", summarize_progress)
        self.progress_details.trace_add("write", summarize_progress)
        ttk.Label(self.progress_heading, textvariable=self.progress_line, style="CardMuted.TLabel",
                  width=1, anchor="w").grid(row=0, column=0, sticky="ew")
        ttk.Checkbutton(self.progress_heading, text="자세히", variable=self.detailed_progress, command=self._toggle_progress,
                        style="Plain.TCheckbutton").grid(row=0, column=1, sticky="e", padx=(12, 0))
        self.log_frame = theme.card(shell, fill="both", expand=True, pady=(0, 12))
        log_heading = self.log_heading = ttk.Frame(self.log_frame, style="Plain.TFrame")
        log_heading.pack(fill="x", pady=(0, 8))
        self.log_title = ttk.Label(log_heading, text="최근 작업", style="CardBold.TLabel")
        self.log_title.pack(side="left")
        ttk.Button(log_heading, text="화면 비우기", command=self._clear_log,
                   style="Ghost.TButton").pack(side="right")
        self._wrapping_label(self.log_frame, textvariable=self.status_detail, style="CardMuted.TLabel",
                             wraplength=800).pack(fill="x", pady=(0, 8))
        self.progress_detail_label = self._wrapping_label(self.log_frame, textvariable=self.progress_detail_text,
                                                         style="CardMuted.TLabel", wraplength=800)
        log_body = ttk.Frame(self.log_frame, style="Plain.TFrame")
        log_body.pack(fill="both", expand=True)
        self.log = tk.Text(log_body, height=5, wrap="word", bg=theme.CARD, fg=theme.INK, relief="flat", borderwidth=0,
                           highlightthickness=0, padx=2, pady=4, state="disabled", font=theme.SMALL)
        scroll = ttk.Scrollbar(log_body, command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        self.log.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.log_placeholder = ttk.Label(log_body, text="저장을 시작하면 진행 내역이 여기에 표시됩니다.",
                                          style="CardMuted.TLabel", wraplength=550, justify="center")
        self.log_placeholder.place(relx=.5, rely=.5, anchor="center")
        self.log.tag_configure("time", foreground=theme.MUTED)
        self.log.tag_configure("error", foreground=theme.RED)
        self.log.tag_configure("warning", foreground=theme.ORANGE)
        if self.detailed_progress.get():
            self._toggle_progress()
        self.root.bind("<Control-Return>", lambda _: self.start())
        self.root.bind("<F6>", lambda _: self.show_history())
        self.root.bind("<Control-comma>", lambda _: self.show_options())

    def _focus_dashboard(self):
        self.root.lift()
        if self._setup_collapsed:
            self._toggle_setup()
        self.content_view.canvas.yview_moveto(0)
        self.account_entry.focus_set()

    def _toggle_setup(self):
        self._setup_collapsed = not self._setup_collapsed
        if self._setup_collapsed:
            self.setup_card.pack_forget()
        else:
            self.setup_card.pack(fill="x", before=self.log_frame, pady=(0, 8))
        self.setup_toggle_button.configure(text="계정 · 폴더 보기" if self._setup_collapsed else "계정 · 폴더 접기")
        self.content_view._schedule_layout()
        self._schedule_dashboard_layout()

    def _schedule_dashboard_layout(self, event=None):
        if event is not None and event.widget not in (
                self.root, self.sidebar, self.setup_card, self.work_card, self.log_heading):
            return
        if not self._ui_closed and self._dashboard_layout_timer is None:
            self._dashboard_layout_timer = self.root.after_idle(self._fit_dashboard_minimum)

    def _fit_dashboard_minimum(self):
        self._dashboard_layout_timer = None
        if self._ui_closed:
            return
        measure = lambda text: int(self.root.tk.call("font", "measure", self.account_entry.cget("font"), text))
        # Reserve a useful account field instead of allowing buttons and the
        # connection status to squeeze it to a few pixels at high DPI.
        field_width = (measure("저장 폴더") + 12 + measure("abcdefghijklmnop") + 28 + 16 +
                       self.login_button.master.winfo_reqwidth() + 84)
        action_width = self.work_actions.winfo_reqwidth() + measure("이번 저장") + 100
        minimum_width = min(self._dashboard_screen_limit[0], max(
            self._dashboard_base_minimum[0], self.sidebar.winfo_reqwidth(), field_width, action_width))
        linespace = int(self.root.tk.call("font", "metrics", self.log.cget("font"), "-linespace"))
        # Three readable log lines and its heading are the central viewport's
        # useful minimum. Account/folder controls can be scrolled or folded;
        # the work card never needs to be clipped to make them fit.
        middle_height = max(120, linespace * 3 + self.log_heading.winfo_reqheight() + 44)
        needed_height = self.sidebar.winfo_reqheight() + self.work_card.winfo_reqheight() + middle_height + 40
        compact_height = needed_height > self._dashboard_screen_limit[1]
        if compact_height != self._dashboard_compact_height:
            self._dashboard_compact_height = compact_height
            self.content_view.content.configure(padding=(24, 4, 24, 0) if compact_height else (24, 16, 24, 8))
            self.log_frame.configure(padding=(18, 8) if compact_height else (18, 14))
            self.log_frame.pack_configure(pady=(0, 0) if compact_height else (0, 12))
            self.log.configure(height=3 if compact_height else 5)
        minimum_height = min(self._dashboard_screen_limit[1], max(
            self._dashboard_base_minimum[1], needed_height))
        minimum = (minimum_width, minimum_height)
        if self.root.minsize() != minimum:
            self.root.minsize(*minimum)

    def _clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        self.log_placeholder.place(relx=.5, rely=.5, anchor="center")

    def _wrapping_label(self, parent, **kwargs):
        label = ttk.Label(parent, justify="left", **kwargs)
        def resize(event):
            # wraplength measures the text, while Configure includes ttk's
            # border/padding elements. Reserve their horizontal inset so a
            # line that just fits is not clipped at the label's right edge.
            width = max(1, event.width - 4)
            if int(label.cget("wraplength")) != width:
                label.configure(wraplength=width)
            view = getattr(label.winfo_toplevel(), "content_view", self.content_view)
            view._schedule_layout()
        label.bind("<Configure>", resize)
        return label

    def _toggle_progress(self):
        if self.detailed_progress.get():
            self.progress_detail_label.pack(after=self.log_heading, fill="x", pady=(0, 8))
        else:
            self.progress_detail_label.pack_forget()
        self.content_view._schedule_layout()

    def _update_theme_caption(self):
        self.theme_button_text.set("라이트 테마" if self.appearance.get() == "dark" else "다크 테마")

    def toggle_theme(self):
        self.appearance.set("light" if self.appearance.get() == "dark" else "dark")
        theme.apply(self.root, mode=self.appearance.get())
        self._update_theme_caption()
        try:
            data = load_settings()
            data["appearance"] = self.appearance.get()
            self._write_settings(data)
        except OSError:
            self._log("테마 설정을 기억하지 못했습니다. 다음 실행에서 다시 선택해 주세요.")

    def _update_option_summary(self):
        layout = {"flat": "한 폴더", "author": "작성자별 폴더", "date": "날짜별 폴더"}[FOLDER_MODES[self.folder_mode.get()]]
        if self.separate_accounts.get():
            layout += "(아이디 폴더 안)"
        gif = "GIF"
        if GIF_FORMATS[self.gif_format.get()] == "mp4":
            gif = "GIF(MP4 그대로)"
        elif GIF_SIZES[self.gif_size.get()] != "original":
            gif = f"GIF {GIF_SIZES[self.gif_size.get()]}px"
        media = ["사진"] + (["동영상"] if self.include_videos.get() else []) + ([gif] if self.include_gifs.get() else [])
        size = self.max_video_mb.get().strip()
        limit = "" if len(media) == 1 else (" (영상 크기 제한 없음)" if size == "0" else f" (영상 {size}MB까지)")
        scan = "새 Like만" if SCAN_MODES[self.scan_mode.get()] == "quick" else "전체 확인"
        test = " · 시험 20개" if self.limited.get() else ""
        self._sync_option_controls()
        description = f"{layout} · {'·'.join(media)}{limit}"
        review = " · 하나씩 확인" if self.confirm_each.get() else ""
        self.option_summary.set(f"{description} · {scan}{test}{review}")
        self.scheduled_option_summary.set(f"{description} · 새 Like만")

    def _gif_options(self):
        """GIF size preset and media size limit for converting saved MP4 GIFs."""
        try:
            size = max(0, int(self.max_video_mb.get().strip()))
        except ValueError:
            size = 200
        return GIF_SIZES[self.gif_size.get()], size

    def _history_location(self):
        if self.relocating:
            raise ValueError("저장 기록을 옮기고 있습니다.")
        account = _normal_account(self.account.get())
        if not self.folder.get().strip():
            raise ValueError("저장 폴더를 입력하세요.")
        return account, resolve_storage(self.folder.get(), account, self.separate_accounts.get())

    def _preview_location(self):
        try:
            if not self.folder.get().strip():
                raise ValueError
            account = _normal_account(self.account.get()) if self.account.get().strip() else "my_account"
            paths = resolve_storage(self.folder.get(), account, self.separate_accounts.get())
            suffix = {"author": "작성자", "date": "YYYY-MM-DD", "flat": ""}[FOLDER_MODES[self.folder_mode.get()]]
            preview = str(paths.media_dir / suffix) if suffix else str(paths.media_dir)
            self.destination_preview.set(preview)
            chosen = os.path.normcase(os.path.abspath(os.path.expanduser(self.folder.get().strip())))
            same = os.path.normcase(os.path.abspath(preview)) == chosen
            self.destination_text.set("" if same else "실제 저장 위치  " + preview)
        except (ValueError, OSError):
            self.destination_preview.set("아이디와 저장 폴더를 확인하세요.")
            self.destination_text.set(self.destination_preview.get())
        label = getattr(self, "preview_label", None)
        if label is not None:
            if self.destination_text.get():
                label.grid()
            else:
                label.grid_remove()

    def show_history(self):
        if not self._ensure_storage("history"):
            return
        from .history_gui import HistoryWindow
        if self.history_window is not None and self.history_window.winfo_exists():
            self.history_window.lift()
            self.history_window.refresh()
        else:
            self.history_window = HistoryWindow(self.root, self._history_location, lambda: self.running,
                                                self._gif_options, self.retry_missing)
        try:
            account, paths = self._history_location()
            self.record_locations[account] = str(paths.output_dir)
            self._save_settings()
        except (ValueError, OSError):
            self._log("기록 위치 설정을 기억하지 못했습니다. 폴더 권한을 확인해 주세요.")

    def _refresh_history(self):
        self.history_refresh_pending = False
        if self.history_window is not None and self.history_window.winfo_exists():
            self.history_window.refresh()

    def _schedule_history_refresh(self):
        if not self.history_refresh_pending and self.history_window is not None and self.history_window.winfo_exists():
            self.history_refresh_pending = True
            self.root.after(self.history_window.auto_refresh_delay(), self._refresh_history)

    def _update_counts(self):
        self.saved_text.set(f"{self.saved:,}개")
        self.skipped_text.set(f"{self.skipped:,}개")
        self.size_text.set(f"{self.size_skipped:,}개")
        self.counts.set(f"이번 저장 {self.saved:,}개 · 이미 저장됨 {self.skipped:,}개 · 용량 초과 {self.size_skipped:,}개")
        self._update_review_summary()

    def _update_review_summary(self):
        text = "사진·영상·GIF를 보고 선택합니다 · 예약 자동 저장은 확인 없이 진행합니다." if self.confirm_each.get() else "켜면 사진·영상·GIF를 보고 저장 여부를 선택합니다."
        if self.review_skipped:
            text += f" · 이번엔 건너뜀 {self.review_skipped:,}개"
        if self.review_never:
            text += f" · 저장 안 함 {self.review_never:,}개"
        self.review_summary.set(text)

    def _review_mode_changed(self):
        enabled = self.confirm_each.get()
        window = self.review_window
        try:
            # Stop the decoder before letting the child move its open source.
            if not enabled and window is not None and window.winfo_exists():
                self._review_auto_from_toggle = True
                window.choose("auto")
            else:
                self.review_control.set_enabled(enabled)
        except OSError:
            self._log("확인 옵션을 적용하지 못해 작업을 멈춥니다.")
            self.stop()
        self._update_review_summary()
        if self.options_window is None:
            try:
                data = load_settings()
                data["confirm_each"] = enabled
                self._write_settings(data)
            except OSError:
                self._log("확인 옵션을 기억하지 못했습니다. 다음 실행에서 다시 선택해 주세요.")

    def _destroy_review(self):
        window, self.review_window = self.review_window, None
        self._review_candidate = None
        self._review_auto_from_toggle = False
        if window is not None and window.winfo_exists():
            window.destroy()

    def _show_review(self, event):
        if not self.running or self.stopping or self.closing:
            return
        ident = event.get("id")
        if not isinstance(ident, str) or not re.fullmatch(r"[0-9a-f]{32}", ident):
            self.stop()
            return
        if not self.confirm_each.get():
            self.review_control.respond(ident, "save", enabled=False)
            return
        if self.review_window is not None:
            # The child has only one outstanding request; duplicated events
            # must never replace a candidate that still has an open decoder.
            if (self._review_candidate or {}).get("id") != ident:
                self.stop()
            return
        context = self._review_context
        if context is None:
            self.stop()
            return
        root = context.media_dir.resolve()
        signature = event.get("signature")

        def permitted(path):
            from .recycling import file_signature
            try:
                return (isinstance(signature, list) and len(signature) == 4 and
                        Path(path).resolve().is_relative_to(root) and
                        list(file_signature(path)) == signature)
            except (OSError, RuntimeError):
                return False

        if not isinstance(event.get("path"), str) or not permitted(event["path"]):
            self._log("미리보기 파일 위치가 바뀌어 저장을 멈춥니다.")
            self.stop()
            return
        from .review_gui import ReviewWindow
        self._review_candidate = event
        self.progress.stop()
        self.status.set("저장할지 골라 주세요")
        self.status_detail.set("저장하거나 이번만 건너뛰고, ‘저장 안 함’으로 다음에도 받지 않도록 고를 수 있습니다.")
        self.review_window = ReviewWindow(self.root, event,
                                         lambda action: self._review_decision(ident, action), permitted)

    def _review_decision(self, ident, action):
        if (self._review_candidate or {}).get("id") != ident:
            return
        from_toggle = self._review_auto_from_toggle
        self._destroy_review()
        if action == "stop":
            self.stop()
        if action == "auto" and not from_toggle:
            self.confirm_each.set(False)
        try:
            self.review_control.respond(ident, "save" if action == "auto" else action,
                                        enabled=self.confirm_each.get())
        except (OSError, ValueError):
            self._log("저장 선택을 전달하지 못해 작업을 멈춥니다.")
            self.stop()
        if not self.stopping:
            self.status.set("저장을 이어가고 있습니다…")
            self.status_detail.set("선택에 따라 다음 파일을 처리합니다.")

    def _update_progress(self, event):
        from .presentation import format_bytes, format_date, MEDIA_LABELS
        downloaded = max(0, int(event.get("downloaded") or 0))
        total = max(0, int(event.get("total") or 0))
        filename = str(event.get("filename") or "현재 파일")
        kind = MEDIA_LABELS.get(event.get("media_type"), "파일")
        if total:
            percent = min(100, downloaded * 100 / total)
            if str(self.progress.cget("mode")) != "determinate":
                self.progress.stop()
            self.progress.configure(mode="determinate", maximum=100, value=percent)
            amount = f"{format_bytes(downloaded)} / {format_bytes(total)} · {percent:.0f}%"
        else:
            if str(self.progress.cget("mode")) != "indeterminate":
                self.progress.configure(mode="indeterminate")
                self.progress.start(12)
            amount = f"{format_bytes(downloaded)} / 전체 용량 미확인"
        self.progress_summary.set(f"{kind} · {filename} · {amount}")
        author = str(event.get("author") or "작성자 미확인")
        self.progress_details.set(f"속도 {format_bytes(event.get('speed') or 0)}/초 · 작성자 @{author} · 트윗 {event.get('tweet_id') or '미확인'}\n게시일 {format_date(event.get('posted_at'))} · 막대는 지금 받는 파일의 진행률입니다.")
        if not self.stopping:
            self.status.set("파일을 저장하고 있습니다…")

    CONVERSION_STAGES = {"started": ("GIF로 변환 중", "GIF로 변환하고 있습니다…"),
                         "finished": ("GIF 변환 완료", "GIF 파일을 마무리하고 있습니다…"),
                         "fallback": ("GIF 대신 원본 MP4로 저장", "원본 MP4로 저장하고 있습니다…")}

    def _update_conversion(self, event):
        from .presentation import format_date
        stage = event.get("stage") if event.get("stage") in self.CONVERSION_STAGES else "started"
        summary, status = self.CONVERSION_STAGES[stage]
        self.progress_summary.set(summary + " · " + str(event.get("filename") or "현재 파일"))
        author = str(event.get("author") or "작성자 미확인")
        self.progress_details.set(f"작성자 @{author} · 트윗 게시일 {format_date(event.get('posted_at'))}")
        if stage == "started":
            self.progress.stop()
            self.progress.configure(mode="indeterminate", value=0)
            self.progress.start()
        if not self.stopping:
            self.status.set(status)

    # ------------------------------------------------------------------- login
    def _refresh_accounts(self):
        self._connected_accounts = list_accounts()
        self._active_identity = next((row for row in self._connected_accounts if row.get("active")), None)
        self.account_entry.configure(values=[row["handle"] for row in self._connected_accounts])
        if self._active_identity is not None and AUTH_MODES[self.auth.get()] == "managed":
            self.account.set(self._active_identity["handle"])

    def _choose_account(self, _event=None):
        if AUTH_MODES[self.auth.get()] != "managed":
            return
        if self.running or self.relocating or self.login_busy:
            if self._active_identity:
                self.account.set(self._active_identity["handle"])
            return
        selected = next((row for row in self._connected_accounts
                         if row["handle"] == self.account.get().strip().lstrip("@").lower()), None)
        if selected is None:
            return
        previous = self._active_identity
        try:
            select_account(selected["user_id"])
        except (OSError, ValueError):
            if previous:
                self.account.set(previous["handle"])
            dialogs.showerror("계정 선택", "이 계정의 연결을 사용할 수 없습니다. 다시 연결해 주세요.", parent=self.root)
            return
        self._refresh_accounts()
        directory = self.record_locations.get(selected["handle"])
        if directory:
            self.folder.set(directory)
        self._auth_changed()
        self.status.set(f"@{selected['handle']} 계정을 선택했습니다")

    def _auth_changed(self):
        mode = AUTH_MODES[self.auth.get()]
        if mode == "managed":
            if self._active_identity:
                self.account.set(self._active_identity["handle"])
            connected = has_session()
            identity = self._active_identity
            self.login_text.set("● 연결됨" if connected and identity
                                else "● 연결 정보 저장됨" if connected else "● 연결 필요")
        else:
            self.token.set("")
            connected = bool(self.cookies.get().strip())
            self.login_text.set("● cookies.txt 파일 사용" if connected else "● cookies.txt 파일을 골라 주세요")
        self.login_label.configure(style="Connected.TLabel" if connected else "Disconnected.TLabel")
        self.login_button.configure(text="계정 추가" if connected and self._active_identity
                                    else "연결 변경" if connected else "로그인 연결")
        self.account_entry.configure(state="disabled" if self.running or self.relocating or self.login_busy
                                     else "readonly" if mode == "managed" and self._active_identity else "normal")
        window = self.connect_window
        if window is not None and window.winfo_exists():
            if mode == "managed":
                self.cookie_controls.pack_forget()
                self.token_controls.pack(fill="x")
                self.clear_button.configure(state="normal" if has_session() and not self.running and not self.login_busy else "disabled")
            else:
                self.token_controls.pack_forget()
                self.cookie_controls.pack(fill="x")
            if mode != "managed":
                self.login_cancel.set()
            self.browser_button.configure(state="disabled" if self.login_busy else "normal")
            self.default_browser_button.configure(state="disabled" if self.login_busy else "normal")
            self.token_button.configure(state="disabled" if self.login_busy else "normal")
            self.browser_button.configure(text="로그인 기다리는 중…" if self.login_busy else "Edge에서 X 로그인")

    def show_connect(self, notice=""):
        """Connect an existing default-browser login or add a new browser login."""
        window = self.connect_window
        if window is not None and window.winfo_exists():
            window.lift()
            window.focus_set()
            return
        window = self.connect_window = tk.Toplevel(self.root)
        window.withdraw()
        window.title("X 로그인 연결")
        window.configure(bg=theme.CARD)
        window.transient(self.root)
        window.resizable(True, True)
        heading = ttk.Frame(window, style="Plain.TFrame", padding=(28, 24, 28, 8))
        heading.pack(fill="x")
        ttk.Label(heading, text="X 로그인 연결", style="CardTitle.TLabel").pack(anchor="w")
        self._wrapping_label(heading, text="로그인한 계정을 자동으로 확인하고 목록에 추가합니다.",
                             style="CardMuted.TLabel", wraplength=640).pack(fill="x", pady=(6, 0))
        body = self._popup_content(window, padding=(28, 12, 28, 8))
        if notice:
            self._wrapping_label(body, text=notice, style="CardMuted.TLabel", foreground=theme.ORANGE, wraplength=640).pack(fill="x", pady=(6, 0))
        modes = ttk.Frame(body, style="Plain.TFrame")
        modes.pack(anchor="w", pady=(18, 16))
        for label in AUTH_MODES:
            ttk.Radiobutton(modes, text="브라우저 로그인 (권장)" if AUTH_MODES[label] == "managed" else label,
                            value=label, variable=self.auth).pack(anchor="w", pady=(0, 4))

        self.token_controls = ttk.Frame(body, style="Plain.TFrame")
        self.default_browser_button = ttk.Button(self.token_controls, text="기본 브라우저 계정 연결",
                                                 style="DialogPrimary.TButton", command=self.connect_default_browser)
        self.default_browser_button.pack(fill="x", pady=(0, 8))
        self._wrapping_label(self.token_controls, text="기본 브라우저에 로그인된 X 계정을 사용합니다. 처음 한 번 연결 확장을 설치해 주세요.",
                             style="CardMuted.TLabel", wraplength=640).pack(fill="x", pady=(0, 8))
        ttk.Button(self.token_controls, text="연결 확장 설치 안내", style="Ghost.TButton",
                   command=self.open_extension_folder).pack(anchor="w", pady=(0, 16))
        self.browser_button = ttk.Button(self.token_controls, text="Edge에서 X 로그인", command=self.connect_browser)
        self.browser_button.pack(fill="x", pady=(0, 8))
        self._wrapping_label(self.token_controls, text="다른 계정으로 새로 로그인하려면 Edge 창을 사용할 수 있습니다.",
                             style="CardMuted.TLabel", wraplength=640).pack(fill="x")
        self.browser_status = tk.StringVar(self.root)
        self._wrapping_label(self.token_controls, textvariable=self.browser_status,
                             style="CardMuted.TLabel", wraplength=640).pack(fill="x", pady=(6, 0))
        self.manual_button = ttk.Button(self.token_controls, text="토큰 직접 입력 ▾", style="Ghost.TButton", command=self.toggle_manual_token)
        self.manual_button.pack(anchor="w", pady=(8, 0))
        self.manual_controls = ttk.Frame(self.token_controls, style="Plain.TFrame")
        self._wrapping_label(self.manual_controls, text=TOKEN_STEPS, style="Card.TLabel", width=1, wraplength=640).pack(fill="x")
        row = ttk.Frame(self.manual_controls, style="Plain.TFrame")
        row.pack(fill="x", pady=(16, 8))
        self.token_entry = ttk.Entry(row, textvariable=self.token, show="●", width=1, font=theme.BODY)
        self.token_entry.pack(side="left", fill="x", expand=True)
        self.token_entry.bind("<Return>", lambda _: self._connect_from_window())
        self.token_button = ttk.Button(row, text="연결 저장", width=11, command=self._connect_from_window)
        self.token_button.pack(side="left", padx=(8, 0))
        self._wrapping_label(self.token_controls, text="연결 정보는 이 Windows 계정에 암호화해 저장합니다.",
                             style="CardMuted.TLabel", wraplength=640).pack(fill="x")
        self.clear_button = ttk.Button(self.token_controls, text="선택한 계정 연결 해제", command=self.disconnect)
        self.clear_button.pack(anchor="w", pady=(10, 0))

        self.cookie_controls = ttk.Frame(body, style="Plain.TFrame")
        self._wrapping_label(self.cookie_controls, text="로그인된 x.com 쿠키를 Netscape 형식(cookies.txt)으로 내보낸 파일을 고릅니다.",
                             style="Card.TLabel", wraplength=640).pack(fill="x")
        row = ttk.Frame(self.cookie_controls, style="Plain.TFrame")
        row.pack(fill="x", pady=(16, 8))
        self.cookie_entry = ttk.Entry(row, textvariable=self.cookies, width=1, font=theme.BODY)
        self.cookie_entry.pack(side="left", fill="x", expand=True)
        self.cookie_button = ttk.Button(row, text="파일 선택", width=11, command=self.choose_cookies)
        self.cookie_button.pack(side="left", padx=(8, 0))
        self._wrapping_label(self.cookie_controls, text="파일 위치는 기억하지 않아 앱을 다시 열면 다시 골라야 합니다. 예약 실행에는 쓸 수 없습니다.",
                             style="CardMuted.TLabel", wraplength=640).pack(fill="x")

        footer = window.footer = ttk.Frame(window, style="Plain.TFrame", padding=(28, 14, 28, 20))
        footer.pack(side="bottom", fill="x", before=window.content_view)
        footer.columnconfigure((0, 2), weight=1, uniform="connect-action")
        footer.columnconfigure(1, minsize=8)
        ttk.Button(footer, text="쿠키 확인 방법", command=lambda: webbrowser.open("https://developer.chrome.com/docs/devtools/application/cookies/")).grid(
            row=0, column=0, sticky="nsew")
        ttk.Button(footer, text="닫기", command=self._close_connect).grid(row=0, column=2, sticky="nsew")
        window.protocol("WM_DELETE_WINDOW", self._close_connect)
        window.bind("<Escape>", lambda _: self._close_connect())
        self._auth_changed()
        self._fit_popup(window)
        (self.browser_button if AUTH_MODES[self.auth.get()] == "managed" else self.cookie_entry).focus_set()

    def toggle_manual_token(self):
        if self.manual_controls.winfo_manager():
            self.manual_controls.pack_forget()
            self.manual_button.configure(text="토큰 직접 입력 ▾")
        else:
            self.manual_controls.pack(fill="x", after=self.manual_button, pady=(12, 8))
            self.manual_button.configure(text="토큰 직접 입력 ▴")
            self.token_entry.focus_set()
        self._fit_popup(self.connect_window)

    def connect_browser(self):
        if self.login_busy or self.running or self.closing:
            return
        self.login_cancel = threading.Event()
        self.login_busy = True
        self.browser_status.set("Edge 창에서 로그인해 주세요. 닫기를 누르면 취소합니다.")
        self._auth_changed()
        # The worker owns no Tk widgets or variables, including on shutdown.
        threading.Thread(target=App._browser_worker, args=(self.events, self.login_cancel), daemon=True).start()

    def open_extension_folder(self):
        from .browser_bridge import extension_directory
        directory = extension_directory()
        try:
            os.startfile(str(directory))
        except (OSError, AttributeError):
            dialogs.showinfo("연결 확장 설치", f"Chrome·Edge·Brave의 확장 관리에서 개발자 모드를 켜고 "
                             f"‘압축해제된 확장 로드’로 chromium 폴더를 선택하세요. "
                             f"Firefox는 firefox 폴더의 안내를 확인하세요.\n\n{directory}",
                             parent=self.connect_window or self.root)
            return
        dialogs.showinfo("연결 확장 설치", "Chrome·Edge·Brave의 확장 관리에서 개발자 모드를 켜고 ‘압축해제된 확장 로드’로 "
                         "방금 열린 폴더 안의 chromium을 선택하세요. Firefox는 firefox 폴더의 안내를 확인하세요. "
                         "설치 후 ‘기본 브라우저 계정 연결’을 누릅니다.",
                         parent=self.connect_window or self.root)

    def connect_default_browser(self):
        if self.login_busy or self.running or self.closing:
            return
        self.login_cancel = threading.Event()
        self.login_busy = True
        self.browser_status.set("기본 브라우저에서 연결할 X 계정을 확인해 주세요. 연결 확장이 처음 한 번 필요합니다.")
        self._auth_changed()
        threading.Thread(target=App._default_browser_worker,
                         args=(self.events, self.login_cancel), daemon=True).start()

    @staticmethod
    def _default_browser_worker(events, cancel):
        from .browser_bridge import BrowserBridge, BrowserBridgeError
        from .login import SessionCancelled
        message, identity = "", None
        try:
            with BrowserBridge(timeout=180) as bridge:
                if cancel.is_set():
                    return
                events.put({"type": "browser-pairing", "code": bridge.pairing_code})
                bridge.open_browser()
                session = bridge.wait(cancel=cancel)
                if cancel.is_set():
                    return
                identity = save_authenticated_session(session.auth_token, session.identity, cancel=cancel)
        except BrowserBridgeError as exc:
            message = str(exc)
        except SessionCancelled:
            message = "로그인 연결을 취소했습니다."
        except Exception:
            message = "기본 브라우저 연결을 마치지 못했습니다. 연결 확장을 확인하고 다시 시도해 주세요."
        finally:
            if cancel.is_set() and identity is None:
                message = "로그인 연결을 취소했습니다."
            events.put({"type": "browser-login", "message": message, "identity": identity})

    def _browser_pairing(self, event):
        code = event.get("code")
        if (self.login_busy and isinstance(code, str) and re.fullmatch(r"[A-Z0-9_-]{8}", code)
                and self.connect_window is not None and self.connect_window.winfo_exists()):
            self.browser_status.set(f"연결 코드 {code} · 브라우저 확장에 같은 코드가 표시되는지 확인한 뒤 연결을 누르세요.")

    @staticmethod
    def _browser_worker(events, cancel):
        from .browser_login import sign_in, SignInError
        message, identity = "", None
        try:
            identity = sign_in(cancel)
        except SignInError as exc:
            message = str(exc)
        except Exception:
            message = "로그인 연결을 마치지 못했습니다. 다시 시도하거나 ‘토큰 직접 입력’을 사용해 주세요."
        events.put({"type": "browser-login", "message": message, "identity": identity})

    def _browser_result(self, event):
        self.login_busy = False
        self._refresh_accounts()
        self._auth_changed()
        message = event.get("message")
        if message:
            self.status.set(message)
            if self.connect_window is not None and self.connect_window.winfo_exists():
                self.browser_status.set(message)
        else:
            identity = event.get("identity")
            if isinstance(identity, dict) and AUTH_MODES[self.auth.get()] == "managed":
                try:
                    identity = normalize_identity(identity)
                    self.account.set(identity["handle"])
                except ValueError:
                    identity = None
                if identity:
                    directory = self.record_locations.get(identity["handle"])
                    if directory:
                        self.folder.set(directory)
            self.status.set("로그인을 연결했습니다")
            self.status_detail.set("연결 정보 저장됨 · 이제 저장을 시작할 수 있습니다.")
            self._close_connect()

    @staticmethod
    def _popup_content(window, *, padding):
        view = window.content_view = theme.ScrollableContent(window, padding=padding)
        view.configure(style="Plain.TFrame")
        view.canvas.configure(background=theme.CARD)
        view.content.configure(style="Plain.TFrame")
        view.pack(fill="both", expand=True)
        return view.content

    def _fit_popup(self, window):
        # Set a readable width before measuring wrapped text. Natural widget
        # widths can otherwise push a single long instruction off the screen.
        hidden = window.state() == "withdrawn"
        width = (min(740, max(480, self.root.winfo_screenwidth() - 80))
                 if hidden else window.winfo_width())
        max_height = max(360, self.root.winfo_screenheight() - 120)
        if hidden:
            window.geometry(f"{width}x{max_height}")
        window.update_idletasks()
        body = window.content_view.content
        extra = sum(child.winfo_reqheight() for child in window.winfo_children()
                    if child is not window.content_view and child.winfo_manager() == "pack")
        height = min(body.winfo_reqheight() + extra, max_height)
        window.minsize(min(600, width), min(360, height))
        window.geometry(f"{width}x{height}")
        if hidden:
            # Settle wrapping and geometry while withdrawn. The first mapped
            # frame must already have the final size, including on Windows.
            window.update_idletasks()
            window.deiconify()

    def _close_connect(self):
        if self.login_busy:
            self.login_cancel.set()
        window, self.connect_window = self.connect_window, None
        self.token.set("")
        if window is not None and window.winfo_exists():
            window.destroy()

    def _connect_from_window(self):
        if self.connect_token():
            self._close_connect()

    def connect_token(self) -> bool:
        if self.running or self.login_busy:
            return False
        try:
            save_auth_token(self.token.get())
        except ValueError as exc:
            dialogs.showerror("연결 저장 실패", str(exc), parent=self.connect_window or self.root)
            return False
        except OSError:
            dialogs.showerror("연결 저장 실패", "연결 정보를 저장하지 못했습니다. Windows 계정과 저장 권한을 확인해 주세요.", parent=self.connect_window or self.root)
            return False
        finally:
            self.token.set("")
        self._refresh_accounts()
        self._auth_changed()
        self.status.set("로그인을 연결했습니다")
        self.status_detail.set("연결 저장됨 · 실제 유효성은 저장을 시작할 때 확인합니다.")
        self._log("인증 토큰을 이 Windows 계정에 암호화 저장했습니다. X 로그인 요청은 보내지 않았습니다.")
        return True

    def disconnect(self):
        if self.running or self.login_busy:
            return
        try:
            if self._active_identity:
                remove_account(self._active_identity["user_id"])
            elif self._connected_accounts:
                clear_active_session()
            else:
                clear_session()
        except (OSError, ValueError):
            dialogs.showerror("연결 지우기 실패", "로그인 정보를 지우지 못했습니다. 잠시 후 다시 시도해 주세요.", parent=self.root)
            return
        self.token.set("")
        self._refresh_accounts()
        self._auth_changed()
        self.status.set("저장된 로그인 연결을 지웠습니다")
        self.status_detail.set(f"@{self._active_identity['handle']} 계정의 연결을 사용합니다."
                               if self._active_identity else "다시 저장하려면 ‘로그인 연결’을 눌러 연결하세요.")

    def choose_cookies(self):
        value = filedialog.askopenfilename(title="X 로그인 쿠키 파일 선택", filetypes=[("Netscape 쿠키 파일", "*.txt"), ("모든 파일", "*.*")],
                                           parent=self.connect_window or self.root)
        if value:
            self.cookies.set(value)

    # ----------------------------------------------------------------- options
    def show_options(self):
        if self.running or self.relocating:
            return
        window = self.options_window
        if window is not None and window.winfo_exists():
            window.lift()
            return
        window = self.options_window = tk.Toplevel(self.root)
        window.withdraw()
        window.title("저장 설정")
        window.configure(bg=theme.CARD)
        window.transient(self.root)
        window.resizable(True, True)
        self._option_snapshot = {name: getattr(self, name).get() for name in (
            "folder_mode", "separate_accounts", "include_videos", "include_gifs", "max_video_mb",
            "gif_format", "gif_size", "scan_mode", "limited", "confirm_each")}
        heading = ttk.Frame(window, style="Plain.TFrame", padding=(28, 24, 28, 8))
        heading.pack(fill="x")
        ttk.Label(heading, text="저장 설정", style="CardTitle.TLabel").pack(anchor="w")
        self._wrapping_label(heading, text="변경한 설정은 다음 저장부터 적용됩니다.",
                             style="CardMuted.TLabel", wraplength=640).pack(fill="x", pady=(6, 0))
        footer = window.footer = ttk.Frame(window, style="Plain.TFrame", padding=(28, 14, 28, 20))
        footer.pack(side="bottom", fill="x")
        footer.columnconfigure(0, weight=1)
        footer.columnconfigure((1, 3), uniform="settings-action")
        footer.columnconfigure(2, minsize=8)
        ttk.Button(footer, text="설정 저장", command=self._close_options,
                   style="DialogPrimary.TButton").grid(row=0, column=3, sticky="nsew")
        ttk.Button(footer, text="취소", command=lambda: self._close_options(save=False, restore=True)).grid(
            row=0, column=1, sticky="nsew")
        body = self._popup_content(window, padding=(28, 8, 28, 8))
        self.option_tabs = ttk.Notebook(body, style="Card.TNotebook")
        self.option_tabs.pack(fill="both", expand=True)
        pages = []
        for title in ("저장 위치", "미디어", "확인 방식"):
            page = ttk.Frame(self.option_tabs, style="Plain.TFrame", padding=(14, 18))
            self.option_tabs.add(page, text=title)
            pages.append(page)
        folders, media, advanced = pages

        def section(parent, title, note=""):
            ttk.Label(parent, text=title, style="CardBold.TLabel").pack(anchor="w", pady=(0, 8))
            if note:
                self._wrapping_label(parent, text=note, style="CardMuted.TLabel", wraplength=580).pack(fill="x", pady=(0, 12))

        section(folders, "폴더 정리", "기본값은 선택한 저장 폴더에 바로 저장입니다.")
        for key, (_, example) in FOLDER_CHOICES.items():
            row = ttk.Frame(folders, style="Plain.TFrame", padding=(0, 4))
            row.pack(fill="x", pady=(0, 8))
            ttk.Radiobutton(row, text=key, value=key, variable=self.folder_mode).pack(anchor="w")
            self._wrapping_label(row, text=example, style="CardMuted.TLabel", wraplength=560).pack(fill="x", padx=(32, 0), pady=(2, 0))
        ttk.Separator(folders).pack(fill="x", pady=(10, 18))
        ttk.Checkbutton(folders, text="계정별 폴더도 만들기", variable=self.separate_accounts).pack(anchor="w")
        self._wrapping_label(folders, text="여러 계정을 사용할 때 내 아이디 폴더 안에 저장합니다.",
                             style="CardMuted.TLabel", wraplength=560).pack(fill="x", padx=(56, 0), pady=(4, 0))

        section(media, "저장할 미디어", "사진은 원본 크기로 항상 저장합니다.")
        ttk.Checkbutton(media, text="동영상 · 최고 화질 MP4", variable=self.include_videos).pack(anchor="w", pady=(0, 8))
        ttk.Checkbutton(media, text="움직이는 GIF", variable=self.include_gifs).pack(anchor="w")
        limit = ttk.Frame(media, style="Plain.TFrame")
        limit.pack(fill="x", pady=(14, 0))
        ttk.Label(limit, text="영상·GIF 1개 최대", style="Card.TLabel").pack(side="left")
        self.video_limit_entry = ttk.Spinbox(limit, from_=0, to=100000, increment=50, width=7,
                                            textvariable=self.max_video_mb, font=theme.BODY)
        self.video_limit_entry.pack(side="left", padx=8)
        ttk.Label(limit, text="MB", style="CardMuted.TLabel").pack(side="left")
        self._wrapping_label(media, text="0은 제한 없음 · 원본 영상과 변환된 GIF에 적용합니다.",
                             style="CardMuted.TLabel", wraplength=580).pack(fill="x", pady=(6, 0))
        ttk.Separator(media).pack(fill="x", pady=(18, 16))
        section(media, "GIF 저장 형식")
        self.gif_format_controls = []
        for key, value in GIF_FORMATS.items():
            button = ttk.Radiobutton(media, text=key, value=key, variable=self.gif_format)
            button.pack(anchor="w", pady=(0, 4))
            self.gif_format_controls.append(button)
        self.gif_hint_label = self._wrapping_label(media, text="", style="CardMuted.TLabel", wraplength=580)
        self.gif_hint_label.pack(fill="x", pady=(4, 12))
        gif_row = ttk.Frame(media, style="Plain.TFrame")
        gif_row.pack(fill="x")
        ttk.Label(gif_row, text="GIF 크기", style="Card.TLabel").pack(side="left")
        self.gif_size_combo = ttk.Combobox(gif_row, textvariable=self.gif_size, values=tuple(GIF_SIZES),
                                            state="readonly", width=27, font=theme.BODY)
        self.gif_size_combo.pack(side="left", padx=8, fill="x", expand=True)
        self._wrapping_label(media, text="작게 저장하면 용량이 줄어듭니다. 기존 파일은 바뀌지 않습니다.",
                             style="CardMuted.TLabel", wraplength=580).pack(fill="x", pady=(6, 0))

        section(advanced, "어디까지 확인할까요?")
        for key, value in SCAN_MODES.items():
            caption = "새 Like만 확인 · 권장" if value == "quick" else "전체 목록 다시 확인"
            ttk.Radiobutton(advanced, text=caption, value=key, variable=self.scan_mode).pack(anchor="w")
            self._wrapping_label(advanced, text=SCAN_HINTS[value], style="CardMuted.TLabel",
                                 wraplength=560).pack(fill="x", padx=(32, 0), pady=(4, 16))
        ttk.Separator(advanced).pack(fill="x", pady=(4, 18))
        ttk.Checkbutton(advanced, text="이번에는 20개만 시험 저장", variable=self.limited).pack(anchor="w")
        self._wrapping_label(advanced, text="새 파일 20개를 저장한 뒤 멈춥니다. 이번 실행에만 적용됩니다.",
                             style="CardMuted.TLabel", wraplength=560).pack(fill="x", padx=(56, 0), pady=(4, 0))
        ttk.Separator(advanced).pack(fill="x", pady=(18, 16))
        ttk.Checkbutton(advanced, text="저장 전에 하나씩 확인", variable=self.confirm_each).pack(anchor="w")
        self._wrapping_label(advanced, text="사진·동영상·GIF를 보고 저장하거나 건너뜁니다. 메인 화면에서 진행 중에도 바꿀 수 있습니다. 예약 자동 저장에는 적용하지 않습니다.",
                             style="CardMuted.TLabel", wraplength=560).pack(fill="x", padx=(32, 0), pady=(4, 0))
        window.protocol("WM_DELETE_WINDOW", lambda: self._close_options(save=False, restore=True))
        window.bind("<Escape>", lambda _: self._close_options(save=False, restore=True))
        self._sync_option_controls()
        self._fit_popup(window)
        window.grab_set()
        window.focus_set()

    def _options_editing(self):
        """Finish saving or cancelling draft options before another action."""
        window = self.options_window
        if window is not None and window.winfo_exists():
            window.lift()
            window.focus_set()
            return True
        return False

    def _sync_option_controls(self):
        if self.options_window is None or not self.options_window.winfo_exists() or not hasattr(self, "gif_size_combo"):
            return
        enabled = self.include_gifs.get()
        for control in self.gif_format_controls:
            control.configure(state="normal" if enabled else "disabled")
        self.gif_size_combo.configure(state="readonly" if enabled and GIF_FORMATS[self.gif_format.get()] == "gif" else "disabled")
        self.video_limit_entry.configure(state="normal" if enabled or self.include_videos.get() else "disabled")
        self.gif_hint_label.configure(text=GIF_FORMAT_HINTS[GIF_FORMATS[self.gif_format.get()]] if enabled else "GIF 저장을 켜면 형식을 선택할 수 있습니다.")

    def _video_limit(self):
        try:
            value = int(self.max_video_mb.get().strip())
        except ValueError:
            raise ValueError("영상 최대 크기는 0 이상의 정수(MB)로 입력해 주세요. 0은 제한 없음입니다.") from None
        if value < 0:
            raise ValueError("영상 최대 크기는 0 이상으로 입력해 주세요. 0은 제한 없음입니다.")
        return value

    def _save_option_preferences(self):
        data = load_settings()
        data.update({"folder_mode": FOLDER_MODES[self.folder_mode.get()], "scan_mode": SCAN_MODES[self.scan_mode.get()],
                     "separate_accounts": self.separate_accounts.get(), "include_videos": self.include_videos.get(),
                     "include_gifs": self.include_gifs.get(), "max_video_mb": self._video_limit(),
                     "gif_format": GIF_FORMATS[self.gif_format.get()], "gif_size": GIF_SIZES[self.gif_size.get()],
                     "confirm_each": self.confirm_each.get()})
        self._write_settings(data)

    def _close_options(self, *, save=True, restore=False):
        window = self.options_window
        if window is None or not window.winfo_exists():
            self.options_window = None
            return
        if restore:
            for name, value in self._option_snapshot.items():
                getattr(self, name).set(value)
        elif save:
            try:
                self._save_option_preferences()
            except (ValueError, OSError) as exc:
                message = str(exc) if isinstance(exc, ValueError) else "설정을 저장하지 못했습니다. 다시 시도해 주세요."
                dialogs.showerror("설정 확인", message, parent=window)
                self.option_tabs.select(1)
                self.video_limit_entry.focus_set()
                return
        self.options_window = None
        window.destroy()

    # ------------------------------------------------------------------ folder
    def choose_folder(self):
        if self._options_editing():
            return
        value = filedialog.askdirectory(title="미디어 저장 폴더 선택", mustexist=False)
        if value:
            self.folder.set(value)
            self._ensure_storage()

    def _save_settings(self, options=None):
        account = options.account if options is not None else _normal_account(self.account.get())
        directory = options.output_dir if options is not None else str(Path(self.folder.get()).expanduser().resolve())
        try:
            size = int(self.max_video_mb.get().strip())
        except ValueError:
            size = 200
        data = {"account": account, "output_dir": directory,
                "auth_mode": AUTH_MODES[self.auth.get()],
                "folder_mode": FOLDER_MODES[self.folder_mode.get()], "scan_mode": SCAN_MODES[self.scan_mode.get()],
                "include_videos": self.include_videos.get(), "include_gifs": self.include_gifs.get(),
                "max_video_mb": size, "separate_accounts": self.separate_accounts.get(),
                "gif_format": GIF_FORMATS[self.gif_format.get()], "gif_size": GIF_SIZES[self.gif_size.get()],
                "detailed_progress": self.detailed_progress.get(),
                "confirm_each": self.confirm_each.get(),
                "appearance": self.appearance.get(),
                "schedule_time": self.schedule_time.get(), "schedule_prompted": self.schedule_prompted,
                "record_locations": self.record_locations}
        self._write_settings(data)

    def _write_settings(self, data):
        path = settings_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        name = None
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                             prefix="settings-", suffix=".tmp", delete=False) as stream:
                name = Path(stream.name)
                json.dump(data, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            name.replace(path)
        finally:
            if name is not None:
                name.unlink(missing_ok=True)

    def _ensure_storage(self, action=None):
        """Carry the account's records when a completed path edit is committed."""
        if self._options_editing():
            return False
        if self.relocating:
            if action is not None:
                self._storage_action = action
            return False
        if self.running:
            return action == "history"
        try:
            account, destination = self._history_location()
        except (ValueError, OSError):
            # Start and History retain their normal, specific input validation.
            return True
        old_directory = self.record_locations.get(account)
        if old_directory is None:
            return True
        try:
            source = resolve_storage(old_directory, account, self.separate_accounts.get())
        except (ValueError, OSError):
            self.folder.set(old_directory)
            self.status.set("이전 기록 폴더에 접근하지 못했습니다")
            self.status_detail.set("경로와 드라이브 연결을 확인해 주세요.")
            self._log(self.status.get())
            return False
        if source.output_dir == destination.output_dir:
            return True
        if self._library_job_active():
            self.folder.set(str(source.output_dir))
            dialogs.showinfo("기록 화면 작업 중", "라이브러리 작업이 끝난 뒤 저장 폴더를 바꿔 주세요.", parent=self.root)
            return False
        fresh = not source.output_dir.is_dir()
        if fresh and not dialogs.askyesno(
                "이전 저장 폴더 연결 확인",
                "이전 폴더를 찾을 수 없습니다. 새 폴더에서 시작할까요?\n\n"
                "이전 기록을 못 가져와 같은 파일을 다시 받을 수 있습니다.\n"
                "나중에 이전 폴더를 다시 고르면 기록을 합칠 수 있습니다.",
                parent=self.root, default="no"):
            self.folder.set(str(source.output_dir))
            self.status.set("이전 저장 폴더를 유지했습니다")
            self.status_detail.set("폴더가 있는 드라이브의 연결을 확인해 주세요.")
            return False
        self.relocating = True
        self._storage_fresh = fresh
        self._storage_action = action
        self._storage_plan = (source, destination)
        self._set_running(True)
        self.stop_button.configure(state="disabled")
        self.status.set("새 저장 폴더를 준비하고 있습니다…" if fresh else "저장 기록을 새 폴더로 옮기고 있습니다…")
        self.status_detail.set("기존 사진·영상은 원래 위치에 그대로 둡니다.")
        self.progress.configure(mode="indeterminate", value=0)
        self.progress.start(12)
        self._begin_relocation()
        return False

    def _begin_relocation(self):
        window = self.history_window
        if window is not None and window.winfo_exists():
            if window.busy:
                self.root.after(100, self._begin_relocation)
                return
            window.destroy()
        self.history_window = None
        source, destination = self._storage_plan
        threading.Thread(target=self._relocate_records, args=(source, destination), daemon=True).start()

    def _relocate_records(self, source, destination):
        from .storage import MigrationError, initialize_storage, migrate_records
        try:
            if self._storage_fresh:
                initialize_storage(destination)
            else:
                migrate_records(source, destination)
            self.events.put({"type": "storage-finished", "ok": True})
        except MigrationError as exc:
            # This domain error contains only the app's fixed help messages.
            self.events.put({"type": "storage-finished", "ok": False, "message": str(exc)})
        except Exception:
            # Do not forward database/library exception text or private paths.
            self.events.put({"type": "storage-finished", "ok": False})

    def _finish_relocation(self, event):
        source, destination = self._storage_plan
        action = self._storage_action
        fresh = self._storage_fresh
        success = event.get("ok") is True
        if success:
            self.record_locations[source.account] = str(destination.output_dir)
            try:
                self._save_settings()
            except OSError:
                success = False
        if not success:
            self.record_locations[source.account] = str(source.output_dir)
            self.folder.set(str(source.output_dir))
        self.relocating = False
        self._storage_action = self._storage_plan = None
        self._storage_fresh = False
        self.progress.stop()
        self.progress.configure(mode="determinate", value=0)
        self._set_running(False)
        if success:
            if fresh:
                self.status.set("새 저장 폴더를 준비했습니다")
                self.status_detail.set("이전 폴더의 기록은 그 폴더가 연결되면 가져올 수 있습니다.")
                self._log("새 저장 폴더를 준비했습니다. 이전 폴더의 기록은 연결 후 가져올 수 있습니다.")
            else:
                self.status.set("저장 폴더를 바꿨습니다")
                self.status_detail.set("기존 기록을 이어서 사용합니다. 예전 사진·영상은 원래 위치에 있습니다.")
                self._log("기록 자동 이전 완료 · 기존 사진·영상은 원래 위치에 있습니다.")
        else:
            self.status.set("기록을 옮기지 못해 이전 저장 폴더를 유지했습니다")
            self.status_detail.set("")
            self._log(self.status.get())
            if not self.closing:
                detail = event.get("message") or "이전·새 폴더의 연결과 권한을 확인하고 다른 저장 작업을 닫아 주세요. 기존 기록은 보존했습니다."
                dialogs.showerror("기록 이전 실패", detail, parent=self.root)
        if self.closing:
            self.root.destroy()
        elif success and action == "start":
            self.start()
        elif success and action == "history":
            self.show_history()
        elif success and action == "schedule":
            self.register_schedule()

    def open_folder(self):
        try:
            if not self.folder.get().strip():
                raise ValueError
            if self.separate_accounts.get():
                _, paths = self._history_location()
                folder = paths.media_dir
            else:
                folder = Path(self.folder.get()).expanduser().resolve()
            folder.mkdir(parents=True, exist_ok=True)
            os.startfile(str(folder))
        except (OSError, ValueError):
            dialogs.showerror("폴더 열기 실패", "저장 위치와 폴더 권한을 확인해 주세요.", parent=self.root)

    # -------------------------------------------------------------------- help
    HELP_SECTIONS = (
        ("처음 시작하기", (
            "1. ‘로그인 연결 → 기본 브라우저 계정 연결’을 누릅니다. 처음에는 연결 확장 설치 안내를 확인하세요.",
            "2. 브라우저 확장에서 연결 코드와 X 계정을 확인합니다. 로그인한 계정이 자동으로 목록에 추가됩니다.",
            "3. X 계정 목록에서 사용할 계정과 저장할 폴더를 고르고 ‘저장 시작’을 누릅니다.",
        ), "한 번 받은 파일은 건너뛰고, 다음부터는 새 Like만 받습니다."),
        ("로그인이 안 될 때", (
            "확장 설치가 어려우면 ‘Edge에서 X 로그인’으로 별도 로그인할 수 있습니다.",
            "기존 토큰·쿠키 연결은 계속 사용할 수 있습니다. 필요하면 ‘토큰 직접 입력’을 펼쳐 안내대로 넣습니다.",
            "연결이 끊기면 같은 방법으로 다시 연결합니다.",
        ), "로그인 정보는 이 PC에만 보관합니다."),
        ("저장 설정", (
            "폴더 정리: 한 폴더에 모두, 작성자별, 저장한 날짜별 중에서 고릅니다.",
            "동영상·GIF를 받을지와 영상 하나의 최대 크기(기본 200MB)를 정합니다.",
            "평소에는 ‘새 Like만 확인’, 예전 Like까지 다시 보려면 ‘전체 목록 다시 확인’을 고릅니다.",
        ), None),
        ("보고 골라 저장하기", (
            "메인 화면의 ‘저장 전에 하나씩 확인’을 켜고 저장을 시작합니다. 진행 중에도 켜고 끌 수 있습니다.",
            "사진은 크게, 동영상·GIF는 소리 없이 처음 30초를 반복합니다. ‘저장’, ‘이번엔 건너뛰기’, ‘저장 안 함’ 중에서 고릅니다.",
            "‘저장 안 함’을 고른 파일은 다시 묻지 않고 예약 자동 저장도 받지 않습니다. 필요하면 ‘사라진 파일’의 ‘숨긴 파일도 보기’에서 골라 ‘다시 받기’를 누릅니다.",
            "‘이후 자동 저장’은 지금 파일부터 확인 없이 저장합니다. 확인창을 닫으면 전체 작업을 중단합니다.",
            "미리보기용 임시 파일을 먼저 받으므로 인터넷 데이터가 사용됩니다. 동기화 폴더에서는 임시 파일도 동기화될 수 있습니다.",
            "건너뛴 새 파일은 다음 저장에서 다시 처리합니다. 확인을 끄거나 예약 저장을 실행하면 자동 저장될 수 있습니다. ‘사라진 파일’의 다시 받기에서 건너뛴 파일은 필요하면 다시 선택합니다.",
        ), "예약 자동 저장은 이 옵션과 관계없이 확인창 없이 진행합니다."),
        ("기록 · 통계", (
            "받은 파일을 목록이나 갤러리로 보고, 작성자·내용으로 찾습니다.",
            "끌어서 여러 개를 고르거나 ‘여러 개 선택’을 켜고 하나씩 클릭합니다.",
            "고른 파일은 ‘휴지통으로 보내기’ 버튼이나 우클릭 메뉴로 정리합니다. 개수·용량을 확인한 뒤 진행합니다.",
            "보낸 파일은 ‘사라진 파일’에서 확인합니다. 휴지통을 사용할 수 없는 위치는 건너뜁니다.",
            "통계에서 작성자를 누르면 그 작성자의 파일이 최근 게시물부터 모두 나옵니다. 여기서도 골라서 휴지통으로 보낼 수 있습니다.",
        ), None),
        ("파일을 지웠을 때", (
            "탐색기에서 직접 지운 파일도 기록 창의 ‘새로고침’으로 ‘사라진 파일’에 반영됩니다.",
            "휴지통으로 보낼 때 ‘이 계정에서 다시 받지 않기’는 기본으로 켜져 있습니다. 필요하면 ‘숨긴 파일도 보기’에서 다시 받습니다.",
            "Windows 휴지통에서 원래 위치로 복원한 뒤 ‘새로고침’을 누르면 다시 표시됩니다.",
            "지운 파일은 자동으로 다시 받지 않습니다. 필요하면 골라서 ‘다시 받기’를 누릅니다.",
            "‘사라진 파일’에서 고르면 작은 미리보기와 원래 폴더를 볼 수 있습니다.",
            "저장 폴더를 옮겼다면 메인 화면의 ‘저장 폴더’에서 새 위치를 고릅니다.",
        ), None),
        ("매일 자동 저장", (
            "‘자동 저장’에서 시각을 고르고 ‘예약 켜기’를 누르면 매일 새 Like를 저장합니다.",
            "PC가 꺼져 있던 날은 다음에 켤 때 저장하고, 실패하면 알려 줍니다.",
        ), "기본 브라우저나 Edge로 연결한 계정을 사용할 수 있습니다."),
        ("알아 두세요", (
            "X 공식 기능이 아니어서, 천천히 받아도 계정이 제한될 수 있습니다.",
            "‘요청 한도’ 안내가 나오면 알려 준 시각 이후에 다시 시작합니다.",
            "멈추거나 오류가 나도 이미 받은 파일은 그대로 남습니다.",
        ), None),
    )

    def help(self):
        window = self.help_window
        if window is not None and window.winfo_exists():
            window.lift()
            return
        popup = self.help_window = tk.Toplevel(self.root)
        def close():
            # Mapping installs native cleanup on destroy; resolve it at close time.
            popup.destroy()
        popup.protocol("WM_DELETE_WINDOW", close)
        popup.title("러브박스 사용 방법")
        width = min(800, max(600, self.root.winfo_screenwidth() - 80))
        height = min(800, max(480, self.root.winfo_screenheight() - 120))
        popup.geometry(f"{width}x{height}")
        popup.minsize(min(640, width), min(480, height))
        popup.configure(bg=theme.BG)
        popup.transient(self.root)
        heading = ttk.Frame(popup, padding=(24, 20, 24, 0))
        heading.pack(fill="x")
        ttk.Label(heading, text="사용 방법", style="Title.TLabel").pack(anchor="w")
        ttk.Label(heading, text="처음 연결부터 저장 기록 확인까지", style="Muted.TLabel").pack(anchor="w", pady=(4, 0))
        links = ttk.Frame(popup, padding=(24, 12, 24, 20))
        links.pack(side="bottom", fill="x")
        links.columnconfigure((0, 2), weight=1, uniform="help-action")
        links.columnconfigure(1, minsize=8)
        ttk.Button(links, text="자동 저장 예약", command=self.show_schedule).grid(row=0, column=0, sticky="nsew", pady=(0, 8))
        ttk.Button(links, text="자동 저장 실행 기록", command=self.open_schedule_logs).grid(row=0, column=2, sticky="nsew", pady=(0, 8))
        ttk.Button(links, text="X 자동화 정책", command=lambda: webbrowser.open("https://help.x.com/en/rules-and-policies/x-automation")).grid(row=1, column=0, sticky="nsew")
        ttk.Button(links, text="닫기", width=9, command=close).grid(row=1, column=2, sticky="nsew")
        body = theme.card(popup, fill="both", expand=True, padx=24, pady=(16, 0))
        content = tk.Text(body, wrap="word", background=theme.CARD, foreground=theme.INK, relief="flat", borderwidth=0,
                          highlightthickness=0, padx=4, pady=2, font=theme.BODY, spacing1=2, spacing3=4, cursor="arrow")
        scrollbar = ttk.Scrollbar(body, command=content.yview)
        content.configure(yscrollcommand=scrollbar.set)
        content.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        content.tag_configure("heading", font=theme.HEADING, spacing1=4, spacing3=6)
        content.tag_configure("item", lmargin1=8, lmargin2=26)
        content.tag_configure("note", font=theme.SMALL, foreground=theme.MUTED, lmargin1=8, lmargin2=8, spacing1=4, spacing3=18)
        for title, items, note in self.HELP_SECTIONS:
            content.insert("end", title + "\n", "heading")
            for item in items:
                content.insert("end", ("" if item[:1].isdigit() else "• ") + item + "\n", "item")
            content.insert("end", (note or "") + "\n", "note")
        content.configure(state="disabled")

    # ---------------------------------------------------------------- schedule
    def show_schedule(self):
        """Turn the daily automatic save on or off and see how it went."""
        window = self.schedule_window
        if window is not None and window.winfo_exists():
            window.lift()
            self.refresh_schedule()
            return
        window = self.schedule_window = tk.Toplevel(self.root)
        window.withdraw()
        window.title("자동 저장")
        window.configure(bg=theme.CARD)
        window.transient(self.root)
        heading = ttk.Frame(window, style="Plain.TFrame", padding=(28, 24, 28, 8))
        heading.pack(fill="x")
        ttk.Label(heading, text="매일 자동 저장", style="CardTitle.TLabel").pack(anchor="w")
        self._wrapping_label(heading, text="정한 시각에 새 Like를 저장합니다. 로그인 연결이 필요합니다.",
                             style="CardMuted.TLabel", wraplength=640).pack(fill="x", pady=(6, 0))
        footer = window.footer = ttk.Frame(window, style="Plain.TFrame", padding=(28, 14, 28, 20))
        footer.pack(side="bottom", fill="x")
        ttk.Button(footer, text="닫기", command=self._close_schedule, style="Ghost.TButton").pack(side="right")
        body = self._popup_content(window, padding=(28, 12, 28, 8))
        target = ttk.Frame(body, style="Plain.TFrame")
        target.pack(fill="x", pady=(0, 4))
        ttk.Label(target, text="이 계정과 폴더로 저장합니다", style="CardBold.TLabel").pack(anchor="w")
        self._wrapping_label(target, textvariable=self.account, style="Card.TLabel", wraplength=640).pack(fill="x", pady=(6, 0))
        self._wrapping_label(target, textvariable=self.folder, style="CardMuted.TLabel", wraplength=640).pack(fill="x", pady=(4, 0))
        self._wrapping_label(target, textvariable=self.scheduled_option_summary, style="CardMuted.TLabel", wraplength=640).pack(fill="x", pady=(4, 0))
        ttk.Separator(body).pack(fill="x", pady=(18, 14))
        ttk.Label(body, text="지금 상태", style="CardBold.TLabel").pack(anchor="w")
        self._wrapping_label(body, textvariable=self.schedule_state, style="Card.TLabel", wraplength=640).pack(fill="x", pady=(4, 0))
        self._wrapping_label(body, textvariable=self.schedule_last, style="CardMuted.TLabel", wraplength=640).pack(fill="x", pady=(6, 0))
        row = ttk.Frame(body, style="Plain.TFrame")
        row.pack(anchor="w", pady=(18, 0))
        ttk.Label(row, text="실행 시각", style="Card.TLabel").pack(side="left")
        times = [f"{hour:02d}:{minute:02d}" for hour in range(24) for minute in (0, 30)]
        ttk.Combobox(row, textvariable=self.schedule_time, values=times, width=7, font=theme.BODY).pack(side="left", padx=8)
        ttk.Label(row, text="(24시간 형식)", style="CardMuted.TLabel").pack(side="left")
        buttons = ttk.Frame(body, style="Plain.TFrame")
        buttons.pack(anchor="w", pady=(16, 0))
        buttons.columnconfigure((0, 2, 4), weight=1, uniform="schedule-action")
        buttons.columnconfigure((1, 3), minsize=8)
        self.schedule_on_button = ttk.Button(buttons, text="예약 켜기", style="DialogPrimary.TButton", command=self.register_schedule)
        self.schedule_on_button.grid(row=0, column=0, sticky="nsew")
        self.schedule_off_button = ttk.Button(buttons, text="예약 끄기", command=self.unregister_schedule)
        self.schedule_off_button.grid(row=0, column=2, sticky="nsew")
        ttk.Button(buttons, text="실행 기록", command=self.open_schedule_logs).grid(row=0, column=4, sticky="nsew")
        self._wrapping_label(body, text="PC가 꺼져 있던 날은 다음에 켤 때, 배터리일 때도 실행합니다. "
                                        "Windows에 로그인해 있어야 하며, 실패하면 알림이 뜹니다.",
                             style="CardMuted.TLabel", wraplength=640).pack(fill="x", pady=(16, 0))
        window.protocol("WM_DELETE_WINDOW", self._close_schedule)
        window.bind("<Escape>", lambda _: self._close_schedule())
        self._show_schedule_info()
        self._fit_popup(window)
        self.refresh_schedule()

    def _close_schedule(self):
        window, self.schedule_window = self.schedule_window, None
        if window is not None and window.winfo_exists():
            window.destroy()

    def _schedule_job(self, action, work, **details):
        """Run a Task Scheduler call off the UI thread; the result is an event."""
        if self.schedule_busy:
            return False
        self.schedule_busy = True
        self._update_schedule_buttons()
        if action != "status":
            self.schedule_state.set("Windows 작업 스케줄러에 반영하는 중…")
        events = self.events

        def run():
            from .scheduled import ScheduleError, task_status
            try:
                work()
                event = {"type": "schedule", "action": action, "result": task_status(), **details}
            except (ScheduleError, ValueError) as exc:
                event = {"type": "schedule", "action": action, "error": str(exc), **details}
            # Scheduler queries can finish after the window has closed. The
            # worker owns only the event queue, never the App/Tk interpreter.
            events.put(event)

        threading.Thread(target=run, daemon=True).start()
        return True

    def refresh_schedule(self, *, prompt=False):
        self._schedule_job("status", lambda: None, prompt=prompt)

    def register_schedule(self):
        from .scheduled import normal_time
        if self._options_editing():
            return
        if self.schedule_busy or self.running or self.login_busy:
            return
        if self.relocating:
            self._storage_action = "schedule"
            return
        if AUTH_MODES[self.auth.get()] != "managed":
            dialogs.showinfo("자동 저장", "자동 저장은 브라우저 로그인 연결에서만 쓸 수 있습니다. ‘로그인 연결’에서 바꿔 주세요.",
                             parent=self.schedule_window or self.root)
            return
        try:
            when = normal_time(self.schedule_time.get())
            options = self._validated_options(scheduled=True)
        except (ValueError, OSError) as exc:
            message = str(exc) if isinstance(exc, ValueError) else "설정을 저장하지 못했습니다. 폴더 권한을 확인해 주세요."
            dialogs.showerror("자동 저장", message, parent=self.schedule_window or self.root)
            return
        if self.token.get() and not self.connect_token():
            return
        if not has_session():
            self.status.set("먼저 X 로그인을 연결해 주세요")
            self.show_connect("자동 저장을 켜려면 먼저 로그인 연결이 필요합니다.")
            return
        if not self._ensure_storage("schedule"):
            return
        try:
            self.schedule_time.set(when)
            self.account.set(options.account)
            self.folder.set(options.output_dir)
            self.record_locations[options.account] = options.output_dir
            self._save_settings(options)
        except OSError:
            dialogs.showerror("자동 저장", "설정을 저장하지 못했습니다. 폴더 권한을 확인해 주세요.",
                              parent=self.schedule_window or self.root)
            return
        from .scheduled import register_task
        self._schedule_job("register", lambda: register_task(when), time=when)

    def unregister_schedule(self):
        if not dialogs.askyesno("자동 저장 끄기", "매일 자동 저장을 끌까요? 저장한 파일과 기록은 그대로 남습니다.",
                                parent=self.schedule_window or self.root):
            return
        from .scheduled import unregister_task
        self._schedule_job("unregister", unregister_task)

    def _update_schedule_buttons(self):
        if self.schedule_window is None or not self.schedule_window.winfo_exists():
            return
        info = self.schedule_info or {}
        busy = "disabled" if self.schedule_busy or self.running or self.relocating else "normal"
        self.schedule_on_button.configure(state=busy, text="예약 갱신" if info.get("registered") else "예약 켜기")
        self.schedule_off_button.configure(state=busy if info.get("registered") else "disabled")

    def _show_schedule_info(self, error=""):
        from .presentation import format_short
        from .scheduled import last_run
        info = self.schedule_info
        if error:
            state = error
            summary = "자동 저장 상태를 확인하지 못했습니다. ‘자동 저장’에서 다시 확인해 주세요."
        elif info is None:
            state = summary = "자동 저장 상태를 확인하는 중…"
        elif not info.get("registered"):
            state = "꺼져 있습니다. 실행 시각을 고르고 ‘예약 켜기’를 누르세요."
            summary = "꺼짐 · ‘자동 저장’에서 매일 정한 시각에 새 Like를 자동으로 저장할 수 있습니다."
        else:
            when = info.get("time") or "시각 미확인"
            upcoming = f" · 다음 실행 {format_short(info['next_run'])}" if info.get("next_run") else ""
            if not info.get("current"):
                state = (f"매일 {when}, 다른 실행 파일({Path(info.get('program') or '').name or '알 수 없음'})로 등록돼 있습니다. "
                         "‘예약 갱신’으로 이 버전으로 바꾸세요.")
                summary = f"매일 {when} · 다른 실행 파일로 등록됨 · ‘자동 저장’에서 갱신하세요."
            elif not (info.get("catch_up") and info.get("on_battery")):
                state = f"매일 {when} · 켜짐. 예전 방식이라 놓친 날은 건너뜁니다. ‘예약 갱신’을 누르세요."
                summary = f"매일 {when} · 켜짐 · 예전 설정이라 ‘자동 저장’에서 갱신하세요."
            elif not info.get("enabled"):
                state = f"매일 {when} · 작업 스케줄러에서 꺼져 있습니다. ‘예약 갱신’을 누르면 켜집니다."
                summary = f"매일 {when} · 작업 스케줄러에서 꺼져 있음"
            else:
                state = f"켜져 있습니다 · 매일 {when}{upcoming}"
                summary = f"매일 {when} · 켜짐{upcoming}"
        last = last_run()
        if last:
            if last.get("busy"):
                result = "다른 저장 작업이 실행 중이라 건너뜀"
            elif last.get("failed"):
                result = "실패 · " + str(last.get("message") or "실행 기록을 확인해 주세요.")
            elif last.get("status") == "finished":
                result = f"완료 · 새로 저장 {int(last.get('saved') or 0):,}개"
            else:
                result = STATUS_TEXT.get(last.get("status"), "끝남")
            self.schedule_last.set(f"마지막 자동 저장: {format_short(last.get('finished_at'))} · {result}")
        else:
            self.schedule_last.set("마지막 자동 저장: 아직 없음")
        self.schedule_state.set(state)
        self.schedule_text.set(summary)
        self._update_schedule_buttons()

    def _schedule_result(self, event):
        self.schedule_busy = False
        action = event.get("action")
        if "error" in event:
            if action == "status":
                self._show_schedule_info(event["error"])
            else:
                self._show_schedule_info()
                dialogs.showerror("자동 저장", event["error"], parent=self.schedule_window or self.root)
            return
        self.schedule_info = event.get("result") or {"registered": False}
        self._show_schedule_info()
        if action == "register":
            self.status.set(f"자동 저장을 켰습니다 · 매일 {event.get('time')}")
            self.status_detail.set("PC가 꺼져 있었으면 다음에 켰을 때 실행합니다.")
        elif action == "unregister":
            self.status.set("자동 저장을 껐습니다")
            self.status_detail.set("")
        elif event.get("prompt"):
            self._offer_schedule_update()

    def _offer_schedule_update(self):
        """After updating the app, move the daily task to this version once."""
        info = self.schedule_info or {}
        current = self._schedule_identity()
        if not info.get("registered") or info.get("current") or self.schedule_prompted == current:
            return
        self.schedule_prompted = current
        try:
            data = load_settings()
            data["schedule_prompted"] = current
            self._write_settings(data)
        except OSError:
            pass
        old = Path(info.get("program") or "").name or "다른 실행 파일"
        if dialogs.askyesno("자동 저장 갱신", f"매일 자동 저장이 {old}로 등록돼 있습니다.\n\n"
                                         "이 버전으로 바꿀까요? 시각은 그대로 둡니다.", parent=self.root):
            if info.get("time"):
                self.schedule_time.set(info["time"])
            self.register_schedule()

    @staticmethod
    def _schedule_identity():
        from .scheduled import launch_parts
        return " ".join(launch_parts())

    def _startup_schedule_check(self):
        from .scheduled import last_run, mark_last_run_seen
        last = last_run()
        if last and last.get("failed") and not last.get("seen") and not self.running:
            from .presentation import format_short
            self.status.set("지난 자동 저장이 실패했습니다")
            self.status_detail.set(f"{format_short(last.get('finished_at'))} · {last.get('message') or '실행 기록을 확인해 주세요.'}")
            self._log("지난 자동 저장 실패 · " + str(last.get("message") or "실행 기록을 확인해 주세요."))
            try:
                mark_last_run_seen(last)
            except OSError:
                pass
        self.refresh_schedule(prompt=True)

    def open_schedule_logs(self):
        from .scheduled import log_directory
        try:
            directory = log_directory()
            directory.mkdir(parents=True, exist_ok=True)
            os.startfile(str(directory))
        except OSError:
            dialogs.showerror("폴더 열기 실패", "예약 실행 기록 폴더를 열지 못했습니다.", parent=self.root)

    # ------------------------------------------------------------------ saving
    def _validated_options(self, *, scheduled=False):
        if AUTH_MODES[self.auth.get()] == "managed" and self._active_identity:
            if _normal_account(self.account.get()) != self._active_identity["handle"]:
                raise ValueError("연결된 계정을 목록에서 다시 선택해 주세요.")
        return JobOptions(account=self.account.get(), output_dir=self.folder.get(), auth_mode=AUTH_MODES[self.auth.get()],
                          cookies_file=self.cookies.get(), photo_limit=0 if scheduled else 20 if self.limited.get() else 0,
                          folder_mode=FOLDER_MODES[self.folder_mode.get()],
                          scan_mode="quick" if scheduled else SCAN_MODES[self.scan_mode.get()],
                          include_videos=self.include_videos.get(), include_gifs=self.include_gifs.get(),
                          max_video_mb=self._video_limit(), separate_accounts=self.separate_accounts.get(),
                          gif_format=GIF_FORMATS[self.gif_format.get()], gif_size=GIF_SIZES[self.gif_size.get()],
                          confirm_each=False if scheduled else self.confirm_each.get()).validate()

    def retry_missing(self, targets, account, location) -> bool:
        """Download chosen missing files again (from the library window)."""
        if self.relocating or self.running or not targets:
            return False
        # Check the selected catalog before changing credentials, settings or
        # records. Layout options may change, but account/output must match.
        try:
            current_account, paths = self._history_location()
            selected_account = _normal_account(account)
            selected_dir = location.record_dir if isinstance(location, StoragePaths) else Path(location)
            same = (selected_account == current_account and
                    selected_dir.resolve() == paths.record_dir.resolve())
            if isinstance(location, StoragePaths):
                same = same and location.account == selected_account and location.output_dir == paths.output_dir
        except (ValueError, OSError):
            same = False
        if not same:
            dialogs.showinfo("기록 다시 확인", "아이디나 저장 폴더가 바뀌었습니다.\n라이브러리를 새로고침한 뒤 다시 골라 주세요.", parent=self.root)
            return False
        return self.start(targets=tuple(targets))

    def start(self, targets=()):
        if self.login_busy:
            self.status.set("로그인 연결이 끝난 뒤 저장을 시작해 주세요")
            return False
        if self._options_editing():
            return False
        if self.relocating:
            self._storage_action = "start"
            return False
        if self.running:
            return False
        if self._library_job_active():
            dialogs.showinfo("기록 화면 작업 중", "라이브러리 작업이 끝난 뒤 저장을 시작해 주세요.", parent=self.root)
            return False
        if AUTH_MODES[self.auth.get()] == "managed" and self.token.get():
            if not self.connect_token():
                return False
        if AUTH_MODES[self.auth.get()] == "managed" and not has_session():
            self.status.set("먼저 X 로그인을 연결해 주세요")
            self.status_detail.set("‘기본 브라우저 계정 연결’로 로그인한 계정을 연결하세요.")
            self.show_connect("저장하려면 먼저 X 로그인을 연결해야 합니다.")
            return False
        try:
            options = self._validated_options()
            if targets:
                options = replace(options, targets=tuple(targets)).validate()
        except (ValueError, OSError) as exc:
            dialogs.showerror("입력 확인", str(exc), parent=self.root)
            return False
        # After moving records to a changed folder only a normal save resumes
        # by itself; chosen files are picked again from the refreshed list.
        if not self._ensure_storage(None if targets else "start"):
            return False
        if not self._confirm_missing_media(options):
            return False
        options = replace(options, allow_recreate_media=getattr(self, "_allow_recreate_media", False))
        self.record_locations[options.account] = options.output_dir
        self.account.set(options.account)
        self.folder.set(options.output_dir)
        self.saved = self.skipped = self.size_skipped = self.review_skipped = self.review_never = 0
        self._review_context = resolve_storage(options.output_dir, options.account, options.separate_accounts)
        self.review_control.set_enabled(self.confirm_each.get())
        self.received_final = False
        self.final_status = None
        self.running = True
        self.stopping = False
        self._update_counts()
        if targets:
            self.status.set("사라진 파일을 다시 받고 있습니다…")
            self.status_detail.set("트윗마다 X 요청을 1번씩 10초 간격으로 보냅니다.")
        else:
            self.status.set("좋아요 목록을 확인하고 있습니다…")
            self.status_detail.set("목록은 10초에 한 번씩 천천히 요청합니다. 전체 Like 수와 완료율은 미리 알 수 없습니다.")
        self.progress_summary.set("")
        self.progress_details.set("")
        self.progress.configure(mode="indeterminate", value=0)
        self.progress.start(12)
        self._set_running(True)
        if targets:
            self._log(f"새 작업 시작 · 사라진 파일 다시 받기 {len(options.targets):,}개 · " + self.folder_mode.get())
        else:
            self._log("새 작업 시작 · " + ("시험 저장(새 파일 20개)" if options.photo_limit else self.scan_mode.get()) + " · " + self.folder_mode.get())
        try:
            self._save_settings(options)
        except OSError:
            self._log("설정을 기억하지 못했지만 다운로드는 계속 진행합니다.")
        threading.Thread(target=self._run_process, args=(options,), daemon=True).start()
        return True

    def _confirm_missing_media(self, options) -> bool:
        """A moved or disconnected media folder looks like a new, empty one.

        Saving there would quietly start a second, separate library, so ask.
        """
        # Approval applies only to this start. The worker checks again after
        # taking its locks, including when the folder disappears after here.
        self._allow_recreate_media = False
        try:
            paths = resolve_storage(options.output_dir, options.account, options.separate_accounts)
            if not saved_media_missing(paths):
                return True
        except (ValueError, OSError):
            return True   # the worker reports an unusable path itself
        message = missing_media_help(paths)
        anchor = Path(paths.media_dir.anchor) if paths.media_dir.anchor else None
        if anchor is not None and not anchor.exists():
            dialogs.showinfo("저장 폴더를 찾을 수 없음", message, parent=self.root)
        elif dialogs.askyesno("저장 폴더를 찾을 수 없음",
                              message + "\n\n이 위치에 새 폴더를 만들고 저장할까요?\n이미 받은 파일은 다시 받지 않습니다.",
                              parent=self.root, default="no"):
            self._allow_recreate_media = True
            return True
        self.status.set("저장 폴더를 찾을 수 없어 시작하지 않았습니다")
        self.status_detail.set("드라이브를 연결하거나 ‘저장 폴더’에서 새 위치를 골라 주세요.")
        return False

    def _set_running(self, value):
        if value:
            self._close_connect()
            self._close_options(save=False)
        self.start_button.configure(state="disabled" if value else "normal")
        self.stop_button.configure(state="normal" if value else "disabled")
        for widget in self.inputs:
            widget.configure(state="disabled" if value else "normal")
        if not value:
            self._auth_changed()
        self._update_schedule_buttons()
        if self.history_window is not None and self.history_window.winfo_exists():
            self.history_window._update_missing_actions()
            self.history_window._update_file_actions()

    def _run_process(self, options):
        return_code = 1
        try:
            with tempfile.TemporaryDirectory(prefix="luvbox-") as job_dir:
                directory = Path(job_dir)
                request, events, stop = (directory / name for name in ("request.json", "events.jsonl", "stop.flag"))
                request.write_text(json.dumps(asdict(options), ensure_ascii=False), encoding="utf-8")
                events.touch()
                self.review_control.attach(directory / CONTROL_NAME)
                self.stop_path = stop
                if self.stopping:
                    stop.touch()
                kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
                with subprocess.Popen(worker_command(request, events, stop), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs) as process:
                    self.process = process
                    # Read bytes and decode whole lines only. A text-mode read
                    # can end inside a Korean character the worker is still
                    # writing; its decode error used to stop every later update.
                    with events.open("rb") as stream:
                        partial = b""
                        stop_at = None
                        heartbeat_at = time.monotonic()
                        while True:
                            if time.monotonic() - heartbeat_at >= 1:
                                heartbeat_at = time.monotonic()
                                try:
                                    self.review_control.heartbeat()
                                except OSError:
                                    # The child waits for the next beat; only a
                                    # lasting failure stops its confirmation.
                                    pass
                            chunk = stream.read()
                            if chunk:
                                partial += chunk
                                while b"\n" in partial:
                                    line, partial = partial.split(b"\n", 1)
                                    try:
                                        self.events.put(json.loads(line))
                                    except ValueError:
                                        pass
                            if self.stopping:
                                if stop_at is None:
                                    stop.touch(exist_ok=True)
                                    stop_at = time.monotonic()
                                elif time.monotonic() - stop_at > 35 and process.poll() is None:
                                    process.terminate()
                            if process.poll() is not None:
                                # One final read after exit catches the final flush.
                                remaining = stream.read()
                                for line in (partial + remaining).splitlines():
                                    try:
                                        self.events.put(json.loads(line))
                                    except ValueError:
                                        pass
                                return_code = process.returncode
                                break
                            time.sleep(0.1)
                self.review_control.detach()
        except Exception as exc:
            self.events.put({"type": "log", "level": "error", "message": f"작업 프로세스를 실행하지 못했습니다 ({type(exc).__name__})."})
        finally:
            self.review_control.detach()
            self.process = None
            self.stop_path = None
            self.events.put({"type": "process_exit", "code": return_code})

    def stop(self):
        if not self.running or self.stopping or self.received_final:
            return
        self.stopping = True
        if self.stop_path:
            try:
                self.stop_path.touch(exist_ok=True)
            except OSError:
                pass
        self._destroy_review()
        self.status.set("중단하는 중입니다…")
        self.status_detail.set("지금 받는 파일을 정리하고 멈춥니다. 이미 받은 파일은 다음 실행에서 건너뜁니다.")
        self.stop_button.configure(state="disabled")
        self._log("중단 요청 · 이미 저장된 파일은 다음 실행에서 건너뜁니다.")
        if self.stop_path:
            try:
                self.stop_path.touch(exist_ok=True)
            except OSError:
                pass

    FINISH_HEADLINES = {"finished": "완료했습니다", "stopped": "중단했습니다", "error": "문제가 생겨 멈췄습니다"}

    def _poll(self):
        if self._ui_closed or self._polling:
            return
        # A direct refresh (also used by tests) replaces the scheduled poll.
        if self._poll_timer is not None:
            self.root.after_cancel(self._poll_timer)
            self._poll_timer = None
        self._polling = True
        self._poll_logs = []
        self._poll_counts_dirty = False
        try:
            # Reserve roughly 1ms of the 6ms slice for its batched UI writes.
            self._drain_events(time.perf_counter() + .005)
        finally:
            try:
                if not self._ui_closed:
                    if self._poll_counts_dirty:
                        self._update_counts()
                    self._write_logs(self._poll_logs)
                    backlog = self._deferred_event is not None or not self.events.empty()
                    self._poll_timer = self.root.after(8 if backlog else 120, self._poll)
            finally:
                self._poll_logs = None
                self._polling = False

    def _drain_events(self, deadline):
        progress_event = None
        while time.perf_counter() < deadline:
            try:
                if self._deferred_event is not None:
                    event, self._deferred_event = self._deferred_event, None
                else:
                    event = self.events.get_nowait()
            except queue.Empty:
                break
            kind = event.get("type")
            if kind == "progress":
                progress_event = event
                continue
            if progress_event is not None:
                self._update_progress(progress_event)
                progress_event = None
                if time.perf_counter() >= deadline:
                    self._deferred_event = event
                    break
            if kind == "browser-login":
                self._browser_result(event)
            elif kind == "browser-pairing":
                self._browser_pairing(event)
            elif kind == "storage-finished":
                self._finish_relocation(event)
                if self.closing:
                    return
            elif kind == "converting":
                self._update_conversion(event)
            elif kind == "review-request":
                try:
                    self._show_review(event)
                except (OSError, tk.TclError):
                    self._log("미리보기 창을 열지 못해 작업을 멈춥니다.")
                    self.stop()
            elif kind == "review-skipped":
                self.review_skipped += 1
                self._log("이번엔 건너뜀 · " + _file_label(event), event)
                self._poll_counts_dirty = True
            elif kind == "review-never":
                self.review_never += 1
                self._log("저장 안 함 · " + _file_label(event), event)
                self._poll_counts_dirty = True
            elif kind in ("saved", "skipped", "size-skipped"):
                if kind == "saved":
                    self.saved += 1
                    self._log("저장 · " + _file_label(event), event)
                elif kind == "skipped":
                    self.skipped += 1
                    self._log("이미 저장 · " + _file_label(event), event)
                else:
                    self.size_skipped += 1
                    self._log("용량 초과로 건너뜀 · " + _file_label(event), event)
                self._poll_counts_dirty = True
                self._schedule_history_refresh()
                if not self.stopping:
                    self.status.set("파일을 저장하고 있습니다…")
                    self.status_detail.set("이미 저장한 파일은 건너뛰고 새 파일만 받습니다.")
            elif kind == "schedule":
                self._schedule_result(event)
            elif kind == "log":
                message = str(event.get("message", ""))
                if event.get("filename"):
                    message = message.rstrip(".") + " · " + _file_label(event)
                self._log(message, event)
            elif kind == "finished":
                self.received_final = True
                self.final_status = event.get("status")
                self.stop_button.configure(state="disabled")
                self.saved = int(event.get("saved", self.saved))
                self.skipped = int(event.get("skipped", self.skipped))
                self.size_skipped = int(event.get("size_skipped", self.size_skipped))
                self.review_skipped = int(event.get("review_skipped", self.review_skipped))
                self.review_never = int(event.get("review_never", self.review_never))
                self._update_counts()
                self._poll_counts_dirty = False
                message = str(event.get("message", "작업이 종료되었습니다."))
                headline = self.FINISH_HEADLINES.get(self.final_status, message)
                if self.final_status == "finished":
                    headline += (f" · 다시 받은 파일 {self.saved:,}개" if event.get("scan_mode") == "retry"
                                 else f" · 새로 저장 {self.saved:,}개")
                self.status.set(headline)
                self.status_detail.set(message if headline != message else "")
                self._log(message)
                if event.get("metadata_failed"):
                    self._log(f"파일 안 출처·작성자 기록 실패 {int(event['metadata_failed']):,}개 · 파일은 그대로 있습니다. ‘기록 · 통계 → 파일 관리’에서 다시 시도하세요.")
                if event.get("retry_at"):
                    self._log("요청을 다시 시도할 수 있는 시각 (한국 시간) · " + str(event["retry_at"]))
            elif kind == "process_exit":
                self._destroy_review()
                self.running = False
                self._review_context = None
                self.limited.set(False)
                self.progress.stop()
                completed = self.received_final and self.final_status == "finished" and event.get("code", 0) == 0
                self.progress.configure(mode="determinate", value=100 if completed else 0)
                self._set_running(False)
                if not self.received_final:
                    self.status.set("중단되었습니다. 저장된 파일은 유지됩니다." if self.stopping else "작업이 종료되었습니다. 오류 로그와 입력을 확인해 주세요.")
                    self.status_detail.set("")
                    self._log(self.status.get())
                elif self.final_status == "finished" and event.get("code", 0) != 0:
                    self.status.set("작업 프로세스가 오류로 종료되었습니다. 저장된 파일과 로그를 확인해 주세요.")
                    self.status_detail.set("")
                    self._log(self.status.get())
                self._refresh_history()
                if self.closing:
                    self.root.destroy()
                    return
            if self._ui_closed:
                return
            if len(self._poll_logs) >= 64:
                self._write_logs(self._poll_logs)
                self._poll_logs.clear()
        if progress_event is not None:
            self._update_progress(progress_event)

    def _log(self, text, event=None):
        if event and any(event.get(key) for key in ("tweet_id", "author", "posted_at")):
            from .presentation import format_short
            author = str(event.get("author") or "").lstrip("@")
            author = "@" + author if re.fullmatch(r"[A-Za-z0-9_]{1,15}", author) else "작성자 미확인"
            posted = format_short(event.get("posted_at"))
            text = f"{text} · {author} · 게시 {posted if posted != '–' else '미확인'}"
        level = event.get("level", "") if event else ""
        entry = (time.strftime("%H:%M:%S") + "  ", text + "\n",
                 level if level in {"error", "warning"} else ())
        if self._poll_logs is not None:
            self._poll_logs.append(entry)
        else:
            self._write_logs([entry])

    def _write_logs(self, entries):
        if not entries:
            return
        following = self.log.yview()[1] >= .995
        self.log_placeholder.place_forget()
        self.log.configure(state="normal")
        chunks = []
        for stamp, message, level in entries:
            chunks.extend((stamp, "time", message, level))
        self.log.insert("end", *chunks)
        lines = int(self.log.index("end-1c").split(".")[0])
        if lines > 1000:
            removed = ((lines - 1000 + 199) // 200) * 200
            self.log.delete("1.0", f"{removed + 1}.0")
        if following:
            self.log.see("end")
        self.log.configure(state="disabled")

    def _library_job_active(self) -> bool:
        """A library job that writes records or files has not finished yet."""
        window = self.history_window
        return bool(window is not None and window.winfo_exists() and (
            window.metadata_active or window.delete_active or window.choice_active or window.repair_active))

    def close(self):
        if self.login_busy:
            self.closing = True
            self.login_cancel.set()
            self.status.set("로그인 창을 정리한 뒤 종료합니다…")
            self.root.after(150, self.close)
        elif self.relocating:
            self.closing = True
            self.status.set("기록 이전을 마친 뒤 종료합니다…")
        elif self.running:
            self.closing = True
            self.stop()
        elif self._library_job_active():
            self.closing = True
            if self.history_window.metadata_active or self.history_window.delete_active:
                self.history_window.stop_metadata()
            elif self.history_window.repair_active:
                self.status.set("기록 복구를 마친 뒤 종료합니다…")
            else:
                self.status.set("라이브러리 기록을 저장한 뒤 종료합니다…")
            self.root.after(150, self.close)
        else:
            self.root.destroy()


def launch():
    from .settings import AppDataConflict, AppDataError, set_aside_previous

    root = tk.Tk()
    root.withdraw()
    app = None

    def close():
        # Closing the unfinished main window during a dialog already destroyed it.
        try:
            root.destroy()
        except tk.TclError:
            pass

    try:
        try:
            try:
                app = App(root)
            except AppDataConflict as exc:
                choice = dialogs.Dialog(
                    root, "이전 데이터 폴더 정리", f"{exc}\n\n‘옮기고 계속’을 누르면 지금 데이터로 앱을 엽니다. "
                    "XlikeSaver 폴더는 지우지 않고 LuvBox 데이터 폴더의 legacy 안으로 옮겨 보관합니다.\n\n"
                    "예전 버전의 실행 파일·바로가기는 더 이상 쓰지 마세요.", icon="warning",
                    buttons=(("닫기", None), ("옮기고 계속", True)), default=1).run()
                if choice is not True:
                    close()
                    return
                set_aside_previous()
                app = App(root)
        except AppDataError as exc:
            dialogs.showerror("기존 데이터 이전 확인", str(exc), parent=root)
            close()
            return
        root.update_idletasks()
        root.deiconify()
        root.mainloop()
    finally:
        app = None
        root = None
        gc.collect()

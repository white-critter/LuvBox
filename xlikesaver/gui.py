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
from .core import QUICK_STOP_AFTER, JobOptions, _normal_account
from .login import clear_session, has_session, save_auth_token
from .settings import load_settings, settings_path
from .storage import StoragePaths, resolve_storage


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
    "gif": "어디서나 바로 움직이는 .gif 파일입니다. 색이 256개로 줄고 용량이 MP4보다 몇 배 커질 수 있습니다.",
    "mp4": "X가 주는 원본 MP4를 그대로 저장합니다. 용량이 작고 화질이 그대로입니다.",
}
GIF_SIZES = {"원본 크기": "original", "최대 720px · 초당 15장 (용량 절약)": "720",
             "최대 480px · 초당 12장 (가장 작게)": "480"}
SCAN_HINTS = {
    "quick": f"이미 저장한 파일이 {QUICK_STOP_AFTER}개 연속으로 나오면 멈춥니다. 평소에는 이것을 쓰세요.",
    "full": "오래된 Like까지 처음부터 다시 확인합니다. 시간이 오래 걸립니다.",
}
TOKEN_STEPS = ("1. 크롬이나 엣지에서 x.com에 로그인합니다.\n"
               "2. F12를 누르고 Application(애플리케이션) → Cookies → https://x.com 을 엽니다.\n"
               "3. auth_token 줄의 Value 값을 복사합니다.\n"
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
        root.title(f"XlikeSaver {__version__}")
        width = min(1440, max(760, root.winfo_screenwidth() - 80))
        height = min(1320, max(360, root.winfo_screenheight() - 100))
        x = max(0, (root.winfo_screenwidth() - width) // 2)
        # Leave room for the title bar and taskbar when opening a tall window.
        y = max(0, (root.winfo_screenheight() - height - 80) // 2)
        root.geometry(f"{width}x{height}+{x}+{y}")
        root.minsize(min(780, width), min(620, height))
        root.configure(bg=theme.BG)
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.events: queue.Queue = queue.Queue()
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
        self.history_window = None
        self.connect_window = None
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
        self.folder = tk.StringVar(value=str(settings.get("output_dir", Path.home() / "Pictures" / "XlikeSaver")))
        mode = settings.get("auth_mode", "managed")
        self.auth = tk.StringVar(value=next((k for k, v in AUTH_MODES.items() if v == mode), next(iter(AUTH_MODES))))
        self.cookies = tk.StringVar()
        self.token = tk.StringVar()
        layout = settings.get("folder_mode", "flat")
        self.folder_mode = tk.StringVar(value=next((k for k, v in FOLDER_MODES.items() if v == layout), "구분 없이 저장"))
        self.separate_accounts = tk.BooleanVar(value=settings.get("separate_accounts", False) is True)
        self.destination_preview = tk.StringVar()
        self.include_videos = tk.BooleanVar(value=settings.get("include_videos", True) is not False)
        self.include_gifs = tk.BooleanVar(value=settings.get("include_gifs", True) is not False)
        self.max_video_mb = tk.StringVar(value=str(settings.get("max_video_mb", 200)))
        gif_format = settings.get("gif_format", "gif")
        self.gif_format = tk.StringVar(value=next((k for k, v in GIF_FORMATS.items() if v == gif_format), next(iter(GIF_FORMATS))))
        gif_size = settings.get("gif_size", "original")
        self.gif_size = tk.StringVar(value=next((k for k, v in GIF_SIZES.items() if v == gif_size), next(iter(GIF_SIZES))))
        self.detailed_progress = tk.BooleanVar(value=settings.get("detailed_progress", False) is True)
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
                         self.max_video_mb, self.scan_mode, self.limited, self.gif_format, self.gif_size):
            variable.trace_add("write", lambda *_: self._update_option_summary())
        self._auth_changed()
        self._preview_location()
        self._update_option_summary()
        # Short screens can scroll without shrinking the readable type.
        root.minsize(min(780, width), min(620, height))
        self.root.after(120, self._poll)
        self.root.bind("<Destroy>", self._release_ui, add="+")
        # The installed app checks its daily task and the last scheduled run.
        # (Running from source, this happens when the schedule window opens.)
        if getattr(sys, "frozen", False):
            self.root.after(1500, self._startup_schedule_check)

    def _release_ui(self, event):
        if event.widget is self.root:
            theme.release_variables(self)
            self.inputs.clear()

    # ------------------------------------------------------------------ layout
    def _build(self):
        self.workspace = ttk.Frame(self.root)
        self.workspace.pack(fill="both", expand=True)
        self.workspace.columnconfigure(1, weight=1)
        self.workspace.rowconfigure(0, weight=1)

        self.sidebar = ttk.Frame(self.workspace, style="Sidebar.TFrame", padding=(22, 28))
        self.sidebar.grid(row=0, column=0, sticky="nsew")
        self.sidebar.columnconfigure(0, weight=1)
        self.sidebar.rowconfigure(5, weight=1)
        ttk.Label(self.sidebar, text="XlikeSaver", style="SidebarTitle.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(self.sidebar, text="나만의 미디어 보관함", style="SidebarMuted.TLabel").grid(row=1, column=0, sticky="w", pady=(6, 32))
        ttk.Label(self.sidebar, text="작업 공간", style="SidebarEyebrow.TLabel").grid(row=2, column=0, sticky="w", pady=(0, 10))
        navigation = ttk.Frame(self.sidebar, style="Sidebar.TFrame")
        navigation.grid(row=3, column=0, sticky="ew")
        for caption, command, style in (
                ("저장하기", self._focus_dashboard, "NavSelected.TButton"),
                ("기록 · 통계", self.show_history, "Nav.TButton"),
                ("저장 설정", self.show_options, "Nav.TButton"),
                ("자동 저장", self.show_schedule, "Nav.TButton")):
            ttk.Button(navigation, text=caption, command=command, style=style, width=16).pack(fill="x", pady=(0, 5))
        self.schedule_sidebar = ttk.Label(self.sidebar, textvariable=self.schedule_text,
                                           style="SidebarMuted.TLabel", wraplength=170, justify="left")
        self.schedule_sidebar.grid(row=4, column=0, sticky="new", pady=(18, 0))
        footer = ttk.Frame(self.sidebar, style="Sidebar.TFrame")
        footer.grid(row=6, column=0, sticky="sew", pady=(22, 0))
        self.theme_button = ttk.Button(footer, textvariable=self.theme_button_text, command=self.toggle_theme,
                                       style="Nav.TButton")
        self.theme_button.pack(fill="x")
        ttk.Button(footer, text="사용 방법", command=self.help, style="Nav.TButton").pack(fill="x", pady=(4, 18))
        ttk.Label(footer, text=f"VERSION {__version__}", style="SidebarEyebrow.TLabel").pack(anchor="w")
        ttk.Label(footer, text="Ctrl+Enter · 저장 시작", style="SidebarMuted.TLabel").pack(anchor="w", pady=(6, 0))

        self.content_view = theme.ScrollableContent(self.workspace, padding=(30, 24))
        self.content_view.grid(row=0, column=1, sticky="nsew")
        shell = self.content_view.content
        header = ttk.Frame(shell)
        header.pack(fill="x", pady=(0, 20))
        ttk.Label(header, text="저장하기", style="PageTitle.TLabel").pack(anchor="w")
        self._wrapping_label(header, text="좋아요한 사진과 영상을 내 폴더에 차곡차곡.",
                             style="Subtitle.TLabel", wraplength=700).pack(fill="x", pady=(5, 0))
        self.compact_navigation = ttk.Frame(header)
        for caption, command in (("기록 · 통계", self.show_history), ("자동 저장", self.show_schedule), ("사용 방법", self.help)):
            ttk.Button(self.compact_navigation, text=caption, command=command,
                       style="Ghost.TButton").pack(side="left", padx=(0, 6))
        ttk.Button(self.compact_navigation, textvariable=self.theme_button_text,
                   command=self.toggle_theme, style="Ghost.TButton").pack(side="right")

        form = self.setup_card = theme.card(shell, fill="x", pady=(0, 16))
        form.columnconfigure(0, weight=1, uniform="account")
        form.columnconfigure(1, weight=1, uniform="account")
        ttk.Label(form, text="저장 준비", style="CardTitle.TLabel").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 16))
        ttk.Label(form, text="내 X 아이디", style="CardMuted.TLabel").grid(row=1, column=0, sticky="w", pady=(0, 7))
        ttk.Label(form, text="로그인 연결", style="CardMuted.TLabel").grid(row=1, column=1, sticky="w", padx=(24, 0), pady=(0, 7))
        account = self.account_entry = ttk.Entry(form, textvariable=self.account, width=1, font=theme.BODY)
        account.grid(row=2, column=0, sticky="ew")
        self._wrapping_label(form, text="@아이디 또는 x.com/아이디", style="CardMuted.TLabel",
                             wraplength=470).grid(row=3, column=0, sticky="new", pady=(6, 0))
        self.inputs.append(account)
        self.login_label = self._wrapping_label(form, textvariable=self.login_text,
                                                style="Disconnected.TLabel", wraplength=400, anchor="w")
        self.login_label.grid(row=2, column=1, sticky="ew", padx=(24, 0))
        self.login_button = ttk.Button(form, text="로그인 연결", command=self.show_connect, style="Ghost.TButton")
        self.login_button.grid(row=3, column=1, sticky="w", padx=(24, 0), pady=(3, 0))
        self.inputs.append(self.login_button)
        ttk.Label(form, text="저장 폴더", style="CardMuted.TLabel").grid(row=4, column=0, columnspan=2, sticky="w", pady=(16, 7))
        folder_row = ttk.Frame(form, style="Plain.TFrame")
        folder_row.grid(row=5, column=0, columnspan=2, sticky="ew")
        folder_row.columnconfigure(0, weight=1)
        folder = self.folder_entry = ttk.Entry(folder_row, textvariable=self.folder, width=1, font=theme.BODY)
        folder.grid(row=0, column=0, sticky="ew")
        folder.bind("<Return>", lambda _: self._ensure_storage())
        browse = ttk.Button(folder_row, text="찾아보기", command=self.choose_folder, style="Ghost.TButton", width=8)
        browse.grid(row=0, column=1, padx=(8, 0))
        ttk.Button(folder_row, text="열기", command=self.open_folder, style="Ghost.TButton", width=5).grid(row=0, column=2, padx=(6, 0))
        self.inputs.extend((folder, browse))
        self.preview_label = self._wrapping_label(form, textvariable=self.destination_preview,
                                                  style="CardMuted.TLabel", wraplength=700)
        self.preview_label.grid(row=6, column=0, columnspan=2, sticky="ew", pady=(6, 14))
        summary = ttk.Frame(form, style="Plain.TFrame")
        summary.grid(row=7, column=0, columnspan=2, sticky="ew")
        summary.columnconfigure(0, weight=1)
        self.option_summary_label = self._wrapping_label(summary, textvariable=self.option_summary,
                                                         style="CardMuted.TLabel", wraplength=600)
        self.option_summary_label.grid(row=0, column=0, sticky="ew")
        self.options_button = ttk.Button(summary, text="저장 설정", command=self.show_options, style="Ghost.TButton")
        self.options_button.grid(row=0, column=1, sticky="e", padx=(16, 0))
        self.inputs.append(self.options_button)

        hero = self.work_card = theme.card(shell, fill="x", pady=(0, 16))
        head = ttk.Frame(hero, style="Plain.TFrame")
        head.pack(fill="x", pady=(0, 12))
        ttk.Label(head, text="이번 저장", style="CardEyebrow.TLabel").pack(side="left")
        self.work_actions = ttk.Frame(head, style="Plain.TFrame")
        self.work_actions.pack(side="right")
        self.start_button = ttk.Button(self.work_actions, text="저장 시작", style="Primary.TButton", command=self.start)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(self.work_actions, text="중단", style="Ghost.TButton", command=self.stop, state="disabled")
        self._wrapping_label(hero, textvariable=self.status, style="CardStatus.TLabel", wraplength=800).pack(fill="x")
        self._wrapping_label(hero, textvariable=self.status_detail, style="CardMuted.TLabel", wraplength=800).pack(fill="x", pady=(4, 16))
        tiles = ttk.Frame(hero, style="Plain.TFrame")
        tiles.pack(fill="x", pady=(0, 16))
        for column, (variable, caption) in enumerate(((self.saved_text, "새로 저장"), (self.skipped_text, "이미 저장된 파일"),
                                                       (self.size_text, "용량 초과"))):
            metric = ttk.Frame(tiles, style="Plain.TFrame")
            metric.grid(row=0, column=column, sticky="ew", padx=(0 if column == 0 else 20, 0))
            ttk.Label(metric, textvariable=variable, style="HeroNumber.TLabel").pack(anchor="w")
            ttk.Label(metric, text=caption, style="CardMuted.TLabel").pack(anchor="w", pady=(4, 0))
            tiles.columnconfigure(column, weight=1, uniform="metric")
        self.progress = theme.Progressbar(hero, mode="determinate", value=0)
        self.progress.pack(fill="x", pady=(0, 6))
        self.progress_heading = ttk.Frame(hero, style="Plain.TFrame")
        self.progress_heading.pack(fill="x")
        self._wrapping_label(self.progress_heading, textvariable=self.progress_summary,
                             style="CardMuted.TLabel", wraplength=700).pack(side="left", fill="x", expand=True)
        ttk.Checkbutton(self.progress_heading, text="자세히", variable=self.detailed_progress, command=self._toggle_progress,
                        style="Plain.TCheckbutton").pack(side="right", padx=(12, 0))
        self.progress_detail_label = self._wrapping_label(hero, textvariable=self.progress_details,
                                                         style="CardMuted.TLabel", wraplength=800)
        self.log_frame = theme.card(shell, fill="both", expand=True, pady=(0, 12))
        log_heading = ttk.Frame(self.log_frame, style="Plain.TFrame")
        log_heading.pack(fill="x", pady=(0, 8))
        self.log_title = ttk.Label(log_heading, text="최근 작업", style="CardBold.TLabel")
        self.log_title.pack(side="left")
        ttk.Button(log_heading, text="화면 비우기", command=self._clear_log,
                   style="Ghost.TButton").pack(side="right")
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
        self._wrapping_label(shell, text="X 요청 제한 안내가 나오면 기다린 뒤 다시 저장하세요. 계정 제한 위험을 완전히 없앨 수는 없습니다.",
                             style="Muted.TLabel", wraplength=800).pack(fill="x", pady=(0, 4))
        if self.detailed_progress.get():
            self._toggle_progress()
        self.root.bind("<Configure>", self._layout_dashboard, add="+")
        self.root.bind("<Control-Return>", lambda _: self.start())
        self.root.bind("<F6>", lambda _: self.show_history())
        self.root.bind("<Control-comma>", lambda _: self.show_options())
        self._layout_dashboard()

    def _layout_dashboard(self, event=None):
        if event is not None and event.widget is not self.root:
            return
        compact = self.root.winfo_width() < 1020
        if compact:
            self.sidebar.grid_remove()
            if not self.compact_navigation.winfo_manager():
                self.compact_navigation.pack(fill="x", pady=(14, 0))
        else:
            self.sidebar.grid()
            self.compact_navigation.pack_forget()

    def _focus_dashboard(self):
        self.root.lift()
        self.content_view.canvas.yview_moveto(0)
        self.account_entry.focus_set()

    def _clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        self.log_placeholder.place(relx=.5, rely=.5, anchor="center")

    def _wrapping_label(self, parent, **kwargs):
        label = ttk.Label(parent, justify="left", **kwargs)
        def resize(event):
            width = max(1, event.width)
            if int(label.cget("wraplength")) != width:
                label.configure(wraplength=width)
            view = getattr(label.winfo_toplevel(), "content_view", self.content_view)
            view._schedule_layout()
        label.bind("<Configure>", resize)
        return label

    def _toggle_progress(self):
        if self.detailed_progress.get():
            self.progress_detail_label.pack(after=self.progress_heading, fill="x", pady=(2, 0))
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
        self.option_summary.set(f"{description} · {scan}{test}")
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
            self.destination_preview.set(str(paths.media_dir / suffix) if suffix else str(paths.media_dir))
        except (ValueError, OSError):
            self.destination_preview.set("아이디와 저장 폴더를 확인하세요.")

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

    def _update_progress(self, event):
        from .history_gui import format_bytes, format_date, MEDIA_LABELS
        downloaded = max(0, int(event.get("downloaded") or 0))
        total = max(0, int(event.get("total") or 0))
        filename = str(event.get("filename") or "현재 파일")
        kind = MEDIA_LABELS.get(event.get("media_type"), "파일")
        if total:
            percent = min(100, downloaded * 100 / total)
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
        self.progress_details.set(f"속도 {format_bytes(event.get('speed') or 0)}/초 · 작성자 @{author} · 트윗 {event.get('tweet_id') or '미확인'}\n트윗 게시일 {format_date(event.get('posted_at'))} · 현재 파일의 진행률이며 전체 Like 목록의 완료율은 알 수 없습니다.")
        if not self.stopping:
            self.status.set("파일을 저장하고 있습니다…")

    CONVERSION_STAGES = {"started": ("GIF로 변환 중", "GIF로 변환하고 있습니다…"),
                         "finished": ("GIF 변환 완료", "GIF 파일을 마무리하고 있습니다…"),
                         "fallback": ("GIF 대신 원본 MP4로 저장", "원본 MP4로 저장하고 있습니다…")}

    def _update_conversion(self, event):
        from .history_gui import format_date
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
    def _auth_changed(self):
        mode = AUTH_MODES[self.auth.get()]
        if mode == "managed":
            connected = has_session()
            self.login_text.set("● 연결 정보 저장됨" if connected else "● 연결 필요")
        else:
            self.token.set("")
            connected = bool(self.cookies.get().strip())
            self.login_text.set("● cookies.txt 파일 사용" if connected else "● cookies.txt 파일을 골라 주세요")
        self.login_label.configure(style="Connected.TLabel" if connected else "Disconnected.TLabel")
        self.login_button.configure(text="연결 변경" if connected else "로그인 연결")
        window = self.connect_window
        if window is not None and window.winfo_exists():
            if mode == "managed":
                self.cookie_controls.pack_forget()
                self.token_controls.pack(fill="x")
                self.clear_button.configure(state="normal" if has_session() and not self.running else "disabled")
            else:
                self.token_controls.pack_forget()
                self.cookie_controls.pack(fill="x")

    def show_connect(self, notice=""):
        """Open the small login window (token or cookies.txt)."""
        window = self.connect_window
        if window is not None and window.winfo_exists():
            window.lift()
            window.focus_set()
            return
        window = self.connect_window = tk.Toplevel(self.root)
        window.title("X 로그인 연결")
        window.configure(bg=theme.CARD)
        window.transient(self.root)
        window.resizable(True, True)
        heading = ttk.Frame(window, style="Plain.TFrame", padding=(28, 24, 28, 8))
        heading.pack(fill="x")
        ttk.Label(heading, text="X 로그인 연결", style="CardTitle.TLabel").pack(anchor="w")
        self._wrapping_label(heading, text="로그인된 브라우저의 연결 정보를 이 PC에 저장합니다.",
                             style="CardMuted.TLabel", wraplength=640).pack(fill="x", pady=(6, 0))
        body = self._popup_content(window, padding=(28, 12, 28, 8))
        if notice:
            self._wrapping_label(body, text=notice, style="CardMuted.TLabel", foreground=theme.ORANGE, wraplength=640).pack(fill="x", pady=(6, 0))
        modes = ttk.Frame(body, style="Plain.TFrame")
        modes.pack(anchor="w", pady=(18, 16))
        for label in AUTH_MODES:
            ttk.Radiobutton(modes, text=label, value=label, variable=self.auth).pack(anchor="w", pady=(0, 4))

        self.token_controls = ttk.Frame(body, style="Plain.TFrame")
        self._wrapping_label(self.token_controls, text=TOKEN_STEPS, style="Card.TLabel", wraplength=640).pack(fill="x")
        row = ttk.Frame(self.token_controls, style="Plain.TFrame")
        row.pack(fill="x", pady=(16, 8))
        self.token_entry = ttk.Entry(row, textvariable=self.token, show="●", width=1, font=theme.BODY)
        self.token_entry.pack(side="left", fill="x", expand=True)
        self.token_entry.bind("<Return>", lambda _: self._connect_from_window())
        self.token_button = ttk.Button(row, text="연결 저장", width=11, command=self._connect_from_window)
        self.token_button.pack(side="left", padx=(8, 0))
        self._wrapping_label(self.token_controls, text="토큰은 이 PC의 Windows 계정에서만 열 수 있게 암호화해 저장합니다.\n비밀번호처럼 다루고 다른 사람에게 보내지 마세요.",
                             style="CardMuted.TLabel", wraplength=640).pack(fill="x")
        self.clear_button = ttk.Button(self.token_controls, text="저장된 연결 지우기", command=self.disconnect)
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
        ttk.Button(footer, text="쿠키 확인 방법", command=lambda: webbrowser.open("https://developer.chrome.com/docs/devtools/application/cookies/")).pack(side="left")
        ttk.Button(footer, text="닫기", width=9, command=self._close_connect).pack(side="right")
        window.protocol("WM_DELETE_WINDOW", self._close_connect)
        window.bind("<Escape>", lambda _: self._close_connect())
        self._auth_changed()
        self._fit_popup(window)
        (self.token_entry if AUTH_MODES[self.auth.get()] == "managed" else self.cookie_entry).focus_set()

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
        width = min(740, max(480, self.root.winfo_screenwidth() - 80))
        max_height = max(360, self.root.winfo_screenheight() - 120)
        window.geometry(f"{width}x{max_height}")
        window.update_idletasks()
        body = window.content_view.content
        extra = sum(child.winfo_reqheight() for child in window.winfo_children()
                    if child is not window.content_view and child.winfo_manager() == "pack")
        height = min(body.winfo_reqheight() + extra, max_height)
        window.minsize(min(600, width), min(360, height))
        window.geometry(f"{width}x{height}")

    def _close_connect(self):
        window, self.connect_window = self.connect_window, None
        self.token.set("")
        if window is not None and window.winfo_exists():
            window.destroy()

    def _connect_from_window(self):
        if self.connect_token():
            self._close_connect()

    def connect_token(self) -> bool:
        if self.running:
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
        self._auth_changed()
        self.status.set("로그인을 연결했습니다")
        self.status_detail.set("연결 저장됨 · 실제 유효성은 저장을 시작할 때 확인합니다.")
        self._log("인증 토큰을 이 Windows 계정에 암호화 저장했습니다. X 로그인 요청은 보내지 않았습니다.")
        return True

    def disconnect(self):
        if self.running:
            return
        try:
            clear_session()
        except OSError:
            dialogs.showerror("연결 지우기 실패", "로그인 정보를 지우지 못했습니다. 잠시 후 다시 시도해 주세요.", parent=self.root)
            return
        self.token.set("")
        self._auth_changed()
        self.status.set("저장된 로그인 연결을 지웠습니다")
        self.status_detail.set("다시 저장하려면 ‘로그인 연결’을 눌러 연결하세요.")

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
        window.title("저장 설정")
        window.configure(bg=theme.CARD)
        window.transient(self.root)
        window.resizable(True, True)
        self._option_snapshot = {name: getattr(self, name).get() for name in (
            "folder_mode", "separate_accounts", "include_videos", "include_gifs", "max_video_mb",
            "gif_format", "gif_size", "scan_mode", "limited")}
        heading = ttk.Frame(window, style="Plain.TFrame", padding=(28, 24, 28, 8))
        heading.pack(fill="x")
        ttk.Label(heading, text="저장 설정", style="CardTitle.TLabel").pack(anchor="w")
        self._wrapping_label(heading, text="변경한 설정은 다음 저장부터 적용됩니다.",
                             style="CardMuted.TLabel", wraplength=640).pack(fill="x", pady=(6, 0))
        footer = window.footer = ttk.Frame(window, style="Plain.TFrame", padding=(28, 14, 28, 20))
        footer.pack(side="bottom", fill="x")
        ttk.Button(footer, text="설정 저장", command=self._close_options,
                   style="DialogPrimary.TButton").pack(side="right")
        ttk.Button(footer, text="취소", command=lambda: self._close_options(save=False, restore=True),
                   style="Ghost.TButton").pack(side="right", padx=(0, 8))
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
        self._wrapping_label(advanced, text="직전 실행이 중단되거나 미디어 설정을 바꾸면 다음 한 번은 전체를 확인합니다.",
                             style="CardMuted.TLabel", wraplength=580).pack(fill="x")
        ttk.Separator(advanced).pack(fill="x", pady=(20, 18))
        ttk.Checkbutton(advanced, text="이번에는 20개만 시험 저장", variable=self.limited).pack(anchor="w")
        self._wrapping_label(advanced, text="새 파일 20개를 저장한 뒤 멈춥니다. 이번 실행에만 적용됩니다.",
                             style="CardMuted.TLabel", wraplength=560).pack(fill="x", padx=(56, 0), pady=(4, 0))
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
                     "gif_format": GIF_FORMATS[self.gif_format.get()], "gif_size": GIF_SIZES[self.gif_size.get()]})
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
                "이전 폴더를 찾을 수 없습니다.\n새 폴더에서 저장을 시작할까요?\n\n"
                "이전 기록을 가져오지 못해 중복 저장될 수 있습니다.\n"
                "기존 파일·기록은 보존됩니다. 이전 폴더를 다시 선택하면 기록을 합칠 수 있습니다.",
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
            "1. ‘내 X 아이디’에 본인 아이디를 입력합니다.",
            "2. ‘로그인 연결’을 눌러 X 로그인을 연결합니다. 처음 한 번만 하면 됩니다.",
            "3. 저장할 폴더를 고르고 ‘저장 시작’을 누릅니다.",
        ), "이미 저장한 파일은 다음부터 자동으로 건너뜁니다."),
        ("로그인 연결 방법", TOKEN_STEPS.replace("아래 칸에", "‘로그인 연결’ 창의 칸에").splitlines(),
         "토큰은 이 PC에 암호화해 저장합니다. 비밀번호처럼 다루세요. 토큰이 만료되면 같은 방법으로 다시 연결합니다. "
         "cookies.txt 파일로도 연결할 수 있습니다."),
        ("저장 설정", (
            f"새 Like만 빠르게: 이미 저장한 파일이 {QUICK_STOP_AFTER}개 연속으로 나오면 멈춥니다. 평소에는 이것을 쓰세요.",
            "전체 다시 확인: 오래된 Like까지 처음부터 확인합니다. 오래 걸립니다.",
            "직전 실행이 중단됐거나 옵션을 바꾸면 다음 한 번은 자동으로 전체를 확인합니다.",
        ), "폴더 정리 방식, 동영상·GIF 포함 여부, 영상 최대 크기는 ‘저장 설정’에서 바꿉니다."),
        ("저장되는 파일", (
            "사진은 원본 크기로, 동영상은 최고 화질의 MP4로 저장합니다. 움직이는 GIF는 GIF로 변환해 저장합니다.",
            "영상이 최대 크기(기본 200MB)보다 크면 건너뜁니다. 크기를 늘리면 다음에 받습니다.",
            "새로 받은 파일 안에는 트윗 주소와 작성자 아이디가 기록됩니다.",
        ), None),
        ("기록 · 통계", (
            "현재 파일 수와 과거 저장 기록·실행 기록을 봅니다.",
            "파일을 고르면 썸네일이 보입니다. ‘파일 열기’나 ‘트윗 열기’로 원본을 확인하세요.",
            "예전 파일에 출처·작성자를 넣으려면 ‘출처·작성자 기록’을 누르세요.",
        ), "이 작업은 파일을 다시 쓰므로 Google Drive·OneDrive 폴더라면 파일이 다시 올라갈 수 있습니다."),
        ("매일 자동 저장", (
            "메뉴에서 ‘자동 저장’을 열어 시각을 고르고 ‘예약 켜기’를 누릅니다.",
            "매일 그 시각에 저장된 계정과 설정으로 새 Like만 저장합니다.",
            "PC가 꺼져 있었으면 다음에 켰을 때 바로 실행하고, 배터리로 쓰는 중에도 실행합니다.",
            "실패하면 알림 창이 뜨고, 다음에 앱을 열 때도 알려 줍니다. 결과는 ‘실행 기록’에 남습니다.",
        ), "인증 토큰 연결에서만 동작하며 Windows에 로그인해 있을 때 실행됩니다. 새 버전을 처음 열면 예약을 그 버전으로 바꿀지 묻습니다."),
        ("주의할 점", (
            "X 공식 API가 아닌 방식이라 천천히 요청해도 계정이 제한될 수 있습니다.",
            "‘요청 한도’ 안내가 나오면 알려 준 시각까지 기다렸다가 다시 시작하세요.",
            "중단하거나 오류가 나도 이미 받은 파일은 그대로 남습니다.",
        ), None),
    )

    def help(self):
        window = self.help_window
        if window is not None and window.winfo_exists():
            window.lift()
            return
        popup = self.help_window = tk.Toplevel(self.root)
        popup.title("XlikeSaver 사용 방법")
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
        links.columnconfigure(0, weight=1)
        links.columnconfigure(1, weight=1)
        ttk.Button(links, text="자동 저장 예약", command=self.show_schedule).grid(row=0, column=0, sticky="ew", padx=(0, 8), pady=(0, 8))
        ttk.Button(links, text="자동 저장 실행 기록", command=self.open_schedule_logs).grid(row=0, column=1, sticky="ew", pady=(0, 8))
        ttk.Button(links, text="X 자동화 정책", command=lambda: webbrowser.open("https://help.x.com/en/rules-and-policies/x-automation")).grid(row=1, column=0, sticky="w")
        ttk.Button(links, text="닫기", width=9, command=popup.destroy).grid(row=1, column=1, sticky="e")
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
        buttons.pack(fill="x", pady=(16, 0))
        self.schedule_on_button = ttk.Button(buttons, text="예약 켜기", style="DialogPrimary.TButton", command=self.register_schedule)
        self.schedule_on_button.pack(side="left")
        self.schedule_off_button = ttk.Button(buttons, text="예약 끄기", command=self.unregister_schedule)
        self.schedule_off_button.pack(side="left", padx=8)
        ttk.Button(buttons, text="실행 기록", command=self.open_schedule_logs).pack(side="left")
        self._wrapping_label(body, text="PC가 꺼져 있거나 잠자기 상태였으면 다음에 켰을 때 바로 실행하고, 배터리로 쓰는 중에도 실행합니다. "
                                        "Windows에 로그인해 있을 때만 실행됩니다. 실패하면 알림 창이 뜹니다. "
                                        "Windows 작업 스케줄러에 ‘XlikeSaver 자동 저장’으로 등록됩니다.",
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
        if self.schedule_busy or self.running:
            return
        if self.relocating:
            self._storage_action = "schedule"
            return
        if AUTH_MODES[self.auth.get()] != "managed":
            dialogs.showinfo("자동 저장", "자동 저장은 ‘인증 토큰’ 연결에서만 사용할 수 있습니다. 로그인 연결 방식을 바꿔 주세요.",
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
        from .history_gui import format_short
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
                state = (f"매일 {when}에 다른 실행 파일({Path(info.get('program') or '').name or '알 수 없음'})로 등록돼 있습니다. "
                         "‘예약 갱신’을 누르면 이 버전으로 바꿉니다.")
                summary = f"매일 {when} · 다른 실행 파일로 등록됨 · ‘자동 저장’에서 갱신하세요."
            elif not (info.get("catch_up") and info.get("on_battery")):
                state = (f"매일 {when}에 켜져 있지만, 예전 방식으로 등록돼 PC가 꺼져 있었거나 배터리일 때는 건너뜁니다. "
                         "‘예약 갱신’을 누르면 고쳐집니다.")
                summary = f"매일 {when} · 켜짐 · 놓친 실행을 건너뛰는 예전 설정입니다. ‘자동 저장’에서 갱신하세요."
            elif not info.get("enabled"):
                state = f"매일 {when}로 등록돼 있지만 Windows 작업 스케줄러에서 꺼져 있습니다. ‘예약 갱신’을 누르면 다시 켜집니다."
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
            from .history_gui import format_short
            self.status.set("지난 자동 저장이 실패했습니다")
            self.status_detail.set(f"{format_short(last.get('finished_at'))} · {last.get('message') or '실행 기록을 확인해 주세요.'}")
            self._log("지난 자동 저장 실패 · " + str(last.get("message") or "실행 기록을 확인해 주세요."))
            try:
                mark_last_run_seen()
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
        return JobOptions(account=self.account.get(), output_dir=self.folder.get(), auth_mode=AUTH_MODES[self.auth.get()],
                          cookies_file=self.cookies.get(), photo_limit=0 if scheduled else 20 if self.limited.get() else 0,
                          folder_mode=FOLDER_MODES[self.folder_mode.get()],
                          scan_mode="quick" if scheduled else SCAN_MODES[self.scan_mode.get()],
                          include_videos=self.include_videos.get(), include_gifs=self.include_gifs.get(),
                          max_video_mb=self._video_limit(), separate_accounts=self.separate_accounts.get(),
                          gif_format=GIF_FORMATS[self.gif_format.get()], gif_size=GIF_SIZES[self.gif_size.get()]).validate()

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
            self.status_detail.set("열린 창의 안내대로 auth_token을 붙여 넣으면 됩니다.")
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
        self.record_locations[options.account] = options.output_dir
        self.account.set(options.account)
        self.folder.set(options.output_dir)
        self.saved = self.skipped = self.size_skipped = 0
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

    def _set_running(self, value):
        if value:
            self._close_connect()
            self._close_options(save=False)
        self.start_button.configure(state="disabled" if value else "normal")
        self.stop_button.configure(state="normal" if value else "disabled")
        if value and self.running:
            self.stop_button.pack(side="left", padx=(8, 0))
        else:
            self.stop_button.pack_forget()
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
            with tempfile.TemporaryDirectory(prefix="xlikesaver-") as job_dir:
                directory = Path(job_dir)
                request, events, stop = (directory / name for name in ("request.json", "events.jsonl", "stop.flag"))
                request.write_text(json.dumps(asdict(options), ensure_ascii=False), encoding="utf-8")
                events.touch()
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
                        while True:
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
        except Exception as exc:
            self.events.put({"type": "log", "level": "error", "message": f"작업 프로세스를 실행하지 못했습니다 ({type(exc).__name__})."})
        finally:
            self.process = None
            self.stop_path = None
            self.events.put({"type": "process_exit", "code": return_code})

    def stop(self):
        if not self.running or self.stopping or self.received_final:
            return
        self.stopping = True
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
        # Bound each tick so a large backlog never monopolizes the GUI thread.
        for _ in range(100):
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            kind = event.get("type")
            if kind == "storage-finished":
                self._finish_relocation(event)
                if self.closing:
                    return
            elif kind == "progress":
                self._update_progress(event)
            elif kind == "converting":
                self._update_conversion(event)
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
                self._update_counts()
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
                self._update_counts()
                message = str(event.get("message", "작업이 종료되었습니다."))
                headline = self.FINISH_HEADLINES.get(self.final_status, message)
                if self.final_status == "finished":
                    headline += (f" · 다시 받은 파일 {self.saved:,}개" if event.get("scan_mode") == "retry"
                                 else f" · 새로 저장 {self.saved:,}개")
                self.status.set(headline)
                self.status_detail.set(message if headline != message else "")
                self._log(message)
                if event.get("metadata_failed"):
                    self._log(f"파일 내부 출처·작성자 기록 실패 {int(event['metadata_failed']):,}개 · 미디어는 보존했습니다. ‘기록 · 통계 → 파일 관리’에서 다시 시도할 수 있습니다.")
                if event.get("retry_at"):
                    self._log("요청을 다시 시도할 수 있는 시각 (한국 시간) · " + str(event["retry_at"]))
            elif kind == "process_exit":
                self.running = False
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
        self.root.after(120, self._poll)

    def _log(self, text, event=None):
        if event and any(event.get(key) for key in ("tweet_id", "author", "posted_at")):
            from .history_gui import format_short
            author = str(event.get("author") or "").lstrip("@")
            author = "@" + author if re.fullmatch(r"[A-Za-z0-9_]{1,15}", author) else "작성자 미확인"
            posted = format_short(event.get("posted_at"))
            text = f"{text} · {author} · 게시 {posted if posted != '–' else '미확인'}"
        following = self.log.yview()[1] >= .995
        self.log_placeholder.place_forget()
        self.log.configure(state="normal")
        self.log.insert("end", time.strftime("%H:%M:%S") + "  ", "time")
        level = event.get("level", "") if event else ""
        self.log.insert("end", text + "\n", level if level in {"error", "warning"} else ())
        if int(self.log.index("end-1c").split(".")[0]) > 1000:
            self.log.delete("1.0", "201.0")
        if following:
            self.log.see("end")
        self.log.configure(state="disabled")

    def _library_job_active(self) -> bool:
        """A library job that writes records or files has not finished yet."""
        window = self.history_window
        return bool(window is not None and window.winfo_exists() and (
            window.metadata_active or window.choice_active or window.delete_active or window.repair_active))

    def close(self):
        if self.relocating:
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
    root = tk.Tk()
    app = App(root)
    try:
        root.mainloop()
    finally:
        app = None
        root = None
        gc.collect()

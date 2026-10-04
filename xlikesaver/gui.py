"""Main window: account, folder, one Start button, and plain progress.

Login and the less common save options open in their own small windows so the
main screen stays short. Download work runs in a separate worker process.
"""

from dataclasses import asdict
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import webbrowser

from . import __version__, theme
from .core import QUICK_STOP_AFTER, JobOptions, _normal_account
from .login import clear_session, has_session, save_auth_token
from .settings import load_settings, settings_path
from .storage import resolve_storage
from .theme import BG, CARD


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
        width = min(960, max(760, root.winfo_screenwidth() - 60))
        height = min(880, max(680, root.winfo_screenheight() - 80))
        root.geometry(f"{width}x{height}")
        root.minsize(min(780, width), min(760, height))
        root.configure(bg=BG)
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
        self.history_refresh_pending = False
        self.received_final = False
        self.final_status = None
        self.inputs: list[tk.Widget] = []
        settings = load_settings()
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
        self.detailed_progress = tk.BooleanVar(value=settings.get("detailed_progress", False) is True)
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
        theme.apply_style(self.root)
        self._build()
        self._update_counts()
        self.auth.trace_add("write", lambda *_: self._auth_changed())
        self.cookies.trace_add("write", lambda *_: self._auth_changed())
        for variable in (self.account, self.folder, self.folder_mode, self.separate_accounts):
            variable.trace_add("write", lambda *_: self._preview_location())
        for variable in (self.folder_mode, self.separate_accounts, self.include_videos, self.include_gifs,
                         self.max_video_mb, self.scan_mode, self.limited):
            variable.trace_add("write", lambda *_: self._update_option_summary())
        self._auth_changed()
        self._preview_location()
        self._update_option_summary()
        # Short screens can scroll without shrinking the readable type.
        root.minsize(min(780, width), min(760, root.winfo_screenheight() - 80))
        self.root.after(120, self._poll)

    # ------------------------------------------------------------------ layout
    def _build(self):
        self.content_view = theme.ScrollableContent(self.root)
        self.content_view.pack(fill="both", expand=True)
        shell = self.content_view.content
        header = ttk.Frame(shell)
        header.pack(fill="x")
        titles = ttk.Frame(header)
        titles.pack(side="left")
        ttk.Label(titles, text="XlikeSaver", style="Title.TLabel").pack(anchor="w")
        ttk.Label(titles, text="내가 좋아요한 사진·동영상을 PC에 저장합니다", style="Subtitle.TLabel").pack(anchor="w")
        ttk.Button(header, text="사용 방법", command=self.help).pack(side="right", anchor="n")
        ttk.Button(header, text="기록 · 통계", command=self.show_history).pack(side="right", anchor="n", padx=8)

        form = theme.card(shell, fill="x", pady=(14, 0))
        form.columnconfigure(1, weight=1)
        self._label(form, "내 X 아이디", 0)
        account = ttk.Entry(form, textvariable=self.account, font=theme.BODY)
        account.grid(row=0, column=1, sticky="ew", pady=(4, 0))
        self._wrapping_label(form, text="예: my_account 또는 x.com/my_account", style="CardMuted.TLabel", wraplength=470).grid(row=1, column=1, sticky="ew", pady=(1, 4))
        self.inputs.append(account)

        self._label(form, "로그인", 2)
        self.login_label = self._wrapping_label(form, textvariable=self.login_text, style="Disconnected.TLabel", wraplength=470)
        self.login_label.grid(row=2, column=1, sticky="ew", pady=4)
        self.login_button = ttk.Button(form, text="로그인 연결", command=self.show_connect)
        self.login_button.grid(row=2, column=2, sticky="e", padx=(12, 0), pady=4)
        self.inputs.append(self.login_button)

        self._label(form, "저장 폴더", 3)
        folder = ttk.Entry(form, textvariable=self.folder, font=theme.BODY)
        folder.grid(row=3, column=1, sticky="ew", pady=(10, 0))
        folder.bind("<Return>", lambda _: self._ensure_storage())
        folder_buttons = ttk.Frame(form, style="Plain.TFrame")
        folder_buttons.grid(row=3, column=2, sticky="e", padx=(12, 0), pady=(10, 0))
        browse = ttk.Button(folder_buttons, text="찾아보기", command=self.choose_folder)
        browse.pack(side="left")
        ttk.Button(folder_buttons, text="열기", command=self.open_folder).pack(side="left", padx=(6, 0))
        self.inputs.extend((folder, browse))
        preview = ttk.Frame(form, style="Plain.TFrame")
        preview.grid(row=4, column=1, columnspan=2, sticky="ew", pady=(1, 2))
        ttk.Label(preview, text="파일 저장 위치:", style="CardMuted.TLabel").pack(side="left")
        self.preview_label = self._wrapping_label(preview, textvariable=self.destination_preview, style="CardMuted.TLabel", wraplength=600)
        self.preview_label.pack(side="left", fill="x", expand=True, padx=(6, 0))

        ttk.Separator(form).grid(row=5, column=0, columnspan=3, sticky="ew", pady=6)
        self._label(form, "저장 방식", 6)
        self.option_summary_label = self._wrapping_label(form, textvariable=self.option_summary, style="Card.TLabel", wraplength=470)
        self.option_summary_label.grid(row=6, column=1, sticky="ew", pady=4)
        self.options_button = ttk.Button(form, text="옵션 변경", command=self.show_options)
        self.options_button.grid(row=6, column=2, sticky="e", padx=(12, 0), pady=4)
        self.inputs.append(self.options_button)

        actions = ttk.Frame(shell)
        actions.pack(fill="x", pady=(12, 8))
        self.start_button = ttk.Button(actions, text="저장 시작", style="Primary.TButton", command=self.start)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(actions, text="중단", style="Big.TButton", command=self.stop, state="disabled")
        self.stop_button.pack(side="left", padx=10)

        self._wrapping_label(shell, text="X 공식 API가 아닌 방식이라 계정이 제한될 수 있습니다. 너무 자주 반복 실행하지 마세요.",
                             style="Warn.TLabel", wraplength=880).pack(side="bottom", fill="x", pady=(8, 0))
        self._wrapping_label(shell, textvariable=self.status, style="Status.TLabel", wraplength=880).pack(fill="x")
        self._wrapping_label(shell, textvariable=self.status_detail, style="Muted.TLabel", wraplength=880).pack(fill="x", pady=(2, 8))
        tiles = ttk.Frame(shell)
        tiles.pack(fill="x")
        for column, (variable, caption) in enumerate(((self.saved_text, "이번에 새로 저장"), (self.skipped_text, "이미 저장돼 있음"),
                                                       (self.size_text, "용량 초과로 건너뜀"))):
            theme.tile(tiles, variable, caption).grid(row=0, column=column, sticky="ew", padx=(0 if column == 0 else 10, 0))
            tiles.columnconfigure(column, weight=1, uniform="tile")
        self.progress = ttk.Progressbar(shell, mode="determinate", value=0)
        self.progress.pack(fill="x", pady=(10, 4))
        self.progress_heading = ttk.Frame(shell)
        self.progress_heading.pack(fill="x")
        self._wrapping_label(self.progress_heading, textvariable=self.progress_summary, style="Muted.TLabel", wraplength=720).pack(side="left", fill="x", expand=True)
        ttk.Checkbutton(self.progress_heading, text="자세히", variable=self.detailed_progress, command=self._toggle_progress,
                        style="Plain.TCheckbutton").pack(side="right")
        self.progress_detail_label = self._wrapping_label(shell, textvariable=self.progress_details, style="Muted.TLabel", wraplength=880)
        self.log_title = ttk.Label(shell, text="작업 기록", style="Muted.TLabel")
        self.log_title.pack(anchor="w", pady=(6, 2))
        self.log_frame = ttk.Frame(shell, style="Card.TFrame", padding=(10, 8))
        self.log_frame.pack(fill="both", expand=True)
        self.log = tk.Text(self.log_frame, height=3, wrap="word", bg=CARD, fg=theme.INK, relief="flat", borderwidth=0,
                           highlightthickness=0, padx=4, pady=4, state="disabled", font=theme.SMALL)
        scroll = ttk.Scrollbar(self.log_frame, command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        self.log.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        if self.detailed_progress.get():
            self._toggle_progress()

    def _wrapping_label(self, parent, **kwargs):
        label = ttk.Label(parent, justify="left", **kwargs)
        def resize(event):
            width = max(1, event.width)
            if int(label.cget("wraplength")) != width:
                label.configure(wraplength=width)
            self.content_view._schedule_layout()
        label.bind("<Configure>", resize)
        return label

    @staticmethod
    def _label(parent, text, row):
        ttk.Label(parent, text=text, style="CardBold.TLabel").grid(row=row, column=0, sticky="w", padx=(0, 18),
                                                                   pady=(10 if row == 3 else 4, 0))

    def _toggle_progress(self):
        if self.detailed_progress.get():
            self.progress_detail_label.pack(after=self.progress_heading, fill="x", pady=(2, 0))
        else:
            self.progress_detail_label.pack_forget()
        self.content_view._schedule_layout()

    def _update_option_summary(self):
        layout = {"flat": "한 폴더", "author": "작성자별 폴더", "date": "날짜별 폴더"}[FOLDER_MODES[self.folder_mode.get()]]
        if self.separate_accounts.get():
            layout += "(아이디 폴더 안)"
        media = ["사진"] + (["동영상"] if self.include_videos.get() else []) + (["GIF"] if self.include_gifs.get() else [])
        size = self.max_video_mb.get().strip()
        limit = "" if len(media) == 1 else (" (영상 크기 제한 없음)" if size == "0" else f" (영상 {size}MB까지)")
        scan = "새 Like만" if SCAN_MODES[self.scan_mode.get()] == "quick" else "전체 확인"
        test = " · 시험 20개" if self.limited.get() else ""
        self.option_summary.set(f"{layout} · {'·'.join(media)}{limit} · {scan}{test}")

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
            self.history_window = HistoryWindow(self.root, self._history_location, lambda: self.running)
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
            self.root.after(2000, self._refresh_history)

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

    # ------------------------------------------------------------------- login
    def _auth_changed(self):
        mode = AUTH_MODES[self.auth.get()]
        if mode == "managed":
            connected = has_session()
            self.login_text.set("● 연결 정보 저장됨" if connected else "● 연결 필요 — 오른쪽 버튼을 눌러 연결하세요")
        else:
            self.token.set("")
            connected = bool(self.cookies.get().strip())
            self.login_text.set("● cookies.txt 파일 사용" if connected else "● cookies.txt 파일을 골라 주세요")
        self.login_label.configure(style="Connected.TLabel" if connected else "Disconnected.TLabel")
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
        window.configure(bg=CARD)
        window.transient(self.root)
        window.resizable(False, False)
        body = self._popup_content(window, padding=(26, 22))
        ttk.Label(body, text="X 로그인 연결", style="CardHeading.TLabel").pack(anchor="w")
        if notice:
            ttk.Label(body, text=notice, style="CardMuted.TLabel", foreground=theme.ORANGE, wraplength=600).pack(anchor="w", pady=(4, 0))
        modes = ttk.Frame(body, style="Plain.TFrame")
        modes.pack(anchor="w", pady=(12, 10))
        for label in AUTH_MODES:
            ttk.Radiobutton(modes, text=label, value=label, variable=self.auth).pack(side="left", padx=(0, 18))

        self.token_controls = ttk.Frame(body, style="Plain.TFrame")
        ttk.Label(self.token_controls, text=TOKEN_STEPS, style="Card.TLabel", justify="left").pack(anchor="w")
        row = ttk.Frame(self.token_controls, style="Plain.TFrame")
        row.pack(fill="x", pady=(12, 6))
        self.token_entry = ttk.Entry(row, textvariable=self.token, show="●", width=40, font=theme.BODY)
        self.token_entry.pack(side="left", fill="x", expand=True)
        self.token_entry.bind("<Return>", lambda _: self._connect_from_window())
        self.token_button = ttk.Button(row, text="연결 저장", command=self._connect_from_window)
        self.token_button.pack(side="left", padx=(8, 0))
        ttk.Label(self.token_controls, text="토큰은 이 PC의 Windows 계정에서만 열 수 있게 암호화해 저장합니다.\n비밀번호처럼 다루고 다른 사람에게 보내지 마세요.",
                  style="CardMuted.TLabel", justify="left").pack(anchor="w")
        self.clear_button = ttk.Button(self.token_controls, text="저장된 연결 지우기", command=self.disconnect)
        self.clear_button.pack(anchor="w", pady=(10, 0))

        self.cookie_controls = ttk.Frame(body, style="Plain.TFrame")
        ttk.Label(self.cookie_controls, text="로그인된 x.com 쿠키를 Netscape 형식(cookies.txt)으로 내보낸 파일을 고릅니다.",
                  style="Card.TLabel", wraplength=600, justify="left").pack(anchor="w")
        row = ttk.Frame(self.cookie_controls, style="Plain.TFrame")
        row.pack(fill="x", pady=(12, 6))
        self.cookie_entry = ttk.Entry(row, textvariable=self.cookies, width=40, font=theme.BODY)
        self.cookie_entry.pack(side="left", fill="x", expand=True)
        self.cookie_button = ttk.Button(row, text="파일 선택", command=self.choose_cookies)
        self.cookie_button.pack(side="left", padx=(8, 0))
        ttk.Label(self.cookie_controls, text="파일 위치는 기억하지 않아 앱을 다시 열면 다시 골라야 합니다. 예약 실행에는 쓸 수 없습니다.",
                  style="CardMuted.TLabel", wraplength=600, justify="left").pack(anchor="w")

        footer = ttk.Frame(body, style="Plain.TFrame")
        footer.pack(side="bottom", fill="x", pady=(18, 0))
        ttk.Button(footer, text="크롬 쿠키 화면 안내", command=lambda: webbrowser.open("https://developer.chrome.com/docs/devtools/application/cookies/")).pack(side="left")
        ttk.Button(footer, text="닫기", command=self._close_connect).pack(side="right")
        window.protocol("WM_DELETE_WINDOW", self._close_connect)
        self._auth_changed()
        self._fit_popup(window)
        self.token_entry.focus_set()

    @staticmethod
    def _popup_content(window, *, padding):
        view = window.content_view = theme.ScrollableContent(window, padding=padding)
        view.configure(style="Plain.TFrame")
        view.canvas.configure(background=CARD)
        view.content.configure(style="Plain.TFrame")
        view.pack(fill="both", expand=True)
        return view.content

    def _fit_popup(self, window):
        window.update_idletasks()
        body = window.content_view.content
        width = min(body.winfo_reqwidth(), self.root.winfo_screenwidth() - 80)
        height = min(body.winfo_reqheight(), self.root.winfo_screenheight() - 120)
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
            messagebox.showerror("연결 저장 실패", str(exc), parent=self.connect_window or self.root)
            return False
        except OSError:
            messagebox.showerror("연결 저장 실패", "연결 정보를 저장하지 못했습니다. Windows 계정과 저장 권한을 확인해 주세요.", parent=self.connect_window or self.root)
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
            messagebox.showerror("연결 지우기 실패", "로그인 정보를 지우지 못했습니다. 잠시 후 다시 시도해 주세요.", parent=self.root)
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
        """Open the save options; choices apply as soon as they are changed."""
        window = self.options_window
        if window is not None and window.winfo_exists():
            window.lift()
            return
        window = self.options_window = tk.Toplevel(self.root)
        window.title("저장 옵션")
        window.configure(bg=CARD)
        window.transient(self.root)
        window.resizable(False, False)
        body = self._popup_content(window, padding=(26, 20))

        def heading(text, first=False):
            ttk.Label(body, text=text, style="CardHeading.TLabel").pack(anchor="w", pady=(0 if first else 18, 6))

        heading("폴더 정리", first=True)
        for key, (label, example) in FOLDER_CHOICES.items():
            row = ttk.Frame(body, style="Plain.TFrame")
            row.pack(fill="x")
            ttk.Radiobutton(row, text=label, value=key, variable=self.folder_mode).pack(side="left")
            ttk.Label(row, text="예) " + example, style="CardMuted.TLabel").pack(side="right", padx=(24, 0))
        ttk.Checkbutton(body, text="내 아이디 이름의 폴더 안에 저장 (여러 계정을 쓸 때)", variable=self.separate_accounts).pack(anchor="w", pady=(6, 0))

        heading("저장할 미디어")
        ttk.Label(body, text="사진은 항상 저장합니다.", style="CardMuted.TLabel").pack(anchor="w")
        media = ttk.Frame(body, style="Plain.TFrame")
        media.pack(anchor="w", pady=(4, 0))
        ttk.Checkbutton(media, text="동영상", variable=self.include_videos).pack(side="left")
        ttk.Checkbutton(media, text="움직이는 GIF", variable=self.include_gifs).pack(side="left", padx=(18, 0))
        size = ttk.Frame(body, style="Plain.TFrame")
        size.pack(anchor="w", pady=(8, 0))
        ttk.Label(size, text="영상 1개 최대", style="Card.TLabel").pack(side="left")
        ttk.Spinbox(size, from_=0, to=100000, increment=50, width=7, textvariable=self.max_video_mb, font=theme.BODY).pack(side="left", padx=8)
        ttk.Label(size, text="MB   (0 = 제한 없음, 넘는 영상은 건너뜀)", style="CardMuted.TLabel").pack(side="left")

        heading("확인 범위")
        for key, value in SCAN_MODES.items():
            ttk.Radiobutton(body, text=key, value=key, variable=self.scan_mode).pack(anchor="w")
            ttk.Label(body, text=SCAN_HINTS[value], style="CardMuted.TLabel").pack(anchor="w", padx=(28, 0), pady=(0, 4))
        ttk.Label(body, text="직전 실행이 중단됐거나 미디어 옵션을 바꾸면, 다음 한 번은 자동으로 전체를 확인합니다.",
                  style="CardMuted.TLabel", wraplength=620, justify="left").pack(anchor="w")

        heading("시험")
        ttk.Checkbutton(body, text="시험 저장 — 새 파일 20개만 받고 멈춤 (이번 실행에만 적용)", variable=self.limited).pack(anchor="w")
        ttk.Button(body, text="닫기", command=self._close_options).pack(anchor="e", pady=(20, 0))
        window.protocol("WM_DELETE_WINDOW", self._close_options)
        self._fit_popup(window)

    def _close_options(self):
        window, self.options_window = self.options_window, None
        if window is not None and window.winfo_exists():
            window.destroy()

    # ------------------------------------------------------------------ folder
    def choose_folder(self):
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
                "detailed_progress": self.detailed_progress.get(),
                "record_locations": self.record_locations}
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
        window = self.history_window
        if window is not None and window.winfo_exists() and window.metadata_active:
            self.folder.set(str(source.output_dir))
            messagebox.showinfo("메타데이터 기록 중", "출처·작성자 기록이 끝난 뒤 저장 폴더를 바꿔 주세요.", parent=self.root)
            return False
        fresh = not source.output_dir.is_dir()
        if fresh and not messagebox.askyesno(
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
                messagebox.showerror("기록 이전 실패", detail, parent=self.root)
        if self.closing:
            self.root.destroy()
        elif success and action == "start":
            self.start()
        elif success and action == "history":
            self.show_history()

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
            messagebox.showerror("폴더 열기 실패", "저장 위치와 폴더 권한을 확인해 주세요.", parent=self.root)

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
        ("저장 방식", (
            f"새 Like만 빠르게: 이미 저장한 파일이 {QUICK_STOP_AFTER}개 연속으로 나오면 멈춥니다. 평소에는 이것을 쓰세요.",
            "전체 다시 확인: 오래된 Like까지 처음부터 확인합니다. 오래 걸립니다.",
            "직전 실행이 중단됐거나 옵션을 바꾸면 다음 한 번은 자동으로 전체를 확인합니다.",
        ), "폴더 정리 방식, 동영상·GIF 포함 여부, 영상 최대 크기는 ‘옵션 변경’에서 바꿉니다."),
        ("저장되는 파일", (
            "사진은 원본 크기로, 동영상과 움직이는 GIF는 가장 좋은 화질의 MP4로 저장합니다.",
            "영상이 최대 크기(기본 200MB)보다 크면 건너뜁니다. 크기를 늘리면 다음에 받습니다.",
            "새로 받은 파일 안에는 트윗 주소와 작성자 아이디가 기록됩니다.",
        ), None),
        ("기록 · 통계", (
            "지금까지 저장한 모든 파일과 실행 기록을 봅니다.",
            "파일을 고르고 ‘파일 열기’나 ‘트윗 열기’를 누르세요.",
            "예전 파일에 출처·작성자를 넣으려면 ‘출처·작성자 기록’을 누르세요.",
        ), "이 작업은 파일을 다시 쓰므로 Google Drive·OneDrive 폴더라면 파일이 다시 올라갈 수 있습니다."),
        ("매일 자동 저장", (
            "아래 ‘예약 명령 복사’를 누르고, 명령 프롬프트(cmd)에 붙여 넣어 실행합니다.",
            "매일 오전 9시에 마지막으로 쓴 설정으로 새 Like만 저장합니다. 결과는 ‘예약 실행 기록’에 남습니다.",
            "해제: schtasks /Delete /TN \"XlikeSaver 자동 저장\" /F",
        ), "인증 토큰 연결에서만 동작합니다. 실행 파일을 옮기거나 새 버전으로 바꾸면 다시 등록하세요."),
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
        popup.geometry(f"780x{min(760, max(560, self.root.winfo_screenheight() - 120))}")
        popup.minsize(640, 480)
        popup.configure(bg=BG)
        popup.transient(self.root)
        links = ttk.Frame(popup, padding=(18, 10))
        links.pack(side="bottom", fill="x")
        ttk.Button(links, text="예약 명령 복사", command=self.copy_schedule_command).pack(side="left")
        ttk.Button(links, text="예약 실행 기록", command=self.open_schedule_logs).pack(side="left", padx=8)
        ttk.Button(links, text="X 자동화 정책", command=lambda: webbrowser.open("https://help.x.com/en/rules-and-policies/x-automation")).pack(side="left")
        ttk.Button(links, text="닫기", command=popup.destroy).pack(side="right")
        body = ttk.Frame(popup, padding=(18, 16, 18, 0))
        body.pack(fill="both", expand=True)
        content = tk.Text(body, wrap="word", background=CARD, foreground=theme.INK, relief="solid", borderwidth=1,
                          highlightthickness=0, padx=22, pady=14, font=theme.BODY, spacing1=2, spacing3=4, cursor="arrow")
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

    def copy_schedule_command(self):
        from .scheduled import schtasks_command
        self.root.clipboard_clear()
        self.root.clipboard_append(schtasks_command())
        self.status.set("예약 등록 명령을 복사했습니다")
        self.status_detail.set("명령 프롬프트(cmd)에 붙여 넣어 실행하세요.")

    def open_schedule_logs(self):
        from .scheduled import log_directory
        try:
            directory = log_directory()
            directory.mkdir(parents=True, exist_ok=True)
            os.startfile(str(directory))
        except OSError:
            messagebox.showerror("폴더 열기 실패", "예약 실행 기록 폴더를 열지 못했습니다.", parent=self.root)

    # ------------------------------------------------------------------ saving
    def start(self):
        if self.relocating:
            self._storage_action = "start"
            return
        if self.running:
            return
        if self.history_window is not None and self.history_window.winfo_exists() and self.history_window.metadata_active:
            messagebox.showinfo("메타데이터 기록 중", "기존 파일의 출처·작성자 기록을 완료하거나 중단한 뒤 저장을 시작해 주세요.", parent=self.root)
            return
        if AUTH_MODES[self.auth.get()] == "managed" and self.token.get():
            if not self.connect_token():
                return
        if AUTH_MODES[self.auth.get()] == "managed" and not has_session():
            self.status.set("먼저 X 로그인을 연결해 주세요")
            self.status_detail.set("열린 창의 안내대로 auth_token을 붙여 넣으면 됩니다.")
            self.show_connect("저장하려면 먼저 X 로그인을 연결해야 합니다.")
            return
        try:
            try:
                size_limit = int(self.max_video_mb.get().strip())
            except ValueError:
                raise ValueError("영상 최대 크기는 0 이상의 정수(MB)로 입력해 주세요. 0은 제한 없음입니다.") from None
            options = JobOptions(account=self.account.get(), output_dir=self.folder.get(), auth_mode=AUTH_MODES[self.auth.get()], cookies_file=self.cookies.get(), photo_limit=20 if self.limited.get() else 0, folder_mode=FOLDER_MODES[self.folder_mode.get()], scan_mode=SCAN_MODES[self.scan_mode.get()], include_videos=self.include_videos.get(), include_gifs=self.include_gifs.get(), max_video_mb=size_limit, separate_accounts=self.separate_accounts.get()).validate()
        except (ValueError, OSError) as exc:
            messagebox.showerror("입력 확인", str(exc), parent=self.root)
            return
        if not self._ensure_storage("start"):
            return
        self.record_locations[options.account] = options.output_dir
        self.account.set(options.account)
        self.folder.set(options.output_dir)
        self.saved = self.skipped = self.size_skipped = 0
        self.received_final = False
        self.final_status = None
        self.running = True
        self.stopping = False
        self._update_counts()
        self.status.set("좋아요 목록을 확인하고 있습니다…")
        self.status_detail.set("목록은 10초에 한 번씩 천천히 요청합니다. 전체 Like 수와 완료율은 미리 알 수 없습니다.")
        self.progress_summary.set("")
        self.progress_details.set("")
        self.progress.configure(mode="indeterminate", value=0)
        self.progress.start(12)
        self._set_running(True)
        self._log("새 작업 시작 · " + ("시험 저장(새 파일 20개)" if options.photo_limit else self.scan_mode.get()) + " · " + self.folder_mode.get())
        try:
            self._save_settings(options)
        except OSError:
            self._log("설정을 기억하지 못했지만 다운로드는 계속 진행합니다.")
        threading.Thread(target=self._run_process, args=(options,), daemon=True).start()

    def _set_running(self, value):
        if value:
            self._close_connect()
            self._close_options()
        self.start_button.configure(state="disabled" if value else "normal")
        self.stop_button.configure(state="normal" if value else "disabled")
        for widget in self.inputs:
            widget.configure(state="disabled" if value else "normal")
        if not value:
            self._auth_changed()

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
                    with events.open("r", encoding="utf-8") as stream:
                        partial = ""
                        stop_at = None
                        while True:
                            chunk = stream.read()
                            if chunk:
                                partial += chunk
                                while "\n" in partial:
                                    line, partial = partial.split("\n", 1)
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
        if not self.running or self.stopping:
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
            elif kind in ("saved", "skipped", "size-skipped"):
                if kind == "saved":
                    self.saved += 1
                    self._log("저장 · " + str(event.get("path", "")))
                elif kind == "skipped":
                    self.skipped += 1
                else:
                    self.size_skipped += 1
                    self._log("용량 초과로 건너뜀 · " + str(event.get("filename") or event.get("tweet_id") or "파일"))
                self._update_counts()
                self._schedule_history_refresh()
                if not self.stopping:
                    self.status.set("파일을 저장하고 있습니다…")
                    self.status_detail.set("이미 저장한 파일은 건너뛰고 새 파일만 받습니다.")
            elif kind == "log":
                self._log(str(event.get("message", "")))
            elif kind == "finished":
                self.received_final = True
                self.final_status = event.get("status")
                self.saved = int(event.get("saved", self.saved))
                self.skipped = int(event.get("skipped", self.skipped))
                self.size_skipped = int(event.get("size_skipped", self.size_skipped))
                self._update_counts()
                message = str(event.get("message", "작업이 종료되었습니다."))
                headline = self.FINISH_HEADLINES.get(self.final_status, message)
                if self.final_status == "finished":
                    headline += f" · 새로 저장 {self.saved:,}개"
                self.status.set(headline)
                self.status_detail.set(message if headline != message else "")
                self._log(message)
                if event.get("metadata_failed"):
                    self._log(f"파일 내부 출처·작성자 기록 실패 {int(event['metadata_failed']):,}개 · 미디어는 보존했습니다. ‘기록 · 통계’에서 ‘출처·작성자 기록’으로 다시 시도할 수 있습니다.")
                if event.get("retry_at"):
                    self._log("요청을 다시 시도할 수 있는 시각 (한국 시간) · " + str(event["retry_at"]))
            elif kind == "process_exit":
                self.running = False
                self.limited.set(False)
                self.progress.stop()
                self.progress.configure(mode="determinate", value=100 if self.final_status == "finished" else 0)
                self._set_running(False)
                if not self.received_final:
                    self.status.set("중단되었습니다. 저장된 파일은 유지됩니다." if self.stopping else "작업이 종료되었습니다. 오류 로그와 입력을 확인해 주세요.")
                    self.status_detail.set("")
                    self._log(self.status.get())
                elif self.final_status == "finished" and event.get("code", 0) != 0 and not self.stopping:
                    self.status.set("작업 프로세스가 오류로 종료되었습니다. 저장된 파일과 로그를 확인해 주세요.")
                    self.status_detail.set("")
                    self._log(self.status.get())
                self._refresh_history()
                if self.closing:
                    self.root.destroy()
                    return
        self.root.after(120, self._poll)

    def _log(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", time.strftime("%H:%M:%S") + "  " + text + "\n")
        if int(self.log.index("end-1c").split(".")[0]) > 1000:
            self.log.delete("1.0", "201.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def close(self):
        if self.relocating:
            self.closing = True
            self.status.set("기록 이전을 마친 뒤 종료합니다…")
        elif self.running:
            self.closing = True
            self.stop()
        elif self.history_window is not None and self.history_window.winfo_exists() and self.history_window.metadata_active:
            self.closing = True
            self.history_window.stop_metadata()
            self.root.after(150, self.close)
        else:
            self.root.destroy()


def launch():
    root = tk.Tk()
    App(root)
    root.mainloop()

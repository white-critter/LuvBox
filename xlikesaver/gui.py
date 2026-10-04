"""Native interface with two local authentication methods."""

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

from . import __version__
from .core import QUICK_STOP_AFTER, JobOptions, _normal_account
from .login import clear_session, has_session, save_auth_token
from .settings import load_settings, settings_path
from .storage import resolve_storage


AUTH_MODES = {
    "인증 토큰 연결 · 한 번 저장": "managed",
    "cookies.txt 파일 · 호환 방식": "file",
}
FOLDER_MODES = {"작성자별 폴더": "author", "저장일별 폴더": "date", "구분 없이 저장": "flat"}
SCAN_MODES = {"새 Like만 빠르게 확인 (권장)": "quick", "전체 목록 다시 확인": "full"}
BG = "#f5f7fa"
INK = "#162337"
MUTED = "#596a80"
BLUE = "#1768ce"


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
        width = min(880, max(740, root.winfo_screenwidth() - 100))
        height = min(790, max(650, root.winfo_screenheight() - 100))
        root.geometry(f"{width}x{height}")
        root.minsize(740, 650)
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
        self.progress_summary = tk.StringVar(value="파일 다운로드 대기 중")
        self.progress_details = tk.StringVar()
        # Normal saving covers all available Likes. A short test is opt-in for
        # each session, including after loading settings from older versions.
        self.limited = tk.BooleanVar(value=False)
        scan = settings.get("scan_mode", "quick")
        self.scan_mode = tk.StringVar(value=next((k for k, v in SCAN_MODES.items() if v == scan), next(iter(SCAN_MODES))))
        self.status = tk.StringVar(value="시작할 준비가 되었습니다")
        self.counts = tk.StringVar(value="이번 저장 0개    ·    이미 저장됨 0개    ·    용량 초과 0개")
        self._style()
        self._build()
        self.auth.trace_add("write", lambda *_: self._auth_changed())
        for variable in (self.account, self.folder, self.folder_mode, self.separate_accounts):
            variable.trace_add("write", lambda *_: self._preview_location())
        self._auth_changed()
        self._preview_location()
        self.tabs.bind("<<NotebookTabChanged>>", lambda _: self._resize_tab())
        self.root.after_idle(self._resize_tab)
        self.root.after(120, self._poll)

    def _style(self):
        self.root.option_add("*Font", ("맑은 고딕", 10))
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("TFrame", background=BG)
        style.configure("Card.TFrame", background="white")
        style.configure("TLabel", background=BG, foreground=INK)
        style.configure("Card.TLabel", background="white", foreground=INK)
        style.configure("Hint.TLabel", background="white", foreground=MUTED, font=("맑은 고딕", 9))
        style.configure("TEntry", padding=5, fieldbackground="white")
        style.configure("TCombobox", padding=5)
        style.configure("TButton", padding=(11, 6))
        style.configure("Primary.TButton", background=BLUE, foreground="white", font=("맑은 고딕", 10, "bold"))
        style.map("Primary.TButton", background=[("active", "#1055b0"), ("disabled", "#a8bbd3")])
        style.configure("TCheckbutton", background="white", foreground=INK)
        style.configure("Progress.TCheckbutton", background=BG, foreground=INK, font=("맑은 고딕", 9))
        style.configure("Horizontal.TProgressbar", background=BLUE, troughcolor="#e7edf5")

    def _build(self):
        shell = ttk.Frame(self.root, padding=(18, 10))
        shell.pack(fill="both", expand=True)
        header = ttk.Frame(shell)
        header.pack(fill="x")
        ttk.Label(header, text="XlikeSaver", font=("맑은 고딕", 20, "bold")).pack(side="left")
        ttk.Button(header, text="기록 · 통계", command=self.show_history).pack(side="right")
        ttk.Label(shell, text="내가 좋아요한 사진·동영상·GIF를 저장하세요.", foreground=MUTED).pack(anchor="w", pady=(2, 7))

        self.tabs = ttk.Notebook(shell)
        self.tabs.pack(fill="x")
        card = ttk.Frame(self.tabs, style="Card.TFrame", padding=12)
        rules = ttk.Frame(self.tabs, style="Card.TFrame", padding=12)
        self.tabs.add(card, text="기본 설정")
        self.tabs.add(rules, text="저장 옵션")
        card.columnconfigure(1, weight=1)
        self._field_label(card, "X 아이디", 0)
        account = ttk.Entry(card, textvariable=self.account)
        account.grid(row=0, column=1, columnspan=2, sticky="ew", padx=(16, 0))
        self.inputs.append(account)
        ttk.Label(card, text="@아이디 또는 내 프로필 주소", style="Hint.TLabel").grid(row=1, column=1, columnspan=2, sticky="w", padx=16, pady=(2, 5))

        self._field_label(card, "로그인 연결", 2)
        self.auth_combo = ttk.Combobox(card, textvariable=self.auth, values=list(AUTH_MODES), state="readonly")
        self.auth_combo.grid(row=2, column=1, columnspan=2, sticky="ew", padx=(16, 0))
        self.inputs.append(self.auth_combo)
        self.auth_hint = ttk.Label(card, style="Hint.TLabel", wraplength=520)
        self.auth_hint.grid(row=3, column=1, columnspan=2, sticky="w", padx=16, pady=(2, 5))
        self.cookie_entry = ttk.Entry(card, textvariable=self.cookies)
        self.cookie_entry.grid(row=4, column=1, sticky="ew", padx=(16, 8))
        self.cookie_button = ttk.Button(card, text="쿠키 파일 선택", command=self.choose_cookies)
        self.cookie_button.grid(row=4, column=2)
        self.inputs.extend((self.cookie_entry, self.cookie_button))
        self.token_controls = ttk.Frame(card, style="Card.TFrame")
        self.token_controls.grid(row=4, column=1, columnspan=2, sticky="ew", padx=(16, 0))
        self.token_entry = ttk.Entry(self.token_controls, textvariable=self.token, show="●", width=18)
        self.token_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.token_button = ttk.Button(self.token_controls, text="연결 저장", command=self.connect_token)
        self.token_button.pack(side="left")
        self.clear_button = ttk.Button(self.token_controls, text="연결 지우기", command=self.disconnect)
        self.clear_button.pack(side="left", padx=8)
        self.inputs.extend((self.token_entry, self.token_button, self.clear_button))
        self._field_label(card, "저장 폴더", 5)
        folder = ttk.Entry(card, textvariable=self.folder)
        folder.grid(row=5, column=1, sticky="ew", padx=(16, 8), pady=(8, 0))
        folder_button = ttk.Button(card, text="폴더 선택", command=self.choose_folder)
        folder_button.grid(row=5, column=2, pady=(8, 0))
        self.inputs.extend((folder, folder_button))
        folder.bind("<Return>", lambda _: self._ensure_storage())
        # Wait until a clicked Start/History button has had a chance to queue
        # its action; never migrate on each keystroke of a path being typed.
        ttk.Label(card, text="파일 위치", style="Hint.TLabel").grid(row=6, column=0, sticky="w", pady=(5, 0))
        self.preview_entry = ttk.Entry(card, textvariable=self.destination_preview, state="readonly", font=("맑은 고딕", 9))
        self.preview_entry.grid(row=6, column=1, columnspan=2, sticky="ew", padx=(16, 0), pady=(5, 0))
        rules.columnconfigure(1, weight=1)
        ttk.Label(rules, text="폴더 정리 방식", style="Card.TLabel", font=("맑은 고딕", 10, "bold")).grid(row=0, column=0, sticky="w")
        self.layout_combo = ttk.Combobox(rules, textvariable=self.folder_mode, values=list(FOLDER_MODES), state="readonly")
        self.layout_combo.grid(row=0, column=1, sticky="ew", padx=(16, 0))
        self.inputs.append(self.layout_combo)
        account_folders = ttk.Checkbutton(rules, text="계정별 폴더로 나누기", variable=self.separate_accounts)
        account_folders.grid(row=1, column=1, sticky="w", padx=12, pady=(3, 3))
        self.inputs.append(account_folders)
        ttk.Label(rules, text="구분 없이: 선택한 폴더에 바로 저장 · 저장일: 한국 시간 기준", style="Hint.TLabel", wraplength=500).grid(row=2, column=1, sticky="w", padx=16, pady=(0, 9))
        ttk.Label(rules, text="미디어", style="Card.TLabel", font=("맑은 고딕", 10, "bold")).grid(row=3, column=0, sticky="w")
        media = ttk.Frame(rules, style="Card.TFrame")
        media.grid(row=3, column=1, sticky="w", padx=12)
        videos = ttk.Checkbutton(media, text="동영상 포함", variable=self.include_videos)
        videos.pack(side="left")
        gifs = ttk.Checkbutton(media, text="움직이는 GIF 포함", variable=self.include_gifs)
        gifs.pack(side="left", padx=12)
        self.inputs.extend((videos, gifs))
        ttk.Label(rules, text="사진은 항상 저장 · 동영상은 최고 화질 · GIF는 MP4", style="Hint.TLabel", wraplength=500).grid(row=4, column=1, sticky="w", padx=16, pady=(0, 9))
        ttk.Label(rules, text="영상 용량 제한", style="Card.TLabel", font=("맑은 고딕", 10, "bold")).grid(row=5, column=0, sticky="w")
        size = ttk.Frame(rules, style="Card.TFrame")
        size.grid(row=5, column=1, sticky="w", padx=16)
        size_entry = ttk.Entry(size, textvariable=self.max_video_mb, width=8)
        size_entry.pack(side="left")
        self.inputs.append(size_entry)
        ttk.Label(size, text="MiB / 파일 · 0 = 제한 없음 · 동영상·GIF의 큰 파일은 건너뜀", style="Hint.TLabel").pack(side="left", padx=8)
        ttk.Label(rules, text="확인 범위", style="Card.TLabel", font=("맑은 고딕", 10, "bold")).grid(row=6, column=0, sticky="w", pady=(4, 0))
        self.scan_combo = ttk.Combobox(rules, textvariable=self.scan_mode, values=list(SCAN_MODES), state="readonly")
        self.scan_combo.grid(row=6, column=1, sticky="ew", padx=(16, 0), pady=(4, 0))
        self.inputs.append(self.scan_combo)
        ttk.Label(rules, text=f"저장된 파일이 {QUICK_STOP_AFTER}개 연속이면 멈춤 · 필요하면 자동으로 전체 확인", style="Hint.TLabel", wraplength=500).grid(row=7, column=1, sticky="w", padx=16, pady=(1, 2))
        limit = ttk.Checkbutton(rules, text="시험 저장: 새 파일 20개에서 중단", variable=self.limited)
        limit.grid(row=8, column=1, sticky="w", padx=12)
        self.inputs.append(limit)

        actions = ttk.Frame(shell)
        actions.pack(fill="x", pady=8)
        self.start_button = ttk.Button(actions, text="저장 시작", style="Primary.TButton", command=self.start)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(actions, text="중단", command=self.stop, state="disabled")
        self.stop_button.pack(side="left", padx=8)
        ttk.Button(actions, text="저장 폴더 열기", command=self.open_folder).pack(side="right")
        ttk.Button(actions, text="사용 방법", command=self.help).pack(side="right", padx=8)

        ttk.Label(shell, textvariable=self.status, font=("맑은 고딕", 11, "bold"), wraplength=700, justify="left").pack(anchor="w")
        ttk.Label(shell, textvariable=self.counts, foreground=MUTED).pack(anchor="w", pady=(3, 5))
        progress_heading = ttk.Frame(shell)
        progress_heading.pack(fill="x")
        ttk.Label(progress_heading, textvariable=self.progress_summary, foreground=MUTED, wraplength=540, font=("맑은 고딕", 9)).pack(side="left", fill="x", expand=True)
        ttk.Checkbutton(progress_heading, text="자세히 보기", variable=self.detailed_progress, command=self._toggle_progress, style="Progress.TCheckbutton").pack(side="right")
        self.progress = ttk.Progressbar(shell, mode="indeterminate")
        self.progress.pack(fill="x", pady=(3, 5))
        self.progress_detail_label = ttk.Label(shell, textvariable=self.progress_details, foreground=MUTED, wraplength=700, font=("맑은 고딕", 9))
        self.progress_detail_label.pack(fill="x", pady=(0, 5))
        if not self.detailed_progress.get():
            self.progress_detail_label.pack_forget()
        ttk.Label(shell, text="X의 비공식 수집 방식입니다. 제한 안내가 뜨면 기다리세요. 계정 제한 위험은 남습니다.", foreground="#945011", font=("맑은 고딕", 9), wraplength=700).pack(side="bottom", anchor="w", pady=(5, 0))
        log_frame = ttk.Frame(shell)
        log_frame.pack(fill="both", expand=True)
        self.log = tk.Text(log_frame, height=4, wrap="word", bg="#142237", fg="#dde7f7", insertbackground="white", relief="flat", padx=10, pady=6, state="disabled", font=("맑은 고딕", 9))
        scroll = ttk.Scrollbar(log_frame, command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        self.log.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self._log("최근 좋아요부터 저장하며, 이미 저장한 파일은 건너뜁니다.")

    def _resize_tab(self):
        selected = self.tabs.select()
        if selected:
            child = self.tabs.nametowidget(selected)
            self.tabs.configure(height=child.winfo_reqheight())

    def _toggle_progress(self):
        if self.detailed_progress.get():
            self.progress_detail_label.pack(before=self.log.master, fill="x", pady=(0, 5))
        else:
            self.progress_detail_label.pack_forget()

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
            self.destination_preview.set("X 아이디와 저장 폴더를 확인하세요.")
        self.preview_entry.xview_moveto(1)

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
        self.counts.set(f"이번 저장 {self.saved:,}개    ·    이미 저장됨 {self.skipped:,}개    ·    용량 초과 {self.size_skipped:,}개")

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

    @staticmethod
    def _field_label(parent, text, row):
        ttk.Label(parent, text=text, style="Card.TLabel", font=("맑은 고딕", 10, "bold")).grid(row=row, column=0, sticky="w", pady=(8 if row == 5 else 0, 0))

    def _auth_changed(self):
        mode = AUTH_MODES[self.auth.get()]
        file_mode = mode == "file"
        managed = mode == "managed"
        self.cookie_entry.configure(state="normal" if file_mode and not self.running else "disabled")
        self.cookie_button.configure(state="normal" if file_mode and not self.running else "disabled")
        if file_mode:
            self.cookie_entry.grid()
            self.cookie_button.grid()
        else:
            self.cookie_entry.grid_remove()
            self.cookie_button.grid_remove()
        if managed:
            self.token_controls.grid()
        else:
            self.token_controls.grid_remove()
            self.token.set("")
        if managed:
            hint = ("연결 정보 저장됨 · 만료되면 토큰을 다시 입력하세요." if has_session() else "auth_token을 붙여 넣으세요. 찾는 방법은 아래 ‘사용 방법’에 있습니다.")
        else:
            hint = "로그인된 x.com 쿠키를 Netscape 형식으로 내보내 선택하세요."
        self.auth_hint.configure(text=hint)
        self._resize_tab()

    def connect_token(self) -> bool:
        if self.running:
            return False
        try:
            save_auth_token(self.token.get())
        except ValueError as exc:
            messagebox.showerror("연결 저장 실패", str(exc), parent=self.root)
            return False
        except OSError:
            messagebox.showerror("연결 저장 실패", "연결 정보를 저장하지 못했습니다. Windows 계정과 저장 권한을 확인해 주세요.", parent=self.root)
            return False
        finally:
            self.token.set("")
        self._auth_changed()
        self.status.set("연결 저장됨 · 실제 유효성은 저장을 시작할 때 확인합니다.")
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
        self.status.set("이 앱에 저장한 로그인 연결을 지웠습니다.")

    def choose_cookies(self):
        value = filedialog.askopenfilename(title="X 로그인 쿠키 파일 선택", filetypes=[("Netscape 쿠키 파일", "*.txt"), ("모든 파일", "*.*")])
        if value:
            self.cookies.set(value)

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
            self.status.set("이전 기록 폴더에 접근하지 못했습니다. 경로와 연결을 확인해 주세요.")
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
            self.status.set("이전 저장 폴더를 유지했습니다. 폴더 연결을 확인해 주세요.")
            return False
        self.relocating = True
        self._storage_fresh = fresh
        self._storage_action = action
        self._storage_plan = (source, destination)
        self._set_running(True)
        self.stop_button.configure(state="disabled")
        self.status.set("새 저장 폴더를 준비하고 있습니다…" if fresh else "저장 기록을 새 폴더로 옮기고 있습니다…")
        self.progress_summary.set("기존 사진·영상은 원래 위치에 보관합니다")
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
        self._set_running(False)
        if success:
            if fresh:
                self.status.set("새 저장 폴더를 준비했습니다. 이전 폴더의 기록은 연결 후 가져올 수 있습니다.")
                self._log(self.status.get())
            else:
                self.status.set("저장 폴더를 변경했습니다. 기존 기록을 이어서 사용합니다.")
                self._log("기록 자동 이전 완료 · 기존 사진·영상은 원래 위치에 있습니다.")
        else:
            self.status.set("기록을 옮기지 못해 이전 저장 폴더를 유지했습니다.")
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

    def help(self):
        popup = tk.Toplevel(self.root)
        popup.title("XlikeSaver 사용 방법")
        popup.geometry("720x620")
        popup.minsize(600, 440)
        popup.configure(bg=BG)
        popup.transient(self.root)
        text = ("처음 연결하기 (추천)\n"
                "1. 평소 사용하는 브라우저에서 x.com에 로그인하세요.\n"
                "2. F12 → Application → Cookies → https://x.com을 선택하세요. Application이 안 보이면 » 메뉴를 확인하세요.\n"
                "3. auth_token 행의 Value 값을 복사하세요.\n"
                "4. 앱에 붙여 넣고 ‘연결 저장’을 누르세요.\n\n"
                "연결 정보는 이 Windows 계정에 암호화해 보관합니다. 입력칸은 저장 후 비워지며, 연결 유효성은 저장을 시작할 때 확인합니다. 토큰과 쿠키 파일은 비밀번호처럼 보관하고 다른 사람에게 보내지 마세요.\n\n"
                "쿠키 파일로 연결하기\n"
                "로그인된 x.com 쿠키를 Netscape 형식 cookies.txt로 내보내세요. 앱에서 ‘cookies.txt 파일 · 호환 방식’을 선택하고 파일을 고르면 됩니다. JSON은 지원하지 않습니다. 아래 ‘쿠키 파일 안내’를 참고하세요.\n\n"
                "저장하기\n"
                "내 X 아이디와 저장 폴더를 입력하고 ‘저장 시작’을 누르세요. 최근 좋아요부터 확인하고, 완료한 파일은 다음에 건너뜁니다. 기본은 선택한 폴더에 바로 저장합니다. ‘저장 옵션’에서 계정별·작성자별·저장일별 정리를 선택할 수 있습니다. ‘파일 위치’에 최종 경로를 보여줍니다.\n\n"
                f"‘새 Like만 빠르게 확인’은 이미 저장한 파일이 {QUICK_STOP_AFTER}개 연속으로 나오면 멈춰 목록 요청을 크게 줄입니다. 첫 실행, 이전 실행이 중단·오류·시험 저장으로 끝난 경우, 동영상·GIF·용량 제한을 바꾼 경우에는 자동으로 전체 목록을 확인합니다. 옛 Like를 다시 훑고 싶으면 ‘전체 목록 다시 확인’을 고르세요.\n\n"
                "정기적으로 자동 저장하기\n"
                "인증 토큰 연결로 한 번 저장을 마친 뒤 ‘예약 명령 복사’를 누르고, 명령 프롬프트(cmd)에 붙여 넣어 실행하면 매일 오전 9시에 새 Like만 저장합니다. 예약 실행은 마지막으로 저장을 시작할 때의 설정을 사용하며 창 없이 실행되고, 결과는 ‘예약 실행 기록’에 남습니다. 시각을 바꾸려면 명령의 09:00을 고치세요. 해제: schtasks /Delete /TN \"XlikeSaver 자동 저장\" /F. 실행 파일을 옮기거나 새 버전으로 바꾸면 다시 등록하세요. 너무 자주 예약하지 마세요.\n\n"
                "동영상은 최고 제공 MP4 화질로, 움직이는 GIF도 MP4로 저장합니다. 용량 한도는 파일당 200MiB이며 0은 무제한입니다. 큰 파일은 건너뛰고, 한도를 올리면 다음에 다시 확인합니다.\n\n"
                "출처와 기록 확인하기\n"
                "파일 내부에 트윗 URL과 작성자 @아이디·프로필 주소를 기록합니다. 숫자 사용자 ID가 제공되면 함께 보관합니다. 재인코딩하지 않고 기존 설명을 보존합니다. ‘기록 · 통계’에서 파일과 트윗을 열고 CSV로 내보낼 수 있습니다. ‘기존 파일에 출처·작성자 기록’은 PC에 남은 정보로 처리하며, 작성자 정보가 없는 파일은 출처만 기록합니다. 실패해도 미디어는 보존합니다.\n\n"
                "게시일 범위는 저장된 트윗의 범위이며, 그 날짜까지 전부 저장했다는 뜻은 아닙니다. 예전 저장 시각과 좋아요 시각은 복원할 수 없습니다. 진행률도 현재 파일 기준입니다.\n\n"
                "중단과 요청 제한\n"
                "중단·오류가 생겨도 완료 파일과 기록은 남습니다. 다시 시작하면 최근 좋아요부터 확인합니다. 저장 폴더를 바꾸면 기록을 자동으로 옮겨 이어서 사용합니다. 경로를 직접 입력한 경우 Enter 또는 저장 시작·기록 보기를 누르면 적용합니다. 이전 폴더가 연결되지 않았으면 새 폴더에서 시작할지 확인합니다. 기존 사진·영상은 원래 위치에 있습니다. 작성자별·저장일별 정리 방식을 바꾸면 복사본이 생길 수 있습니다.\n\n"
                "X의 비공식 수집 방식이라 느리게 요청해도 계정 제한 위험은 남습니다. 제한이 뜨면 안내된 시각까지 기다리세요. 계정 잠금·로그인 경고는 평소 X에서 확인하세요. 연결 정보를 바꿔도 제한은 해제되지 않습니다. 자주 반복 실행하지 마세요.")
        body = ttk.Frame(popup, padding=18)
        body.pack(fill="both", expand=True)
        content = tk.Text(body, wrap="word", background=BG, foreground=INK, relief="flat", padx=4, pady=4)
        scrollbar = ttk.Scrollbar(body, command=content.yview)
        content.configure(yscrollcommand=scrollbar.set)
        content.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        content.insert("1.0", text)
        content.configure(state="disabled")
        links = ttk.Frame(popup)
        links.pack(pady=(0, 20))
        ttk.Button(links, text="Chrome 쿠키 찾기", command=lambda: webbrowser.open("https://developer.chrome.com/docs/devtools/application/cookies/")).pack(side="left", padx=(0, 8))
        ttk.Button(links, text="쿠키 파일 안내", command=lambda: webbrowser.open("https://github.com/mikf/gallery-dl#cookies")).pack(side="left")
        ttk.Button(links, text="X 자동화 정책", command=lambda: webbrowser.open("https://help.x.com/en/rules-and-policies/x-automation")).pack(side="left", padx=8)
        ttk.Button(links, text="예약 명령 복사", command=self.copy_schedule_command).pack(side="left")
        ttk.Button(links, text="예약 실행 기록", command=self.open_schedule_logs).pack(side="left", padx=8)

    def copy_schedule_command(self):
        from .scheduled import schtasks_command
        self.root.clipboard_clear()
        self.root.clipboard_append(schtasks_command())
        self.status.set("예약 등록 명령을 복사했습니다. 명령 프롬프트(cmd)에 붙여 넣어 실행하세요.")

    def open_schedule_logs(self):
        from .scheduled import log_directory
        try:
            directory = log_directory()
            directory.mkdir(parents=True, exist_ok=True)
            os.startfile(str(directory))
        except OSError:
            messagebox.showerror("폴더 열기 실패", "예약 실행 기록 폴더를 열지 못했습니다.", parent=self.root)

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
            messagebox.showinfo("연결 정보 필요", "평소 로그인된 X의 auth_token 값을 입력하고 ‘연결 저장’을 누르세요. 찾는 방법은 ‘사용 방법’에 있습니다.", parent=self.root)
            return
        try:
            try:
                size_limit = int(self.max_video_mb.get().strip())
            except ValueError:
                raise ValueError("영상 용량 제한은 0 이상의 정수 MiB로 입력해 주세요. 0은 제한 없음입니다.") from None
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
        self.progress_summary.set("목록 조회 중 · 전체 Like 수와 완료율은 알 수 없습니다")
        self.progress_details.set("")
        self.progress.configure(mode="indeterminate", value=0)
        self.progress.start(12)
        self._set_running(True)
        self._log("새 작업 시작 · " + ("새 파일 20개 확인" if options.photo_limit else self.scan_mode.get()))
        self._log(f"저장 방식 · {self.folder_mode.get()} · 최근 Like부터 확인, 저장 완료 파일은 건너뛰기")
        self._log("요청 제한·계정 경고가 나타나면 반복 실행하지 마세요.")
        try:
            self._save_settings(options)
        except OSError:
            self._log("설정을 기억하지 못했지만 다운로드는 계속 진행합니다.")
        threading.Thread(target=self._run_process, args=(options,), daemon=True).start()

    def _set_running(self, value):
        self.start_button.configure(state="disabled" if value else "normal")
        self.stop_button.configure(state="normal" if value else "disabled")
        for widget in self.inputs:
            widget.configure(state="disabled" if value else "normal")
        if not value:
            self.auth_combo.configure(state="readonly")
            self.layout_combo.configure(state="readonly")
            self.scan_combo.configure(state="readonly")
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
        self.status.set("중단 중입니다…")
        self.stop_button.configure(state="disabled")
        self._log("중단 요청 · 이미 저장된 파일은 다음 실행에서 건너뜁니다.")
        if self.stop_path:
            try:
                self.stop_path.touch(exist_ok=True)
            except OSError:
                pass

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
            elif kind == "log":
                self._log(str(event.get("message", "")))
            elif kind == "finished":
                self.received_final = True
                self.final_status = event.get("status")
                self.saved = int(event.get("saved", self.saved))
                self.skipped = int(event.get("skipped", self.skipped))
                self.size_skipped = int(event.get("size_skipped", self.size_skipped))
                self._update_counts()
                self.status.set(str(event.get("message", "작업이 종료되었습니다.")))
                self._log(self.status.get())
                if event.get("metadata_failed"):
                    self._log(f"파일 내부 출처·작성자 기록 실패 {int(event['metadata_failed']):,}개 · 미디어는 보존했습니다. ‘기록 · 통계’에서 ‘기존 파일에 출처·작성자 기록’으로 다시 시도할 수 있습니다.")
                if event.get("retry_at"):
                    self._log("요청을 다시 시도할 수 있는 시각 (한국 시간) · " + str(event["retry_at"]))
            elif kind == "process_exit":
                self.running = False
                self.progress.stop()
                self._set_running(False)
                if not self.received_final:
                    self.status.set("중단되었습니다. 저장된 파일은 유지됩니다." if self.stopping else "작업이 종료되었습니다. 오류 로그와 입력을 확인해 주세요.")
                    self._log(self.status.get())
                elif self.final_status == "finished" and event.get("code", 0) != 0 and not self.stopping:
                    self.status.set("작업 프로세스가 오류로 종료되었습니다. 저장된 파일과 로그를 확인해 주세요.")
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

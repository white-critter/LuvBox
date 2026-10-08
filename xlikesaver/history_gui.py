"""Local records and statistics, without contacting X."""

from __future__ import annotations

from contextlib import closing, nullcontext
from datetime import date, datetime
from functools import partial
import os
from pathlib import Path
import queue
import re
import sqlite3
import threading
import time
import tkinter as tk
from tkinter import filedialog, ttk
import webbrowser

from PIL import ImageTk

from . import dialogs, theme
from .charts import MonthlyBars
from .gallery import TILE, ThumbnailGrid
from .history import CatalogDamaged, HistoryError, HistoryStore
from .preview import PreviewLoader
from .storage import MigrationError, OutputGuard, StoragePaths, publish_snapshot


MEDIA_LABELS = {"all": "전체", "photo": "사진", "video": "동영상", "animated_gif": "움직이는 GIF"}
STATUS_LABELS = {"running": "진행 중", "finished": "완료", "stopped": "직접 중단", "error": "오류", "interrupted": "비정상 종료", "limited": "시험 저장 완료", "partial": "일부만 완료", "cancelled": "취소됨"}
METADATA_LABELS = {"embedded": "파일에 출처 기록됨", "pending": "출처 기록 전", "failed": "파일 출처 기록 실패", "unsupported": "메타데이터 미지원 형식"}
AUTHOR_METADATA_LABELS = {"embedded": "작성자 기록됨", "pending": "작성자 기록 전", "error": "작성자 기록 실패", "unknown": "작성자 정보 없음"}
TILES = {"files": "현재 파일", "posts": "기록된 트윗", "photo": "사진", "video": "동영상", "animated_gif": "움직이는 GIF", "bytes": "확인된 용량"}
PAGE_SIZE = 100
PAGE_SIZES = (50, 100, 200)
ORDER_LABELS = {"saved": "저장한 순", "liked": "Like한 순", "posted": "게시일 최신순", "posted_oldest": "게시일 오래된 순"}
PERIOD_LABELS = {"posted": "게시일", "saved": "저장일"}
MONTH_FIELDS = {"saved": "저장한 달", "posted": "게시된 달", "liked": "Like한 달"}
MONTH_NOTES = {
    "saved": "현재 파일의 저장월입니다. 중복 기록은 한 번만 셉니다.",
    "posted": "현재 파일의 트윗 게시월입니다. 3년이 넘으면 연도별로 보여 드립니다.",
    "liked": "새로 누른 Like를 앱이 발견한 달입니다. 실제 Like 시각과 다를 수 있습니다.",
}
GIF_SIZE_LABELS = {"original": "원본 크기", "720": "최대 720px · 초당 15장", "480": "최대 480px · 초당 12장"}
NO_SELECTION = "파일을 선택하면 미리보기와 출처를 보여 드립니다."
NO_RUN_SELECTION = "실행을 고르면 어떻게 끝났는지 볼 수 있습니다."
SCAN_LABELS = {"quick": "새 Like만 빠르게 확인", "full": "전체 목록 확인", "retry": "사라진 파일 다시 받기"}
MISSING_HELP = ("저장한 뒤 사라진 파일입니다. 필요하면 ‘다시 받기’, 일부러 지웠다면 ‘다시 받지 않기’를 누르세요. "
                "다른 폴더로 옮긴 파일도 여기 나옵니다.")
ALL_AUTHORS = "작성자 전체"
FILE_HINT = "끌어서 고르거나 ‘여러 개 선택’을 켜고 클릭하세요."
SAMPLE_HINT = "작성자를 고르면 최근 게시물 사진을 보여 드립니다."
_HANDLE = re.compile(r"[A-Za-z0-9_]{1,15}")
DELETION_LABELS = {"pending": "처리 중", "recycled": "휴지통으로 보냄", "failed": "실패",
                   "cancelled": "취소됨", "interrupted": "중단된 처리"}


def format_bytes(value: int | float | None) -> str:
    if value is None:
        return "알 수 없음"
    size = max(0, float(value))
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:,.1f} {unit}" if unit != "B" else f"{size:,.0f} B"
        size /= 1024
    return ""


def format_date(value: str | None) -> str:
    if not value:
        return "기록 없음"
    try:
        return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return "기록 없음"


def format_short(value: str | None) -> str:
    if not value:
        return "–"
    try:
        return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return "–"


def _publish(location):
    """Refresh the media folder's copy of the records after a local change."""
    if isinstance(location, StoragePaths):
        try:
            publish_snapshot(location)
        except (MigrationError, OSError, sqlite3.Error, ValueError):
            pass   # the next save writes it again


def _finish_local_change(store, location, output_guards, account_guard):
    """Finish local writes and release every lock before reporting completion."""
    failed = False
    if store is not None:
        try:
            store.close()
            _publish(location)
        except Exception:
            failed = True
    for guard in reversed(output_guards):
        try:
            guard.close()
        except Exception:
            failed = True
    if account_guard is not None:
        try:
            account_guard.close()
        except Exception:
            failed = True
    return not failed


def liked_period(row: dict) -> str:
    """When the tweet was liked, as far as the app could tell.

    X does not report Like times; runs record when a Like was first seen and,
    for new Likes, the previous complete run before which it did not exist.
    """
    after, before = format_short(row.get("liked_after")), format_short(row.get("liked_before"))
    if before == "–":
        return "기록 전 (다음 저장 실행부터 기록)"
    if after == "–":
        return f"{before} 이전"
    return f"{after} ~ {before} 사이"


def tweet_text(row: dict) -> str:
    if row.get("content") is None:
        return "기록 전 (다음 저장 실행부터 기록)"
    text = " ".join(filter(None, (row.get("content") or "", row.get("hashtags") or "")))
    return text or "(글 없음)"


def format_day(value: str | None) -> str:
    try:
        return datetime.fromisoformat(value).strftime("%Y-%m-%d") if value else "–"
    except (ValueError, TypeError):
        return "–"


class HistoryWindow(tk.Toplevel):
    def __init__(self, parent, get_location, is_downloading=lambda: False,
                 get_gif_options=lambda: ("original", 200), retry_missing=None):
        super().__init__(parent)
        theme.apply_style(self)
        self.title("XlikeSaver · 라이브러리")
        width = min(1500, max(1, self.winfo_screenwidth() - 80))
        height = min(1100, max(1, self.winfo_screenheight() - 100))
        self.geometry(f"{width}x{height}")
        self.minsize(min(760, width), min(640, height))
        self.configure(bg=theme.BG)
        # Keep the library a normal window: on Windows a transient window
        # loses its minimize/maximize controls even when it is resizable.
        self.resizable(True, True)
        self.get_location = get_location
        self.is_downloading = is_downloading
        # (GIF size preset, media size limit in MB) chosen in save options.
        self.get_gif_options = get_gif_options
        # Starts the main window's download of chosen missing files.
        self.retry_missing = retry_missing
        self.page = 0
        self.displayed_page = 0
        self.has_next_page = False
        self.total_pages = 0
        self.run_page = 0
        self.displayed_run_page = 0
        self.run_total_pages = 0
        self.displayed_page_size = PAGE_SIZE
        self.busy = False
        self.metadata_active = False
        self.choice_active = False
        self.delete_active = False
        # A record repair replaces the catalog file; it is finished, never cut off.
        self.repair_active = False
        # 'Select several' mode: a plain click adds or removes one file.
        self.select_several = False
        self._tree_drag = None
        self._tree_scroll = None
        self._sample_token = 0
        self._sample_author = None
        self._sample_results = queue.Queue()
        self.sample_rows = {}
        self._context_menu = None
        # How long the last full catalog load took; it paces refreshes during a save.
        self.last_load_seconds = 0.0
        self._load_started = None
        self.delete_stop = threading.Event()
        self.metadata_stop = threading.Event()
        self.pending = False
        self.pending_import = False
        self.generation = 0
        self.closing = False
        self._destroyed = False
        self._poll_timer = None
        self._pager_layout_timer = None
        self._pagers = []
        self._preview_token = 0
        self._preview_key = None
        self._preview_image = None
        self._preview_results = queue.Queue(maxsize=2)
        self._preview_loader = PreviewLoader(self._preview_results)
        self._preview_side = None
        self._filter_open = False
        self._stats_stacked = None
        self._toolbar_layout = None
        self._tools_window = None
        self.loaded_locations = set()
        self.results = queue.Queue()
        self.rows = {}
        self.run_rows = {}
        self.deletion_rows = {}
        self._delete_next_identity = None
        self._delete_select_first = False
        self.current_account_dir = None
        self.current_account = ""
        self.current_storage = None
        self.current_summary = {}
        self.completion_text = ""
        self.summary_text = tk.StringVar(self, value="기록을 불러오는 중…")
        self.tile_values = {key: tk.StringVar(self, value="–") for key in TILES}
        self.date_text = tk.StringVar(self)
        self.location_text = tk.StringVar(self)
        self.status_text = tk.StringVar(self)
        self.table_note = tk.StringVar(self, value="기록을 불러오는 중…")
        self.selection_text = tk.StringVar(self, value=NO_SELECTION)
        self.file_selection_text = tk.StringVar(self, value=FILE_HINT)
        self.author_choice = tk.StringVar(self, value=ALL_AUTHORS)
        self.author_query = tk.StringVar(self)
        self.sample_text = tk.StringVar(self, value=SAMPLE_HINT)
        self.media_filter = tk.StringVar(self, value="전체")
        self.query = tk.StringVar(self)
        self.order = tk.StringVar(self, value=ORDER_LABELS["saved"])
        self.period_field = tk.StringVar(self, value=PERIOD_LABELS["posted"])
        self.period_from = tk.StringVar(self)
        self.period_to = tk.StringVar(self)
        self.applied_period = None
        self.author_filter = ""
        self.view_mode = "table"
        self.month_field = tk.StringVar(self, value=MONTH_FIELDS["saved"])
        self.month_note = tk.StringVar(self, value=MONTH_NOTES["saved"])
        self.stats = {}
        self.stats_authors = {}
        self._authors_stale = False
        self.page_text = tk.StringVar(self)
        self.run_page_text = tk.StringVar(self)
        self.page_size = tk.StringVar(self, value=str(PAGE_SIZE))
        self.page_input = tk.StringVar(self, value="1")
        self.run_page_input = tk.StringVar(self, value="1")
        self.missing_page = 0
        self.missing_total_pages = 0
        self.missing_page_text = tk.StringVar(self)
        self.missing_page_input = tk.StringVar(self, value="1")
        self.deletion_page = 0
        self.displayed_deletion_page = 0
        self.deletion_total_pages = 0
        self.deletion_page_text = tk.StringVar(self)
        self.deletion_page_input = tk.StringVar(self, value="1")
        self.deletion_note = tk.StringVar(self, value="아직 삭제 기록이 없습니다.")
        self.deletion_detail = tk.StringVar(self, value="기록을 선택하면 원래 위치와 처리 결과를 보여 드립니다.")
        self.preview_note = tk.StringVar(self, value="파일을 고르면 미리보기가 나옵니다.")
        self.media_counts = tk.StringVar(self)
        self.missing_items = []
        self.missing_rows = {}
        self.missing_text = tk.StringVar(self)
        self.missing_note = tk.StringVar(self)
        self.show_ignored = tk.BooleanVar(self, value=False)
        self._build()
        for kind, tree in (("files", self.tree), ("authors", self.author_tree), ("runs", self.run_tree),
                           ("missing", self.missing_tree), ("deletions", self.deletion_tree)):
            tree.bind("<Button-3>", lambda event, kind=kind: self._tree_context(event, kind))
        self.wrapping_labels = []

        widgets = list(self.winfo_children())
        while widgets:
            widget = widgets.pop()
            if (isinstance(widget, ttk.Label) and int(widget.cget("wraplength") or 0)
                    and not str(widget).startswith(str(self.preview_card) + ".")):
                self.wrapping_labels.append(widget)
            widgets.extend(widget.winfo_children())
        self.bind("<Configure>", self._resize_text, add="+")
        for key in ("Prior", "Next", "Home", "End"):
            self.bind("<" + key + ">", self._page_key, add="+")
        self.bind("<Control-f>", self.focus_search, add="+")
        self.bind("<Control-a>", self._file_select_all_key, add="+")
        self.bind("<Delete>", self._file_delete_key, add="+")
        self.bind("<Escape>", self._escape, add="+")
        self.protocol("WM_DELETE_WINDOW", self.close)
        self._poll_timer = self.after(100, self._poll)
        self.refresh(import_legacy=True)

    def _build(self):
        self.content_view = theme.ScrollableContent(self, padding=(24, 20))
        self.content_view.pack(fill="both", expand=True)
        body = self.content_view.content
        body.columnconfigure(0, weight=1)
        body.rowconfigure(4, weight=1)
        heading = ttk.Frame(body)
        heading.grid(row=0, column=0, sticky="ew")
        ttk.Label(heading, text="라이브러리", style="PageTitle.TLabel").pack(side="left")
        self.refresh_button = ttk.Button(heading, text="새로고침", command=lambda: self.refresh(import_legacy=True))
        self.refresh_button.pack(side="right")
        ttk.Button(heading, text="기록 위치", style="Ghost.TButton", command=self.show_locations).pack(side="right", padx=8)
        self.source_stop_button = ttk.Button(heading, text="출처·작성자 기록 중단", command=self.stop_metadata, state="disabled")
        self.repair_button = ttk.Button(heading, text="기록 복구", style="Primary.TButton", command=self.repair_records)
        ttk.Label(body, textvariable=self.location_text, style="Muted.TLabel", wraplength=1000).grid(row=1, column=0, sticky="w", pady=(4, 16))

        overview = ttk.Frame(body)
        overview.grid(row=2, column=0, sticky="ew")
        self._tile_frame = overview
        self._tiles = []
        self._tile_keys = ("files", "posts", "bytes")
        self._tile_columns = 3
        for column, key in enumerate(self._tile_keys):
            tile = theme.tile(overview, self.tile_values[key], TILES[key])
            tile.grid(row=0, column=column, sticky="ew", padx=(0 if column == 0 else 10, 0))
            self._tiles.append(tile)
            tile.bind("<Configure>", self._queue_pager_layout)
            for label in tile.winfo_children():
                label.bind("<Configure>", self._queue_pager_layout)
            overview.columnconfigure(column, weight=1, uniform="tile")
        overview.bind("<Configure>", self._queue_pager_layout)
        overview_notes = ttk.Frame(body)
        overview_notes.grid(row=3, column=0, sticky="ew", pady=(10, 16))
        ttk.Label(overview_notes, textvariable=self.media_counts, style="Muted.TLabel", wraplength=1000,
                  justify="left").pack(anchor="w")

        tabs = self.tabs = ttk.Notebook(body)
        tabs.grid(row=4, column=0, sticky="nsew")
        files = ttk.Frame(tabs)
        runs = ttk.Frame(tabs)
        stats = self.stats_view = ttk.Frame(tabs)
        missing = self.missing_view = ttk.Frame(tabs)
        deletions = self.deletion_view = ttk.Frame(tabs)
        tabs.add(files, text="파일")
        tabs.add(runs, text="실행 기록")
        tabs.add(stats, text="통계")
        tabs.add(missing, text="사라진 파일")
        tabs.add(deletions, text="삭제 기록")
        tabs.bind("<<NotebookTabChanged>>", self._reset_scroll, add="+")
        tabs.bind("<<NotebookTabChanged>>", self._queue_pager_layout, add="+")
        tabs.bind("<<NotebookTabChanged>>", self._show_stale_authors, add="+")
        self._file_page_parent = files
        files.columnconfigure(0, weight=1)
        files.rowconfigure(1, weight=1)
        runs.columnconfigure(0, weight=1)
        runs.rowconfigure(0, weight=1)

        filters = ttk.Frame(files)
        filters.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        search = ttk.Frame(filters)
        search.pack(fill="x")
        combo = ttk.Combobox(search, textvariable=self.media_filter, values=list(MEDIA_LABELS.values()),
                             state="readonly", width=9, font=theme.BODY)
        combo.pack(side="left")
        combo.bind("<<ComboboxSelected>>", lambda _: self.search())
        # Type part of a handle and open the list to see only matching authors.
        self.author_combo = ttk.Combobox(search, textvariable=self.author_choice, width=18, font=theme.BODY,
                                         postcommand=self._author_choices)
        self.author_combo.pack(side="left", padx=(8, 0))
        self.author_combo.bind("<<ComboboxSelected>>", lambda _: self._choose_author())
        self.author_combo.bind("<Return>", lambda _: self._choose_author())
        entry = ttk.Entry(search, textvariable=self.query, font=theme.BODY)
        entry.pack(side="left", fill="x", expand=True, padx=(8, 8))
        entry.bind("<Return>", lambda _: self.search())
        self.search_entry = entry
        ttk.Button(search, text="검색", command=self.search).pack(side="left")
        views = ttk.Frame(search)
        views.pack(side="right", padx=(10, 0))
        self.view_buttons = {}
        for mode, label in (("table", "목록"), ("grid", "갤러리")):
            button = ttk.Button(views, text=label, style="Segment.TButton", command=lambda mode=mode: self.set_view(mode))
            button.pack(side="left", padx=(0 if mode == "table" else 4, 0))
            self.view_buttons[mode] = button
        self._update_view_buttons()
        toolbar = self.library_toolbar = ttk.Frame(filters)
        toolbar.pack(fill="x", pady=(8, 0))
        toolbar.columnconfigure(0, weight=1)
        self.sort_controls = ttk.Frame(toolbar)
        self.sort_controls.grid(row=0, column=0, sticky="w")
        self.tool_controls = ttk.Frame(toolbar)
        self.tool_controls.grid(row=0, column=2, sticky="e")
        ttk.Label(self.sort_controls, text="정렬", style="Muted.TLabel").pack(side="left")
        order = ttk.Combobox(self.sort_controls, textvariable=self.order, values=list(ORDER_LABELS.values()),
                             state="readonly", width=16, font=theme.SMALL)
        order.pack(side="left", padx=(6, 10))
        order.bind("<<ComboboxSelected>>", lambda _: self.search())
        self.filter_button = ttk.Button(self.sort_controls, text="기간 필터", style="Compact.TButton", command=self.toggle_filters)
        self.filter_button.pack(side="left")
        self.tools_button = ttk.Button(self.tool_controls, text="파일 관리", style="Compact.TButton", command=self.show_tools)
        self.tools_button.pack(side="right")
        self.export_button = ttk.Button(self.tool_controls, text="CSV 내보내기", style="Compact.TButton", command=self.export)
        self.export_button.pack(side="right", padx=(0, 8))
        self.search_hint = ttk.Label(toolbar, text="작성자 · 파일명 · 본문 검색", style="Muted.TLabel")
        self.search_hint.grid(row=0, column=1, padx=12)
        toolbar.bind("<Configure>", self._queue_pager_layout)

        refine = self.filter_panel = ttk.Frame(filters, style="Card.TFrame", padding=(12, 10))
        date_inputs = ttk.Frame(refine, style="Plain.TFrame")
        date_inputs.pack(fill="x")
        date_actions = ttk.Frame(refine, style="Plain.TFrame")
        date_actions.pack(fill="x", pady=(8, 0))
        ttk.Label(date_inputs, text="기간", style="CardMuted.TLabel").pack(side="left")
        field = ttk.Combobox(date_inputs, textvariable=self.period_field, values=list(PERIOD_LABELS.values()),
                             state="readonly", width=7, font=theme.SMALL)
        field.pack(side="left", padx=6)
        field.bind("<<ComboboxSelected>>", lambda _: self.search() if self.applied_period else None)
        self.period_boxes = []
        for variable in (self.period_from, self.period_to):
            box = ttk.Entry(date_inputs, textvariable=variable, width=11, font=theme.SMALL)
            box.pack(side="left")
            box.bind("<Return>", lambda _: self.search())
            self.period_boxes.append(box)
            if variable is self.period_from:
                ttk.Label(date_inputs, text="→", style="CardMuted.TLabel").pack(side="left", padx=5)
        ttk.Label(date_actions, text="예: 2026-10-06", style="CardMuted.TLabel").pack(side="left")
        ttk.Button(date_actions, text="적용", style="CompactPrimary.TButton", command=self.search).pack(side="right", padx=(8, 0))
        ttk.Button(date_actions, text="초기화", style="Compact.TButton", command=self.clear_period).pack(side="right")

        columns = ("posted", "kind", "author", "filename", "size", "saved")
        self.file_browser = ttk.Frame(files)
        self.file_browser.grid(row=1, column=0, sticky="nsew")
        self.file_browser.columnconfigure(0, weight=1)
        self.file_browser.rowconfigure(0, weight=1)
        self.tree = self._table(self.file_browser, columns, row=0)
        self.tree.configure(selectmode="extended")
        for key, title, width in zip(columns, ("트윗 게시일", "종류", "작성자", "파일명", "크기", "저장한 때"),
                                     (145, 85, 120, 260, 85, 145)):
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, minwidth=60, stretch=key == "filename")
        self.tree.bind("<<TreeviewSelect>>", self.select_record)
        self.tree.bind("<ButtonPress-1>", self._tree_press)
        self.tree.bind("<B1-Motion>", self._tree_motion)
        self.tree.bind("<ButtonRelease-1>", self._tree_release)
        self.tree.bind("<Double-1>", lambda _: self.open_file())
        self.tree.bind("<Return>", lambda _: self.open_file())
        self._file_columns = columns
        self._shown_columns = columns
        self.tree.bind("<Configure>", lambda _: self._layout_columns(), add="+")
        self.table_message = ttk.Label(self.tree.master, textvariable=self.table_note, style="CardMuted.TLabel",
                                       justify="center", font=theme.BODY)
        self.preview_card = ttk.Frame(self.file_browser, style="Card.TFrame", padding=14)
        self.preview_box = ttk.Frame(self.preview_card, style="Plain.TFrame", width=220, height=168)
        self.preview_box.grid_propagate(False)
        self.preview_box.columnconfigure(0, weight=1)
        self.preview_box.rowconfigure(0, weight=1)
        self.preview_label = ttk.Label(self.preview_box, style="CardMuted.TLabel", anchor="center",
                                       text="파일을 선택하세요", font=theme.BODY)
        self.preview_label.grid(row=0, column=0, sticky="nsew")
        self.preview_info = ttk.Frame(self.preview_card, style="Plain.TFrame")
        self.preview_note_label = ttk.Label(self.preview_info, textvariable=self.preview_note, style="CardMuted.TLabel",
                                            wraplength=220, justify="left")
        self.preview_note_label.pack(anchor="w", pady=(5, 0))
        self.preview_info.bind("<Configure>", self._wrap_preview_note, add="+")
        self.preview_actions = ttk.Frame(self.preview_card, style="Plain.TFrame")
        self.open_file_button = ttk.Button(self.preview_actions, text="파일 열기", command=self.open_file, style="Compact.TButton")
        self.open_file_button.grid(row=0, column=0, sticky="ew")
        self.open_tweet_button = ttk.Button(self.preview_actions, text="트윗 열기", command=self.open_tweet, style="Compact.TButton")
        self.open_tweet_button.grid(row=0, column=1, sticky="ew", padx=(6, 0))
        self.author_button = ttk.Button(self.preview_actions, text="이 작성자 파일", command=self.filter_selected_author,
                                       style="Compact.TButton")
        self.author_button.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        for column in (0, 1):
            self.preview_actions.columnconfigure(column, weight=1)
        self.preview_details = ttk.Frame(self.preview_card, style="Plain.TFrame")
        self.preview_details.columnconfigure(0, weight=1)
        self.preview_details.rowconfigure(0, weight=1)
        self.selection_detail = tk.Text(self.preview_details, height=5, width=1, wrap="word", font=theme.SMALL,
                                        background=theme.CARD, foreground=theme.INK, borderwidth=0,
                                        highlightthickness=0, state="disabled")
        self.selection_detail.grid(row=0, column=0, sticky="nsew")
        detail_scroll = ttk.Scrollbar(self.preview_details, command=self.selection_detail.yview)
        detail_scroll.grid(row=0, column=1, sticky="ns")
        self.selection_detail.configure(yscrollcommand=detail_scroll.set)
        self._selection_trace = self.selection_text.trace_add("write", self._update_selection_detail)
        self._update_selection_detail()
        self.file_browser.bind("<Configure>", self._queue_pager_layout)
        self.grid_view = ThumbnailGrid(self.file_browser, on_select=self._select_key,
                                       on_open=lambda key: (self._select_key(key), self.open_file()),
                                       on_selection=self._select_gallery, on_context=self._gallery_context)
        selection_bar = self.file_selection_bar = ttk.Frame(filters)
        selection_bar.pack(fill="x", pady=(10, 0))
        selection_bar.columnconfigure(0, weight=1)
        self.file_selection_label = ttk.Label(selection_bar, textvariable=self.file_selection_text,
                                               style="Muted.TLabel", wraplength=360, justify="left")
        self.file_selection_label.grid(row=0, column=0, sticky="w", padx=(0, 12))
        self.file_selection_actions = ttk.Frame(selection_bar)
        self.file_selection_actions.grid(row=0, column=1, sticky="e")
        self.toggle_button = ttk.Button(self.file_selection_actions, text="여러 개 선택",
                                        command=self.toggle_select_several, style="Compact.TButton")
        self.toggle_button.pack(side="left")
        self.file_clear_button = ttk.Button(self.file_selection_actions, text="선택 해제",
                                            command=self.clear_file_selection, style="Compact.TButton")
        self.file_clear_button.pack(side="left", padx=(8, 0))
        self.file_all_button = ttk.Button(self.file_selection_actions, text="이 페이지 모두 선택",
                                          command=self.select_all_files, style="Compact.TButton")
        self.file_all_button.pack(side="left", padx=(8, 0))
        self.delete_button = ttk.Button(self.file_selection_actions, text="선택한 파일 삭제",
                                        command=self.start_deletion, style="Compact.TButton")
        self.delete_button.pack(side="left", padx=(8, 0))
        selection_bar.bind("<Configure>", self._queue_pager_layout)
        self._pagination(files, row=2)

        # Batch operations have their own quiet dialog, separate from browsing.
        self._tools_window = tk.Toplevel(self)
        self._tools_window.withdraw()
        self._tools_window.title("라이브러리 · 파일 관리")
        self._tools_window.transient(self)
        self._tools_window.configure(background=theme.BG)
        self._tools_window.protocol("WM_DELETE_WINDOW", self._tools_window.withdraw)
        self._tools_window.bind("<Escape>", lambda _: self._tools_window.withdraw())
        tools = theme.card(self._tools_window, fill="both", expand=True, padx=18, pady=18)
        ttk.Label(tools, text="파일 관리", style="CardHeading.TLabel").pack(anchor="w", pady=(0, 10))
        self.backfill_button = ttk.Button(tools, text="파일에 출처·작성자 기록", command=lambda: self._run_tool(self.backfill))
        self.backfill_button.pack(fill="x")
        ttk.Label(tools, text="예전에 받은 파일에도 출처를 남깁니다.", style="CardMuted.TLabel").pack(anchor="w", pady=(6, 18))
        self.convert_button = ttk.Button(tools, text="MP4 GIF를 GIF로", command=lambda: self._run_tool(self.convert_gifs))
        self.convert_button.pack(fill="x")
        ttk.Label(tools, text="기존 MP4 애니메이션을 GIF로 바꿉니다.", style="CardMuted.TLabel").pack(anchor="w", pady=(6, 18))
        ttk.Label(tools, text="X에 요청하지 않는 작업입니다.", style="CardMuted.TLabel").pack(anchor="w")
        ttk.Button(tools, text="닫기", command=self._tools_window.withdraw, style="Compact.TButton").pack(anchor="e", pady=(14, 0))

        run_columns = ("started", "ended", "status", "saved", "skipped", "size", "visited", "last")
        self.run_tree = self._table(runs, run_columns, row=0)
        for key, title, width in zip(run_columns,
                ("시작", "끝", "결과", "새로 저장", "이미 있음", "용량 초과", "확인한 트윗", "마지막 트윗 게시일"),
                (150, 150, 100, 85, 85, 85, 100, 160)):
            self.run_tree.heading(key, text=title)
            self.run_tree.column(key, width=width, minwidth=60, stretch=key == "last")
        self.run_text = tk.StringVar(self, value=NO_RUN_SELECTION)
        self.run_tree.bind("<<TreeviewSelect>>", self.select_run)
        run_details = theme.card(runs)
        self._pagination(runs, row=1, runs=True)
        run_details.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        ttk.Label(run_details, textvariable=self.run_text, style="Card.TLabel", wraplength=1000, justify="left").pack(anchor="w")

        stats.columnconfigure(0, weight=1, uniform="stats")
        stats.columnconfigure(1, weight=1, uniform="stats")
        stats.rowconfigure(0, weight=1)
        ranking = self.ranking_card = ttk.Frame(stats, style="Card.TFrame", padding=(16, 14))
        ranking.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        ranking.columnconfigure(0, weight=1)
        ranking.rowconfigure(3, weight=1)
        ranking_head = ttk.Frame(ranking, style="Plain.TFrame")
        ranking_head.grid(row=0, column=0, columnspan=2, sticky="ew")
        ttk.Label(ranking_head, text="작성자", style="CardHeading.TLabel").pack(side="left")
        author_search = ttk.Entry(ranking_head, textvariable=self.author_query, width=16, font=theme.SMALL)
        author_search.pack(side="right")
        ttk.Label(ranking_head, text="찾기", style="CardMuted.TLabel").pack(side="right", padx=(0, 6))
        self.author_query.trace_add("write", lambda *_: self._fill_authors())
        author_columns = ("rank", "author", "files", "posts", "latest")
        self.author_tree = ttk.Treeview(ranking, columns=author_columns, show="headings", height=4, selectmode="browse")
        for key, title, width in zip(author_columns, ("순위", "작성자", "파일", "트윗", "최근 게시일"), (42, 120, 52, 52, 90)):
            self.author_tree.heading(key, text=title)
            sample = {"rank": "1,000", "author": "@daylight_art", "files": "100,000", "posts": "100,000", "latest": "2026-10-06"}[key]
            width = max(width, int(self.tk.call("font", "measure", theme.SMALL, sample)) + 20)
            self.author_tree.column(key, width=width, minwidth=40, stretch=key == "author",
                                    anchor="w" if key == "author" else "center")
        self.author_tree.grid(row=1, column=0, sticky="nsew", pady=(12, 10))
        self._ranking_need = sum(int(self.author_tree.column(key, "width")) for key in author_columns) + 48
        ranking_scroll = ttk.Scrollbar(ranking, command=self.author_tree.yview)
        ranking_scroll.grid(row=1, column=1, sticky="ns", pady=(12, 10))
        self.author_tree.configure(yscrollcommand=ranking_scroll.set)
        self.author_tree.bind("<Double-1>", lambda _: self.show_ranked_author())
        self.author_tree.bind("<Return>", lambda _: self.show_ranked_author())
        self.author_tree.bind("<<TreeviewSelect>>", self._show_author_samples)
        ttk.Label(ranking, textvariable=self.sample_text, style="CardMuted.TLabel").grid(
            row=2, column=0, columnspan=2, sticky="w")
        self.sample_view = ThumbnailGrid(ranking, on_select=lambda _key: None, on_open=self._open_sample,
                                         on_context=self._sample_context)
        # Keep a thumbnail row visible on short screens; scroll for all eight.
        self.sample_view.canvas.configure(height=TILE[1] + 58)
        self.sample_view.grid(row=3, column=0, columnspan=2, sticky="nsew", pady=(6, 10))
        ttk.Button(ranking, text="이 작성자 파일 전체 보기", style="Compact.TButton",
                   command=self.show_ranked_author).grid(row=4, column=0, sticky="w")
        months = self.months_card = ttk.Frame(stats, style="Card.TFrame", padding=(16, 14))
        months.grid(row=0, column=1, sticky="nsew")
        months.columnconfigure(0, weight=1)
        months.rowconfigure(2, weight=1)
        header = ttk.Frame(months, style="Plain.TFrame")
        header.grid(row=0, column=0, sticky="ew")
        ttk.Label(header, text="월별 흐름", style="CardHeading.TLabel").pack(side="left")
        field = ttk.Combobox(header, textvariable=self.month_field, values=list(MONTH_FIELDS.values()),
                             state="readonly", width=10, font=theme.SMALL)
        field.pack(side="right")
        field.bind("<<ComboboxSelected>>", lambda _: self._draw_months())
        note = ttk.Label(months, textvariable=self.month_note, style="CardMuted.TLabel", justify="left")
        note.grid(row=1, column=0, sticky="ew", pady=(8, 12))
        months.bind("<Configure>", lambda event: note.configure(wraplength=max(200, event.width - 32)), add="+")
        self.month_chart = MonthlyBars(months)
        self.month_chart.grid(row=2, column=0, sticky="nsew")
        self.stats_note = ttk.Label(stats, textvariable=self.date_text, style="Muted.TLabel", wraplength=1000,
                                    justify="left")
        self.stats_note.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        stats.bind("<Configure>", self._queue_pager_layout)
        self._build_missing(missing)
        self._build_deletions(deletions)
        ttk.Label(body, textvariable=self.status_text, style="Muted.TLabel", wraplength=1000,
                  justify="left").grid(row=5, column=0, sticky="w", pady=(12, 0))

    def _build_missing(self, page):
        page.columnconfigure(0, weight=1)
        page.rowconfigure(2, weight=1)
        ttk.Label(page, text=MISSING_HELP, style="Muted.TLabel", wraplength=1000,
                  justify="left").grid(row=0, column=0, sticky="ew")
        bar = ttk.Frame(page)
        bar.grid(row=1, column=0, sticky="ew", pady=(10, 10))
        ttk.Label(bar, textvariable=self.missing_text, style="Muted.TLabel").pack(side="left")
        ttk.Checkbutton(bar, text="숨긴 파일도 보기", variable=self.show_ignored,
                        command=self._filter_missing).pack(side="right")
        columns = ("posted", "kind", "author", "filename", "saved", "text")
        self.missing_tree = tree = self._table(page, columns, row=2)
        tree.configure(selectmode="extended")
        for key, title, width in zip(columns, ("트윗 게시일", "종류", "작성자", "파일명", "저장한 때", "내용"),
                                     (140, 90, 130, 210, 140, 320)):
            tree.heading(key, text=title)
            tree.column(key, width=width, minwidth=60, stretch=key == "text")
        tree.bind("<<TreeviewSelect>>", lambda _: self._update_missing_actions())
        tree.bind("<Double-1>", lambda _: self.open_missing_tweet())
        tree.bind("<Return>", lambda _: self.open_missing_tweet())
        tree.bind("<Control-a>", self.select_all_missing)
        self.missing_message = ttk.Label(tree.master, textvariable=self.missing_note, style="CardMuted.TLabel",
                                         justify="center", font=theme.BODY)
        actions = ttk.Frame(page)
        self._pagination(page, row=3, missing=True)
        actions.grid(row=4, column=0, sticky="ew", pady=(10, 0))
        self.missing_open_button = ttk.Button(actions, text="트윗 열기", style="Compact.TButton",
                                              command=self.open_missing_tweet)
        self.missing_open_button.pack(side="left")
        self.missing_all_button = ttk.Button(actions, text="이 페이지 모두 선택", style="Compact.TButton",
                                             command=self.select_all_missing)
        self.missing_all_button.pack(side="left", padx=(8, 0))
        self.retry_button = ttk.Button(actions, text="다시 받기", style="CompactPrimary.TButton",
                                       command=self.retry_missing_files)
        self.retry_button.pack(side="right")
        self.ignore_button = ttk.Button(actions, text="다시 받지 않기", style="Compact.TButton",
                                        command=lambda: self.choose_missing(True))
        self.ignore_button.pack(side="right", padx=(0, 8))
        self.unignore_button = ttk.Button(actions, text="숨김 해제", style="Compact.TButton",
                                          command=lambda: self.choose_missing(False))
        self.unignore_button.pack(side="right", padx=(0, 8))
        page.bind("<Configure>", self._queue_pager_layout)
        self._show_missing()

    def toggle_filters(self):
        self._filter_open = not self._filter_open
        if self._filter_open:
            self.filter_panel.pack(fill="x", pady=(8, 0))
            self.period_boxes[0].focus_set()
        else:
            self.filter_panel.pack_forget()
        self.filter_button.configure(text="기간 필터 닫기" if self._filter_open else
                                     "기간 필터 · 적용 중" if self.applied_period else "기간 필터")

    def _build_deletions(self, page):
        page.columnconfigure(0, weight=1)
        page.rowconfigure(1, weight=1)
        ttk.Label(page, text="휴지통으로 보낸 파일의 처리 기록입니다. 파일 복원은 Windows 휴지통에서 하세요.",
                  style="Muted.TLabel", wraplength=1000, justify="left").grid(row=0, column=0, sticky="ew", pady=(0, 10))
        columns = ("finished", "status", "filename", "author", "size", "keep")
        self.deletion_tree = tree = self._table(page, columns, row=1)
        for key, title, width in zip(columns, ("처리한 때", "결과", "파일명", "작성자", "크기", "처리 당시 설정"),
                                     (150, 140, 280, 140, 100, 150)):
            tree.heading(key, text=title)
            tree.column(key, width=width, minwidth=65, stretch=key == "filename")
        tree.bind("<<TreeviewSelect>>", self.select_deletion)
        tree.bind("<Double-1>", lambda _: self.open_deletion_tweet())
        tree.bind("<Return>", lambda _: self.open_deletion_tweet())
        self.deletion_message = ttk.Label(tree.master, textvariable=self.deletion_note, style="CardMuted.TLabel",
                                          font=theme.BODY, justify="center")
        self._pagination(page, row=2, deletions=True)
        details = theme.card(page)
        details.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        ttk.Label(details, textvariable=self.deletion_detail, style="Card.TLabel", wraplength=1000,
                  justify="left").pack(anchor="w")
        self.deletion_open_button = ttk.Button(details, text="트윗 열기", command=self.open_deletion_tweet,
                                                style="Compact.TButton", state="disabled")
        self.deletion_open_button.pack(anchor="e", pady=(8, 0))
        page.bind("<Configure>", self._queue_pager_layout)

    def _show_deletions(self, paging):
        selection = self.deletion_tree.selection()
        selected = self.deletion_rows.get(selection[0]) if selection else None
        selected_id = selected.get("id") if selected else None
        self.deletion_tree.delete(*self.deletion_tree.get_children())
        self.deletion_rows.clear()
        for index, row in enumerate(paging.get("rows", [])):
            key = str(index)
            self.deletion_rows[key] = row
            self.deletion_tree.insert("", "end", iid=key, values=(
                format_short(row.get("finished_at") or row.get("created_at")),
                DELETION_LABELS.get(row.get("status"), "기록 없음"),
                row.get("filename") or Path(row.get("path") or "").name,
                "@" + row["author"] if row.get("author") else "–", format_bytes(row.get("filesize")),
                ("다시 받지 않음" if row.get("keep_deleted") else "다시 받기 가능")
                if row.get("status") == "recycled" else
                "확인 전" if row.get("status") == "pending" else
                "확인 필요" if row.get("status") == "interrupted" else "변경 없음",
            ))
            if selected_id is not None and row.get("id") == selected_id:
                self.deletion_tree.selection_set(key)
                self.deletion_tree.focus(key)
        total = int(paging.get("total", 0) or 0)
        self.displayed_deletion_page = self.deletion_page = int(paging.get("page", 0) or 0)
        self.deletion_total_pages = int(paging.get("pages", 0) or 0)
        self.deletion_page_text.set(f"처리 기록 {total:,}개 · {self.deletion_page + 1 if total else 0} / {self.deletion_total_pages:,}페이지")
        self.deletion_page_input.set(str(self.deletion_page + 1))
        self.tabs.tab(self.deletion_view, text=f"삭제 기록 {total:,}" if total else "삭제 기록")
        if self.deletion_rows:
            self.deletion_message.place_forget()
        else:
            self.deletion_message.place(relx=0.5, rely=0.5, anchor="center")
        self.select_deletion()

    def select_deletion(self, _=None):
        selection = self.deletion_tree.selection()
        row = self.deletion_rows.get(selection[0]) if selection else None
        valid = row is not None and str(row.get("tweet_id", "")).isdigit()
        self.deletion_open_button.configure(state="normal" if valid else "disabled")
        if row is None:
            self.deletion_detail.set("기록을 선택하면 원래 위치와 처리 결과를 보여 드립니다.")
            return
        self.deletion_detail.set(
            f"{DELETION_LABELS.get(row.get('status'), '기록 없음')} · {format_short(row.get('finished_at') or row.get('created_at'))}\n"
            f"원래 위치  {row.get('path') or '기록 없음'}\n"
            f"트윗  https://x.com/i/web/status/{row.get('tweet_id', '')}\n"
            f"요청한 설정  {'이 계정에서 다시 받지 않기' if row.get('keep_deleted') else '다음 저장에서 다시 받기 가능'}\n"
            + (row.get("message") or "")
        )

    def open_deletion_tweet(self):
        selection = self.deletion_tree.selection()
        row = self.deletion_rows.get(selection[0]) if selection else None
        if row and str(row.get("tweet_id", "")).isdigit():
            webbrowser.open(f"https://x.com/i/web/status/{row['tweet_id']}")

    def _reset_scroll(self, _=None):
        self.content_view.canvas.yview_moveto(0)

    def focus_search(self, _=None):
        self.tabs.select(0)
        self.search_entry.focus_set()
        self.search_entry.selection_range(0, "end")
        return "break"

    def _escape(self, _=None):
        if self._filter_open:
            self.toggle_filters()
            self.filter_button.focus_set()
            return "break"

    def show_tools(self):
        if self._tools_window is None:
            return
        self._tools_window.update_idletasks()
        width, height = max(420, self._tools_window.winfo_reqwidth()), self._tools_window.winfo_reqheight()
        x = self.winfo_rootx() + max(0, (self.winfo_width() - width) // 2)
        y = self.winfo_rooty() + max(0, (self.winfo_height() - height) // 2)
        self._tools_window.geometry(f"{width}x{height}+{x}+{y}")
        self._tools_window.deiconify()
        self._tools_window.lift()
        self.backfill_button.focus_set()

    def _run_tool(self, callback):
        self._tools_window.withdraw()
        callback()

    def _pagination(self, parent, *, row, runs=False, missing=False, deletions=False):
        bar = ttk.Frame(parent)
        bar.grid(row=row, column=0, sticky="ew", pady=(8, 0))
        bar.columnconfigure(0, weight=1)
        info = ttk.Frame(bar)
        info.grid(row=0, column=0, sticky="ew", padx=(0, 12))
        navigation = ttk.Frame(bar)
        navigation.grid(row=0, column=1, sticky="e")
        arrows = ttk.Frame(navigation)
        arrows.grid(row=0, column=0, sticky="w")
        jump_box = ttk.Frame(navigation)
        jump_box.grid(row=0, column=1, sticky="e", padx=(12, 0))
        prefix = "deletion_" if deletions else "missing_" if missing else "run_" if runs else ""
        text = getattr(self, prefix + "page_text")
        value = getattr(self, prefix + "page_input")
        ttk.Label(info, textvariable=text, style="Muted.TLabel").pack(side="left")
        ttk.Label(info, text="개씩", style="Muted.TLabel").pack(side="right")
        size = ttk.Combobox(info, textvariable=self.page_size, values=[str(v) for v in PAGE_SIZES],
                            state="readonly", width=4, font=theme.SMALL)
        size.pack(side="right", padx=(8, 4))
        size.bind("<<ComboboxSelected>>", lambda _: self.change_page_size())
        buttons = {}
        for column, (name, caption, destination) in enumerate(
                (("first", "처음", 0), ("previous", "이전", -1), ("next", "다음", 1), ("last", "마지막", None))):
            if name in {"first", "last"}:
                callback = lambda dest=destination: self.go_page(dest, runs=runs, missing=missing, deletions=deletions)
            else:
                callback = lambda amount=destination: self.change_page(amount, runs=runs, missing=missing, deletions=deletions)
            button = ttk.Button(arrows, text=caption, command=callback, width=4 if name != "last" else 5, style="Compact.TButton")
            button.grid(row=0, column=column if column < 2 else column + 5, padx=2)
            buttons[name] = button
        page_buttons = []
        for index in range(5):
            button = ttk.Button(arrows, width=2, style="Compact.TButton")
            button.grid(row=0, column=index + 2, padx=2)
            button.grid_remove()
            page_buttons.append(button)
        ttk.Label(jump_box, text="페이지", style="Muted.TLabel").grid(row=0, column=0, padx=(0, 4))
        entry = ttk.Entry(jump_box, textvariable=value, width=4, justify="center", font=theme.SMALL)
        entry.grid(row=0, column=1, padx=(0, 4))
        entry.bind("<Return>", lambda _: self.jump_page(runs=runs, missing=missing, deletions=deletions))
        jump = ttk.Button(jump_box, text="이동", command=lambda: self.jump_page(runs=runs, missing=missing, deletions=deletions), width=4, style="Compact.TButton")
        jump.grid(row=0, column=2)
        navigation._arrows = arrows
        navigation._jump_box = jump_box
        self._pagers.append((bar, info, navigation))
        bar.bind("<Configure>", self._queue_pager_layout)
        for name, button in buttons.items():
            setattr(self, prefix + name + "_button", button)
        setattr(self, prefix + "page_entry", entry)
        setattr(self, prefix + "jump_button", jump)
        setattr(self, prefix + "size_combo", size)
        setattr(self, prefix + "page_buttons", page_buttons)

    def _queue_pager_layout(self, _=None):
        if self._pager_layout_timer is None and not self._destroyed:
            self._pager_layout_timer = self.after_idle(self._layout_pagers)

    def _layout_pagers(self):
        self._pager_layout_timer = None
        self._layout_tiles()
        self._layout_preview()
        self._layout_stats()
        toolbar = self.library_toolbar
        width = toolbar.winfo_width()
        if width > 1:
            core = self.sort_controls.winfo_reqwidth() + self.tool_controls.winfo_reqwidth() + 12
            folded = core > width
            hint = not folded and core + self.search_hint.winfo_reqwidth() + 24 <= width
            if (folded, hint) != self._toolbar_layout:
                self._toolbar_layout = folded, hint
                self.tool_controls.grid_configure(row=1 if folded else 0, column=0 if folded else 2,
                                                   columnspan=3 if folded else 1, pady=(8 if folded else 0, 0))
                if hint:
                    self.search_hint.grid()
                else:
                    self.search_hint.grid_remove()
        selection_bar = self.file_selection_bar
        width = selection_bar.winfo_width()
        if width > 1:
            actions_width = self.file_selection_actions.winfo_reqwidth()
            folded = actions_width + 230 > width
            self.file_selection_label.grid_configure(columnspan=2 if folded else 1)
            self.file_selection_label.configure(wraplength=max(120, width if folded else width - actions_width - 12))
            self.file_selection_actions.grid_configure(row=1 if folded else 0, column=0 if folded else 1,
                                                       columnspan=2 if folded else 1, pady=(6 if folded else 0, 0))
        for bar, info, navigation in self._pagers:
            if bar.winfo_width() <= 1:
                continue
            arrows, jump = navigation._arrows, navigation._jump_box
            navigation_need = arrows.winfo_reqwidth() + jump.winfo_reqwidth() + 12
            folded = info.winfo_reqwidth() + navigation_need + 12 > bar.winfo_width()
            info.grid_configure(columnspan=2 if folded else 1, padx=(0, 0 if folded else 12))
            navigation.grid_configure(row=1 if folded else 0, column=0 if folded else 1,
                                      columnspan=2 if folded else 1, pady=(6 if folded else 0, 0))
            available = bar.winfo_width() if folded else bar.winfo_width() - info.winfo_reqwidth() - 12
            jump_folded = navigation_need > available
            jump.grid_configure(row=1 if jump_folded else 0, column=0 if jump_folded else 1,
                                sticky="e", pady=(6 if jump_folded else 0, 0),
                                padx=(0 if jump_folded else 12, 0))

    def _page_width(self, widget):
        """A widget's width, or the tabs' width while its page is hidden.

        A hidden page keeps the width it last had, while the notebook asks
        for its tallest page. Laying it out for the current width keeps a
        narrow, stacked page from making every other tab scroll.
        """
        return widget.winfo_width() if widget.winfo_ismapped() else self.tabs.winfo_width()

    def _layout_preview(self):
        width = self._page_width(self.file_browser)
        if width <= 1:
            return
        core = sum(self._column_need(key) for key in self._file_columns if key not in self.OPTIONAL_COLUMNS)
        scroll = next(child.winfo_reqwidth() for child in self.tree.master.winfo_children()
                      if isinstance(child, ttk.Scrollbar) and str(child.cget("orient")) == "vertical")
        preview = max(self.preview_box.winfo_reqwidth(), self.preview_actions.winfo_reqwidth()) + 28
        side = width >= core + scroll + preview + 16
        if side == self._preview_side:
            return
        self._preview_side = side
        self.preview_card.grid(row=0 if side else 2, column=1 if side else 0,
                               sticky="nsew" if side else "ew",
                               padx=(12 if side else 0, 0), pady=(0 if side else 8, 0))
        self.preview_card.columnconfigure(0, weight=1 if side else 0)
        self.preview_card.columnconfigure(1, weight=0 if side else 1)
        self.preview_card.rowconfigure(2, weight=1 if side else 0)
        self.preview_box.grid(row=0, column=0, rowspan=1 if side else 3, sticky="n")
        self.preview_info.grid(row=1 if side else 0, column=0 if side else 1, sticky="ew",
                               padx=(0 if side else 16, 0), pady=(10 if side else 0, 0))
        self.preview_details.grid(row=2 if side else 1, column=0 if side else 1, sticky="nsew",
                                  padx=(0 if side else 16, 0), pady=(10, 10))
        self.preview_actions.grid(row=3 if side else 2, column=0 if side else 1, sticky="ew",
                                  padx=(0 if side else 16, 0))
        self.selection_detail.configure(height=3)
        # Narrow windows put page navigation before the inspector. The next
        # page stays reachable even when the selected file's details scroll.
        if self._pagers:
            bar = self._pagers[0][0]
            bar.grid(in_=self._file_page_parent if side else self.file_browser,
                     row=2 if side else 1, column=0, sticky="ew", pady=(8, 0))

    def _wrap_preview_note(self, event):
        # A label requests a little more than its wrap width. Using its
        # parent's full width fed that request back into the inspector and
        # could make a long note alternate between two layouts forever.
        width = max(120, event.width - 4)
        if self._preview_side is not False:
            width = min(width, max(self.preview_box.winfo_reqwidth(), self.preview_actions.winfo_reqwidth()))
        if int(self.preview_note_label.cget("wraplength")) != width:
            self.preview_note_label.configure(wraplength=width)

    def _layout_stats(self):
        width = self._page_width(self.stats_view)
        if width <= 1:
            return
        stacked = width * 0.5 < self._ranking_need + 12
        if stacked == self._stats_stacked:
            return
        self._stats_stacked = stacked
        self.stats_view.columnconfigure(0, weight=1, uniform="" if stacked else "stats")
        self.stats_view.columnconfigure(1, weight=0 if stacked else 1, uniform="" if stacked else "stats")
        self.stats_view.rowconfigure(1, weight=1 if stacked else 0)
        self.ranking_card.grid_configure(row=0, column=0, padx=(0, 0 if stacked else 12))
        self.months_card.grid_configure(row=1 if stacked else 0, column=0 if stacked else 1,
                                        pady=(12 if stacked else 0, 0))
        self.stats_note.grid_configure(row=2 if stacked else 1, columnspan=1 if stacked else 2)

    # Columns hidden first when the table is narrower than its contents; the
    # selected file's details still show them. A cut-off file name is worse.
    OPTIONAL_COLUMNS = ("saved", "size")
    COLUMN_SAMPLES = {
        "posted": "2026-10-06 22:15 *",
        "kind": "동영상",
        "author": "@WWWWWWWWWWWWWWW",
        "filename": "1840025000000000000_video_1.mp4",
        "size": "1,023.9 GiB",
        "saved": "2026-10-06 22:15",
    }

    def _column_need(self, key):
        style = ttk.Style(self)
        name = str(self.tree.cget("style") or "Treeview")
        body_font = style.lookup(name, "font") or theme.SMALL
        heading_font = style.lookup(name + ".Heading", "font") or theme.BOLD
        value = int(self.tk.call("font", "measure", body_font, self.COLUMN_SAMPLES[key]))
        heading = int(self.tk.call("font", "measure", heading_font, self.tree.heading(key, "text")))
        return max(value, heading) + 24

    def _layout_columns(self):
        # Until the window is laid out, its content takes the table's own
        # requested width, which hiding a column changes. Decide only from the
        # window's actual width, which the table cannot affect.
        available = self.tree.winfo_width()
        if available <= 1 or not self.winfo_viewable() or self.content_view.canvas.winfo_width() <= 1:
            return
        needs = {key: self._column_need(key) for key in self._file_columns}
        for key, needed in needs.items():
            options = {}
            if int(self.tree.column(key, "minwidth")) != needed:
                options["minwidth"] = needed
            width = int(self.tree.column(key, "width"))
            if width < needed or (key != "filename" and width != needed):
                options["width"] = needed
            if options:
                self.tree.column(key, **options)
        shown = list(self._file_columns)
        for key in self.OPTIONAL_COLUMNS:
            if sum(needs[column] for column in shown) + 2 <= available:
                break
            shown.remove(key)
        if tuple(shown) != self._shown_columns:
            self._shown_columns = tuple(shown)
            self.tree.configure(displaycolumns=shown)

    def _layout_tiles(self):
        available = self._tile_frame.winfo_width()
        if available <= 1:
            return
        largest = max(tile.winfo_reqwidth() for tile in self._tiles)
        columns = next((count for count in (3, 2) if count * largest + (count - 1) * 10 <= available), 1)
        if columns == self._tile_columns:
            return
        self._tile_columns = columns
        # Empty columns retain their weight after re-gridding unless explicitly
        # reset; that would steal width from the visible statistics cards.
        for column in range(len(self._tiles)):
            self._tile_frame.columnconfigure(column, weight=1 if column < columns else 0,
                                              uniform="tile" if column < columns else "", minsize=0)
        for index, tile in enumerate(self._tiles):
            row, column = divmod(index, columns)
            tile.grid_configure(row=row, column=column,
                                padx=(10 if column else 0, 0), pady=(10 if row else 0, 0))

    def _resize_text(self, event):
        if event.widget is self:
            width = max(300, self.winfo_width() - 90)
            for label in self.wrapping_labels:
                if int(label.cget("wraplength")) != width:
                    label.configure(wraplength=width)

    @staticmethod
    def _table(parent, columns, *, row):
        """A Treeview inside its own frame, so the frame can never cover it."""
        frame = ttk.Frame(parent)
        frame.grid(row=row, column=0, sticky="nsew")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        tree = ttk.Treeview(frame, columns=columns, show="headings", selectmode="browse", height=3)
        tree.grid(row=0, column=0, sticky="nsew")
        vertical = ttk.Scrollbar(frame, command=tree.yview)
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal = ttk.Scrollbar(frame, orient="horizontal", command=tree.xview)
        horizontal.grid(row=1, column=0, sticky="ew")
        tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        return tree

    def _show_table_note(self, text):
        if text:
            self.table_note.set(text)
            self.table_message.place(relx=0.5, rely=0.5, anchor="center")
        else:
            self.table_message.place_forget()

    def _update_selection_detail(self, *_):
        text = self.selection_text.get()
        if self.selection_detail.get("1.0", "end-1c") == text:
            return
        self.selection_detail.configure(state="normal")
        self.selection_detail.delete("1.0", "end")
        self.selection_detail.insert("1.0", text)
        self.selection_detail.configure(state="disabled")
        self.selection_detail.yview_moveto(0)

    def _update_controls(self):
        ready = not self.busy and self.current_storage is not None
        self.refresh_button.configure(state="disabled" if self.busy else "normal")
        self.tools_button.configure(state="normal" if ready else "disabled")
        for runs in (False, True):
            prefix = "run_" if runs else ""
            page = self.run_page if runs else self.page
            displayed = self.displayed_run_page if runs else self.displayed_page
            pages = self.run_total_pages if runs else self.total_pages
            page_ready = ready and page == displayed and self._page_size() == self.displayed_page_size
            self._update_pager(prefix, displayed, pages, page_ready, runs=runs)
        self._queue_pager_layout()
        self.export_button.configure(state="normal" if ready else "disabled")
        self.backfill_button.configure(state="normal" if ready else "disabled")
        mp4_gifs = int(self.current_summary.get("mp4_gifs", 0) or 0)
        self.convert_button.configure(text=f"MP4 GIF {mp4_gifs:,}개를 GIF로" if mp4_gifs else "MP4 GIF를 GIF로",
                                      state="normal" if ready and mp4_gifs else "disabled")
        self._update_missing_actions()
        self._update_file_actions()
        self._update_pager("deletion_", self.displayed_deletion_page, self.deletion_total_pages,
                           ready and self.deletion_page == self.displayed_deletion_page,
                           deletions=True)

    def _update_pager(self, prefix, page, pages, ready, *, runs=False, missing=False, deletions=False):
        for name in ("first", "previous"):
            getattr(self, prefix + name + "_button").configure(state="normal" if ready and page else "disabled")
        for name in ("next", "last"):
            getattr(self, prefix + name + "_button").configure(state="normal" if ready and page + 1 < pages else "disabled")
        for name in ("page_entry", "jump_button"):
            getattr(self, prefix + name).configure(state="normal" if ready and pages else "disabled")
        first = max(0, min(page - 2, pages - 5))
        for index, button in enumerate(getattr(self, prefix + "page_buttons")):
            destination = first + index
            if destination >= pages:
                button.grid_remove()
                continue
            button.configure(text=str(destination + 1), width=max(2, len(str(destination + 1))),
                             style="CompactPrimary.TButton" if destination == page else "Compact.TButton",
                             command=lambda page=destination: self.go_page(page, runs=runs, missing=missing, deletions=deletions),
                             state="normal" if ready else "disabled")
            button.grid()

    def _page_size(self):
        try:
            size = int(self.page_size.get())
        except ValueError:
            return PAGE_SIZE
        return size if size in PAGE_SIZES else PAGE_SIZE

    def _period(self):
        """(field, first day, last day) from the date boxes, or None.

        Accepts 2026-10-01, 2026.10.01 or 20261001; raises ValueError.
        """
        days = []
        for variable in (self.period_from, self.period_to):
            value = variable.get().strip()
            match = re.fullmatch(r"(\d{4})[-./ ]?(\d{1,2})[-./ ]?(\d{1,2})\.?", value)
            if value and not match:
                raise ValueError(value)
            days.append(date(int(match[1]), int(match[2]), int(match[3])) if match else None)
        if not any(days):
            return None
        if all(days) and days[0] > days[1]:
            days.reverse()
        for variable, day in zip((self.period_from, self.period_to), days):
            variable.set(day.isoformat() if day else "")
        field = next(key for key, label in PERIOD_LABELS.items() if label == self.period_field.get())
        return field, *(day.isoformat() if day else "" for day in days)

    def search(self):
        try:
            self.applied_period = self._period()
        except ValueError:
            self.status_text.set("기간은 2026-10-01처럼 연-월-일로 입력해 주세요. 한쪽만 입력해도 됩니다.")
            return
        self.filter_button.configure(text="기간 필터 닫기" if self._filter_open else
                                     "기간 필터 · 적용 중" if self.applied_period else "기간 필터")
        self.page = 0
        self.refresh(view_only=True)

    def clear_period(self):
        self.period_from.set("")
        self.period_to.set("")
        self.filter_button.configure(text="기간 필터 닫기" if self._filter_open else "기간 필터")
        if self.applied_period is not None:
            self.applied_period = None
            self.page = 0
            self.refresh(view_only=True)

    def change_page_size(self):
        self.page = self.run_page = self.missing_page = self.deletion_page = 0
        self.refresh(view_only=True)

    def change_page(self, amount, *, runs=False, missing=False, deletions=False):
        current = self.deletion_page if deletions else self.missing_page if missing else self.run_page if runs else self.page
        self.go_page(current + amount, runs=runs, missing=missing, deletions=deletions)

    def go_page(self, page, *, runs=False, missing=False, deletions=False):
        if deletions:
            if self.busy or self.deletion_page != self.displayed_deletion_page or not self.deletion_total_pages:
                return
            page = self.deletion_total_pages - 1 if page is None else min(max(0, page), self.deletion_total_pages - 1)
            if page != self.deletion_page:
                self.deletion_page = page
                self.refresh(view_only=True)
            return
        if missing:
            if self.busy or not self.missing_total_pages:
                return
            page = self.missing_total_pages - 1 if page is None else min(max(0, page), self.missing_total_pages - 1)
            if page != self.missing_page:
                self.missing_page = page
                self.missing_tree.selection_remove(self.missing_tree.selection())
                self._show_missing()
            return
        current = self.run_page if runs else self.page
        displayed = self.displayed_run_page if runs else self.displayed_page
        pages = self.run_total_pages if runs else self.total_pages
        if self.busy or current != displayed or self._page_size() != self.displayed_page_size or not pages:
            return
        page = pages - 1 if page is None else min(max(0, page), pages - 1)
        if page == current:
            return
        if runs:
            self.run_page = page
        else:
            self.page = page
        self.refresh(view_only=True)

    def jump_page(self, *, runs=False, missing=False, deletions=False):
        value = self.deletion_page_input if deletions else self.missing_page_input if missing else self.run_page_input if runs else self.page_input
        current = self.displayed_deletion_page if deletions else self.missing_page if missing else self.displayed_run_page if runs else self.displayed_page
        try:
            page = int(value.get().strip()) - 1
        except ValueError:
            value.set(str(current + 1))
            return
        self.go_page(page, runs=runs, missing=missing, deletions=deletions)
        value.set(str((self.deletion_page if deletions else self.missing_page if missing else self.run_page if runs else self.page) + 1))

    def _page_key(self, event):
        # Keep Home/End and Page Up/Down editing/scrolling behavior in fields
        # and selected-file text. In a table these keys scroll its rows, so
        # pages change there only with Ctrl; other widgets need no modifier.
        widget_class = event.widget.winfo_class()
        if widget_class in {"Entry", "TEntry", "Text", "TCombobox", "Spinbox", "TSpinbox"}:
            return
        if widget_class == "Treeview" and not getattr(event, "state", 0) & 0x4:
            return
        tab = self.tabs.index("current")
        if tab == 2:
            return
        runs, missing, deletions = tab == 1, tab == 3, tab == 4
        if event.keysym == "Prior":
            self.change_page(-1, runs=runs, missing=missing, deletions=deletions)
        elif event.keysym == "Next":
            self.change_page(1, runs=runs, missing=missing, deletions=deletions)
        else:
            self.go_page(0 if event.keysym == "Home" else None, runs=runs, missing=missing, deletions=deletions)
        return "break"

    def refresh(self, *, import_legacy=False, view_only=False):
        """Load the catalog; ``view_only`` is a page, search or order change.

        Only the shown records depend on those, so the catalog-wide totals,
        charts and missing list of the last load are reused instead of being
        rebuilt from every record (seconds in a large library). A deferred
        refresh, a new location or a rescan always rebuilds them.
        """
        if self.busy:
            self.pending = True
            self.pending_import = self.pending_import or import_legacy
            return
        try:
            account, directory = self.get_location()
        except (ValueError, OSError):
            self.current_account_dir = None
            self.current_account = ""
            self.current_storage = None
            self.completion_text = ""
            self.page = self.run_page = self.missing_page = self.deletion_page = 0
            self._delete_next_identity = None
            self._delete_select_first = False
            self.location_text.set("")
            self._display({}, [], [])
            self.status_text.set("먼저 메인 화면에 X 아이디와 저장 폴더를 입력해 주세요.")
            self._show_table_note("메인 화면에 X 아이디와 저장 폴더를 입력하면\n그 폴더에 저장한 기록이 여기에 나옵니다.")
            return
        location = directory
        directory = location.record_dir if isinstance(location, StoragePaths) else Path(location)
        if directory != self.current_account_dir:
            view_only = False
            self.page = self.run_page = self.missing_page = self.deletion_page = 0
            self._delete_next_identity = None
            self._delete_select_first = False
            self.completion_text = ""
            # A media identity or run ID is local to its account catalog.
            # Clear the previous catalog before accepting the new snapshot.
            self._display({}, [], [])
        self.current_account_dir = directory
        self.current_account = account
        self.current_storage = location
        media_dir = location.media_dir if isinstance(location, StoragePaths) else directory
        self.location_text.set(f"@{account} · 저장 폴더 {media_dir}")
        self.busy = True
        self._update_controls()
        self.generation += 1
        generation = self.generation
        kind = next(k for k, v in MEDIA_LABELS.items() if v == self.media_filter.get())
        # The running worker records every file it handles, so a whole-library
        # rescan adds nothing then and only competes with it for the catalog.
        do_import = (import_legacy or directory not in self.loaded_locations) and not self.is_downloading()
        self.status_text.set("기록을 불러오는 중…" if do_import or not import_legacy else
                             "저장 작업 중에는 폴더 재확인을 건너뛰고 지금까지의 기록만 보여 줍니다.")
        if not self.tree.get_children():
            self._show_table_note("기록을 불러오는 중…")
        query = self.query.get().strip()
        if re.fullmatch(r"@[A-Za-z0-9_]{1,15}", query):
            query = query[1:]
        order = next(key for key, label in ORDER_LABELS.items() if label == self.order.get())
        reuse = (dict(self.current_summary) if view_only and not do_import and "files" in self.current_summary
                 and directory in self.loaded_locations else None)
        self._load_started = time.monotonic() if reuse is None and not do_import else None
        threading.Thread(target=partial(HistoryWindow._load_worker, self.results), args=(generation, location, kind, query, self.page, do_import,
                                                 self._page_size(), self.run_page, order, self.applied_period,
                                                 self.author_filter, self.deletion_page, reuse), daemon=True).start()

    def auto_refresh_delay(self) -> int:
        """Milliseconds until a save's next automatic refresh.

        Two seconds normally; a library that takes long to load is refreshed
        less often so it is not rebuilt continuously during a long save.
        """
        return int(min(30.0, max(2.0, self.last_load_seconds * 3)) * 1000)

    @staticmethod
    def _store(location):
        return HistoryStore.for_storage(location) if isinstance(location, StoragePaths) else HistoryStore(location)

    def _load(self, generation, directory, kind, query, page, do_import, page_size=PAGE_SIZE, run_page=0,
              order="saved", period=None, author="", deletion_page=0, summary=None):
        return self._load_worker(self.results, generation, directory, kind, query, page, do_import, page_size, run_page,
                                 order, period, author, deletion_page, summary)

    @staticmethod
    def _load_worker(results, generation, directory, kind, query, page, do_import, page_size=PAGE_SIZE, run_page=0,
                     order="saved", period=None, author="", deletion_page=0, summary=None):
        store = None
        try:
            store = HistoryWindow._store(directory)

            def snapshot(warnings):
                # Use one deduplicated catalog snapshot for all totals/charts.
                # The rescan below verifies files in the background; paging
                # must not trigger another whole-library filesystem scan.
                current = store.overview(verify_files=False, top=None) if summary is None else dict(summary)
                current["warnings"] = warnings
                current["deletion_paging"] = store.deletion_page(deletion_page, page_size)
                files = store.record_page(page, page_size, kind, query, order=order, period=period, author=author,
                                          include_deleted=False)
                runs = store.run_page(run_page, page_size)
                return store.account_dir, current, files["rows"], runs["rows"], files["page"], files, runs

            if do_import:
                # Show what the catalog already holds at once. Re-checking every
                # file of a large library takes seconds, and the table must not
                # look empty meanwhile.
                results.put((generation, "preview", snapshot([])))
                imported = store.import_legacy(refresh=True)
                results.put((generation, "loaded", snapshot(imported.get("warnings", []))))
            else:
                results.put((generation, "loaded", snapshot([])))
        except CatalogDamaged as exc:
            results.put((generation, "damaged", str(exc)))
        except Exception:
            results.put((generation, "error", "기록을 읽지 못했습니다. 저장 폴더 권한과 다른 실행 중인 작업을 확인해 주세요."))
        finally:
            if store is not None:
                store.close()

    def _poll(self):
        if self._destroyed:
            return
        if self._poll_timer is not None:
            # Tcl-only cancellation leaves command ownership with the window.
            # It also keeps direct refresh calls from scheduling duplicate polls.
            self.tk.call("after", "cancel", self._poll_timer)
            self._poll_timer = None
        try:
            while True:
                generation, kind, result = self.results.get_nowait()
                if generation != self.generation:
                    continue
                if kind == "delete-progress":
                    self.status_text.set(result.get("message") or
                                         f"휴지통으로 보내는 중 · 완료 {result.get('recycled', 0):,}개 / 실패 {result.get('failed', 0):,}개")
                    continue
                if kind in {"metadata-progress", "convert-progress"}:
                    self.status_text.set(result)
                    continue
                if kind == "preview":
                    if self.pending:
                        continue
                    self._display(*result[1:], scanning=True)
                    self.status_text.set("폴더의 파일을 다시 확인하는 중입니다… 그동안 지금까지의 기록을 먼저 보여 드립니다.")
                    continue
                self.busy = False
                self.choice_active = self.repair_active = False
                if self.metadata_active or self.delete_active:
                    self.metadata_active = False
                    self.delete_active = False
                    self.source_stop_button.configure(state="disabled")
                    self.source_stop_button.pack_forget()
                if self.closing:
                    self.destroy()
                    return
                if kind != "damaged":
                    self.repair_button.pack_forget()
                if kind == "damaged":
                    self._display({}, [], [])
                    self._show_table_note("기록 DB 파일이 손상되어 읽을 수 없습니다.\n위의 ‘기록 복구’를 눌러 주세요.")
                    self.status_text.set(result)
                    self.repair_button.configure(state="normal")
                    self.repair_button.pack(side="right", padx=(0, 8))
                elif kind == "loaded":
                    directory, *snapshot = result
                    self.loaded_locations.add(directory)
                    if self._load_started is not None:
                        self.last_load_seconds = time.monotonic() - self._load_started
                        self._load_started = None
                    if not self.pending:
                        self._display(*snapshot)
                elif kind == "deleted":
                    self._selection_after_deletion(result.get("paths", []))
                    self.deletion_page = 0
                    self.completion_text = result.get("message") or "파일 처리를 마쳤습니다. ‘삭제 기록’에서 결과를 확인하세요."
                    self.status_text.set(self.completion_text)
                else:
                    self.status_text.set(result)
                    if kind in {"backfilled", "converted", "repaired", "missing-choice"}:
                        self.completion_text = result
                    elif kind == "error" and not self.tree.get_children():
                        self._show_table_note("기록을 읽지 못했습니다.\n아래 안내를 확인해 주세요.")
                self._update_controls()
                if self.pending:
                    self.pending = False
                    do_import, self.pending_import = self.pending_import, False
                    self.refresh(import_legacy=do_import)
                elif kind in {"backfilled", "converted", "missing-choice", "deleted"}:
                    self.refresh()
                elif kind == "repaired":
                    self.loaded_locations.discard(self.current_account_dir)
                    self.refresh(import_legacy=True)
        except queue.Empty:
            pass
        self._poll_previews()
        self._poll_samples()
        if self.view_mode == "grid":
            self.grid_view.poll()
        if self.winfo_exists():
            self._poll_timer = self.after(120, self._poll)

    def _display(self, summary, records, runs, page=None, file_paging=None, run_paging=None, *, scanning=False):
        self.current_summary = summary
        files = summary.get("files", 0) or 0
        values = {"files": files, "posts": summary.get("posts", 0) or 0, "photo": summary.get("photo", 0) or 0,
                  "video": summary.get("video", 0) or 0, "animated_gif": summary.get("animated_gif", 0) or 0}
        for key, value in values.items():
            self.tile_values[key].set(f"{value:,}")
        self.tile_values["bytes"].set(format_bytes(summary.get("bytes", 0)))
        self.summary_text.set(
            f"현재 파일 {files:,}개 · 기록된 트윗 {values['posts']:,}개 · 사진 {values['photo']:,}개 / 동영상 {values['video']:,}개 / "
            f"움직이는 GIF {values['animated_gif']:,}개 · 확인된 용량 {format_bytes(summary.get('bytes', 0))}")
        self.media_counts.set(f"사진 {values['photo']:,} · 동영상 {values['video']:,} · GIF {values['animated_gif']:,}")
        oldest, newest = summary.get("oldest_posted_at"), summary.get("newest_posted_at")
        notes = []
        if summary.get("estimated_dates", 0):
            notes.append("* 표시는 트윗 번호로 추정한 날짜")
        if summary.get("missing_files", 0):
            notes.append(f"사라진 파일 {summary['missing_files']:,}개 (‘사라진 파일’ 탭)")
        range_text = (f"트윗 게시일 {format_day(oldest)} → {format_day(newest)} · 기록에 남아 있는 범위"
                      if oldest else "기록된 트윗의 게시일: 아직 없음")
        self.date_text.set(range_text + ("  ·  " + " · ".join(notes) if notes else ""))
        if oldest:
            self.media_counts.set(self.media_counts.get() + f"    ·    게시일 {format_day(oldest)} → {format_day(newest)}")
        selected = self.selected()
        selected_identity = self._record_identity(selected) if selected else None
        selected_identities = {self._record_identity(self.rows[key]) for key in self.tree.selection() if key in self.rows}
        if self._delete_next_identity is not None:
            selected_identity = self._delete_next_identity
            selected_identities = {selected_identity}
        self.tree.delete(*self.tree.get_children())
        self.rows.clear()
        page_size = file_paging["page_size"] if file_paging else self._page_size()
        for index, row in enumerate(records[:page_size]):
            key = str(index)
            self.rows[key] = row
            posted = format_short(row.get("posted_at"))
            if row.get("posted_source") == "id_estimate":
                posted += " *"
            filename = row.get("filename") or (Path(row["path"]).name if row.get("path") else f"{row['tweet_id']}_{row['num']} (위치 없음)")
            author = "@" + row["author"] if row.get("author") else "–"
            media_label = "GIF" if row.get("media_type") == "animated_gif" else MEDIA_LABELS.get(row.get("media_type"), "알 수 없음")
            self.tree.insert("", "end", iid=key, values=(posted, media_label, author, filename,
                                                         format_bytes(row.get("filesize")), format_short(row.get("saved_at"))))
        selection = [key for key, row in self.rows.items() if self._record_identity(row) in selected_identities]
        if not selection and self._delete_select_first and self.rows:
            selection = [next(iter(self.rows))]
            selected_identity = self._record_identity(self.rows[selection[0]])
        self.tree.selection_set(selection)
        if selection:
            focus = next((key for key in selection if self._record_identity(self.rows[key]) == selected_identity), selection[0])
            self.tree.focus(focus)
            self.tree.see(focus)
        self._delete_next_identity = None
        self._delete_select_first = False
        if records:
            self._show_table_note("")
        elif scanning:
            self._show_table_note("폴더의 파일을 확인하는 중입니다…\n확인이 끝나면 저장한 기록을 보여 드립니다.")
        elif summary.get("record_count", files):
            self._show_table_note("찾는 조건에 맞는 파일이 없습니다.")
        else:
            self._show_table_note("아직 저장한 파일이 없습니다.\n메인 화면에서 ‘저장 시작’을 누르면 여기에 쌓입니다.")
        self.displayed_page = self.page if page is None else page
        self.displayed_page_size = page_size
        total = file_paging["total"] if file_paging else len(records)
        self.total_pages = file_paging["pages"] if file_paging else (total + page_size - 1) // page_size
        self.page = self.displayed_page
        self.has_next_page = self.displayed_page + 1 < self.total_pages
        self.page_text.set(f"기록 {total:,}개 · {self.displayed_page + 1 if total else 0} / {self.total_pages:,}페이지")
        self.page_input.set(str(self.displayed_page + 1))
        run_selection = self.run_tree.selection()
        selected_run = self.run_rows.get(run_selection[0]) if run_selection else None
        selected_run_id = selected_run.get("id") if selected_run else None
        self.run_tree.delete(*self.run_tree.get_children())
        self.run_rows.clear()
        for index, row in enumerate(runs):
            key = str(index)
            self.run_rows[key] = row
            self.run_tree.insert("", "end", iid=key, values=(format_short(row.get("started_at")), format_short(row.get("finished_at")),
                                                             STATUS_LABELS.get(row.get("status"), "기록 없음"), row.get("saved", 0),
                                                             row.get("skipped", 0), row.get("size_skipped", 0), row.get("visited", 0),
                                                             format_short(row.get("last_posted_at"))))
            if selected_run_id is not None and row.get("id") == selected_run_id:
                self.run_tree.selection_set(key)
                self.run_tree.focus(key)
                self.run_tree.see(key)
        run_total = run_paging["total"] if run_paging else len(runs)
        self.run_total_pages = run_paging["pages"] if run_paging else (run_total + page_size - 1) // page_size
        self.displayed_run_page = run_paging["page"] if run_paging else self.run_page
        self.run_page = self.displayed_run_page
        self.run_page_text.set(f"총 {run_total:,}회 · {self.displayed_run_page + 1 if run_total else 0} / {self.run_total_pages:,}페이지")
        self.run_page_input.set(str(self.displayed_run_page + 1))
        self._show_deletions(summary.get("deletion_paging") or {})
        self._queue_pager_layout()
        self._update_controls()
        warning = " ".join(summary.get("warnings", []))
        self.status_text.set((self.completion_text + "\n" if self.completion_text else "") + (warning + "\n" if warning else "") +
                             f"출처 기록 {summary.get('embedded_sources', 0):,}개 · 작성자 기록 {summary.get('embedded_authors', 0):,}개")
        self._show_stats(summary.get("stats") or {})
        self.missing_items = summary.get("missing") or []
        self._show_missing()
        if self.view_mode == "grid":
            self._show_grid()
        self.select_record(force=True)
        self.select_run()

    @staticmethod
    def _record_identity(row):
        return tuple(row.get(key) for key in ("folder_mode", "tweet_id", "num", "media_type"))

    def selected(self):
        selection = self.tree.selection()
        focus = self.tree.focus()
        return self.rows.get(focus if focus in selection else selection[0]) if selection else None

    def select_record(self, _=None, *, force=False):
        row = self.selected()
        self.open_file_button.configure(state="normal" if row and row.get("path") else "disabled")
        self.open_tweet_button.configure(state="normal" if row and str(row.get("tweet_id", "")).isdigit() else "disabled")
        self._start_preview(row, force=force)
        if self.view_mode == "grid":
            self.grid_view.select_many(self.tree.selection(), focus=self.tree.focus())
        self._update_file_actions()
        self._update_author_button()
        if row is None:
            self.selection_text.set(NO_SELECTION)
            return
        source = "트윗 번호로 추정" if row.get("posted_source") == "id_estimate" else "X 목록"
        saved = format_short(row.get("saved_at"))
        metadata = METADATA_LABELS.get(row.get("metadata_status"), "출처 기록 전")
        author_status = AUTHOR_METADATA_LABELS.get(row.get("author_metadata_status"), "작성자 기록 전")
        if not row.get("author") and not row.get("author_id"):
            author_status = AUTHOR_METADATA_LABELS["unknown"]
        author = "@" + row["author"] if row.get("author") else "정보 없음"
        if row.get("author_id"):
            author += " (ID " + row["author_id"] + ")"
        self.selection_text.set(
            f"작성자  {author}\n"
            f"내용     {tweet_text(row)}\n"
            f"트윗     {row.get('source_url') or 'https://x.com/i/web/status/' + row['tweet_id']}\n"
            f"저장한 때 {saved if saved != '–' else '알 수 없음'} · 게시일 {format_short(row.get('posted_at'))}\n"
            f"Like한 시기  {liked_period(row)}\n"
            f"파일     {row.get('path') or '위치 미확인 (이전 다운로드 기록만 있음)'}\n"
            f"파일 안 기록: {metadata} · {author_status} · 게시일 출처: {source}")

    def _start_preview(self, row, *, force=False):
        key = (str(self.current_account_dir), row.get("path")) if row else None
        if key == self._preview_key and not force:
            return
        self._preview_key = key
        self._preview_token += 1
        self._preview_image = None
        self.preview_label.configure(image="", text="미리보기")
        if not row:
            self.preview_note.set("파일을 고르면 미리보기가 나옵니다.")
            return
        if not row.get("path") or self.current_storage is None:
            self.preview_note.set("파일 위치가 기록되어 있지 않습니다.")
            return
        self.preview_note.set("미리보기를 불러오는 중…")
        location = self.current_storage

        def permitted(path):
            with closing(HistoryWindow._store(location)) as store:
                return store.is_media_path(path)

        self._preview_loader.request(self._preview_token, row["path"], permitted)

    def _poll_previews(self):
        try:
            while True:
                token, result = self._preview_results.get_nowait()
                if token != self._preview_token:
                    continue
                self.preview_note.set(result.note)
                if result.image is not None:
                    self._preview_image = ImageTk.PhotoImage(result.image, master=self)
                    self.preview_label.configure(image=self._preview_image, text="")
                else:
                    self._preview_image = None
                    self.preview_label.configure(image="", text="미리보기 없음")
        except queue.Empty:
            pass

    def select_run(self, _=None):
        selection = self.run_tree.selection()
        row = self.run_rows.get(selection[0]) if selection else None
        if row:
            scan = SCAN_LABELS.get(row.get("scan_mode"), SCAN_LABELS["full"])
            self.run_text.set(f"{row.get('message') or '실행 중이거나 종료 설명이 없습니다.'}\n확인 방식: {scan} · 마지막으로 확인한 트윗: {row.get('last_tweet_id') or '기록 없음'}")
        else:
            self.run_text.set(NO_RUN_SELECTION)

    def open_file(self):
        row = self.selected()
        if row:
            self._open_path(row.get("path"))

    def _open_path(self, value):
        path = Path(value) if value else None
        permitted = False
        if path is not None and self.current_storage is not None:
            try:
                with closing(self._store(self.current_storage)) as store:
                    permitted = store.is_media_path(path)
            except Exception:
                permitted = False
        if not permitted or path is None or not path.is_file():
            dialogs.showinfo("파일 위치 확인", "파일을 찾지 못했습니다. 이동·삭제되었거나 이전 다운로드 기록에 위치가 남아 있지 않습니다.", parent=self)
            return
        try:
            os.startfile(str(path))
        except OSError:
            dialogs.showerror("파일 열기 실패", "이 파일을 여는 프로그램과 파일 접근 권한을 확인해 주세요.", parent=self)

    def open_tweet(self):
        row = self.selected()
        if row and str(row["tweet_id"]).isdigit():
            webbrowser.open(f"https://x.com/i/web/status/{row['tweet_id']}")

    def show_locations(self):
        if self.current_account_dir is None:
            return
        archives = self.current_summary.get("archivepaths", [])
        archive_text = "\n".join(map(str, archives)) or "아직 다운로드 기록 파일이 없습니다."
        snapshot = (str(self.current_storage.snapshot_dir) if isinstance(self.current_storage, StoragePaths)
                    else "저장 폴더의 .xlikesaver")
        media_dir = self.current_storage.media_dir if isinstance(self.current_storage, StoragePaths) else self.current_account_dir
        dialogs.showinfo("기록 저장 위치", f"저장 폴더:\n{media_dir}\n\n이 PC의 기록·통계:\n{self.current_account_dir / '.xlikesaver-history.sqlite3'}\n\n"
                         f"이 PC의 중복 방지 기록:\n{archive_text}\n\n저장 폴더에 두는 사본:\n{snapshot}\n\n"
                         "기록은 이 PC에 두고, 저장할 때마다 저장 폴더의 .xlikesaver에 사본을 둡니다. "
                         "다른 PC나 새로 설치한 앱은 이 사본에서 기록을 가져옵니다.", parent=self)

    def export(self):
        if self.busy or self.current_account_dir is None:
            return
        destination = filedialog.asksaveasfilename(parent=self, title="전체 저장 기록 CSV 내보내기", defaultextension=".csv", initialfile="XlikeSaver-records.csv", filetypes=[("CSV 파일", "*.csv")])
        if not destination:
            return
        self.busy = True
        self._update_controls()
        self.generation += 1
        self.status_text.set("전체 파일 기록을 CSV로 내보내고 있습니다…")
        threading.Thread(target=partial(HistoryWindow._export_worker, self.results), args=(self.generation, self.current_storage, destination), daemon=True).start()

    def backfill(self):
        if self.busy or self.current_account_dir is None:
            return
        if self.is_downloading():
            dialogs.showinfo("다운로드 진행 중", "저장 작업이 끝난 뒤 출처·작성자를 기록해 주세요. 이 작업은 X에 요청하지 않습니다.", parent=self)
            return
        from .metadata import available
        if not available():
            dialogs.showinfo("출처 기록 도구 없음", "배포 파일을 다시 받아주세요. 개발 중이면 setup.ps1을 실행해 주세요.", parent=self)
            return
        account, location, generation = self.current_account, self.current_storage, self.generation
        if not dialogs.askyesno(
                "출처·작성자 기록",
                "예전에 받은 파일 안에 트윗 주소와 작성자를 기록합니다.\n\n"
                "· 화질은 그대로이고 X에 요청하지 않습니다.\n"
                "· 파일을 다시 쓰므로 동기화 폴더라면 다시 올라갑니다.\n"
                "· 이미 기록된 파일은 건너뛰며 언제든 멈출 수 있습니다.\n\n계속할까요?", parent=self):
            return
        if not self._local_context_valid(account, location, generation):
            return
        self._start_local_job("출처·작성자 기록 중단", "출처와 작성자를 기록하는 중입니다… 화질은 그대로이며 X에 요청하지 않습니다.")
        threading.Thread(target=partial(HistoryWindow._backfill_worker, self.results, self.metadata_stop),
                         args=(self.generation, self.current_storage), daemon=True).start()

    def repair_records(self):
        if self.busy or self.current_storage is None:
            return
        if self.is_downloading():
            dialogs.showinfo("저장 진행 중", "저장 작업이 끝난 뒤 기록을 복구해 주세요.", parent=self)
            return
        account, location, generation = self.current_account, self.current_storage, self.generation
        if not dialogs.askyesno(
                "기록 복구",
                "손상된 기록 DB를 고칩니다.\n\n"
                "· 손상된 파일은 먼저 백업해 둡니다.\n"
                "· 읽을 수 있는 기록을 살리고, 폴더를 다시 확인해 빠진 기록을 채웁니다.\n"
                "· 사진·영상과 중복 방지 기록은 건드리지 않습니다.\n\n계속할까요?", parent=self):
            return
        if not self._local_context_valid(account, location, generation):
            return
        self.repair_button.configure(state="disabled")
        self.busy = self.repair_active = True
        self._update_controls()
        self.generation += 1
        self.status_text.set("기록 DB를 복구하는 중입니다…")
        threading.Thread(target=partial(HistoryWindow._repair_worker, self.results),
                         args=(self.generation, self.current_storage), daemon=True).start()

    @staticmethod
    def _repair_worker(results, generation, location):
        from .protection import AccountGuard, ProtectionError
        from .recovery import repair_history
        account_guard = output_guard = None
        kind = "damaged"
        message = ("기록을 복구하지 못했습니다. 손상된 원본은 그대로 두었습니다. "
                   "저장 폴더 권한을 확인하고 다시 시도해 주세요.")
        try:
            record_dir = location.record_dir if isinstance(location, StoragePaths) else Path(location)
            account = location.account if isinstance(location, StoragePaths) else record_dir.name
            if isinstance(location, StoragePaths):
                account_guard = AccountGuard(location.account, sleep=lambda *_: None)
                account_guard.acquire(respect_cooldown=False)
                output_guard = OutputGuard(location.output_dir)
                output_guard.acquire()
            result = repair_history(record_dir, account)
            _publish(location)
            counts = result["counts"]
            message = (f"기록 복구 완료 · 파일 기록 {counts.get('media', 0):,}개, 실행 기록 {counts.get('runs', 0):,}개를 살렸습니다. "
                       f"손상된 원본은 {result['kept']}에 보관했습니다.")
            kind = "repaired"
        except ProtectionError as exc:
            message = str(exc)
        except Exception:
            pass
        finally:
            guards = [output_guard] if output_guard is not None else []
            if not _finish_local_change(None, location, guards, account_guard):
                kind = "damaged"
                message = "기록 복구 정리를 마치지 못했습니다. 다른 실행 중인 작업과 기록 DB 접근 권한을 확인해 주세요."
        results.put((generation, kind, message))

    def _start_local_job(self, stop_text, status):
        """Start one local file job (metadata or GIF conversion) at a time."""
        self.completion_text = ""
        self.busy = True
        self._update_controls()
        self.metadata_active = True
        self.metadata_stop.clear()
        self.source_stop_button.configure(text=stop_text, state="normal")
        self.source_stop_button.pack(side="right", padx=(0, 8))
        self.generation += 1
        self.status_text.set(status)

    def convert_gifs(self):
        count = int(self.current_summary.get("mp4_gifs", 0) or 0)
        if self.busy or self.current_storage is None or not count:
            return
        if self.is_downloading():
            dialogs.showinfo("저장 진행 중", "저장 작업이 끝난 뒤 GIF로 바꿔 주세요.", parent=self)
            return
        from .media import ffmpeg_path
        if ffmpeg_path() is None:
            dialogs.showinfo("GIF 변환 도구 없음", "배포 파일을 다시 받아 주세요. 개발 중이면 setup.ps1을 실행해 주세요.", parent=self)
            return
        size, max_mb = self.get_gif_options()
        account, location, generation = self.current_account, self.current_storage, self.generation
        if not dialogs.askyesno(
                "MP4 GIF를 GIF로 바꾸기",
                f"MP4로 저장된 움직이는 GIF {count:,}개를 .gif로 바꿉니다.\n\n"
                f"· 크기: {GIF_SIZE_LABELS.get(size, size)} (저장 설정에서 변경)\n"
                "· GIF는 보통 MP4보다 몇 배 큽니다. 용량 제한을 넘으면 MP4로 둡니다.\n"
                "· 원본 MP4는 휴지통으로 옮깁니다. 휴지통이 없는 위치는 건너뜁니다.\n"
                "· 동기화 폴더라면 새 GIF가 올라갑니다.\n"
                "· X에 요청하지 않으며 언제든 멈출 수 있습니다.\n\n계속할까요?", parent=self):
            return
        if not self._local_context_valid(account, location, generation):
            return
        self._start_local_job("GIF 변환 중단", "MP4 GIF를 GIF로 바꾸는 중입니다… X에 요청하지 않습니다.")
        threading.Thread(target=partial(HistoryWindow._convert_worker, self.results, self.metadata_stop),
                         args=(self.generation, self.current_storage, size, max_mb), daemon=True).start()

    @staticmethod
    def _convert_worker(results, stop, generation, directory, size, max_mb):
        from .media import (ConversionError, ConversionSizeLimit, can_recycle, convert_mp4_to_gif,
                            move_without_overwrite, recycle)
        from .metadata import author_identity, available, embed_source, persistent
        from .protection import AccountGuard, ProtectionError

        class Stopped(Exception):
            pass

        def check_stop():
            if stop.is_set():
                raise Stopped()

        def source_unchanged(path, original):
            """Never recycle an MP4 that an editor or sync changed meanwhile."""
            try:
                current = path.stat()
                if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
                    return False
                return all(getattr(current, field) == getattr(original, field)
                           for field in ("st_dev", "st_ino", "st_size", "st_mtime_ns"))
            except OSError:
                return False

        store = account_guard = None
        output_guards = []
        converted = kept = failed = missing = 0
        recyclable = {}
        kind = "error"
        message = "GIF 변환을 마치지 못했습니다. 기록 DB와 파일 접근 권한을 확인해 주세요."

        def report(state):
            return (f"{state} · 바꿈 {converted:,}개 / MP4로 둠 {kept:,}개 / "
                    f"실패 {failed:,}개 / 파일 없음 {missing:,}개")

        try:
            if isinstance(directory, StoragePaths):
                account_guard = AccountGuard(directory.account, sleep=lambda *_: None)
                account_guard.acquire(respect_cooldown=False)
            store = HistoryWindow._store(directory)
            if isinstance(directory, StoragePaths):
                for root in sorted(store.media_roots, key=lambda value: os.path.normcase(str(value))):
                    guard = OutputGuard(root)
                    output_guards.append(guard)
                    guard.acquire()
            tagging = available()
            with persistent() if tagging else nullcontext():
                rows = store.mp4_gif_records()
                for index, row in enumerate(rows, 1):
                    if stop.is_set():
                        break
                    results.put((generation, "convert-progress", report(f"GIF로 바꾸는 중 {index:,}/{len(rows):,}")))
                    source = Path(row["path"])
                    if not store.is_media_path(source) or not source.is_file():
                        missing += 1
                        continue
                    destination = source.with_suffix(".gif")
                    drive = os.path.normcase(os.path.splitdrive(str(source))[0])
                    if drive not in recyclable:
                        recyclable[drive] = can_recycle(source)
                    # Never replace another file, and never delete an MP4 that
                    # could not be restored from the Recycle Bin.
                    if destination.exists() or not recyclable[drive]:
                        kept += 1
                        continue
                    try:
                        original = source.stat()
                        temporary = convert_mp4_to_gif(source, check_stop=check_stop,
                                                       max_bytes=max_mb * 1024 * 1024, size=size)
                    except Stopped:
                        break
                    except ConversionSizeLimit:
                        kept += 1
                        continue
                    except ConversionError:
                        failed += 1
                        continue
                    except OSError:
                        failed += 1
                        continue
                    status = author_status = "pending"
                    try:
                        if not source_unchanged(source, original):
                            raise OSError("GIF source changed")
                        if tagging:
                            identity = author_identity(row.get("author", ""), row.get("author_id", ""))
                            tagged = embed_source(temporary, row["tweet_id"], author=identity["author"],
                                                  author_id=identity["author_id"])
                            status = tagged["status"]
                            author_status = tagged.get("author_status", "error" if identity["author"] or identity["author_id"] else "unknown")
                        # Keep the original file time, which sorts by tweet date.
                        if not source_unchanged(source, original):
                            raise OSError("GIF source changed")
                        os.utime(temporary, ns=(original.st_atime_ns, original.st_mtime_ns))
                        move_without_overwrite(temporary, destination)
                    except OSError:
                        failed += 1
                        continue
                    finally:
                        temporary.unlink(missing_ok=True)
                    # Record first, then recycle. A late input change or a
                    # refused recycle restores the MP4 record before cleanup.
                    try:
                        recorded = store.replace_media_file(row, destination, status, author_status=author_status)
                    except HistoryError:
                        recorded = False
                    if recorded and (not source_unchanged(source, original) or not recycle(source)):
                        restored = store.replace_media_file(dict(row, path=str(destination.resolve())), source,
                                                            row["metadata_status"], author_status=row["author_metadata_status"])
                        if not restored:
                            # Keep both files if the catalog changed again: a
                            # referenced GIF must not be deleted by a rollback.
                            raise HistoryError("GIF catalog changed during rollback")
                        recorded = False
                    if not recorded:
                        destination.unlink(missing_ok=True)
                        failed += 1
                        continue
                    converted += 1
            state = "GIF 변환 중단" if stop.is_set() else "GIF 변환 끝"
            recycled = " 바꾼 파일의 원본 MP4는 휴지통에 있습니다." if converted else ""
            kind, message = "converted", report(state) + "." + recycled + " X 요청은 보내지 않았습니다."
        except ProtectionError as exc:
            message = str(exc)
        except Exception:
            pass
        finally:
            if not _finish_local_change(store, directory, output_guards, account_guard):
                kind = "error"
                message = "GIF 변환 정리를 마치지 못했습니다. 기록 DB와 파일 접근 권한을 확인해 주세요."
        results.put((generation, kind, message))

    def _backfill(self, generation, directory):
        return HistoryWindow._backfill_worker(self.results, self.metadata_stop, generation, directory)

    @staticmethod
    def _backfill_worker(results, metadata_stop, generation, directory):
        from .metadata import author_identity, embed_source, persistent
        from .protection import AccountGuard, ProtectionError
        store = None
        account_guard = None
        output_guards = []
        success = failed = unavailable = 0
        kind = "error"
        message = "메타데이터 기록을 마치지 못했습니다. 기록 DB와 파일 접근 권한을 확인해 주세요."
        try:
            if isinstance(directory, StoragePaths):
                # This is a local operation: exclude account record relocation
                # without requiring the X request cooldown to have expired.
                account_guard = AccountGuard(directory.account, sleep=lambda *_: None)
                account_guard.acquire(respect_cooldown=False)
            store = HistoryWindow._store(directory)
            if isinstance(directory, StoragePaths):
                # Records can refer to files retained in earlier output roots.
                # Lock every such root before changing any file metadata.
                for root in sorted(store.media_roots, key=lambda value: os.path.normcase(str(value))):
                    guard = OutputGuard(root)
                    output_guards.append(guard)
                    guard.acquire()
            # One resident ExifTool for the whole pass instead of three launches per file.
            with persistent():
                offset = 0
                while True:
                    rows = store.records(limit=1000, offset=offset)
                    if not rows:
                        break
                    for row in rows:
                        if metadata_stop.is_set():
                            break
                        # Catalog text may be an old folder name; only a valid
                        # handle or numeric ID counts as a known author.
                        identity = author_identity(row.get("author", ""), row.get("author_id", ""))
                        known_author = bool(identity["author"] or identity["author_id"])
                        if (row.get("metadata_status") == "unsupported" or
                                (row.get("metadata_status") == "embedded" and
                                 (row.get("author_metadata_status") == "embedded" or not known_author))):
                            continue
                        path = Path(row["path"]) if row.get("path") else None
                        if path is None or not store.is_media_path(path) or not path.is_file():
                            unavailable += 1
                            continue
                        result = embed_source(path, row["tweet_id"], author=identity["author"], author_id=identity["author_id"])
                        source_status = result["status"]
                        author_status = result.get("author_status", "error" if known_author else "unknown")
                        if source_status == "failed" and row.get("metadata_status") == "embedded":
                            source_status = "embedded"
                        store.mark_metadata(row, source_status, author_status=author_status)
                        if result["status"] == "embedded" and author_status != "error":
                            success += 1
                        else:
                            failed += 1
                        results.put((generation, "metadata-progress", f"출처·작성자 기록 · 완료 {success:,}개 / 실패·미지원 {failed:,}개 / 파일 없음 {unavailable:,}개"))
                    if metadata_stop.is_set():
                        break
                    offset += len(rows)
            state = "중단" if metadata_stop.is_set() else "종료"
            kind = "backfilled"
            message = f"출처·작성자 기록 {state} · 완료 {success:,}개 / 실패·미지원 {failed:,}개 / 파일 없음 {unavailable:,}개. X 요청은 보내지 않았습니다."
        except ProtectionError as exc:
            message = str(exc)
        except Exception:
            pass
        finally:
            if not _finish_local_change(store, directory, output_guards, account_guard):
                kind = "error"
                message = "메타데이터 기록 정리를 마치지 못했습니다. 기록 DB와 파일 접근 권한을 확인해 주세요."
        results.put((generation, kind, message))

    def stop_metadata(self):
        if self.delete_active:
            self.delete_stop.set()
        else:
            self.metadata_stop.set()
        self.source_stop_button.configure(state="disabled")
        self.status_text.set("현재 파일 처리가 끝나면 작업을 멈춥니다…")

    def close(self):
        self.metadata_stop.set()
        self.delete_stop.set()
        if self.metadata_active or self.delete_active:
            self.closing = True
            self.stop_metadata()
        elif self.choice_active or self.repair_active:
            self.closing = True
            self.status_text.set("기록 복구를 마친 뒤 창을 닫습니다…" if self.repair_active else
                                 "선택을 기록한 뒤 창을 닫습니다…")
        else:
            self.destroy()

    def destroy(self):
        if self._destroyed:
            return
        self._tree_release()
        self._sample_token += 1
        self._destroyed = True
        self.delete_stop.set()
        self._preview_loader.close()
        self.grid_view.release()
        self.sample_view.release()
        self._preview_image = None
        self.selection_text.trace_remove("write", self._selection_trace)
        if self._pager_layout_timer is not None:
            self.tk.call("after", "cancel", self._pager_layout_timer)
            self._pager_layout_timer = None
        if self._poll_timer is not None:
            self.tk.call("after", "cancel", self._poll_timer)
            self._poll_timer = None
        widgets = list(self.winfo_children())
        descendants = []
        while widgets:
            widget = widgets.pop()
            descendants.append(widget)
            widgets.extend(widget.winfo_children())
        super().destroy()
        # Tk finalizers must run on the UI thread. Closed Toplevels otherwise
        # form cycles through their stored widgets/parent links and callbacks,
        # which a thumbnail decoder's ordinary Python GC could later collect.
        theme.release_variables(self)
        # Compound controls keep Python references to their destroyed children
        # (e.g. ScrollableContent.canvas). Remove only those dead parent links;
        # an open widget's normal hierarchy is never changed.
        for widget in descendants:
            widget.master = None
        for name in ("_pagers", "_tiles", "wrapping_labels"):
            getattr(self, name, []).clear()
        for name, value in tuple(self.__dict__.items()):
            if name != "master" and isinstance(value, tk.Misc):
                setattr(self, name, None)
            elif isinstance(value, list) and any(isinstance(child, tk.Misc) for child in value):
                value.clear()
        self.get_location = self.is_downloading = self.get_gif_options = self.retry_missing = None

    # ----------------------------------------------------------- views/stats
    def set_view(self, mode):
        if mode == self.view_mode:
            return
        self.view_mode = mode
        table = self.tree.master
        if mode == "grid":
            info = table.grid_info()
            table.grid_remove()
            self.grid_view.grid(row=info.get("row", 0), column=info.get("column", 0), sticky="nsew")
            self._show_grid()
            selection = self.tree.selection()
            self.grid_view.select_many(selection, focus=self.tree.focus())
        else:
            self.grid_view.grid_remove()
            self.grid_view.show([], lambda paths: set())
            table.grid()
        self._update_view_buttons()

    def _update_view_buttons(self):
        for mode, button in self.view_buttons.items():
            button.configure(style="SegmentSelected.TButton" if mode == self.view_mode else "Segment.TButton")

    def _show_grid(self):
        location = self.current_storage

        def permitted(paths):
            if location is None:
                return set()
            with closing(HistoryWindow._store(location)) as store:
                return {path for path in paths if store.is_media_path(path)}

        rows = [(key, self.rows[key]) for key in self.tree.get_children()]
        self.grid_view.show(rows, permitted, empty_text=self.table_note.get() or "표시할 파일이 없습니다.")

    def _select_key(self, key):
        if key not in self.rows:
            return
        self.tree.selection_set(key)
        self.tree.focus(key)
        self.tree.see(key)
        self.select_record()

    def _select_gallery(self, keys, focus):
        keys = tuple(key for key in keys if key in self.rows)
        self.tree.selection_set(keys)
        if focus in keys:
            self.tree.focus(focus)
            self.tree.see(focus)
        else:
            self.tree.focus("")
        self.select_record()

    @staticmethod
    def _path_key(value):
        return os.path.normcase(os.path.abspath(str(value))) if value else None

    def _selected_files(self):
        """A physical file appears once even if it has several layout records."""
        files = {}
        for key in self.tree.selection():
            row = self.rows.get(key)
            if row and row.get("path"):
                files.setdefault(self._path_key(row["path"]), row)
        return list(files.values())

    def _update_file_actions(self):
        files = self._selected_files()
        if files:
            size = sum(int(row.get("filesize") or 0) for row in files)
            self.file_selection_text.set(f"파일 {len(files):,}개 선택 · {format_bytes(size)}"
                                         + (" (일부 용량 미확인)" if any(row.get("filesize") is None for row in files) else ""))
        elif self.tree.selection():
            self.file_selection_text.set("선택한 기록에 파일 위치가 없습니다.")
        else:
            self.file_selection_text.set("클릭할 때마다 하나씩 고릅니다." if self.select_several else FILE_HINT)
        ready = not self.busy and self.current_storage is not None
        downloading = bool(self.is_downloading()) if self.is_downloading else False
        self.file_all_button.configure(state="normal" if ready and self.rows else "disabled")
        self.file_clear_button.configure(state="normal" if self.tree.selection() else "disabled")
        self.delete_button.configure(state="normal" if ready and files and not downloading else "disabled")

    def toggle_select_several(self):
        """Mouse-only multiple selection: each click adds or removes a file."""
        self.select_several = not self.select_several
        self.grid_view.toggle_mode = self.select_several
        self.toggle_button.configure(text="여러 개 선택 끝내기" if self.select_several else "여러 개 선택",
                                     style="CompactPrimary.TButton" if self.select_several else "Compact.TButton")
        self._update_file_actions()

    def clear_file_selection(self):
        self.tree.selection_set(())
        self.select_record()

    # A press in the table starts a range; dragging extends it to the row under
    # the pointer, scrolling when the pointer leaves the table.
    def _tree_press(self, event):
        self._tree_release()
        if event.state & 0x0001 or self.tree.identify_region(event.x, event.y) not in {"cell", "tree"}:
            return None
        row = self.tree.identify_row(event.y)
        if not row:
            return None
        adding = bool(event.state & 0x0004) or self.select_several
        self._tree_drag = {"anchor": row, "current": row, "y": event.y, "moved": False,
                           "base": set(self.tree.selection()) if adding else set()}
        if self.select_several and not event.state & 0x0004:
            self.tree.focus_set()
            self.tree.selection_toggle(row)
            self.tree.focus(row)
            return "break"
        return None

    def _tree_motion(self, event):
        drag = self._tree_drag
        if drag is None:
            return None
        if not drag["moved"] and abs(event.y - drag["y"]) < 6:
            return "break"
        drag["moved"] = True
        row = self.tree.identify_row(event.y)
        if row:
            drag["current"] = row
        drag["scroll"] = -1 if event.y < 0 else 1 if event.y > self.tree.winfo_height() else 0
        self._extend_tree_drag()
        if drag["scroll"] and self._tree_scroll is None:
            self._tree_scroll = self.after(60, self._scroll_tree_drag)
        return "break"

    def _scroll_tree_drag(self):
        self._tree_scroll = None
        drag = self._tree_drag
        if drag is None or not drag.get("scroll") or self._destroyed:
            return
        self.tree.yview_scroll(drag["scroll"], "units")
        self.tree.update_idletasks()
        visible = [row for row in self.tree.get_children() if self.tree.bbox(row)]
        if visible:
            drag["current"] = visible[0] if drag["scroll"] < 0 else visible[-1]
        self._extend_tree_drag()
        self._tree_scroll = self.after(60, self._scroll_tree_drag)

    def _extend_tree_drag(self):
        drag = self._tree_drag
        rows = self.tree.get_children()
        if drag["anchor"] not in rows or drag["current"] not in rows:
            return
        first, last = sorted((rows.index(drag["anchor"]), rows.index(drag["current"])))
        chosen = drag["base"] | set(rows[first:last + 1])
        if chosen != set(self.tree.selection()):
            self.tree.selection_set(tuple(row for row in rows if row in chosen))
            self.tree.focus(drag["current"])

    def _tree_release(self, _event=None):
        self._tree_drag = None
        if self._tree_scroll is not None:
            self.after_cancel(self._tree_scroll)
            self._tree_scroll = None

    def select_all_files(self, _=None):
        if not self.busy and self.rows:
            self.tree.selection_set(tuple(self.rows))
            if self.tree.focus() not in self.rows:
                self.tree.focus(next(iter(self.rows)))
            self.select_record()
        return "break"

    # ----------------------------------------------------------- context menus
    def _tree_context(self, event, kind):
        tree = {"files": self.tree, "authors": self.author_tree, "runs": self.run_tree,
                "missing": self.missing_tree, "deletions": self.deletion_tree}[kind]
        if tree.identify_region(event.x, event.y) not in {"cell", "tree"}:
            return "break"
        key = tree.identify_row(event.y)
        if not key:
            return "break"
        # Right-click on one of several chosen rows keeps the whole selection.
        if key not in tree.selection():
            tree.selection_set(key)
        tree.focus(key)
        tree.focus_set()
        if kind == "files":
            self._tree_release()
            self.select_record()
            commands = self._file_menu_commands()
        elif kind == "authors":
            self._show_author_samples()
            row = self.stats_authors.get(key) or {}
            handle = row.get("author") or ""
            commands = [("이 작성자 파일 전체 보기", lambda: self.set_author(handle), bool(_HANDLE.fullmatch(handle))),
                        ("작성자 아이디 복사", lambda: self._copy_text("@" + handle), bool(handle))]
        elif kind == "missing":
            self._update_missing_actions()
            commands = [(label, callback, not button.instate(["disabled"])) for label, callback, button in (
                ("트윗 열기", self.open_missing_tweet, self.missing_open_button),
                ("다시 받기", self.retry_missing_files, self.retry_button),
                ("다시 받지 않기", lambda: self.choose_missing(True), self.ignore_button),
                ("숨김 해제", lambda: self.choose_missing(False), self.unignore_button),
                ("이 페이지 모두 선택", self.select_all_missing, self.missing_all_button))]
        elif kind == "deletions":
            self.select_deletion()
            commands = [("트윗 열기", self.open_deletion_tweet, not self.deletion_open_button.instate(["disabled"])),
                        ("삭제 기록 복사", lambda: self._copy_text(self.deletion_detail.get()), True)]
        else:
            self.select_run()
            commands = [("실행 기록 복사", lambda: self._copy_text(self.run_text.get()), True)]
        return self._post_context(event, commands)

    def _file_menu_commands(self):
        self._update_file_actions()
        self._update_author_button()
        return [(label, callback, not button.instate(["disabled"]) and
                 (label != "선택한 파일 경로 복사" or bool(self._selected_files()))) for label, callback, button in (
            ("파일 열기", self.open_file, self.open_file_button),
            ("트윗 열기", self.open_tweet, self.open_tweet_button),
            ("이 작성자 파일 보기", self.filter_selected_author, self.author_button),
            ("선택한 파일 경로 복사", self._copy_file_paths, self.file_clear_button),
            ("이 페이지 모두 선택", self.select_all_files, self.file_all_button),
            ("선택 해제", self.clear_file_selection, self.file_clear_button),
            ("선택한 파일 삭제", self.start_deletion, self.delete_button))]

    def _gallery_context(self, event, key):
        if key not in self.rows:
            return "break"
        if key not in self.tree.selection():
            self.tree.selection_set(key)
        self.tree.focus(key)
        self.select_record()
        return self._post_context(event, self._file_menu_commands())

    def _sample_context(self, event, key):
        row = self.sample_rows.get(key)
        if row is None:
            return "break"
        self.sample_view.select(key)
        # Snapshot this row; a later author selection must not change the target.
        value, tweet, author = row.get("path"), str(row.get("tweet_id") or ""), self._sample_author or ""
        return self._post_context(event, [
            ("파일 열기", lambda: self._open_path(value), bool(value)),
            ("트윗 열기", lambda: webbrowser.open(f"https://x.com/i/web/status/{tweet}"), tweet.isdigit()),
            ("파일 경로 복사", lambda: self._copy_text(value), bool(value)),
            ("이 작성자 파일 전체 보기", lambda: self.set_author(author), bool(_HANDLE.fullmatch(author)))])

    def _post_context(self, event, commands):
        if self._context_menu is not None:
            self._context_menu.destroy()
        palette = theme.DARK if theme.mode(self) == "dark" else theme.LIGHT
        menu = self._context_menu = tk.Menu(self, tearoff=False, font=theme.SMALL,
                                            background=palette["CARD"], foreground=palette["INK"],
                                            activebackground=palette["BLUE"], activeforeground=palette["PRIMARY_INK"],
                                            disabledforeground=palette["DISABLED"])
        for label, callback, enabled in commands:
            menu.add_command(label=label, command=callback, state="normal" if enabled else "disabled")
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return "break"

    def _copy_text(self, value):
        if value:
            self.clipboard_clear()
            self.clipboard_append(str(value))

    def _copy_file_paths(self):
        self._copy_text("\n".join(row["path"] for row in self._selected_files()))

    def _file_select_all_key(self, event):
        if self.tabs.index("current") != 0 or event.widget.winfo_class() in {
            "Entry", "TEntry", "Text", "TCombobox", "Spinbox", "TSpinbox"
        }:
            return
        return self.select_all_files()

    def _file_delete_key(self, event):
        if self.tabs.index("current") != 0 or event.widget.winfo_class() in {
            "Entry", "TEntry", "Text", "TCombobox", "Spinbox", "TSpinbox"
        }:
            return
        self.start_deletion()
        return "break"

    def _local_context_valid(self, account, location, generation):
        return not (self._destroyed or self.closing or self.busy or self.is_downloading()
                    or generation != self.generation or location != self.current_storage
                    or account != self.current_account)

    def start_deletion(self):
        files = [dict(row) for row in self._selected_files()]
        if self.busy or self.current_storage is None or not files:
            return
        if self.is_downloading():
            dialogs.showinfo("저장 진행 중", "저장 작업이 끝난 뒤 파일을 휴지통으로 보내 주세요.", parent=self)
            return
        account, location, generation = self.current_account, self.current_storage, self.generation
        filenames = [row.get("filename") or Path(row["path"]).name for row in files]
        keep_deleted = dialogs.confirm_delete(len(files), sum(int(row.get("filesize") or 0) for row in files),
                                               filenames, parent=self)
        if keep_deleted is None or not self._local_context_valid(account, location, generation):
            return
        # The main form can change while a modal dialog processes events. Do
        # not delete from an older library while showing a different account.
        try:
            current_account, current_location = self.get_location()
        except (ValueError, OSError):
            current_account = current_location = None
        if current_account != account or current_location != location:
            dialogs.showinfo("저장 위치가 바뀜", "계정이나 저장 폴더가 바뀌었습니다. 라이브러리를 새로고침한 뒤 다시 선택해 주세요.", parent=self)
            return
        self.completion_text = ""
        self.busy = self.delete_active = True
        self.delete_stop.clear()
        self.generation += 1
        self.source_stop_button.configure(text="휴지통 보내기 중단", state="normal")
        self.source_stop_button.pack(side="right", padx=(0, 8))
        self.status_text.set(f"파일 {len(files):,}개를 휴지통으로 보내는 중입니다…")
        self._update_controls()
        threading.Thread(target=partial(HistoryWindow._delete_worker, self.results, self.delete_stop),
                         args=(self.generation, location, files, bool(keep_deleted)), daemon=True).start()

    @staticmethod
    def _delete_worker(results, stop, generation, location, rows, keep_deleted):
        from .deletion import run_deletion
        completed = False

        def emit(event):
            nonlocal completed
            if event.get("type") == "done":
                completed = True
                results.put((generation, "deleted", event))
            elif event.get("type") == "progress":
                results.put((generation, "delete-progress", event))

        try:
            run_deletion(location, rows, keep_deleted, stop, emit)
        except Exception:
            # A complete result always releases the UI's operation lock. The
            # persistent journal lets the protected worker reconcile a crash.
            if not completed:
                emit({"type": "done", "paths": [], "failed": len(rows), "total": len(rows),
                      "message": "파일 처리를 마치지 못했습니다. ‘삭제 기록’을 확인하고 다시 시도해 주세요."})
        if not completed:
            emit({"type": "done", "paths": [], "total": len(rows),
                  "message": "파일 처리가 끝났습니다. ‘삭제 기록’에서 결과를 확인해 주세요."})

    def _selection_after_deletion(self, paths):
        removed = {self._path_key(path) for path in paths}
        row = self.selected()
        if row is None or self._path_key(row.get("path")) not in removed:
            return
        order = list(self.tree.get_children())
        focus = self.tree.focus()
        if focus not in order:
            selection = self.tree.selection()
            focus = selection[0] if selection else order[0]
        position = order.index(focus)
        # Continue forward first; at the end use the nearest earlier file.
        for key in order[position + 1:] + list(reversed(order[:position])):
            candidate = self.rows.get(key)
            if candidate and candidate.get("path") and self._path_key(candidate["path"]) not in removed:
                self._delete_next_identity = self._record_identity(candidate)
                return
        self._delete_select_first = True

    def _update_author_button(self):
        row = self.selected()
        handle = (row or {}).get("author") or ""
        valid = bool(re.fullmatch(r"[A-Za-z0-9_]{1,15}", handle))
        self.author_button.configure(state="normal" if valid and handle.lower() != self.author_filter.lower() else "disabled")

    def filter_selected_author(self):
        row = self.selected()
        if row and re.fullmatch(r"[A-Za-z0-9_]{1,15}", row.get("author") or ""):
            self.set_author(row["author"])

    def set_author(self, handle):
        """List only this author's files (exact handle, any letter case)."""
        self.author_filter = handle
        self.author_choice.set("@" + handle)
        self.tabs.select(0)
        self.search()

    def clear_author(self):
        self.author_filter = ""
        self.author_choice.set(ALL_AUTHORS)
        self.search()

    def _author_choices(self):
        """Fill the author list, narrowed to the text typed so far."""
        typed = self.author_choice.get().strip().lstrip("@").lower()
        if typed == ALL_AUTHORS.lower() or " · " in typed:
            typed = ""
        values = [f"@{row['author']} · {row['files']:,}개" for row in self.stats.get("authors", [])
                  if _HANDLE.fullmatch(row["author"]) and typed in row["author"].lower()]
        self.author_combo.configure(values=[ALL_AUTHORS, *values])

    def _choose_author(self):
        value = self.author_choice.get().strip()
        handle = value.split(" · ")[0].strip().lstrip("@")
        if not handle or value == ALL_AUTHORS:
            if self.author_filter:
                self.clear_author()
            else:
                self.author_choice.set(ALL_AUTHORS)
        elif _HANDLE.fullmatch(handle):
            self.set_author(handle)
        else:
            self.author_choice.set("@" + self.author_filter if self.author_filter else ALL_AUTHORS)
            self.status_text.set("작성자 아이디는 영문·숫자·밑줄로 입력해 주세요.")

    def show_ranked_author(self):
        selection = self.author_tree.selection()
        row = self.stats_authors.get(selection[0]) if selection else None
        if row and re.fullmatch(r"[A-Za-z0-9_]{1,15}", row.get("author") or ""):
            self.set_author(row["author"])

    def _show_stats(self, stats):
        # A page or search change reuses the same statistics; listing every
        # author again would only slow the page down. New statistics arrive
        # on every refresh during a save, so a hidden list waits for its tab
        # (tens of thousands of authors take a noticeable fraction of a second).
        if stats is not self.stats:
            self.stats = stats
            self._sample_token += 1
            self._sample_author = None
            self.sample_rows = {}
            self.sample_view.show([], lambda paths: set(), empty_text="")
            self._authors_stale = True
            self._show_stale_authors()
        self._draw_months()

    def _show_stale_authors(self, _=None):
        if self._authors_stale and not self._destroyed and self.tabs.index("current") == 2:
            self._fill_authors()

    def _fill_authors(self):
        self._authors_stale = False
        selection = self.author_tree.selection()
        previous = self.stats_authors.get(selection[0], {}).get("author") if selection else None
        typed = self.author_query.get().strip().lstrip("@").lower()
        self.author_tree.delete(*self.author_tree.get_children())
        self.stats_authors = {}
        for index, row in enumerate(self.stats.get("authors", [])):
            if typed and typed not in row["author"].lower():
                continue
            key = str(index)
            self.stats_authors[key] = row
            self.author_tree.insert("", "end", iid=key, values=(f"{index + 1:,}", "@" + row["author"], f"{row['files']:,}",
                                                                f"{row['posts']:,}", format_day(row.get("latest"))))
            if previous and row["author"].lower() == previous.lower():
                self.author_tree.selection_set(key)
        if not self.author_tree.selection():
            self._show_author_samples()

    def _show_author_samples(self, _=None):
        """Thumbnails of the selected author's newest tweets, loaded in the background."""
        selection = self.author_tree.selection()
        row = self.stats_authors.get(selection[0]) if selection else None
        if row is not None and row["author"] == self._sample_author:
            return  # Includes an in-flight request and a cached empty result.
        self._sample_token += 1
        if row is None or not _HANDLE.fullmatch(row["author"]) or self.current_storage is None:
            self._sample_author = None
            self.sample_rows = {}
            self.sample_view.show([], lambda paths: set(), empty_text="")
            self.sample_text.set(SAMPLE_HINT)
            return
        self._sample_author = row["author"]
        self.sample_rows = {}
        self.sample_view.show([], lambda paths: set(), empty_text="불러오는 중…")
        self.sample_text.set(f"@{row['author']} · 최근 게시물을 불러오는 중…")
        threading.Thread(target=partial(HistoryWindow._samples_worker, self._sample_results),
                         args=(self._sample_token, self.current_storage, row["author"]), daemon=True).start()

    @staticmethod
    def _samples_worker(results, token, location, author):
        store = None
        try:
            store = HistoryWindow._store(location)
            results.put((token, author, store.author_samples(author, 8)))
        except Exception:
            results.put((token, author, None))
        finally:
            if store is not None:
                store.close()

    def _poll_samples(self):
        while True:
            try:
                token, author, samples = self._sample_results.get_nowait()
            except queue.Empty:
                break
            if token != self._sample_token:
                continue
            if samples is None:
                self._sample_author = None
                self.sample_text.set(f"@{author} · 사진을 불러오지 못했습니다.")
                continue
            self.sample_rows = {str(index): row for index, row in enumerate(samples)}
            location = self.current_storage

            def permitted(paths):
                if location is None:
                    return set()
                with closing(HistoryWindow._store(location)) as store:
                    return {path for path in paths if store.is_media_path(path)}

            self.sample_view.show(list(self.sample_rows.items()), permitted, empty_text="저장된 파일이 없습니다.")
            self.sample_text.set(f"@{author} · 최근 게시물 {len(samples)}개 · 두 번 클릭하면 열립니다"
                                 if samples else f"@{author} · 지금 있는 파일이 없습니다.")
        self.sample_view.poll()

    def _open_sample(self, key):
        row = self.sample_rows.get(key)
        if row:
            self._open_path(row.get("path"))

    def _draw_months(self):
        field = next(key for key, label in MONTH_FIELDS.items() if label == self.month_field.get())
        self.month_note.set(MONTH_NOTES[field])
        empty = {"liked": "아직 기간을 아는 새 Like가 없습니다.\n자동 저장을 켜 두면 새 Like가 쌓입니다."}.get(field, "아직 기록이 없습니다.")
        self.month_chart.show(list(self.stats.get("months", {}).get(field, [])), empty_text=empty)

    # --------------------------------------------------------- missing files
    def _filter_missing(self):
        self.missing_page = 0
        self.missing_tree.selection_remove(self.missing_tree.selection())
        self._show_missing()

    def _show_missing(self):
        tree = self.missing_tree
        selected = set(tree.selection())
        tree.delete(*tree.get_children())
        self.missing_rows.clear()
        show_ignored = self.show_ignored.get()
        visible = [item for item in self.missing_items if not item.get("ignored")]
        hidden = len(self.missing_items) - len(visible)
        items = self.missing_items if show_ignored else visible
        size = self._page_size()
        total = len(items)
        self.missing_total_pages = (total + size - 1) // size
        self.missing_page = min(self.missing_page, max(0, self.missing_total_pages - 1))
        self.missing_page_text.set(f"목록 {total:,}개 · {self.missing_page + 1 if total else 0} / {self.missing_total_pages:,}페이지")
        self.missing_page_input.set(str(self.missing_page + 1))
        start = self.missing_page * size
        for item in items[start:start + size]:
            key = item["archive_key"]
            self.missing_rows[key] = item
            kind = "GIF" if item.get("media_type") == "animated_gif" else MEDIA_LABELS.get(item.get("media_type"), "알 수 없음")
            posted = format_short(item.get("posted_at")) + (" *" if item.get("posted_source") == "id_estimate" else "")
            tree.insert("", "end", iid=key, values=(
                posted, kind + (" · 숨김" if item.get("ignored") else ""),
                "@" + item["author"] if item.get("author") else "–", item.get("filename") or key,
                format_short(item.get("saved_at")), tweet_text(item)))
        kept = [key for key in selected if key in self.missing_rows]
        if kept:
            tree.selection_set(kept)
        text = (f"사라진 파일 {len(visible):,}개 · 트윗 {len({item['tweet_id'] for item in visible}):,}개"
                if visible else "사라진 파일이 없습니다")
        self.missing_text.set(text + (f" · 숨긴 파일 {hidden:,}개" if hidden else ""))
        self.tabs.tab(self.missing_view, text=f"사라진 파일 {len(visible):,}" if visible else "사라진 파일")
        if self.missing_rows:
            self.missing_message.place_forget()
        else:
            self.missing_note.set("사라진 파일이 없습니다." if not hidden or show_ignored else
                                  "사라진 파일이 없습니다.\n숨긴 파일은 ‘숨긴 파일도 보기’에서 볼 수 있습니다.")
            self.missing_message.place(relx=0.5, rely=0.5, anchor="center")
        self._update_missing_actions()

    def _missing_selection(self):
        return [self.missing_rows[key] for key in self.missing_tree.selection() if key in self.missing_rows]

    def _update_missing_actions(self):
        items = self._missing_selection()
        browsing = not self.busy and self.current_storage is not None
        downloading = bool(self.is_downloading()) if self.is_downloading else False
        ready = browsing and not downloading
        self.missing_open_button.configure(state="normal" if items else "disabled")
        self.missing_all_button.configure(state="normal" if self.missing_rows else "disabled")
        self.ignore_button.configure(state="normal" if ready and any(not item.get("ignored") for item in items) else "disabled")
        self.unignore_button.configure(state="normal" if ready and any(item.get("ignored") for item in items) else "disabled")
        self.retry_button.configure(
            text=f"다시 받기 ({len(items):,}개)" if items else "다시 받기",
            state="normal" if ready and items and self.retry_missing is not None else "disabled")
        self._update_pager("missing_", self.missing_page, self.missing_total_pages, browsing, missing=True)
        self._queue_pager_layout()

    def select_all_missing(self, _=None):
        self.missing_tree.selection_set(tuple(self.missing_rows))
        return "break"

    def open_missing_tweet(self):
        items = self._missing_selection()
        if items:
            focused = self.missing_rows.get(self.missing_tree.focus())
            item = focused if focused in items else items[0]
            webbrowser.open(f"https://x.com/i/web/status/{item['tweet_id']}")

    def choose_missing(self, ignored):
        items = [item for item in self._missing_selection() if bool(item.get("ignored")) != ignored]
        if self.busy or self.current_storage is None or not items:
            return
        if self.is_downloading():
            dialogs.showinfo("저장 진행 중", "저장 작업이 끝난 뒤 숨김 선택을 바꿔 주세요.", parent=self)
            return
        location, generation = self.current_storage, self.generation
        if ignored and not dialogs.askyesno(
                "다시 받지 않기",
                f"선택한 파일 {len(items):,}개를 숨기고 앞으로 받지 않습니다.\n\n"
                "· 저장 방식을 바꿔도 받지 않습니다.\n"
                "· 파일과 기록은 지우지 않습니다.\n"
                "· ‘숨긴 파일도 보기’에서 언제든 해제할 수 있습니다.\n\n계속할까요?", parent=self):
            return
        # Modal dialogs process events too; do not apply old selections to a
        # refreshed catalog or start a local write after a download began.
        if (self._destroyed or self.closing or self.busy or self.is_downloading() or
                generation != self.generation or location != self.current_storage):
            return
        self.busy = True
        self.choice_active = True
        self._update_controls()
        self.generation += 1
        self.status_text.set("선택을 기록하는 중…")
        identities = [(item["tweet_id"], item["num"], item["media_type"]) for item in items]
        threading.Thread(target=partial(HistoryWindow._choice_worker, self.results),
                         args=(self.generation, location, identities, ignored), daemon=True).start()

    @staticmethod
    def _choice_worker(results, generation, location, identities, ignored):
        from .protection import AccountGuard, ProtectionError
        from .recovery import backup_catalog
        account_guard = output_guard = None
        try:
            if isinstance(location, StoragePaths):
                account_guard = AccountGuard(location.account, sleep=lambda *_: None)
                account_guard.acquire(respect_cooldown=False)
                output_guard = OutputGuard(location.output_dir)
                output_guard.acquire()
                store = HistoryStore.for_storage(location, protected=True)
            else:
                store = HistoryWindow._store(location)
            with closing(store):
                count = store.set_missing_ignored(identities, ignored)
            message = (f"사라진 파일 {count:,}개를 숨겼습니다. 앞으로 저장할 때도 받지 않습니다." if ignored else
                       f"숨긴 파일 {count:,}개를 다시 목록에 표시합니다.")
            warnings = []
            if isinstance(location, StoragePaths):
                try:
                    publish_snapshot(location)
                except (MigrationError, OSError, sqlite3.Error, ValueError):
                    warnings.append("저장 폴더의 기록 사본")
                try:
                    if backup_catalog(location.record_dir, location.account) is None:
                        warnings.append("이 PC의 기록 백업")
                except (OSError, sqlite3.Error, ValueError):
                    warnings.append("이 PC의 기록 백업")
            if warnings:
                message += "\n선택은 이 PC에 저장했지만 " + "·".join(warnings) + "을 갱신하지 못했습니다. 다음 저장 때 다시 시도합니다."
            kind = "missing-choice"
        except ProtectionError as exc:
            kind, message = "error", str(exc)
        except Exception:
            kind, message = "error", "선택을 기록하지 못했습니다. 저장 폴더 권한과 다른 실행 중인 작업을 확인해 주세요."
        finally:
            if output_guard is not None:
                output_guard.close()
            if account_guard is not None:
                account_guard.close()
        results.put((generation, kind, message))

    def retry_missing_files(self):
        items = self._missing_selection()
        if self.busy or self.current_storage is None or not items or self.retry_missing is None:
            return
        if self.is_downloading():
            dialogs.showinfo("저장 진행 중", "지금 저장 작업이 끝난 뒤 다시 받아 주세요.", parent=self)
            return
        account, location, generation = self.current_account, self.current_storage, self.generation
        tweets = len({item["tweet_id"] for item in items})
        minutes = max(1, round(tweets * 10 / 60))
        hidden = sum(bool(item.get("ignored")) for item in items)
        if not dialogs.askyesno(
                "사라진 파일 다시 받기",
                f"선택한 파일 {len(items):,}개(트윗 {tweets:,}개)를 다시 받습니다.\n\n"
                f"· 트윗마다 10초 간격으로 요청해 약 {minutes:,}분 걸립니다.\n"
                "· 지금 저장 설정의 폴더 방식으로 저장합니다.\n"
                "· 삭제·비공개된 트윗의 파일은 받을 수 없습니다.\n"
                + (f"· 숨긴 파일 {hidden:,}개는 받으면 숨김이 풀립니다.\n" if hidden else "")
                + "· 진행과 중단은 메인 화면에서 합니다.\n\n계속할까요?", parent=self):
            return
        if self._destroyed or self.closing or self.busy or self.is_downloading() or generation != self.generation:
            return
        if self.retry_missing([item["archive_key"] for item in items], account, location):
            self.status_text.set("메인 화면에서 사라진 파일을 다시 받고 있습니다. 받은 파일은 목록에서 빠집니다.")
            self._update_missing_actions()

    def _export(self, generation, directory, destination):
        return self._export_worker(self.results, generation, directory, destination)

    @staticmethod
    def _export_worker(results, generation, directory, destination):
        store = None
        try:
            store = HistoryWindow._store(directory)
            store.export_records(Path(destination))
            results.put((generation, "exported", f"CSV 내보내기 완료 · {destination}"))
        except Exception:
            results.put((generation, "error", "CSV 파일을 저장하지 못했습니다. 파일 접근 권한을 확인해 주세요."))
        finally:
            if store is not None:
                store.close()

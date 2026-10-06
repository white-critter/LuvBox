"""Local records and statistics, without contacting X."""

from __future__ import annotations

from contextlib import closing, nullcontext
from datetime import date, datetime
from functools import partial
import os
from pathlib import Path
import queue
import re
import threading
import tkinter as tk
from tkinter import filedialog, ttk
import webbrowser

from PIL import ImageTk

from . import dialogs, theme
from .history import HistoryError, HistoryStore
from .preview import PreviewLoader
from .storage import OutputGuard, StoragePaths


MEDIA_LABELS = {"all": "전체", "photo": "사진", "video": "동영상", "animated_gif": "움직이는 GIF"}
STATUS_LABELS = {"running": "진행 중", "finished": "완료", "stopped": "직접 중단", "error": "오류", "interrupted": "비정상 종료", "limited": "시험 저장 완료", "partial": "일부만 완료", "cancelled": "취소됨"}
METADATA_LABELS = {"embedded": "파일에 출처 기록됨", "pending": "출처 기록 전", "failed": "파일 출처 기록 실패", "unsupported": "메타데이터 미지원 형식"}
AUTHOR_METADATA_LABELS = {"embedded": "작성자 기록됨", "pending": "작성자 기록 전", "error": "작성자 기록 실패", "unknown": "작성자 정보 없음"}
TILES = {"files": "저장한 파일", "posts": "트윗", "photo": "사진", "video": "동영상", "animated_gif": "움직이는 GIF", "bytes": "확인된 용량"}
PAGE_SIZE = 100
PAGE_SIZES = (50, 100, 200)
ORDER_LABELS = {"saved": "저장한 순", "liked": "Like한 순", "posted": "게시일 최신순", "posted_oldest": "게시일 오래된 순"}
PERIOD_LABELS = {"posted": "게시일", "saved": "저장일"}
GIF_SIZE_LABELS = {"original": "원본 크기", "720": "최대 720px · 초당 15장", "480": "최대 480px · 초당 12장"}
NO_SELECTION = ("표에서 파일을 고르면 미리보기와 출처를 볼 수 있습니다. 두 번 누르면 파일이 열립니다.\n"
                "위 검색칸에서 작성자 아이디, 파일명, 트윗 번호로 찾을 수 있습니다.")
NO_RUN_SELECTION = "실행을 고르면 어떻게 끝났는지 볼 수 있습니다."


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


def format_day(value: str | None) -> str:
    try:
        return datetime.fromisoformat(value).strftime("%Y-%m-%d") if value else "–"
    except (ValueError, TypeError):
        return "–"


class HistoryWindow(tk.Toplevel):
    def __init__(self, parent, get_location, is_downloading=lambda: False,
                 get_gif_options=lambda: ("original", 200)):
        super().__init__(parent)
        theme.apply_style(self)
        self.title("XlikeSaver · 기록과 통계")
        width = min(1120, max(860, self.winfo_screenwidth() - 80))
        height = min(860, max(640, self.winfo_screenheight() - 100))
        self.geometry(f"{width}x{height}")
        self.minsize(min(860, width), min(720, height))
        self.configure(bg=theme.BG)
        self.transient(parent)
        self.get_location = get_location
        self.is_downloading = is_downloading
        # (GIF size preset, media size limit in MB) chosen in save options.
        self.get_gif_options = get_gif_options
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
        self.loaded_locations = set()
        self.results = queue.Queue()
        self.rows = {}
        self.run_rows = {}
        self.current_account_dir = None
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
        self.media_filter = tk.StringVar(self, value="전체")
        self.query = tk.StringVar(self)
        self.order = tk.StringVar(self, value=ORDER_LABELS["saved"])
        self.period_field = tk.StringVar(self, value=PERIOD_LABELS["posted"])
        self.period_from = tk.StringVar(self)
        self.period_to = tk.StringVar(self)
        self.applied_period = None
        self.page_text = tk.StringVar(self)
        self.run_page_text = tk.StringVar(self)
        self.page_size = tk.StringVar(self, value=str(PAGE_SIZE))
        self.page_input = tk.StringVar(self, value="1")
        self.run_page_input = tk.StringVar(self, value="1")
        self.preview_note = tk.StringVar(self, value="파일을 고르면 미리보기가 나옵니다.")
        self._build()
        self.wrapping_labels = []

        widgets = list(self.winfo_children())
        while widgets:
            widget = widgets.pop()
            if isinstance(widget, ttk.Label) and int(widget.cget("wraplength") or 0):
                self.wrapping_labels.append(widget)
            widgets.extend(widget.winfo_children())
        self.bind("<Configure>", self._resize_text, add="+")
        for key in ("Prior", "Next", "Home", "End"):
            self.bind("<" + key + ">", self._page_key, add="+")
        self.protocol("WM_DELETE_WINDOW", self.close)
        self._poll_timer = self.after(100, self._poll)
        self.refresh(import_legacy=True)

    def _build(self):
        self.content_view = theme.ScrollableContent(self, padding=(22, 16))
        self.content_view.pack(fill="both", expand=True)
        body = self.content_view.content
        body.columnconfigure(0, weight=1)
        body.rowconfigure(4, weight=1)
        heading = ttk.Frame(body)
        heading.grid(row=0, column=0, sticky="ew")
        ttk.Label(heading, text="기록 · 통계", style="Title.TLabel").pack(side="left")
        ttk.Button(heading, text="새로고침", command=lambda: self.refresh(import_legacy=True)).pack(side="right")
        ttk.Button(heading, text="기록 위치", command=self.show_locations).pack(side="right", padx=8)
        self.source_stop_button = ttk.Button(heading, text="출처·작성자 기록 중단", command=self.stop_metadata, state="disabled")
        ttk.Label(body, textvariable=self.location_text, style="Muted.TLabel", wraplength=1000).grid(row=1, column=0, sticky="w", pady=(2, 10))

        tiles = ttk.Frame(body)
        tiles.grid(row=2, column=0, sticky="ew")
        self._tile_frame = tiles
        self._tiles = []
        self._tile_columns = 6
        for column, (key, caption) in enumerate(TILES.items()):
            tile = theme.tile(tiles, self.tile_values[key], caption)
            tile.grid(row=0, column=column, sticky="ew", padx=(0 if column == 0 else 8, 0))
            self._tiles.append(tile)
            tile.bind("<Configure>", self._queue_pager_layout)
            for label in tile.winfo_children():
                label.bind("<Configure>", self._queue_pager_layout)
            tiles.columnconfigure(column, weight=1, uniform="tile")
        tiles.bind("<Configure>", self._queue_pager_layout)
        ttk.Label(body, textvariable=self.date_text, style="Muted.TLabel", wraplength=1000, justify="left").grid(row=3, column=0, sticky="w", pady=(8, 10))

        tabs = self.tabs = ttk.Notebook(body)
        tabs.grid(row=4, column=0, sticky="nsew")
        files = ttk.Frame(tabs, padding=(10, 12))
        runs = ttk.Frame(tabs, padding=(10, 12))
        tabs.add(files, text="저장한 파일")
        tabs.add(runs, text="실행 기록")
        files.columnconfigure(0, weight=1)
        files.rowconfigure(1, weight=1)
        runs.columnconfigure(0, weight=1)
        runs.rowconfigure(0, weight=1)

        filters = ttk.Frame(files)
        filters.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        search = ttk.Frame(filters)
        search.pack(fill="x")
        refine = ttk.Frame(filters)
        refine.pack(fill="x", pady=(8, 0))
        ttk.Label(refine, text="정렬", style="Muted.TLabel").pack(side="left")
        order = ttk.Combobox(refine, textvariable=self.order, values=list(ORDER_LABELS.values()),
                             state="readonly", width=13, font=theme.BODY)
        order.pack(side="left", padx=(6, 18))
        order.bind("<<ComboboxSelected>>", lambda _: self.search())
        ttk.Label(refine, text="기간", style="Muted.TLabel").pack(side="left")
        field = ttk.Combobox(refine, textvariable=self.period_field, values=list(PERIOD_LABELS.values()),
                             state="readonly", width=7, font=theme.BODY)
        field.pack(side="left", padx=6)
        field.bind("<<ComboboxSelected>>", lambda _: self.search() if self.applied_period else None)
        self.period_boxes = []
        for variable in (self.period_from, self.period_to):
            box = ttk.Entry(refine, textvariable=variable, width=11, font=theme.BODY)
            box.pack(side="left")
            box.bind("<Return>", lambda _: self.search())
            self.period_boxes.append(box)
            if variable is self.period_from:
                ttk.Label(refine, text="~", style="Muted.TLabel").pack(side="left", padx=4)
        ttk.Button(refine, text="기간 지우기", style="Compact.TButton", command=self.clear_period).pack(side="left", padx=(8, 0))
        combo = ttk.Combobox(search, textvariable=self.media_filter, values=list(MEDIA_LABELS.values()), state="readonly", width=12, font=theme.BODY)
        combo.pack(side="left")
        combo.bind("<<ComboboxSelected>>", lambda _: self.search())
        entry = ttk.Entry(search, textvariable=self.query, font=theme.BODY)
        entry.pack(side="left", fill="x", expand=True, padx=8)
        entry.bind("<Return>", lambda _: self.search())
        ttk.Button(search, text="찾기", command=self.search).pack(side="left")
        self.export_button = ttk.Button(search, text="CSV로 저장", command=self.export)
        self.export_button.pack(side="right", padx=(8, 0))

        columns = ("posted", "kind", "author", "filename", "size", "saved")
        self.file_browser = ttk.Frame(files)
        self.file_browser.grid(row=1, column=0, sticky="nsew")
        self.file_browser.columnconfigure(0, weight=1)
        self.file_browser.rowconfigure(0, weight=1)
        table = self._table(self.file_browser, columns, row=0)
        self.tree = table
        for key, title, width in zip(columns, ("트윗 게시일", "종류", "작성자", "파일명", "크기", "저장한 때"),
                                     (150, 90, 140, 280, 90, 150)):
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, minwidth=60, stretch=key == "filename")
        self.tree.bind("<<TreeviewSelect>>", self.select_record)
        self.tree.bind("<Double-1>", lambda _: self.open_file())
        self._file_columns = columns
        self._shown_columns = columns
        self.tree.bind("<Configure>", lambda _: self._layout_columns(), add="+")
        self.table_message = ttk.Label(self.tree.master, textvariable=self.table_note, style="CardMuted.TLabel",
                                       justify="center", font=theme.BODY)
        self.preview_card = ttk.Frame(self.file_browser, style="Card.TFrame", padding=12)
        self.preview_box = ttk.Frame(self.preview_card, style="Plain.TFrame", width=220, height=168)
        self.preview_box.grid_propagate(False)
        self.preview_box.columnconfigure(0, weight=1)
        self.preview_box.rowconfigure(0, weight=1)
        self.preview_label = ttk.Label(self.preview_box, style="CardMuted.TLabel", anchor="center",
                                       text="미리보기", font=theme.BODY)
        self.preview_label.grid(row=0, column=0, sticky="nsew")
        self.preview_info = ttk.Frame(self.preview_card, style="Plain.TFrame")
        ttk.Label(self.preview_info, text="파일 미리보기", style="CardHeading.TLabel").pack(anchor="w")
        ttk.Label(self.preview_info, textvariable=self.preview_note, style="CardMuted.TLabel",
                  wraplength=205, justify="left").pack(anchor="w", pady=(6, 0))
        ttk.Button(self.preview_info, text="크게 열기", command=self.open_file,
                   style="Compact.TButton").pack(anchor="w", pady=(8, 0))
        self.file_browser.bind("<Configure>", self._queue_pager_layout)

        actions = ttk.Frame(files)
        actions.grid(row=3, column=0, sticky="ew", pady=(8, 6))
        ttk.Button(actions, text="파일 열기", command=self.open_file).pack(side="left")
        ttk.Button(actions, text="트윗 열기", command=self.open_tweet).pack(side="left", padx=8)
        self.backfill_button = ttk.Button(actions, text="출처·작성자 기록", command=self.backfill)
        self.backfill_button.pack(side="left")
        self.convert_button = ttk.Button(actions, text="MP4 GIF를 GIF로", command=self.convert_gifs)
        self.convert_button.pack(side="left", padx=(8, 0))
        self._pagination(files, row=2)
        details = ttk.Frame(files, style="Card.TFrame", padding=(14, 8))
        details.grid(row=4, column=0, sticky="ew")
        details.columnconfigure(0, weight=1)
        self.selection_detail = tk.Text(details, height=4, width=1, wrap="word", font=theme.BODY,
                                        background=theme.CARD, foreground=theme.INK,
                                        borderwidth=0, highlightthickness=0, state="disabled")
        self.selection_detail.grid(row=0, column=0, sticky="ew")
        detail_scroll = ttk.Scrollbar(details, command=self.selection_detail.yview)
        detail_scroll.grid(row=0, column=1, sticky="ns")
        self.selection_detail.configure(yscrollcommand=detail_scroll.set)
        self._selection_trace = self.selection_text.trace_add("write", self._update_selection_detail)
        self._update_selection_detail()

        run_columns = ("started", "ended", "status", "saved", "skipped", "size", "visited", "last")
        self.run_tree = self._table(runs, run_columns, row=0)
        for key, title, width in zip(run_columns,
                ("시작", "끝", "결과", "새로 저장", "이미 있음", "용량 초과", "확인한 트윗", "마지막으로 확인한 트윗 게시일"),
                (160, 160, 110, 90, 90, 90, 100, 220)):
            self.run_tree.heading(key, text=title)
            self.run_tree.column(key, width=width, minwidth=60, stretch=key == "last")
        self.run_text = tk.StringVar(self, value=NO_RUN_SELECTION)
        self.run_tree.bind("<<TreeviewSelect>>", self.select_run)
        run_details = theme.card(runs)
        self._pagination(runs, row=1, runs=True)
        run_details.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        ttk.Label(run_details, textvariable=self.run_text, style="Card.TLabel", wraplength=1000, justify="left").pack(anchor="w")
        ttk.Label(body, textvariable=self.status_text, style="Muted.TLabel", wraplength=1000, justify="left").grid(row=5, column=0, sticky="w", pady=(10, 0))

    def _pagination(self, parent, *, row, runs=False):
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
        text = self.run_page_text if runs else self.page_text
        value = self.run_page_input if runs else self.page_input
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
                callback = lambda dest=destination: self.go_page(dest, runs=runs)
            else:
                callback = lambda amount=destination: self.change_page(amount, runs=runs)
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
        entry.bind("<Return>", lambda _: self.jump_page(runs=runs))
        jump = ttk.Button(jump_box, text="이동", command=lambda: self.jump_page(runs=runs), width=4, style="Compact.TButton")
        jump.grid(row=0, column=2)
        navigation._arrows = arrows
        navigation._jump_box = jump_box
        self._pagers.append((bar, info, navigation))
        bar.bind("<Configure>", self._queue_pager_layout)
        prefix = "run_" if runs else ""
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

    def _layout_preview(self):
        width = self.file_browser.winfo_width()
        if width <= 1:
            return
        side = width >= max(self.preview_box.winfo_reqwidth(), self.preview_info.winfo_reqwidth()) + 674
        if side == self._preview_side:
            return
        self._preview_side = side
        self.preview_card.grid(row=0 if side else 1, column=1 if side else 0,
                               sticky="ns" if side else "ew",
                               padx=(12 if side else 0, 0), pady=(0 if side else 8, 0))
        self.preview_card.columnconfigure(0, weight=0)
        self.preview_card.columnconfigure(1, weight=0 if side else 1)
        self.preview_box.grid(row=0, column=0, sticky="n")
        self.preview_info.grid(row=1 if side else 0, column=0 if side else 1, sticky="nw",
                               padx=(0 if side else 16, 0), pady=(10 if side else 0, 0))

    # Columns hidden first when the table is narrower than its contents; the
    # selected file's details still show them. A cut-off file name is worse.
    OPTIONAL_COLUMNS = ("saved", "size")

    def _column_need(self, key):
        if key == "filename":
            sample = "1840025000000000000_gif_1.gif"
            return int(self.tk.call("font", "measure", theme.SMALL, sample)) + 24
        return int(self.tree.column(key, "width"))

    def _layout_columns(self):
        # Until the window is laid out, its content takes the table's own
        # requested width, which hiding a column changes. Decide only from the
        # window's actual width, which the table cannot affect.
        available = self.tree.winfo_width()
        if available <= 1 or not self.winfo_viewable() or self.content_view.canvas.winfo_width() <= 1:
            return
        shown = list(self._file_columns)
        for key in self.OPTIONAL_COLUMNS:
            if sum(self._column_need(column) for column in shown) <= available:
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
        columns = next((count for count in (6, 3, 2) if count * largest + (count - 1) * 8 <= available), 1)
        if columns == self._tile_columns:
            return
        self._tile_columns = columns
        # Empty columns retain their weight after re-gridding unless explicitly
        # reset; that would steal width from the visible statistics cards.
        for column in range(6):
            self._tile_frame.columnconfigure(column, weight=1 if column < columns else 0,
                                              uniform="tile" if column < columns else "", minsize=0)
        for index, tile in enumerate(self._tiles):
            row, column = divmod(index, columns)
            tile.grid_configure(row=row, column=column,
                                padx=(8 if column else 0, 0), pady=(8 if row else 0, 0))

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
        for runs in (False, True):
            prefix = "run_" if runs else ""
            page = self.run_page if runs else self.page
            displayed = self.displayed_run_page if runs else self.displayed_page
            pages = self.run_total_pages if runs else self.total_pages
            page_ready = ready and page == displayed and self._page_size() == self.displayed_page_size
            for name in ("first", "previous"):
                getattr(self, prefix + name + "_button").configure(state="normal" if page_ready and page else "disabled")
            for name in ("next", "last"):
                getattr(self, prefix + name + "_button").configure(state="normal" if page_ready and page + 1 < pages else "disabled")
            for name in ("page_entry", "jump_button"):
                getattr(self, prefix + name).configure(state="normal" if page_ready and pages else "disabled")
            first = max(0, min(displayed - 2, pages - 5))
            for index, button in enumerate(getattr(self, prefix + "page_buttons")):
                destination = first + index
                if destination >= pages:
                    button.grid_remove()
                    continue
                button.configure(text=str(destination + 1), width=max(2, len(str(destination + 1))),
                                 style="CompactPrimary.TButton" if destination == displayed else "Compact.TButton",
                                 command=lambda page=destination, target=runs: self.go_page(page, runs=target),
                                 state="normal" if page_ready else "disabled")
                button.grid()
        self._queue_pager_layout()
        self.export_button.configure(state="normal" if ready else "disabled")
        self.backfill_button.configure(state="normal" if ready else "disabled")
        mp4_gifs = int(self.current_summary.get("mp4_gifs", 0) or 0)
        self.convert_button.configure(text=f"MP4 GIF {mp4_gifs:,}개를 GIF로" if mp4_gifs else "MP4 GIF를 GIF로",
                                      state="normal" if ready and mp4_gifs else "disabled")

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
        self.page = 0
        self.refresh()

    def clear_period(self):
        self.period_from.set("")
        self.period_to.set("")
        if self.applied_period is not None:
            self.applied_period = None
            self.page = 0
            self.refresh()

    def change_page_size(self):
        self.page = self.run_page = 0
        self.refresh()

    def change_page(self, amount, *, runs=False):
        current = self.run_page if runs else self.page
        self.go_page(current + amount, runs=runs)

    def go_page(self, page, *, runs=False):
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
        self.refresh()

    def jump_page(self, *, runs=False):
        value = self.run_page_input if runs else self.page_input
        current = self.displayed_run_page if runs else self.displayed_page
        try:
            page = int(value.get().strip()) - 1
        except ValueError:
            value.set(str(current + 1))
            return
        self.go_page(page, runs=runs)
        value.set(str((self.run_page if runs else self.page) + 1))

    def _page_key(self, event):
        # Keep Home/End and Page Up/Down editing/scrolling behavior in fields
        # and selected-file text. In a table these keys scroll its rows, so
        # pages change there only with Ctrl; other widgets need no modifier.
        widget_class = event.widget.winfo_class()
        if widget_class in {"Entry", "TEntry", "Text", "TCombobox", "Spinbox", "TSpinbox"}:
            return
        if widget_class == "Treeview" and not getattr(event, "state", 0) & 0x4:
            return
        runs = self.tabs.index("current") == 1
        if event.keysym == "Prior":
            self.change_page(-1, runs=runs)
        elif event.keysym == "Next":
            self.change_page(1, runs=runs)
        else:
            self.go_page(0 if event.keysym == "Home" else None, runs=runs)
        return "break"

    def refresh(self, *, import_legacy=False):
        if self.busy:
            self.pending = True
            self.pending_import = self.pending_import or import_legacy
            return
        try:
            account, directory = self.get_location()
        except (ValueError, OSError):
            self.current_account_dir = None
            self.current_storage = None
            self.completion_text = ""
            self.page = self.run_page = 0
            self.location_text.set("")
            self._display({}, [], [])
            self.status_text.set("먼저 메인 화면에 X 아이디와 저장 폴더를 입력해 주세요.")
            self._show_table_note("메인 화면에 X 아이디와 저장 폴더를 입력하면\n그 폴더에 저장한 기록이 여기에 나옵니다.")
            return
        location = directory
        directory = location.record_dir if isinstance(location, StoragePaths) else Path(location)
        if directory != self.current_account_dir:
            self.page = self.run_page = 0
            self.completion_text = ""
            # A media identity or run ID is local to its account catalog.
            # Clear the previous catalog before accepting the new snapshot.
            self._display({}, [], [])
        self.current_account_dir = directory
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
        threading.Thread(target=partial(HistoryWindow._load_worker, self.results), args=(generation, location, kind, query, self.page, do_import,
                                                 self._page_size(), self.run_page, order, self.applied_period), daemon=True).start()

    @staticmethod
    def _store(location):
        return HistoryStore.for_storage(location) if isinstance(location, StoragePaths) else HistoryStore(location)

    def _load(self, generation, directory, kind, query, page, do_import, page_size=PAGE_SIZE, run_page=0,
              order="saved", period=None):
        return self._load_worker(self.results, generation, directory, kind, query, page, do_import, page_size, run_page,
                                 order, period)

    @staticmethod
    def _load_worker(results, generation, directory, kind, query, page, do_import, page_size=PAGE_SIZE, run_page=0,
                     order="saved", period=None):
        store = None
        try:
            store = HistoryWindow._store(directory)

            def snapshot(warnings):
                summary = store.summary()
                summary["warnings"] = warnings
                files = store.record_page(page, page_size, kind, query, order=order, period=period)
                runs = store.run_page(run_page, page_size)
                return store.account_dir, summary, files["rows"], runs["rows"], files["page"], files, runs

            if do_import:
                # Show what the catalog already holds at once. Re-checking every
                # file of a large library takes seconds, and the table must not
                # look empty meanwhile.
                results.put((generation, "preview", snapshot([])))
                imported = store.import_legacy(refresh=True)
                results.put((generation, "loaded", snapshot(imported.get("warnings", []))))
            else:
                results.put((generation, "loaded", snapshot([])))
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
                if self.metadata_active:
                    self.metadata_active = False
                    self.source_stop_button.configure(state="disabled")
                    self.source_stop_button.pack_forget()
                if self.closing:
                    self.destroy()
                    return
                if kind == "loaded":
                    directory, *snapshot = result
                    self.loaded_locations.add(directory)
                    if not self.pending:
                        self._display(*snapshot)
                else:
                    self.status_text.set(result)
                    if kind in {"backfilled", "converted"}:
                        self.completion_text = result
                    elif kind == "error" and not self.tree.get_children():
                        self._show_table_note("기록을 읽지 못했습니다.\n아래 안내를 확인해 주세요.")
                self._update_controls()
                if self.pending:
                    self.pending = False
                    do_import, self.pending_import = self.pending_import, False
                    self.refresh(import_legacy=do_import)
                elif kind in {"backfilled", "converted"}:
                    self.refresh()
        except queue.Empty:
            pass
        self._poll_previews()
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
            f"파일 기록 {files:,}개 · 트윗 {values['posts']:,}개 · 사진 {values['photo']:,}개 / 동영상 {values['video']:,}개 / "
            f"움직이는 GIF {values['animated_gif']:,}개 · 확인된 용량 {format_bytes(summary.get('bytes', 0))}")
        oldest, newest = summary.get("oldest_posted_at"), summary.get("newest_posted_at")
        notes = []
        if summary.get("estimated_dates", 0):
            notes.append("* 표시는 트윗 번호로 추정한 날짜")
        if summary.get("unknown_bytes", 0):
            notes.append(f"크기를 모르는 파일 {summary['unknown_bytes']:,}개")
        range_text = (f"저장한 트윗의 게시일: {format_day(oldest)} ~ {format_day(newest)} (이 기간의 Like를 모두 저장했다는 뜻은 아닙니다)"
                      if oldest else "저장한 트윗의 게시일: 아직 없음")
        self.date_text.set(range_text + ("  ·  " + " · ".join(notes) if notes else ""))
        selected = self.selected()
        selected_identity = self._record_identity(selected) if selected else None
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
            self.tree.insert("", "end", iid=key, values=(posted, MEDIA_LABELS.get(row.get("media_type"), "알 수 없음"), author, filename,
                                                         format_bytes(row.get("filesize")), format_short(row.get("saved_at"))))
        for key, row in self.rows.items():
            if selected_identity is not None and self._record_identity(row) == selected_identity:
                self.tree.selection_set(key)
                self.tree.focus(key)
                self.tree.see(key)
                break
        if records:
            self._show_table_note("")
        elif scanning:
            self._show_table_note("폴더의 파일을 확인하는 중입니다…\n확인이 끝나면 저장한 기록을 보여 드립니다.")
        elif files:
            self._show_table_note("찾는 조건에 맞는 파일이 없습니다.")
        else:
            self._show_table_note("아직 저장한 파일이 없습니다.\n메인 화면에서 ‘저장 시작’을 누르면 여기에 쌓입니다.")
        self.displayed_page = self.page if page is None else page
        self.displayed_page_size = page_size
        total = file_paging["total"] if file_paging else len(records)
        self.total_pages = file_paging["pages"] if file_paging else (total + page_size - 1) // page_size
        self.page = self.displayed_page
        self.has_next_page = self.displayed_page + 1 < self.total_pages
        self.page_text.set(f"총 {total:,}개 · {self.displayed_page + 1 if total else 0} / {self.total_pages:,}페이지")
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
        self._queue_pager_layout()
        self._update_controls()
        warning = " ".join(summary.get("warnings", []))
        self.status_text.set((self.completion_text + "\n" if self.completion_text else "") + (warning + "\n" if warning else "") +
                             f"파일 안에 출처가 기록된 파일 {summary.get('embedded_sources', 0):,}개 · 작성자까지 기록된 파일 {summary.get('embedded_authors', 0):,}개")
        self.select_record(force=True)
        self.select_run()

    @staticmethod
    def _record_identity(row):
        return tuple(row.get(key) for key in ("folder_mode", "tweet_id", "num", "media_type"))

    def selected(self):
        selection = self.tree.selection()
        return self.rows.get(selection[0]) if selection else None

    def select_record(self, _=None, *, force=False):
        row = self.selected()
        self._start_preview(row, force=force)
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
            f"작성자  {author}   ·   Like한 시기  {liked_period(row)}\n"
            f"트윗     {row.get('source_url') or 'https://x.com/i/web/status/' + row['tweet_id']}\n"
            f"파일     {row.get('path') or '위치 미확인 (이전 다운로드 기록만 있음)'}\n"
            f"저장한 때 {saved if saved != '–' else '알 수 없음'} · 파일 안 기록: {metadata} · {author_status} · 게시일 출처: {source}")

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
            scan = "새 Like만 빠르게 확인" if row.get("scan_mode") == "quick" else "전체 목록 확인"
            self.run_text.set(f"{row.get('message') or '실행 중이거나 종료 설명이 없습니다.'}\n확인 방식: {scan} · 마지막으로 확인한 트윗: {row.get('last_tweet_id') or '기록 없음'}")
        else:
            self.run_text.set(NO_RUN_SELECTION)

    def open_file(self):
        row = self.selected()
        if not row:
            return
        path = Path(row["path"]) if row.get("path") else None
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
        dialogs.showinfo("기록 저장 위치", f"기록·통계:\n{self.current_account_dir / '.xlikesaver-history.sqlite3'}\n\n중복 방지:\n{archive_text}\n\n기록은 계정별로 관리합니다. 저장 폴더의 .xlikesaver 폴더를 지우면 앱이 저장 내역을 잊습니다. 파일 목록은 저장 시각 순서이며 예전 저장 시각은 알 수 없습니다.", parent=self)

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
        if not dialogs.askyesno(
                "출처·작성자 기록",
                "예전에 받은 파일 안에 트윗 주소와 작성자를 기록합니다.\n\n"
                "· 화질은 그대로이며 X에 요청하지 않습니다.\n"
                "· 파일을 다시 쓰므로 Google Drive·OneDrive 폴더라면 다시 올라갈 수 있습니다.\n"
                "· 이미 기록된 파일은 건너뜁니다. 중간에 멈춰도 됩니다.\n\n계속할까요?", parent=self):
            return
        self._start_local_job("출처·작성자 기록 중단", "출처와 작성자를 기록하는 중입니다… 화질은 그대로이며 X에 요청하지 않습니다.")
        threading.Thread(target=partial(HistoryWindow._backfill_worker, self.results, self.metadata_stop),
                         args=(self.generation, self.current_storage), daemon=True).start()

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
        if not dialogs.askyesno(
                "MP4 GIF를 GIF로 바꾸기",
                f"MP4로 저장된 움직이는 GIF {count:,}개를 .gif 파일로 바꿉니다.\n\n"
                f"· GIF 크기: {GIF_SIZE_LABELS.get(size, size)} (저장 옵션에서 바꿀 수 있습니다)\n"
                "· GIF는 보통 MP4보다 용량이 몇 배 커집니다. 영상 용량 제한을 넘는 파일은 MP4로 둡니다.\n"
                "· 바꾼 파일의 원본 MP4는 휴지통으로 옮깁니다. 휴지통이 없는 위치의 파일은 바꾸지 않습니다.\n"
                "· Google Drive·OneDrive 폴더라면 새 GIF가 다시 올라갑니다.\n"
                "· X에 요청하지 않으며, 중간에 멈춰도 됩니다.\n\n계속할까요?", parent=self):
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

        store = account_guard = None
        output_guards = []
        converted = kept = failed = missing = 0
        recyclable = {}

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
                    status = author_status = "pending"
                    try:
                        if tagging:
                            identity = author_identity(row.get("author", ""), row.get("author_id", ""))
                            tagged = embed_source(temporary, row["tweet_id"], author=identity["author"],
                                                  author_id=identity["author_id"])
                            status = tagged["status"]
                            author_status = tagged.get("author_status", "error" if identity["author"] or identity["author_id"] else "unknown")
                        # Keep the original file time, which sorts by tweet date.
                        times = source.stat()
                        os.utime(temporary, ns=(times.st_atime_ns, times.st_mtime_ns))
                        move_without_overwrite(temporary, destination)
                    except OSError:
                        failed += 1
                        continue
                    finally:
                        temporary.unlink(missing_ok=True)
                    # Record first, then recycle: either step failing leaves the
                    # MP4 and its record exactly as before, without the new GIF.
                    try:
                        recorded = store.replace_media_file(row, destination, status, author_status=author_status)
                    except HistoryError:
                        recorded = False
                    if recorded and not recycle(source):
                        store.replace_media_file(dict(row, path=str(destination.resolve())), source,
                                                 row["metadata_status"], author_status=row["author_metadata_status"])
                        recorded = False
                    if not recorded:
                        destination.unlink(missing_ok=True)
                        failed += 1
                        continue
                    converted += 1
            state = "GIF 변환 중단" if stop.is_set() else "GIF 변환 끝"
            recycled = " 바꾼 파일의 원본 MP4는 휴지통에 있습니다." if converted else ""
            results.put((generation, "converted", report(state) + "." + recycled + " X 요청은 보내지 않았습니다."))
        except ProtectionError as exc:
            results.put((generation, "error", str(exc)))
        except Exception:
            results.put((generation, "error", "GIF 변환을 마치지 못했습니다. 기록 DB와 파일 접근 권한을 확인해 주세요."))
        finally:
            try:
                if store is not None:
                    store.close()
            finally:
                try:
                    for guard in reversed(output_guards):
                        guard.close()
                finally:
                    if account_guard is not None:
                        account_guard.close()

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
            results.put((generation, "backfilled", f"출처·작성자 기록 {state} · 완료 {success:,}개 / 실패·미지원 {failed:,}개 / 파일 없음 {unavailable:,}개. X 요청은 보내지 않았습니다."))
        except ProtectionError as exc:
            results.put((generation, "error", str(exc)))
        except Exception:
            results.put((generation, "error", "메타데이터 기록을 마치지 못했습니다. 기록 DB와 파일 접근 권한을 확인해 주세요."))
        finally:
            try:
                if store is not None:
                    store.close()
            finally:
                try:
                    for guard in reversed(output_guards):
                        guard.close()
                finally:
                    if account_guard is not None:
                        account_guard.close()

    def stop_metadata(self):
        self.metadata_stop.set()
        self.source_stop_button.configure(state="disabled")
        self.status_text.set("현재 파일 처리가 끝나면 작업을 멈춥니다…")

    def close(self):
        self.metadata_stop.set()
        if self.metadata_active:
            self.closing = True
            self.stop_metadata()
        else:
            self.destroy()

    def destroy(self):
        if self._destroyed:
            return
        self._destroyed = True
        self._preview_loader.close()
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
        self.get_location = self.is_downloading = self.get_gif_options = None

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

"""Local records and statistics, without contacting X."""

from __future__ import annotations

from contextlib import closing
from datetime import datetime
import os
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import webbrowser

from . import theme
from .history import HistoryStore
from .storage import OutputGuard, StoragePaths


MEDIA_LABELS = {"all": "전체", "photo": "사진", "video": "동영상", "animated_gif": "움직이는 GIF"}
STATUS_LABELS = {"running": "진행 중", "finished": "완료", "stopped": "직접 중단", "error": "오류", "interrupted": "비정상 종료", "limited": "시험 저장 완료", "partial": "일부만 완료", "cancelled": "취소됨"}
METADATA_LABELS = {"embedded": "파일에 출처 기록됨", "pending": "출처 기록 전", "failed": "파일 출처 기록 실패", "unsupported": "메타데이터 미지원 형식"}
AUTHOR_METADATA_LABELS = {"embedded": "작성자 기록됨", "pending": "작성자 기록 전", "error": "작성자 기록 실패", "unknown": "작성자 정보 없음"}
TILES = {"files": "저장한 파일", "posts": "트윗", "photo": "사진", "video": "동영상", "animated_gif": "움직이는 GIF", "bytes": "확인된 용량"}
PAGE_SIZE = 100
NO_SELECTION = ("표에서 파일을 고르면 출처와 저장 위치가 여기에 나옵니다. 두 번 누르면 파일이 열립니다.\n"
                "위 검색칸에서 작성자 아이디, 파일명, 트윗 번호로 찾을 수 있습니다.")


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


def format_day(value: str | None) -> str:
    try:
        return datetime.fromisoformat(value).strftime("%Y-%m-%d") if value else "–"
    except (ValueError, TypeError):
        return "–"


class HistoryWindow(tk.Toplevel):
    def __init__(self, parent, get_location, is_downloading=lambda: False):
        super().__init__(parent)
        theme.apply_style(self)
        self.title("XlikeSaver · 기록과 통계")
        width = min(1120, max(860, self.winfo_screenwidth() - 80))
        height = min(860, max(640, self.winfo_screenheight() - 100))
        self.geometry(f"{width}x{height}")
        self.minsize(min(860, width), min(680, height))
        self.configure(bg=theme.BG)
        self.transient(parent)
        self.get_location = get_location
        self.is_downloading = is_downloading
        self.page = 0
        self.busy = False
        self.metadata_active = False
        self.metadata_stop = threading.Event()
        self.pending = False
        self.pending_import = False
        self.generation = 0
        self.closing = False
        self._destroyed = False
        self._poll_timer = None
        self.loaded_locations = set()
        self.results = queue.Queue()
        self.rows = {}
        self.run_rows = {}
        self.current_account_dir = None
        self.current_storage = None
        self.current_summary = {}
        self.completion_text = ""
        self.summary_text = tk.StringVar(value="기록을 불러오는 중…")
        self.tile_values = {key: tk.StringVar(value="–") for key in TILES}
        self.date_text = tk.StringVar()
        self.location_text = tk.StringVar()
        self.status_text = tk.StringVar()
        self.table_note = tk.StringVar(value="기록을 불러오는 중…")
        self.selection_text = tk.StringVar(value=NO_SELECTION)
        self.media_filter = tk.StringVar(value="전체")
        self.query = tk.StringVar()
        self.page_text = tk.StringVar()
        self._build()
        self.wrapping_labels = []

        def collect_labels(widget):
            for child in widget.winfo_children():
                if isinstance(child, ttk.Label) and int(child.cget("wraplength") or 0):
                    self.wrapping_labels.append(child)
                collect_labels(child)
        collect_labels(self)
        self.bind("<Configure>", self._resize_text)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self._poll_timer = self.after(100, self._poll)
        self.refresh(import_legacy=True)

    def _build(self):
        body = ttk.Frame(self, padding=(22, 16))
        body.pack(fill="both", expand=True)
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
        for column, (key, caption) in enumerate(TILES.items()):
            theme.tile(tiles, self.tile_values[key], caption).grid(row=0, column=column, sticky="ew", padx=(0 if column == 0 else 8, 0))
            tiles.columnconfigure(column, weight=1, uniform="tile")
        ttk.Label(body, textvariable=self.date_text, style="Muted.TLabel", wraplength=1000, justify="left").grid(row=3, column=0, sticky="w", pady=(8, 10))

        tabs = ttk.Notebook(body)
        tabs.grid(row=4, column=0, sticky="nsew")
        files = ttk.Frame(tabs, padding=(10, 12))
        runs = ttk.Frame(tabs, padding=(10, 12))
        tabs.add(files, text="저장한 파일")
        tabs.add(runs, text="실행 기록")
        files.columnconfigure(0, weight=1)
        files.rowconfigure(1, weight=1)
        runs.columnconfigure(0, weight=1)
        runs.rowconfigure(0, weight=1)

        search = ttk.Frame(files)
        search.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        combo = ttk.Combobox(search, textvariable=self.media_filter, values=list(MEDIA_LABELS.values()), state="readonly", width=12, font=theme.BODY)
        combo.pack(side="left")
        combo.bind("<<ComboboxSelected>>", lambda _: self.search())
        entry = ttk.Entry(search, textvariable=self.query, font=theme.BODY)
        entry.pack(side="left", fill="x", expand=True, padx=8)
        entry.bind("<Return>", lambda _: self.search())
        ttk.Button(search, text="찾기", command=self.search).pack(side="left")
        ttk.Button(search, text="CSV로 저장", command=self.export).pack(side="right", padx=(8, 0))

        columns = ("posted", "kind", "author", "filename", "size", "saved")
        table = self._table(files, columns, row=1)
        self.tree = table
        for key, title, width in zip(columns, ("트윗 게시일", "종류", "작성자", "파일명", "크기", "저장한 때"),
                                     (150, 90, 140, 280, 90, 150)):
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, minwidth=60, stretch=key == "filename")
        self.tree.bind("<<TreeviewSelect>>", self.select_record)
        self.tree.bind("<Double-1>", lambda _: self.open_file())
        self.table_message = ttk.Label(self.tree.master, textvariable=self.table_note, style="CardMuted.TLabel",
                                       justify="center", font=theme.BODY)

        actions = ttk.Frame(files)
        actions.grid(row=3, column=0, sticky="ew", pady=(8, 6))
        ttk.Button(actions, text="파일 열기", command=self.open_file).pack(side="left")
        ttk.Button(actions, text="트윗 열기", command=self.open_tweet).pack(side="left", padx=8)
        self.backfill_button = ttk.Button(actions, text="출처·작성자 기록", command=self.backfill)
        self.backfill_button.pack(side="left")
        self.next_button = ttk.Button(actions, text="다음 ▶", command=lambda: self.change_page(1))
        self.next_button.pack(side="right")
        self.previous_button = ttk.Button(actions, text="◀ 이전", command=lambda: self.change_page(-1))
        self.previous_button.pack(side="right", padx=8)
        ttk.Label(actions, textvariable=self.page_text, style="Muted.TLabel").pack(side="right", padx=8)
        details = ttk.Frame(files, style="Card.TFrame", padding=(14, 8))
        details.grid(row=4, column=0, sticky="ew")
        ttk.Label(details, textvariable=self.selection_text, style="Card.TLabel", wraplength=1000, justify="left").pack(anchor="w")

        run_columns = ("started", "ended", "status", "saved", "skipped", "size", "visited", "last")
        self.run_tree = self._table(runs, run_columns, row=0)
        for key, title, width in zip(run_columns,
                ("시작", "끝", "결과", "새로 저장", "이미 있음", "용량 초과", "확인한 트윗", "마지막으로 확인한 트윗 게시일"),
                (160, 160, 110, 90, 90, 90, 100, 220)):
            self.run_tree.heading(key, text=title)
            self.run_tree.column(key, width=width, minwidth=60, stretch=key == "last")
        self.run_text = tk.StringVar(value="실행을 고르면 어떻게 끝났는지 볼 수 있습니다.")
        self.run_tree.bind("<<TreeviewSelect>>", self.select_run)
        run_details = theme.card(runs)
        run_details.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        ttk.Label(run_details, textvariable=self.run_text, style="Card.TLabel", wraplength=1000, justify="left").pack(anchor="w")
        ttk.Label(body, textvariable=self.status_text, style="Muted.TLabel", wraplength=1000, justify="left").grid(row=5, column=0, sticky="w", pady=(10, 0))

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
        tree = ttk.Treeview(frame, columns=columns, show="headings", selectmode="browse", height=8)
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

    def search(self):
        self.page = 0
        self.refresh()

    def change_page(self, amount):
        self.page = max(0, self.page + amount)
        self.refresh()

    def refresh(self, *, import_legacy=False):
        if self.busy:
            self.pending = True
            self.pending_import = self.pending_import or import_legacy
            return
        try:
            account, directory = self.get_location()
        except (ValueError, OSError):
            self.status_text.set("먼저 메인 화면에 X 아이디와 저장 폴더를 입력해 주세요.")
            self._show_table_note("메인 화면에 X 아이디와 저장 폴더를 입력하면\n그 폴더에 저장한 기록이 여기에 나옵니다.")
            return
        location = directory
        directory = location.record_dir if isinstance(location, StoragePaths) else Path(location)
        if directory != self.current_account_dir:
            self.page = 0
            self.completion_text = ""
        self.current_account_dir = directory
        self.current_storage = location
        media_dir = location.media_dir if isinstance(location, StoragePaths) else directory
        self.location_text.set(f"@{account} · 저장 폴더 {media_dir}")
        self.busy = True
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
        threading.Thread(target=self._load, args=(generation, location, kind, self.query.get(), self.page, do_import), daemon=True).start()

    @staticmethod
    def _store(location):
        return HistoryStore.for_storage(location) if isinstance(location, StoragePaths) else HistoryStore(location)

    def _load(self, generation, directory, kind, query, page, do_import):
        store = None
        try:
            store = self._store(directory)

            def snapshot(warnings):
                summary = store.summary()
                summary["warnings"] = warnings
                rows = store.records(limit=PAGE_SIZE + 1, offset=page * PAGE_SIZE, media_type=kind, query=query)
                return store.account_dir, summary, rows, store.runs(limit=30)

            if do_import:
                # Show what the catalog already holds at once. Re-checking every
                # file of a large library takes seconds, and the table must not
                # look empty meanwhile.
                self.results.put((generation, "preview", snapshot([])))
                imported = store.import_legacy(refresh=True)
                self.results.put((generation, "loaded", snapshot(imported.get("warnings", []))))
            else:
                self.results.put((generation, "loaded", snapshot([])))
        except Exception:
            self.results.put((generation, "error", "기록을 읽지 못했습니다. 저장 폴더 권한과 다른 실행 중인 작업을 확인해 주세요."))
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
                if kind == "metadata-progress":
                    self.status_text.set(result)
                    continue
                if kind == "preview":
                    self._display(*result[1:])
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
                    directory, summary, rows, runs = result
                    self.loaded_locations.add(directory)
                    self._display(summary, rows, runs)
                else:
                    self.status_text.set(result)
                    if kind == "backfilled":
                        self.completion_text = result
                    elif kind == "error" and not self.tree.get_children():
                        self._show_table_note("기록을 읽지 못했습니다.\n아래 안내를 확인해 주세요.")
                if self.pending:
                    self.pending = False
                    do_import, self.pending_import = self.pending_import, False
                    self.refresh(import_legacy=do_import)
                elif kind == "backfilled":
                    self.refresh()
        except queue.Empty:
            pass
        if self.winfo_exists():
            self._poll_timer = self.after(120, self._poll)

    def _display(self, summary, records, runs):
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
        selected = self.tree.selection()
        self.tree.delete(*self.tree.get_children())
        self.rows.clear()
        for index, row in enumerate(records[:PAGE_SIZE]):
            key = str(index)
            self.rows[key] = row
            posted = format_short(row.get("posted_at"))
            if row.get("posted_source") == "id_estimate":
                posted += " *"
            filename = row.get("filename") or (Path(row["path"]).name if row.get("path") else f"{row['tweet_id']}_{row['num']} (위치 없음)")
            author = "@" + row["author"] if row.get("author") else "–"
            self.tree.insert("", "end", iid=key, values=(posted, MEDIA_LABELS.get(row.get("media_type"), "알 수 없음"), author, filename,
                                                         format_bytes(row.get("filesize")), format_short(row.get("saved_at"))))
        if selected and self.tree.exists(selected[0]):
            self.tree.selection_set(selected[0])
        if records:
            self._show_table_note("")
        elif files:
            self._show_table_note("찾는 조건에 맞는 파일이 없습니다.")
        else:
            self._show_table_note("아직 저장한 파일이 없습니다.\n메인 화면에서 ‘저장 시작’을 누르면 여기에 쌓입니다.")
        self.previous_button.configure(state="normal" if self.page else "disabled")
        self.next_button.configure(state="normal" if len(records) > PAGE_SIZE else "disabled")
        start = self.page * PAGE_SIZE
        shown = len(records[:PAGE_SIZE])
        self.page_text.set(f"{start + 1:,}–{start + shown:,}번째" if shown else "")
        self.run_tree.delete(*self.run_tree.get_children())
        self.run_rows.clear()
        for index, row in enumerate(runs):
            key = str(index)
            self.run_rows[key] = row
            self.run_tree.insert("", "end", iid=key, values=(format_short(row.get("started_at")), format_short(row.get("finished_at")),
                                                             STATUS_LABELS.get(row.get("status"), "기록 없음"), row.get("saved", 0),
                                                             row.get("skipped", 0), row.get("size_skipped", 0), row.get("visited", 0),
                                                             format_short(row.get("last_posted_at"))))
        warning = " ".join(summary.get("warnings", []))
        self.status_text.set((self.completion_text + "\n" if self.completion_text else "") + (warning + "\n" if warning else "") +
                             f"파일 안에 출처가 기록된 파일 {summary.get('embedded_sources', 0):,}개 · 작성자까지 기록된 파일 {summary.get('embedded_authors', 0):,}개")
        self.select_record()

    def selected(self):
        selection = self.tree.selection()
        return self.rows.get(selection[0]) if selection else None

    def select_record(self, _=None):
        row = self.selected()
        if row is None:
            self.selection_text.set(NO_SELECTION)
            return
        source = "트윗 번호로 추정" if row.get("posted_source") == "id_estimate" else "X 목록"
        metadata = METADATA_LABELS.get(row.get("metadata_status"), "출처 기록 전")
        author_status = AUTHOR_METADATA_LABELS.get(row.get("author_metadata_status"), "작성자 기록 전")
        if not row.get("author") and not row.get("author_id"):
            author_status = AUTHOR_METADATA_LABELS["unknown"]
        author = "@" + row["author"] if row.get("author") else "정보 없음"
        if row.get("author_id"):
            author += " (ID " + row["author_id"] + ")"
        self.selection_text.set(
            f"작성자  {author}\n"
            f"트윗     {row.get('source_url') or 'https://x.com/i/web/status/' + row['tweet_id']}\n"
            f"파일     {row.get('path') or '위치 미확인 (이전 다운로드 기록만 있음)'}\n"
            f"파일 안 기록: {metadata} · {author_status} · 게시일 출처: {source}")

    def select_run(self, _=None):
        selection = self.run_tree.selection()
        row = self.run_rows.get(selection[0]) if selection else None
        if row:
            scan = "새 Like만 빠르게 확인" if row.get("scan_mode") == "quick" else "전체 목록 확인"
            self.run_text.set(f"{row.get('message') or '실행 중이거나 종료 설명이 없습니다.'}\n확인 방식: {scan} · 마지막으로 확인한 트윗: {row.get('last_tweet_id') or '기록 없음'}")

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
            messagebox.showinfo("파일 위치 확인", "파일을 찾지 못했습니다. 이동·삭제되었거나 이전 다운로드 기록에 위치가 남아 있지 않습니다.", parent=self)
            return
        try:
            os.startfile(str(path))
        except OSError:
            messagebox.showerror("파일 열기 실패", "이 파일을 여는 프로그램과 파일 접근 권한을 확인해 주세요.", parent=self)

    def open_tweet(self):
        row = self.selected()
        if row and str(row["tweet_id"]).isdigit():
            webbrowser.open(f"https://x.com/i/web/status/{row['tweet_id']}")

    def show_locations(self):
        if self.current_account_dir is None:
            return
        archives = self.current_summary.get("archivepaths", [])
        archive_text = "\n".join(map(str, archives)) or "아직 다운로드 기록 파일이 없습니다."
        messagebox.showinfo("기록 저장 위치", f"기록·통계:\n{self.current_account_dir / '.xlikesaver-history.sqlite3'}\n\n중복 방지:\n{archive_text}\n\n기록은 계정별로 관리합니다. 저장 폴더의 .xlikesaver 폴더를 지우면 앱이 저장 내역을 잊습니다. 파일 목록은 저장 시각 순서이며 예전 저장 시각은 알 수 없습니다.", parent=self)

    def export(self):
        if self.busy or self.current_account_dir is None:
            return
        destination = filedialog.asksaveasfilename(parent=self, title="전체 저장 기록 CSV 내보내기", defaultextension=".csv", initialfile="XlikeSaver-records.csv", filetypes=[("CSV 파일", "*.csv")])
        if not destination:
            return
        self.busy = True
        self.generation += 1
        self.status_text.set("전체 파일 기록을 CSV로 내보내고 있습니다…")
        threading.Thread(target=self._export, args=(self.generation, self.current_storage, destination), daemon=True).start()

    def backfill(self):
        if self.busy or self.current_account_dir is None:
            return
        if self.is_downloading():
            messagebox.showinfo("다운로드 진행 중", "저장 작업이 끝난 뒤 출처·작성자를 기록해 주세요. 이 작업은 X에 요청하지 않습니다.", parent=self)
            return
        from .metadata import available
        if not available():
            messagebox.showinfo("출처 기록 도구 없음", "배포 파일을 다시 받아주세요. 개발 중이면 setup.ps1을 실행해 주세요.", parent=self)
            return
        if not messagebox.askyesno(
                "출처·작성자 기록",
                "예전에 받은 파일 안에 트윗 주소와 작성자를 기록합니다.\n\n"
                "· 화질은 그대로이며 X에 요청하지 않습니다.\n"
                "· 파일을 다시 쓰므로 Google Drive·OneDrive 폴더라면 다시 올라갈 수 있습니다.\n"
                "· 이미 기록된 파일은 건너뜁니다. 중간에 멈춰도 됩니다.\n\n계속할까요?", parent=self):
            return
        self.completion_text = ""
        self.busy = True
        self.metadata_active = True
        self.metadata_stop.clear()
        self.source_stop_button.configure(state="normal")
        self.source_stop_button.pack(side="right", padx=(0, 8))
        self.generation += 1
        self.status_text.set("출처와 작성자를 기록하는 중입니다… 화질은 그대로이며 X에 요청하지 않습니다.")
        threading.Thread(target=self._backfill, args=(self.generation, self.current_storage), daemon=True).start()

    def _backfill(self, generation, directory):
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
            store = self._store(directory)
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
                        if self.metadata_stop.is_set():
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
                        self.results.put((generation, "metadata-progress", f"출처·작성자 기록 · 완료 {success:,}개 / 실패·미지원 {failed:,}개 / 파일 없음 {unavailable:,}개"))
                    if self.metadata_stop.is_set():
                        break
                    offset += len(rows)
            state = "중단" if self.metadata_stop.is_set() else "종료"
            self.results.put((generation, "backfilled", f"출처·작성자 기록 {state} · 완료 {success:,}개 / 실패·미지원 {failed:,}개 / 파일 없음 {unavailable:,}개. X 요청은 보내지 않았습니다."))
        except ProtectionError as exc:
            self.results.put((generation, "error", str(exc)))
        except Exception:
            self.results.put((generation, "error", "메타데이터 기록을 마치지 못했습니다. 기록 DB와 파일 접근 권한을 확인해 주세요."))
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
        self.status_text.set("현재 파일 처리가 끝나면 메타데이터 기록을 중단합니다…")

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
        if self._poll_timer is not None:
            self.tk.call("after", "cancel", self._poll_timer)
            self._poll_timer = None
        super().destroy()

    def _export(self, generation, directory, destination):
        store = None
        try:
            store = self._store(directory)
            store.export_records(Path(destination))
            self.results.put((generation, "exported", f"CSV 내보내기 완료 · {destination}"))
        except Exception:
            self.results.put((generation, "error", "CSV 파일을 저장하지 못했습니다. 파일 접근 권한을 확인해 주세요."))
        finally:
            if store is not None:
                store.close()

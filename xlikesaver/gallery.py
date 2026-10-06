"""A scrollable grid of thumbnails for one page of records.

Thumbnails decode on a background thread (local files only, permission checked
there against the catalog); Tk images are created and released on the UI
thread. Selecting a tile selects the same record in the table.
"""

from __future__ import annotations

from collections import OrderedDict
import os
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import ttk

from PIL import ImageTk

from . import theme
from .preview import thumbnail


TILE = (150, 112)
_GAP = 10
_CACHE_SIZE = 240
_SELECTED = "#e3eaff"           # theme maps this to the dark selection color
_SELECTED_INK = "#314fbb"


class ThumbnailGrid(ttk.Frame):
    def __init__(self, parent, *, on_select, on_open):
        super().__init__(parent, style="Card.TFrame", padding=8)
        self.on_select, self.on_open = on_select, on_open
        self.canvas = tk.Canvas(self, background=theme.CARD, highlightthickness=0, borderwidth=0, width=1, height=1)
        self.scrollbar = ttk.Scrollbar(self, command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.scrollbar.grid(row=0, column=1, sticky="ns")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.inner = tk.Frame(self.canvas, background=theme.CARD)
        self.window = self.canvas.create_window(0, 0, anchor="nw", window=self.inner)
        self.empty = tk.Label(self.inner, text="", background=theme.CARD, foreground=theme.MUTED, font=theme.BODY)
        self.tiles: dict[str, tk.Label] = {}
        self.images: dict[str, ImageTk.PhotoImage] = {}
        self.order: list[str] = []
        self.selected = None
        self.columns = 0
        self.placeholder = tk.PhotoImage(master=self, width=TILE[0], height=TILE[1])
        self._cache: OrderedDict = OrderedDict()
        self._cache_lock = threading.Lock()
        self._results = queue.Queue()
        self._token = 0
        self._stop = threading.Event()
        self.canvas.bind("<Configure>", lambda _: self._layout())
        for widget in (self.canvas, self.inner, self.empty):
            widget.bind("<MouseWheel>", self._wheel)

    # -------------------------------------------------------------- contents
    def show(self, rows, permitted, *, empty_text="표시할 파일이 없습니다."):
        """rows: [(key, record)] in display order. permitted(paths) runs on
        the decoding thread and returns the subset that may be read."""
        self._token += 1
        self._stop.set()
        self._stop = threading.Event()
        for tile in self.tiles.values():
            tile.destroy()
        self.tiles.clear()
        self.images.clear()
        self.order = [key for key, _ in rows]
        self.selected = None
        self.columns = 0
        jobs = []
        for key, row in rows:
            author = "@" + row["author"] if row.get("author") else "작성자 미확인"
            day = str(row.get("posted_at") or "")[:10] or "게시일 미확인"
            tile = tk.Label(self.inner, image=self.placeholder, text=f"{author}\n{day}", compound="top",
                            background=theme.CARD, foreground=theme.INK, font=theme.SMALL, justify="center",
                            wraplength=TILE[0], padx=4, pady=4, cursor="hand2")
            tile.bind("<Button-1>", lambda _, key=key: self.on_select(key))
            tile.bind("<Double-1>", lambda _, key=key: self.on_open(key))
            tile.bind("<MouseWheel>", self._wheel)
            self.tiles[key] = tile
            if row.get("path"):
                jobs.append((key, row["path"]))
        self.empty.configure(text="" if rows else empty_text)
        self._layout()
        self.canvas.yview_moveto(0)
        if jobs:
            threading.Thread(target=self._decode, args=(self._token, self._stop, jobs, permitted), daemon=True).start()

    def _decode(self, token, stop, jobs, permitted):
        try:
            allowed = set(permitted([path for _, path in jobs]))
        except Exception:
            allowed = set()
        for key, path in jobs:
            if stop.is_set():
                return
            image = None
            if path in allowed:
                try:
                    status = os.stat(path)
                    stamp = (path, status.st_mtime_ns, status.st_size)
                except OSError:
                    stamp = None
                with self._cache_lock:
                    hit = stamp is not None and stamp in self._cache
                    if hit:
                        self._cache.move_to_end(stamp)
                        image = self._cache[stamp]
                if not hit:
                    image = thumbnail(Path(path), TILE).image
                    if stamp is not None:
                        with self._cache_lock:
                            self._cache[stamp] = image
                            while len(self._cache) > _CACHE_SIZE:
                                self._cache.popitem(last=False)
            self._results.put((token, key, image))

    def poll(self, limit=24):
        """Attach decoded thumbnails; called from the window's UI loop."""
        for _ in range(limit):
            try:
                token, key, image = self._results.get_nowait()
            except queue.Empty:
                return
            tile = self.tiles.get(key)
            if token != self._token or tile is None:
                continue
            if image is None:
                tile.configure(image=self.placeholder, text=tile.cget("text").split("\n")[0] + "\n미리보기 없음")
                continue
            self.images[key] = ImageTk.PhotoImage(image, master=self)
            tile.configure(image=self.images[key])

    # ------------------------------------------------------------- selection
    def select(self, key):
        if self.selected in self.tiles:
            self.tiles[self.selected].configure(background=theme.CARD, foreground=theme.INK)
        self.selected = key if key in self.tiles else None
        if self.selected is None:
            return
        tile = self.tiles[self.selected]
        tile.configure(background=theme._tone(_SELECTED), foreground=theme._tone(_SELECTED_INK))
        self.update_idletasks()
        height = self.inner.winfo_height()
        if height > 1:
            top, bottom = tile.winfo_y(), tile.winfo_y() + tile.winfo_height()
            view_top, view_bottom = (fraction * height for fraction in self.canvas.yview())
            if top < view_top:
                self.canvas.yview_moveto(top / height)
            elif bottom > view_bottom:
                self.canvas.yview_moveto((bottom - (view_bottom - view_top)) / height)

    # ---------------------------------------------------------------- layout
    def _layout(self):
        width = self.canvas.winfo_width()
        if width <= 1:
            return
        columns = max(1, (width + _GAP) // (TILE[0] + 8 + _GAP))
        self.canvas.itemconfigure(self.window, width=width)
        if columns != self.columns:
            self.columns = columns
            for column in range(columns):
                self.inner.columnconfigure(column, weight=1, uniform="tile")
            for index, key in enumerate(self.order):
                self.tiles[key].grid(row=index // columns, column=index % columns, padx=_GAP // 2, pady=_GAP // 2, sticky="n")
            if not self.order:
                self.empty.grid(row=0, column=0, columnspan=columns, pady=40)
            else:
                self.empty.grid_remove()
        self.update_idletasks()
        self.canvas.configure(scrollregion=(0, 0, width, max(self.inner.winfo_reqheight(), self.canvas.winfo_height())))

    def _wheel(self, event):
        if event.delta:
            self.canvas.yview_scroll(-int(event.delta / 120) or (-1 if event.delta > 0 else 1), "units")
        return "break"

    def release(self):
        """Stop decoding and drop Tk images; call on the UI thread."""
        self._token += 1
        self._stop.set()
        self.images.clear()
        for tile in self.tiles.values():
            tile.destroy()
        self.tiles.clear()
        self.placeholder = None

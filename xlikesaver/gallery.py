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


class _GridDecoder:
    """One UI-free worker; a newer page replaces any pending page."""

    def __init__(self, results, *, decoder=thumbnail):
        self.results = results
        self.decoder = decoder
        self.cache = OrderedDict()
        self.condition = threading.Condition()
        self.pending = None
        self.stop = threading.Event()
        self.closed = False
        self.thread = None

    def request(self, token, jobs, permitted):
        with self.condition:
            if self.closed:
                return
            self.stop.set()
            self.stop = threading.Event()
            self.pending = (token, self.stop, jobs, permitted) if jobs else None
            self._clear_results()
            if jobs and self.thread is None:
                self.thread = threading.Thread(target=self._work, daemon=True, name="local-gallery-thumbnails")
                self.thread.start()
            self.condition.notify()

    def close(self):
        with self.condition:
            self.closed = True
            self.stop.set()
            self.pending = None
            self.cache.clear()
            self._clear_results()
            self.condition.notify()

    def _clear_results(self):
        while True:
            try:
                self.results.get_nowait()
            except queue.Empty:
                return

    def _work(self):
        while True:
            with self.condition:
                self.condition.wait_for(lambda: self.closed or self.pending is not None)
                if self.closed:
                    return
                token, stop, jobs, permitted = self.pending
                self.pending = None
            try:
                allowed = set(permitted([path for _, path in jobs]))
            except Exception:
                allowed = set()
            for key, path in jobs:
                if stop.is_set():
                    break
                image = None
                if path in allowed:
                    try:
                        status = os.stat(path)
                        stamp = (path, status.st_mtime_ns, status.st_size)
                    except OSError:
                        stamp = None
                    with self.condition:
                        hit = stamp is not None and stamp in self.cache
                        if hit:
                            self.cache.move_to_end(stamp)
                            image = self.cache[stamp]
                    if not hit:
                        try:
                            image = self.decoder(Path(path), TILE).image
                        except Exception:
                            image = None
                        with self.condition:
                            if self.closed:
                                return
                            if stamp is not None:
                                self.cache[stamp] = image
                                while len(self.cache) > _CACHE_SIZE:
                                    self.cache.popitem(last=False)
                with self.condition:
                    if self.closed:
                        return
                    if stop.is_set():
                        break
                    # Only a page is retained if the UI is temporarily busy.
                    if self.results.full():
                        try:
                            self.results.get_nowait()
                        except queue.Empty:
                            pass
                    self.results.put_nowait((token, key, image))


class ThumbnailGrid(ttk.Frame):
    def __init__(self, parent, *, on_select, on_open, on_selection=None):
        super().__init__(parent, style="Card.TFrame", padding=8)
        self.on_select, self.on_open = on_select, on_open
        self.on_selection = on_selection
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
        self._paths = {}
        self._captions = {}
        self._source_images = {}
        self.order: list[str] = []
        self.selected = None
        self.selected_keys: tuple[str, ...] = ()
        self._anchor = None
        self.columns = 0
        self._weighted_columns = 0
        self.placeholder = tk.PhotoImage(master=self, width=TILE[0], height=TILE[1])
        self._results = queue.Queue(maxsize=_CACHE_SIZE)
        self._decoder = _GridDecoder(self._results)
        self._token = 0
        self._released = False
        self.canvas.bind("<Configure>", lambda _: self._layout())
        for widget in (self.canvas, self.inner, self.empty):
            widget.bind("<MouseWheel>", self._wheel)

    # -------------------------------------------------------------- contents
    def show(self, rows, permitted, *, empty_text="표시할 파일이 없습니다."):
        """rows: [(key, record)] in display order. permitted(paths) runs on
        the decoding thread and returns the subset that may be read."""
        if self._released:
            return
        self._token += 1
        order = [key for key, _ in rows]
        paths = {key: row.get("path") for key, row in rows}
        for key in list(self.tiles):
            if key not in paths or self._paths[key] != paths[key]:
                self.tiles.pop(key).destroy()
                self.images.pop(key, None)
                self._source_images.pop(key, None)
                self._captions.pop(key, None)
        relayout = order != self.order or len(self.tiles) != len(rows)
        self.order = order
        self._paths = paths
        if relayout:
            self.columns = 0
        jobs = []
        for key, row in rows:
            author = "@" + row["author"] if row.get("author") else "작성자 미확인"
            day = str(row.get("posted_at") or "")[:10] or "게시일 미확인"
            caption = f"{author}\n{day}"
            if key not in self.tiles:
                tile = tk.Label(self.inner, image=self.placeholder, text=caption, compound="top",
                                background=theme.CARD, foreground=theme.INK, font=theme.SMALL, justify="center",
                                wraplength=TILE[0], padx=4, pady=4, cursor="hand2", takefocus=True)
                tile.bind("<Button-1>", lambda event, key=key: self._click(event, key))
                tile.bind("<Double-1>", lambda _, key=key: self.on_open(key))
                tile.bind("<MouseWheel>", self._wheel)
                self.tiles[key] = tile
            elif caption != self._captions[key]:
                unavailable = key in self._source_images and self._source_images[key] is None
                self.tiles[key].configure(text=f"{author}\n미리보기 없음" if unavailable else caption)
            self._captions[key] = caption
            if row.get("path"):
                jobs.append((key, row["path"]))
        self.empty.configure(text="" if rows else empty_text)
        # Keep surviving selections on refresh without transferring a selected
        # position to an unrelated record on the next page.
        self.select_many(self.selected_keys, focus=self.selected, scroll=False, _preserve_anchor=True)
        self._layout()
        if relayout:
            self.canvas.yview_moveto(0)
        self._decoder.request(self._token, jobs, permitted)

    def poll(self, limit=24):
        """Attach decoded thumbnails; called from the window's UI loop."""
        if self._released:
            return
        for _ in range(limit):
            try:
                token, key, image = self._results.get_nowait()
            except queue.Empty:
                return
            tile = self.tiles.get(key)
            if token != self._token or tile is None:
                continue
            if key in self._source_images and self._source_images[key] is image:
                continue
            self._source_images[key] = image
            if image is None:
                tile.configure(image=self.placeholder, text=self._captions[key].split("\n")[0] + "\n미리보기 없음")
                self.images.pop(key, None)
                continue
            self.images[key] = ImageTk.PhotoImage(image, master=self)
            tile.configure(image=self.images[key], text=self._captions[key])

    # ------------------------------------------------------------- selection
    def select(self, key):
        """Synchronize one selected record without invoking either callback."""
        self.select_many((key,) if key is not None else (), focus=key)
        self._anchor = self.selected

    def select_many(self, keys, focus=None, *, scroll=True, _preserve_anchor=False):
        """Synchronize selection, retaining display order and a preview focus.

        Callbacks are reserved for user clicks, so table/gallery updates can
        synchronize each other without re-entering selection handlers.
        """
        requested = set(keys)
        previous = set(self.selected_keys)
        previous_focus = self.selected
        self.selected_keys = tuple(key for key in self.order if key in requested)
        selected = set(self.selected_keys)
        self.selected = focus if focus in selected else (
            self.selected if self.selected in selected else next(iter(self.selected_keys), None)
        )
        if self._anchor not in self.tiles or (
            not _preserve_anchor and (previous != selected or previous_focus != self.selected)
        ):
            self._anchor = self.selected
        for key in previous | selected:
            if key in self.tiles:
                chosen = key in selected
                self.tiles[key].configure(
                    background=theme._tone(_SELECTED) if chosen else theme.CARD,
                    foreground=theme._tone(_SELECTED_INK) if chosen else theme.INK,
                )
        if scroll:
            self._scroll_selected()

    def _click(self, event, key):
        if self._released or key not in self.tiles:
            return "break"
        # Let window-level Delete/Ctrl+A shortcuts work from the gallery too.
        self.tiles[key].focus_set()
        shift, control = bool(event.state & 0x0001), bool(event.state & 0x0004)
        chosen = set(self.selected_keys)
        if shift:
            anchor = self._anchor if self._anchor in self.tiles else self.selected or key
            first, last = sorted((self.order.index(anchor), self.order.index(key)))
            span = set(self.order[first:last + 1])
            chosen = chosen | span if control else span
            self._anchor = anchor
        elif control:
            if key in chosen:
                chosen.remove(key)
            else:
                chosen.add(key)
            self._anchor = key
        else:
            chosen = {key}
            self._anchor = key
        self.select_many(chosen, focus=key, _preserve_anchor=True)
        if self.on_selection is not None:
            self.on_selection(self.selected_keys, self.selected)
        elif self.on_select is not None:
            self.on_select(key)
        return "break"

    def _scroll_selected(self):
        if self.selected is None:
            return
        tile = self.tiles[self.selected]
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
            for column in range(columns, self._weighted_columns):
                self.inner.columnconfigure(column, weight=0, uniform="", minsize=0)
            for column in range(columns):
                self.inner.columnconfigure(column, weight=1, uniform="tile")
            self._weighted_columns = columns
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
        if self._released:
            return
        self._released = True
        self._token += 1
        self._decoder.close()
        self.images.clear()
        self._source_images.clear()
        self._captions.clear()
        self._paths.clear()
        for tile in self.tiles.values():
            tile.destroy()
        self.tiles.clear()
        self.order.clear()
        self.selected = self._anchor = None
        self.selected_keys = ()
        self.placeholder = None
        self.on_select = self.on_open = self.on_selection = None

    def destroy(self):
        self.release()
        super().destroy()
        # The decoder owns no Tk references. Drop dead child-parent cycles on
        # the UI thread too, so later worker allocations cannot finalize Tk.
        for name in ("canvas", "scrollbar", "inner", "empty"):
            widget = getattr(self, name, None)
            if widget is not None:
                widget.master = None
                setattr(self, name, None)

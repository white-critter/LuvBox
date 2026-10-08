"""A scrollable grid of thumbnails for one page of records.

Thumbnails decode on a background thread (local files only, permission checked
there against the catalog); Tk images are created and released on the UI
thread. Selecting a tile selects the same record in the table. Dragging draws
a selection box; in 'select several' mode a plain click adds or removes a tile.
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
_DRAG_START = 6        # pixels the pointer moves before a press becomes a drag
_CONTROL = 0x0004


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
                            # Retry failed reads on the next refresh; restored
                            # access need not change the file's metadata.
                            if stamp is not None and image is not None:
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
    def __init__(self, parent, *, on_select, on_open, on_selection=None, on_context=None):
        super().__init__(parent, style="Card.TFrame", padding=8)
        self.on_select, self.on_open = on_select, on_open
        self.on_selection = on_selection
        self.on_context = on_context
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
        # 'Select several' mode: a plain click adds or removes one tile.
        self.toggle_mode = False
        self._drag = None
        self._drag_scroll = None
        self._unavailable = set()
        # Tk cannot overlay a translucent rectangle, so the box is four lines.
        self._band = [tk.Frame(self.inner, width=1, height=1, background=theme.BLUE) for _ in range(4)]
        self.canvas.bind("<Configure>", lambda _: self._layout())
        for widget in (self.canvas, self.inner, self.empty):
            widget.bind("<MouseWheel>", self._wheel)
            self._bind_pointer(widget, None)

    # -------------------------------------------------------------- contents
    def show(self, rows, permitted, *, empty_text="표시할 파일이 없습니다."):
        """rows: [(key, record)] in display order. permitted(paths) runs on
        the decoding thread and returns the subset that may be read."""
        if self._released:
            return
        if self._drag is not None:
            self._drag = None
            self._release_drag(None)
        self._token += 1
        order = [key for key, _ in rows]
        paths = {key: row.get("path") for key, row in rows}
        for key in list(self.tiles):
            if key not in paths or self._paths[key] != paths[key]:
                self.tiles.pop(key).destroy()
                self.images.pop(key, None)
                self._source_images.pop(key, None)
                self._captions.pop(key, None)
                self._unavailable.discard(key)
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
                self._bind_pointer(tile, key)
                tile.bind("<Double-1>", lambda _, key=key: self.on_open(key))
                tile.bind("<MouseWheel>", self._wheel)
                self.tiles[key] = tile
                self._captions[key] = caption
            elif caption != self._captions[key]:
                self._captions[key] = caption
                self.tiles[key].configure(text=self._tile_text(key))
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
                self._unavailable.add(key)
                tile.configure(image=self.placeholder, text=self._tile_text(key))
                self.images.pop(key, None)
                continue
            self._unavailable.discard(key)
            self.images[key] = ImageTk.PhotoImage(image, master=self)
            tile.configure(image=self.images[key], text=self._tile_text(key))

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
                    text=self._tile_text(key),
                )
        if scroll:
            self._scroll_selected()

    def _tile_text(self, key):
        """The caption; a check mark makes each chosen tile obvious."""
        author, _, day = self._captions.get(key, "").partition("\n")
        second = "미리보기 없음" if key in self._unavailable else day
        return ("✓ " if key in self.selected_keys else "") + author + "\n" + second

    def _notify(self, key=None):
        if self.on_selection is not None:
            self.on_selection(self.selected_keys, self.selected)
        elif self.on_select is not None and key is not None:
            self.on_select(key)

    def _click(self, event, key):
        if self._released or key not in self.tiles:
            return "break"
        # Let window-level Delete/Ctrl+A shortcuts work from the gallery too.
        self.tiles[key].focus_set()
        shift = bool(event.state & 0x0001)
        control = bool(event.state & _CONTROL) or self.toggle_mode
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
        self._notify(key)
        return "break"

    # ------------------------------------------------------------ drag box
    def _bind_pointer(self, widget, key):
        widget.bind("<ButtonPress-1>", lambda event: self._press(event, key))
        widget.bind("<B1-Motion>", self._drag_motion)
        widget.bind("<ButtonRelease-1>", self._release_drag)
        if self.on_context is not None and key is not None:
            widget.bind("<Button-3>", lambda event: self.on_context(event, key))

    def _inner_point(self, x_root, y_root):
        # Content coordinates: they stay put while the view scrolls.
        return x_root - self.inner.winfo_rootx(), y_root - self.inner.winfo_rooty()

    def _press(self, event, key):
        if self._released:
            return "break"
        adding = bool(event.state & _CONTROL) or self.toggle_mode
        self._drag = {"origin": self._inner_point(event.x_root, event.y_root), "root": (event.x_root, event.y_root),
                      "moved": False, "key": key, "base": set(self.selected_keys) if adding else set()}
        if key is None:
            self.canvas.focus_set()
            return "break"
        return self._click(event, key)

    def _drag_motion(self, event):
        drag = self._drag
        if drag is None or self._released:
            return "break"
        drag["root"] = (event.x_root, event.y_root)
        if not drag["moved"]:
            x, y = self._inner_point(event.x_root, event.y_root)
            if abs(x - drag["origin"][0]) < _DRAG_START and abs(y - drag["origin"][1]) < _DRAG_START:
                return "break"
            drag["moved"] = True
        self._update_band()
        top = self.canvas.winfo_rooty()
        direction = -1 if event.y_root < top else 1 if event.y_root > top + self.canvas.winfo_height() else 0
        drag["scroll"] = direction
        if direction and self._drag_scroll is None:
            self._drag_scroll = self.after(60, self._scroll_drag)
        return "break"

    def _scroll_drag(self):
        self._drag_scroll = None
        drag = self._drag
        if drag is None or not drag.get("scroll") or self._released:
            return
        self.canvas.yview_scroll(drag["scroll"], "units")
        self.canvas.update_idletasks()
        self._update_band()
        self._drag_scroll = self.after(60, self._scroll_drag)

    def _update_band(self):
        drag = self._drag
        width, height = self.inner.winfo_width(), self.inner.winfo_height()
        x, y = self._inner_point(*drag["root"])
        (x0, x1), (y0, y1) = sorted((drag["origin"][0], x)), sorted((drag["origin"][1], y))
        x0, x1 = max(0, x0), min(width, x1)
        y0, y1 = max(0, y0), min(height, y1)
        span_x, span_y = max(2, x1 - x0), max(2, y1 - y0)
        for frame, place in zip(self._band, ((x0, y0, span_x, 2), (x0, y1 - 2, span_x, 2),
                                             (x0, y0, 2, span_y), (x1 - 2, y0, 2, span_y))):
            frame.configure(background=theme.BLUE)
            frame.place(x=place[0], y=place[1], width=place[2], height=place[3])
            frame.lift()
        hits = set()
        for key in self.order:
            tile = self.tiles.get(key)
            if (tile is not None and tile.winfo_x() < x1 and x0 < tile.winfo_x() + tile.winfo_width()
                    and tile.winfo_y() < y1 and y0 < tile.winfo_y() + tile.winfo_height()):
                hits.add(key)
        chosen = drag["base"] | hits
        if chosen != set(self.selected_keys):
            focus = self.selected if self.selected in chosen else next((key for key in self.order if key in hits), None)
            self.select_many(chosen, focus=focus, scroll=False, _preserve_anchor=True)
            self._notify(focus)

    def _release_drag(self, event):
        drag, self._drag = self._drag, None
        if self._drag_scroll is not None:
            self.after_cancel(self._drag_scroll)
            self._drag_scroll = None
        for frame in self._band:
            frame.place_forget()
        if drag is None or self._released:
            return "break"
        if (not drag["moved"] and drag["key"] is None and not event.state & _CONTROL
                and not self.toggle_mode and self.selected_keys):
            # A click on empty space clears the selection, as in Explorer.
            self.select_many((), scroll=False)
            self._notify()
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
        self._drag = None
        if self._drag_scroll is not None:
            self.after_cancel(self._drag_scroll)
            self._drag_scroll = None
        self._unavailable.clear()
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

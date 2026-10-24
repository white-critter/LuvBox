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
import time
import tkinter as tk
import weakref
from pathlib import Path
from tkinter import font as tkfont, ttk

from PIL import Image, ImageDraw, ImageTk

from . import theme
from .preview import thumbnail
from .thumbcache import cloud_only


TILE = (150, 112)
_GAP = 10
_CACHE_SIZE = 240
_OVERSCAN_ROWS = 1
_SELECTED = "#e3eaff"           # theme maps this to the dark selection color
_SELECTED_INK = "#314fbb"
_DRAG_START = 6        # pixels the pointer moves before a press becomes a drag
_SEE_THROUGH = "#010203"  # never a theme color; the drag box window shows through it
_CONTROL = 0x0004


def _tile_placeholder(master, *, loading):
    """A quiet, shared panel instead of an apparently unfinished blank tile."""
    palette = theme.DARK if theme.mode(master) == "dark" else theme.LIGHT
    with Image.new("RGB", TILE, palette["HEADER"]) as image:
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((0, 0, TILE[0] - 1, TILE[1] - 1), radius=10,
                               fill=palette["HEADER"], outline=palette["LINE"])
        if loading:
            for x in (63, 75, 87):
                draw.ellipse((x - 3, 53, x + 3, 59), fill=palette["MUTED"])
        else:
            draw.rounded_rectangle((53, 36, 97, 74), radius=4, outline=palette["MUTED"], width=2)
            draw.ellipse((80, 43, 86, 49), fill=palette["MUTED"])
            draw.line((55, 67, 67, 53, 79, 66, 87, 58, 95, 68), fill=palette["MUTED"], width=2)
        return ImageTk.PhotoImage(image, master=master)


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

    def request(self, token, jobs, permitted, *, remember=None, recall=None):
        """jobs: (key, path) or (key, path, identity). ``remember(identity,
        image)`` keeps a decoded preview; ``recall(identity)`` returns a kept
        one when the file itself cannot be read. Both run on this thread."""
        with self.condition:
            if self.closed:
                return
            self.stop.set()
            self.stop = threading.Event()
            self.pending = (token, self.stop, jobs, permitted, remember, recall) if jobs else None
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

    def _cache_image(self, stamp, image):
        """Promote a successful thumbnail while ``condition`` is held."""
        self.cache[stamp] = image
        self.cache.move_to_end(stamp)
        while len(self.cache) > _CACHE_SIZE:
            self.cache.popitem(last=False)

    def _load_thumbnail(self, path, retained, stop):
        """Read one permitted path, rechecking its current file identity."""
        try:
            status = os.stat(path)
            stamp = (path, status.st_dev, status.st_ino, status.st_mtime_ns, status.st_size)
        except OSError:
            status = stamp = None
        with self.condition:
            # A slow stat can finish after the user has switched galleries.
            if self.closed or stop.is_set():
                return None, False
            image = self.cache.get(stamp, retained.get(stamp)) if stamp is not None else None
            if image is not None:
                self._cache_image(stamp, image)
                return image, False
        try:
            image = (self.decoder(Path(path), TILE).image
                     if status is None or not cloud_only(path, status=status) else None)
        except Exception:
            image = None
        with self.condition:
            if self.closed:
                return None, False
            # Retry failed reads on the next refresh; restored access need
            # not change the file's metadata.
            if stamp is not None and image is not None:
                self._cache_image(stamp, image)
        return image, image is not None

    def _deliver(self, result, stop):
        """Wait for bounded result space; cancellation interrupts the wait."""
        while not stop.is_set():
            with self.condition:
                if self.closed or stop.is_set():
                    return False
                try:
                    self.results.put_nowait(result)
                except queue.Full:
                    pass
                else:
                    return True
            # A large author gallery can exceed the queue. Keep every current
            # thumbnail until the UI drains it, a new request or close arrives.
            stop.wait(0.05)
        return False

    def _work(self):
        while True:
            with self.condition:
                self.condition.wait_for(lambda: self.closed or self.pending is not None)
                if self.closed:
                    return
                token, stop, jobs, permitted, remember, recall = self.pending
                self.pending = None
                # Keep at most one cache's worth of existing image references
                # for this batch. Decoding its first uncached tiles must not
                # evict later tiles before the batch has a chance to reuse them.
                retained = dict(self.cache)
            try:
                allowed = set(permitted([job[1] for job in jobs]))
            except Exception:
                allowed = set()
            for job in jobs:
                key, path = job[0], job[1]
                identity = job[2] if len(job) > 2 else None
                if stop.is_set():
                    break
                image = None
                decoded = False
                if path in allowed:
                    image, decoded = self._load_thumbnail(path, retained, stop)
                if stop.is_set():
                    break
                if identity is not None:
                    try:
                        if decoded and remember is not None:
                            remember(identity, image)
                        elif image is None and recall is not None:
                            image = recall(identity)
                    except Exception:
                        pass
                if not self._deliver((token, key, image), stop):
                    break
            retained.clear()


class DragBox:
    """The drag-selection box, drawn in a see-through window above the tiles.

    Lines placed among the tiles had to wait until every tile they had
    crossed was repainted, so a fast drag left several boxes behind.
    Windows composes a separate window without repainting what is below.
    The window never takes focus, so the drag keeps the mouse.

    The window covers the visible gallery for the whole drag and only its
    four lines move. Resizing it on every mouse move made Windows show each
    new size blank (white) for a moment before it was drawn, so the box
    blinked. It also appears only after its first drawing is finished.
    """

    REVEAL_MS = 30

    def __init__(self, parent):
        # A weak link: the gallery owns this box, never the other way round.
        self._parent = weakref.ref(parent)
        self.window = None
        self.canvas = None
        self.lines = []
        self.visible = False
        self.revealed = False
        self.area = None
        self._viewport = None
        self._reveal_job = None

    def _create(self):
        parent = self._parent()
        window = tk.Toplevel(parent)
        window.withdraw()
        window.overrideredirect(True)
        window.configure(background=_SEE_THROUGH)
        try:
            window.attributes("-transparentcolor", _SEE_THROUGH)
            window.attributes("-alpha", 0.0)
        except tk.TclError:
            pass
        self.canvas = tk.Canvas(window, background=_SEE_THROUGH, highlightthickness=0, borderwidth=0,
                                width=1, height=1)
        self.canvas.pack(fill="both", expand=True)
        # Left, top, right and bottom sides; a side cut off by the gallery's
        # edge is hidden.
        self.lines = [self.canvas.create_rectangle(0, 0, 0, 0, outline="", fill=theme.BLUE, state="hidden")
                      for _ in range(4)]
        window.update_idletasks()
        if os.name == "nt":
            try:
                import ctypes
                user32 = ctypes.windll.user32
                handle = int(window.wm_frame(), 16)
                # No activation, no taskbar button, mouse input passes through.
                style = user32.GetWindowLongW(handle, -20)
                user32.SetWindowLongW(handle, -20, style | 0x08000000 | 0x00000080 | 0x00000020)
                # Owned by the library window: always just above it, and
                # hidden with it (Tk does not set an owner for this kind).
                from ctypes import wintypes
                user32.SetWindowLongPtrW.argtypes = (wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t)
                user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
                user32.SetWindowLongPtrW(handle, -8, int(parent.winfo_toplevel().wm_frame(), 16))
            except (OSError, AttributeError, ValueError, tk.TclError):
                pass
        self.window = window

    def show(self, viewport, box, edges):
        """``viewport`` is the visible gallery and ``box`` the part of the drag
        box inside it, both (left, top, right, bottom) on the screen.
        ``edges`` say which sides of the box are real (not cut off)."""
        if self.window is None:
            self._create()
        window, canvas = self.window, self.canvas
        left, top, right, bottom = viewport
        if viewport != self._viewport:
            # Only when the drag starts, or the window itself was resized.
            self._viewport = viewport
            self._conceal()
            canvas.configure(width=max(1, right - left), height=max(1, bottom - top))
            window.geometry(f"{max(1, right - left)}x{max(1, bottom - top)}+{left}+{top}")
        x0, y0, x1, y1 = box[0] - left, box[1] - top, box[2] - left, box[3] - top
        self.area = (box[0], box[1], box[2] - box[0], box[3] - box[1])
        for line, drawn, coords in zip(self.lines, edges, ((x0, y0, x0 + 2, y1), (x0, y0, x1, y0 + 2),
                                                           (x1 - 2, y0, x1, y1), (x0, y1 - 2, x1, y1))):
            if drawn:
                canvas.coords(line, *coords)
                canvas.itemconfigure(line, state="normal", fill=theme.BLUE)
            else:
                canvas.itemconfigure(line, state="hidden")
        if not self.visible:
            window.deiconify()
            window.lift()
            self.visible = True
        if not self.revealed and self._reveal_job is None:
            self._reveal_job = window.after(self.REVEAL_MS, self._reveal)

    def _reveal(self):
        self._reveal_job = None
        if self.window is not None and self.visible:
            try:
                self.window.attributes("-alpha", 1.0)
            except tk.TclError:
                pass
            self.revealed = True

    def _conceal(self):
        """Invisible until drawn again: a new size first shows up blank."""
        if self._reveal_job is not None:
            self.window.after_cancel(self._reveal_job)
            self._reveal_job = None
        if self.revealed:
            try:
                self.window.attributes("-alpha", 0.0)
            except tk.TclError:
                pass
            self.revealed = False

    def hide(self):
        if self.window is not None:
            self._conceal()
            if self.visible:
                for line in self.lines:
                    self.canvas.itemconfigure(line, state="hidden")
                self.window.withdraw()
        self.visible = False
        self.area = None
        self._viewport = None

    def destroy(self):
        if self.window is not None and self._reveal_job is not None:
            self.window.after_cancel(self._reveal_job)
            self._reveal_job = None
        window, self.window = self.window, None
        self.visible = self.revealed = False
        if window is not None:
            try:
                window.destroy()
            except tk.TclError:
                pass


class ThumbnailGrid(ttk.Frame):
    def __init__(self, parent, *, on_select, on_open, on_selection=None, on_context=None):
        super().__init__(parent, style="Card.TFrame", padding=8)
        self.on_select, self.on_open = on_select, on_open
        self.on_selection = on_selection
        self.on_context = on_context
        self.canvas = tk.Canvas(self, background=theme.CARD, highlightthickness=0, borderwidth=0, width=1, height=1)
        # Tk initializes a Label's default font before its explicit font. Keep
        # that native resource alive across tile creation instead of resolving
        # it again for every label. This hidden item adds no widget or geometry.
        self._default_font_anchor = self.canvas.create_text(
            0, 0, text="", font="TkDefaultFont", state="hidden")
        self.scrollbar = ttk.Scrollbar(self, command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self._view_changed)
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
        self._source_images = OrderedDict()
        self._valid_sources = set()
        self._jobs = {}
        self._permitted = self._remember = self._recall = None
        self._requested_view = None
        self._pending_keys = set()
        self._poll_timer = None
        self._positions = {}
        self._indices = {}
        self._tile_pool = []
        self._font = tkfont.Font(root=self, font=theme.SMALL)
        self._font_scaling = float(self.tk.call("tk", "scaling"))
        self._fitted_text = OrderedDict()
        self._cell_width = TILE[0] + 8 + _GAP
        self._tile_height = TILE[1] + 8 + 4 + 2 * self._font.metrics("linespace")
        self._cell_height = self._tile_height + _GAP
        self._content_height = 1
        self.order: list[str] = []
        self.selected = None
        self.selected_keys: tuple[str, ...] = ()
        self._selected_set: set[str] = set()
        self._anchor = None
        self.columns = 0
        self._layout_timer = None
        self._region_timer = None
        self._selection_timer = None
        self._viewport_timer = None
        self._window_width = None
        self._window_x = None
        self._window_y = 0
        self._window_height = None
        self._scrollregion = None
        self.placeholder = self.unavailable_placeholder = None
        self._placeholder_appearance = None
        self._results = queue.Queue(maxsize=_CACHE_SIZE)
        self._decoder = _GridDecoder(self._results)
        self._token = 0
        self._released = False
        # 'Select several' mode: a plain click adds or removes one tile.
        self.toggle_mode = False
        self._drag = None
        self._drag_scroll = None
        self._unavailable = set()
        self._band = DragBox(self)
        # Classic canvases already redraw through the theme's normal walk.
        self.canvas.theme_redraw = self._refresh_placeholders
        self._refresh_placeholders()
        self.canvas.bind("<Configure>", self._schedule_layout)
        for widget in (self.canvas, self.inner, self.empty):
            widget.bind("<MouseWheel>", self._wheel)
            self._bind_pointer(widget, None)

    # -------------------------------------------------------------- contents
    def _refresh_placeholders(self):
        if self._released:
            return
        appearance = theme.mode(self)
        # Canvas Map events also request a theme redraw. Reopening a tab in
        # the same appearance must retain its photos instead of blending every
        # mounted image again just as the window becomes visible.
        if appearance == self._placeholder_appearance:
            return
        self.placeholder = _tile_placeholder(self, loading=True)
        self.unavailable_placeholder = _tile_placeholder(self, loading=False)
        for key, tile in self.tiles.items():
            if key not in self.images:
                tile.configure(image=self.unavailable_placeholder if key in self._unavailable else self.placeholder)
            else:
                self._attach_image(key, tile)
        self._placeholder_appearance = appearance

    def show(self, rows, permitted, *, empty_text="표시할 파일이 없습니다.", remember=None, recall=None):
        """rows: [(key, record)] in display order. permitted(paths) runs on
        the decoding thread and returns the subset that may be read.
        remember/recall keep and reuse previews by media identity."""
        if self._released:
            return
        if self._drag is not None:
            self._drag = None
            self._release_drag(None)
        self._token += 1
        self._valid_sources.clear()
        self._pending_keys.clear()
        self._requested_view = None
        self._permitted, self._remember, self._recall = permitted, remember, recall
        order = [key for key, _ in rows]
        paths = {key: row.get("path") for key, row in rows}
        # Logical records outlive viewport widgets. Invalidate hidden previews
        # too: a recycled key must never reveal the previous record's image.
        for key in self._paths.keys() - paths.keys() | {
            key for key in paths.keys() & self._paths.keys() if paths[key] != self._paths[key]
        }:
            self._unmount(key, recycle=False)
            self._source_images.pop(key, None)
            self._captions.pop(key, None)
            self._unavailable.discard(key)
        relayout = order != self.order
        appended = bool(self.order) and order[:len(self.order)] == self.order
        self.order = order
        self._indices = {key: index for index, key in enumerate(order)}
        self._paths = paths
        self._jobs = {}
        for key, row in rows:
            author = "@" + row["author"] if row.get("author") else "작성자 미확인"
            day = str(row.get("posted_at") or "")[:10] or "게시일 미확인"
            caption = f"{author}\n{day}"
            changed = caption != self._captions.get(key)
            self._captions[key] = caption
            if not row.get("path"):
                self._unavailable.add(key)
            if changed and key in self.tiles:
                self.tiles[key].configure(text=self._tile_text(key))
            if row.get("path"):
                identity = ((row["tweet_id"], row["num"], row["media_type"])
                            if all(row.get(name) for name in ("tweet_id", "num", "media_type")) else None)
                self._jobs[key] = (key, row["path"], identity)
        self.empty.configure(text="" if rows else empty_text)
        # Keep surviving selections on refresh without transferring a selected
        # position to an unrelated record on the next page.
        self.select_many(self.selected_keys, focus=self.selected, scroll=False, _preserve_anchor=True)
        self._schedule_layout()
        if relayout and not appended:
            self.canvas.yview_moveto(0)
        # Small initial contents are available immediately, without forcing a
        # recursive geometry flush. Once mapped, mount only visible rows.
        self._render_viewport()

    def _request_viewport(self, keys):
        """Decode visible rows first, then their bounded overscan neighbors."""
        if self._permitted is None:
            return
        view = tuple(keys)
        if view == self._requested_view:
            return
        self._requested_view = view
        missing = [key for key in keys if key in self._jobs and key not in self._valid_sources]
        self._token += 1
        self._pending_keys = set(missing)
        self._decoder.request(self._token, [self._jobs[key] for key in missing], self._permitted,
                              remember=self._remember, recall=self._recall)
        if missing and self._poll_timer is None:
            self._poll_timer = self.after(8, self._poll_ready)

    def _poll_ready(self):
        self._poll_timer = None
        if self._released:
            return
        self.poll(limit=8)
        if self._pending_keys:
            self._poll_timer = self.after(8, self._poll_ready)

    def _trim_sources(self):
        # A very large display may itself show more than one cache's worth.
        # Keep mounted previews, while bounding history independently of N.
        limit = max(_CACHE_SIZE, len(self.tiles))
        if len(self._source_images) <= limit:
            return
        for key in tuple(self._source_images):
            if key not in self.tiles:
                self._source_images.pop(key)
                self._valid_sources.discard(key)
                if len(self._source_images) <= limit:
                    break

    def poll(self, limit=24):
        """Attach results cooperatively; normal delivery has its own UI timer."""
        if self._released:
            return
        deadline = time.perf_counter() + .003
        for _ in range(limit):
            try:
                token, key, image = self._results.get_nowait()
            except queue.Empty:
                break
            if token != self._token or key not in self._paths:
                continue
            self._pending_keys.discard(key)
            unchanged = key in self._source_images and self._source_images[key] is image
            self._source_images[key] = image
            self._source_images.move_to_end(key)
            self._valid_sources.add(key)
            if image is None:
                self._unavailable.add(key)
            else:
                self._unavailable.discard(key)
            if key in self.tiles:
                if unchanged and key in self.images:
                    self.tiles[key].configure(text=self._tile_text(key))
                else:
                    self._attach_image(key, self.tiles[key])
            self._trim_sources()
            if time.perf_counter() >= deadline:
                break

    def _attach_image(self, key, tile, **colors):
        """Only visible previews need a Tcl image and a native label."""
        image = self._source_images.get(key) if key in self._valid_sources or key in self.images else None
        if image is None:
            tile.configure(image=self.unavailable_placeholder if key in self._unavailable else self.placeholder,
                           text=self._tile_text(key), **colors)
            self.images.pop(key, None)
            return
        self._source_images.move_to_end(key)
        # Blend transparent margins/content once in Pillow. Tk otherwise
        # recomposites every translucent pixel whenever a row is exposed.
        background = self._tile_colors(key)["background"]
        with Image.new("RGB", TILE, background) as surface:
            surface.paste(image, ((TILE[0] - image.width) // 2, (TILE[1] - image.height) // 2),
                          image if "A" in image.getbands() else None)
            photo = getattr(tile, "_preview_image", None)
            if photo is None:
                photo = tile._preview_image = ImageTk.PhotoImage(surface, master=self)
            else:
                # Recycled labels keep one Tcl image buffer. Repeated native
                # image creation/deletion made incoming scroll rows expensive.
                photo.paste(surface)
            self.images[key] = photo
        tile.configure(image=self.images[key], text=self._tile_text(key), **colors)

    def _tile_colors(self, key):
        appearance = theme.mode(self)
        palette = theme.DARK if appearance == "dark" else theme.LIGHT
        chosen = key in self._selected_set
        return {"background": theme._tone(_SELECTED, appearance) if chosen else palette["CARD"],
                "foreground": theme._tone(_SELECTED_INK, appearance) if chosen else palette["INK"]}

    def _mount(self, key):
        if self._tile_pool:
            tile = self._tile_pool.pop()
        else:
            # The same named font must have a widget consumer. A separate
            # tuple font let Tk repeatedly realize/free native font resources
            # for each measurement of a newly encountered caption.
            tile = tk.Label(self.inner, compound="top", font=self._font, justify="center",
                            padx=4, pady=4, borderwidth=0, highlightthickness=0,
                            cursor="hand2", takefocus=True)
            # A recycled widget always resolves its current record at dispatch.
            tile.bind("<ButtonPress-1>", lambda event: self._press(event, tile._gallery_key))
            tile.bind("<B1-Motion>", self._drag_motion)
            tile.bind("<ButtonRelease-1>", self._release_drag)
            tile.bind("<Double-1>", lambda _: self.on_open(tile._gallery_key))
            tile.bind("<MouseWheel>", self._wheel)
            if self.on_context is not None:
                tile.bind("<Button-3>", lambda event: self.on_context(event, tile._gallery_key))
        tile._gallery_key = key
        tile.configure(takefocus=True, **self._tile_colors(key))
        self.tiles[key] = tile
        self._attach_image(key, tile)
        return tile

    def _unmount(self, key, *, recycle=True):
        tile = self.tiles.pop(key, None)
        self._positions.pop(key, None)
        self.images.pop(key, None)
        if tile is None:
            return
        if self.focus_get() == tile:
            self.canvas.focus_set()
        if recycle:
            tile.place_forget()
            tile.configure(image=self.placeholder, text="", takefocus=False)
            tile._gallery_key = None
            self._tile_pool.append(tile)
        else:
            tile.destroy()
            tile._preview_image = None

    def _view_changed(self, first, last):
        self.scrollbar.set(first, last)
        if self._viewport_timer is None and not self._released:
            self._viewport_timer = self.after_idle(self._render_viewport)

    def _render_viewport(self):
        if self._viewport_timer is not None:
            self.after_cancel(self._viewport_timer)
        self._viewport_timer = None
        if self._released:
            return
        columns = self.columns or 1
        height = self.canvas.winfo_height()
        if height <= 1:
            # Geometry has not been delivered yet. Bound first-paint work.
            keys = self.order[:24]
            first = 0
            last = (len(keys) + columns - 1) // columns
        else:
            top = max(0, self.canvas.canvasy(0))
            first = max(0, int(top // self._cell_height) - _OVERSCAN_ROWS)
            last = int((top + height) // self._cell_height) + _OVERSCAN_ROWS + 1
            keys = self.order[first * columns:last * columns]
        # The native frame covers a small slice, while the canvas tracks the
        # full logical height. Huge libraries must not create huge OS windows.
        window_y = first * self._cell_height
        if window_y != self._window_y:
            self._window_y = window_y
            self.canvas.coords(self.window, self._window_x or 0, window_y)
        window_height = max(height, (last - first) * self._cell_height)
        if window_height != self._window_height:
            self._window_height = window_height
            self.canvas.itemconfigure(self.window, height=window_height)
        # Keep the implicit mouse grab alive through drag autoscroll.
        drag_key = self._drag.get("key") if self._drag is not None else None
        if drag_key in self._indices and drag_key not in keys:
            keys.append(drag_key)
        visible = set(keys)
        for key in self.tiles.keys() - visible:
            self._unmount(key)
        for key in keys:
            tile = self.tiles.get(key)
            if tile is None:
                tile = self._mount(key)
            row, column = divmod(self._indices[key], columns)
            position = (column * self._cell_width + _GAP // 2,
                        min(window_height + 1, max(-self._tile_height - 1,
                            row * self._cell_height + _GAP // 2 - window_y)),
                        TILE[0] + 8, self._tile_height)
            if self._positions.get(key) != position:
                self._positions[key] = position
                tile.place(x=position[0], y=position[1], width=position[2], height=position[3])
        # A temporary large viewport should not leave hundreds of idle widgets.
        keep = max(columns, len(keys))
        while len(self._tile_pool) > keep:
            tile = self._tile_pool.pop()
            tile.destroy()
            tile._preview_image = None
        top = max(0, self.canvas.canvasy(0))
        bottom = top + height
        in_view, neighbors = [], []
        for key in keys:
            row_top = (self._indices[key] // columns) * self._cell_height
            (in_view if row_top < bottom and row_top + self._cell_height > top else neighbors).append(key)
        self._trim_sources()
        self._request_viewport(in_view + neighbors)

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
        previous = self._selected_set
        previous_focus = self.selected
        self.selected_keys = tuple(key for key in self.order if key in requested)
        selected = set(self.selected_keys)
        self._selected_set = selected
        self.selected = focus if focus in selected else (
            self.selected if self.selected in selected else next(iter(self.selected_keys), None)
        )
        if self._anchor not in self._indices or (
            not _preserve_anchor and (previous != selected or previous_focus != self.selected)
        ):
            self._anchor = self.selected
        # Focus changes and repeated table/gallery synchronization do not
        # change tile appearance. Repaint only tiles whose selection changed.
        for key in previous ^ selected:
            if key in self.tiles:
                colors = self._tile_colors(key)
                if key in self.images:
                    self._attach_image(key, self.tiles[key], **colors)
                else:
                    self.tiles[key].configure(text=self._tile_text(key), **colors)
        if scroll:
            self._scroll_selected()

    def _tile_text(self, key):
        """The caption; a check mark makes each chosen tile obvious."""
        author, _, day = self._captions.get(key, "").partition("\n")
        second = ("미리보기 없음" if key in self._unavailable else
                  "불러오는 중…" if key not in self._valid_sources and key not in self.images else day)
        prefix = "✓ " if key in self._selected_set else ""
        # Fixed rows stay two lines even with large Windows text scaling.
        return self._fit_caption(prefix + author) + "\n" + self._fit_caption(second)

    def _fit_caption(self, text):
        fitted = self._fitted_text.get(text)
        if fitted is not None:
            self._fitted_text.move_to_end(text)
            return fitted
        fitted = text
        if self._font.measure(text) > TILE[0]:
            low, high = 0, len(text)
            while low < high:
                middle = (low + high + 1) // 2
                if self._font.measure(text[:middle] + "…") <= TILE[0]:
                    low = middle
                else:
                    high = middle - 1
            fitted = text[:low] + "…"
        self._fitted_text[text] = fitted
        if len(self._fitted_text) > _CACHE_SIZE:
            self._fitted_text.popitem(last=False)
        return fitted

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
            anchor = self._anchor if self._anchor in self._indices else self.selected or key
            first, last = sorted((self._indices[anchor], self._indices[key]))
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
        return (self.canvas.canvasx(x_root - self.canvas.winfo_rootx()) - (self._window_x or 0),
                self.canvas.canvasy(y_root - self.canvas.winfo_rooty()))

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
        self._update_band()
        self._drag_scroll = self.after(60, self._scroll_drag)

    def _update_band(self):
        drag = self._drag
        width, height = self._window_width or self._cell_width, self._content_height
        x, y = self._inner_point(*drag["root"])
        (x0, x1), (y0, y1) = sorted((drag["origin"][0], x)), sorted((drag["origin"][1], y))
        x0, x1 = max(0, x0), min(width, x1)
        y0, y1 = max(0, y0), min(height, y1)
        # Show only the part inside the visible gallery, on the screen.
        view_left, view_top = self.canvas.winfo_rootx(), self.canvas.winfo_rooty()
        left = view_left - self.canvas.canvasx(0) + (self._window_x or 0)
        top = view_top - self.canvas.canvasy(0)
        view_right, view_bottom = view_left + self.canvas.winfo_width(), view_top + self.canvas.winfo_height()
        # A straight drag still shows a thin box.
        box = (left + x0, top + y0, max(left + x1, left + x0 + 2), max(top + y1, top + y0 + 2))
        shown = (max(box[0], view_left), max(box[1], view_top), min(box[2], view_right), min(box[3], view_bottom))
        if shown[0] < shown[2] and shown[1] < shown[3]:
            self._band.show((view_left, view_top, view_right, view_bottom), shown,
                            edges=(box[0] >= view_left, box[1] >= view_top,
                                   box[2] <= view_right, box[3] <= view_bottom))
        else:
            self._band.hide()
        hits = set()
        first_row = max(0, int(y0 // self._cell_height))
        last_row = min((len(self.order) + self.columns - 1) // self.columns, int(y1 // self._cell_height) + 1)
        for index in range(first_row * self.columns, min(len(self.order), last_row * self.columns)):
            row, column = divmod(index, self.columns)
            left = column * self._cell_width + _GAP // 2
            top = row * self._cell_height + _GAP // 2
            if left < x1 and x0 < left + TILE[0] + 8 and top < y1 and y0 < top + self._tile_height:
                hits.add(self.order[index])
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
        self._band.hide()
        if not self._released:
            self._view_changed(*self.canvas.yview())
        if drag is None or self._released:
            return "break"
        if (not drag["moved"] and drag["key"] is None and not event.state & _CONTROL
                and not self.toggle_mode and self.selected_keys):
            # A click on empty space clears the selection, as in Explorer.
            self.select_many((), scroll=False)
            self._notify()
        return "break"

    def _scroll_selected(self):
        if self.selected is not None and self._selection_timer is None and not self._released:
            self._selection_timer = self.after_idle(self._reveal_selected)

    def _reveal_selected(self):
        self._selection_timer = None
        if self.selected is None or self._released:
            return
        if self._layout_timer is not None or self._region_timer is not None:
            self._selection_timer = self.after_idle(self._reveal_selected)
            return
        index = self._indices.get(self.selected)
        if index is None:
            return
        if not self.columns:
            # A hidden notebook page has not received its first geometry yet.
            # Its Configure handler will reveal the selection when mapped.
            return
        top = (index // self.columns) * self._cell_height
        height = max(self._content_height, self.canvas.winfo_height())
        if height > 1:
            bottom = top + self._cell_height
            view_top, view_bottom = (fraction * height for fraction in self.canvas.yview())
            if top < view_top:
                self.canvas.yview_moveto(top / height)
            elif bottom > view_bottom:
                self.canvas.yview_moveto((bottom - (view_bottom - view_top)) / height)
        self._render_viewport()

    # ---------------------------------------------------------------- layout
    def _schedule_layout(self, _event=None):
        if self._layout_timer is None and not self._released:
            self._layout_timer = self.after_idle(self._layout)

    def _layout(self):
        self._layout_timer = None
        if self._released:
            return
        width = self.canvas.winfo_width()
        if width <= 1:
            return
        # Fixed cells and a logical scrollregion let resize cost depend on
        # the viewport, even when an author has thousands of saved files.
        old_columns = self.columns
        old_cell_height = self._cell_height
        top = max(0, self.canvas.canvasy(0))
        anchor = int(top // old_cell_height) * old_columns
        offset = top % old_cell_height
        scaling = float(self.tk.call("tk", "scaling"))
        if scaling != self._font_scaling:
            self._font_scaling = scaling
            # A displayed named font retains native metrics. Reapply its point
            # size so fitting and labels both resolve the new Tk text scale.
            self._font.configure(size=self._font.cget("size"))
            self._tile_height = TILE[1] + 12 + 2 * self._font.metrics("linespace")
            self._cell_height = self._tile_height + _GAP
            self._fitted_text.clear()
            for key, tile in self.tiles.items():
                tile.configure(text=self._tile_text(key))
        self.columns = max(1, width // self._cell_width)
        content_width = self.columns * self._cell_width
        self._content_height = max(1, ((len(self.order) + self.columns - 1) // self.columns) * self._cell_height)
        if content_width != self._window_width:
            self.canvas.itemconfigure(self.window, width=content_width)
        self._window_width = content_width
        x = max(0, (width - content_width) // 2)
        if x != self._window_x:
            self._window_x = x
            self.canvas.coords(self.window, x, self._window_y)
        if not self.order:
            self.empty.place(x=0, y=40, width=content_width)
        else:
            self.empty.place_forget()
        self._sync_region()
        if old_columns and (old_columns != self.columns or old_cell_height != self._cell_height) and top:
            new_top = ((anchor // self.columns) * self._cell_height
                       + offset * self._cell_height / old_cell_height)
            self.canvas.yview_moveto(new_top / max(self._content_height, self.canvas.winfo_height()))
        self._render_viewport()
        if self.selected is not None and not old_columns:
            self._scroll_selected()

    def _schedule_region(self, _event=None):
        if self._region_timer is None and not self._released:
            self._region_timer = self.after_idle(self._sync_region)

    def _sync_region(self):
        self._region_timer = None
        if self._released:
            return
        region = (0, 0, self.canvas.winfo_width(), max(self._content_height, self.canvas.winfo_height()))
        if region != self._scrollregion:
            self._scrollregion = region
            self.canvas.configure(scrollregion=region)

    def _wheel(self, event):
        if event.delta:
            self.canvas.yview_scroll(-int(event.delta / 120) or (-1 if event.delta > 0 else 1), "units")
        return "break"

    def release(self):
        """Stop decoding and drop Tk images; call on the UI thread."""
        if self._released:
            return
        self._released = True
        for name in ("_layout_timer", "_region_timer", "_selection_timer", "_viewport_timer", "_poll_timer"):
            timer = getattr(self, name)
            if timer is not None:
                self.after_cancel(timer)
                setattr(self, name, None)
        self._token += 1
        self._drag = None
        self._band.destroy()
        if self._drag_scroll is not None:
            self.after_cancel(self._drag_scroll)
            self._drag_scroll = None
        self._unavailable.clear()
        self.canvas.theme_redraw = None
        self._decoder.close()
        self.images.clear()
        self._source_images.clear()
        self._valid_sources.clear()
        self._jobs.clear()
        self._pending_keys.clear()
        self._requested_view = None
        self._permitted = self._remember = self._recall = None
        self._fitted_text.clear()
        self._captions.clear()
        self._positions.clear()
        self._indices.clear()
        self._paths.clear()
        for tile in (*self.tiles.values(), *self._tile_pool):
            tile.destroy()
            tile._preview_image = None
        self._tile_pool.clear()
        self.tiles.clear()
        if self._default_font_anchor is not None:
            self.canvas.delete(self._default_font_anchor)
            self._default_font_anchor = None
        # Font finalization also calls Tcl; release it on the UI thread even
        # if another owner retains this already destroyed gallery object.
        self._font = None
        self.order.clear()
        self.selected = self._anchor = None
        self.selected_keys = ()
        self._selected_set.clear()
        self.placeholder = self.unavailable_placeholder = None
        self._placeholder_appearance = None
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

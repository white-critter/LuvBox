"""Non-modal save/skip chooser with local photo and muted animation previews."""

from __future__ import annotations

from pathlib import Path
import math
import queue
import time
import tkinter as tk
from tkinter import ttk

from PIL import Image, ImageOps, ImageTk

from . import theme
from .review_preview import REVIEW_SIZE, ReviewPreview


class ReviewWindow(tk.Toplevel):
    """Show one candidate without grabbing the main window's ON/OFF switch.

    Decisions are ``save``, ``skip`` (this run only), ``never`` (do not ask
    again), ``stop`` and ``auto``. A user closing the
    window stops the download; an external ``destroy()`` only cleans up. Each
    decision waits asynchronously for the decoder's source handles to close.
    """

    def __init__(self, parent, candidate: dict, on_decision, permitted):
        owner = parent.winfo_toplevel()
        super().__init__(owner)
        self.withdraw()
        theme.apply_style(self)
        self.configure(background=theme.BG)
        self.title("저장할 파일 확인 · 러브박스")
        if owner.winfo_viewable():
            self.transient(owner)
        self.candidate = dict(candidate)
        self._on_decision = on_decision
        self._decision = None
        self._destroyed = False
        self._poll_timer = None
        self._choice_timer = None
        self._layout_timer = None
        self._render_timer = None
        self._photo = None
        self._frame_image = None
        self._next_frame = 0.0
        self.preview = None
        self.protocol("WM_DELETE_WINDOW", self.cancel)
        self.bind("<Escape>", lambda _event: self.cancel())

        screen_width, screen_height = self.winfo_screenwidth(), self.winfo_screenheight()
        width = min(720, max(320, screen_width - 48))
        height = min(650, max(320, screen_height - 100))
        self.geometry(f"{width}x{height}")
        self.minsize(min(380, width), min(340, height))
        body = self.body = ttk.Frame(self, padding=(20, 18))
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(2, weight=1, minsize=32)
        self.heading = ttk.Label(body, text="이 파일을 저장할까요?", style="Section.TLabel")
        self.heading.grid(row=0, column=0, sticky="w")
        # Details may include a long filename and translated guidance. Scroll
        # only that information; choices stay outside it at every window size.
        self.info_view = theme.ScrollableContent(body, padding=(0, 0))
        self.info_view.grid(row=1, column=0, sticky="ew", pady=(8, 10))
        self.info_view.canvas.configure(height=112)
        information = self.info_view.content
        author = str(candidate.get("author") or "작성자 미상")[:100]
        posted = str(candidate.get("posted_at") or "")[:60]
        filename = str(candidate.get("filename") or Path(candidate.get("path") or "").name)[:300]
        self.details = ttk.Label(information, text=f"@{author.lstrip('@')}  {posted}\n{filename}".strip(),
                                 style="Muted.TLabel", width=1, wraplength=max(260, width - 60))
        self.details.pack(fill="x", pady=(0, 8))
        self.explanation = ttk.Label(information, text="미리보기를 위해 파일을 먼저 받습니다. ‘이번엔 건너뛰기’는 이번 실행에만 "
                                                "적용하며, ‘저장 안 함’은 다음에도 받지 않습니다.",
                                     style="Muted.TLabel", width=1, wraplength=max(260, width - 60))
        self.explanation.pack(fill="x")
        self.note = ttk.Label(information, text="미리보기를 준비하고 있습니다…", style="Muted.TLabel",
                              width=1, wraplength=max(260, width - 60))
        self.note.pack(fill="x", pady=(8, 0))
        self.canvas = tk.Canvas(body, width=min(REVIEW_SIZE[0], width - 40),
                                height=min(REVIEW_SIZE[1], max(80, height - 265)),
                                highlightthickness=0, background=theme.CARD)
        self.canvas.grid(row=2, column=0, sticky="nsew")
        self.canvas.theme_redraw = self._render
        self._image_item = self.canvas.create_image(0, 0)
        self.canvas.bind("<Configure>", self._resized)
        self.actions = ttk.Frame(body)
        self.actions.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        self.buttons = {}
        self._action_groups = []
        self._action_columns = {}
        # This file's three choices first, then the choices for the whole run.
        for row, choices in enumerate(((("save", "저장"), ("skip", "이번엔 건너뛰기"), ("never", "저장 안 함")),
                                       (("auto", "이후 자동 저장"), ("stop", "전체 작업 중단")))):
            line = ttk.Frame(self.actions)
            line.pack(fill="x", pady=(0, 8) if row == 0 else (0, 0))
            buttons = []
            for column, (action, label) in enumerate(choices):
                button = ttk.Button(line, text=label, command=lambda action=action: self.choose(action),
                                    style="DialogPrimary.TButton" if action == "save" else "TButton")
                button.grid(row=0, column=column * 2, sticky="nsew")
                self.buttons[action] = button
                buttons.append(button)
            self._action_groups.append((line, buttons))
        self.bind("<Configure>", self._queue_layout, add="+")
        self.info_view.content.bind("<Configure>", self._queue_layout, add="+")

        path = Path(candidate.get("path") or "")
        try:
            allowed = bool(candidate.get("path")) and bool(permitted(path))
        except (OSError, ValueError):
            allowed = False
        if allowed:
            self.preview = ReviewPreview(path, str(candidate.get("media_type") or "photo"))
            self._poll_timer = self.after(25, self._poll)
        else:
            self.note.configure(text="파일 위치를 확인할 수 없습니다. 건너뛰거나 작업을 중단해 주세요.")
            self.buttons["save"].configure(state="disabled")
        self.update_idletasks()
        x = max(0, min(owner.winfo_rootx() + (owner.winfo_width() - width) // 2, screen_width - width))
        y = max(0, min(owner.winfo_rooty() + (owner.winfo_height() - height) // 3, screen_height - height - 40))
        self.geometry(f"+{x}+{y}")
        self.deiconify()
        self.lift()
        # Skipping is the default keyboard focus: Return never silently saves.
        self.buttons["skip"].focus_set()

    def _queue_layout(self, event=None):
        if event is not None and event.widget not in (
                self, self.body, self.actions, self.info_view.canvas, self.info_view.content):
            return
        if not self._destroyed and self._layout_timer is None:
            self._layout_timer = self.after_idle(self._layout)

    def _layout(self):
        self._layout_timer = None
        if self._destroyed:
            return
        width = self.actions.winfo_width()
        if width <= 1:
            return
        for group, (line, buttons) in enumerate(self._action_groups):
            needed = max(button.winfo_reqwidth() for button in buttons)
            columns = max(1, min(len(buttons), (width + 6) // (needed + 6)))
            if self._action_columns.get(group) == columns:
                continue
            self._action_columns[group] = columns
            for column in range(len(buttons) * 2 - 1):
                line.columnconfigure(column, weight=0, minsize=0, uniform="")
            for column in range(columns):
                line.columnconfigure(column * 2, weight=1, uniform=f"review-action-{group}")
                if column:
                    line.columnconfigure(column * 2 - 1, minsize=6)
            for index, button in enumerate(buttons):
                if len(buttons) == 3 and columns == 2:
                    # Give Save its own row, then keep both ways of declining
                    # together instead of leaving a lone button in a third cell.
                    row, column = (0, 0) if index == 0 else (1, index - 1)
                    span = 3 if index == 0 else 1
                else:
                    row, column = divmod(index, columns)
                    span = 1
                button.grid_configure(row=row, column=column * 2, columnspan=span,
                                      pady=(8 if row else 0, 0))
        wrap = max(80, self.info_view.canvas.winfo_width() - 20)
        for label in (self.details, self.explanation, self.note):
            if int(label.cget("wraplength")) != wrap:
                label.configure(wraplength=wrap)
        # Keep a usable information viewport and preview even with large fonts.
        # The normal minimum stays 380x340; a larger action group may require
        # more height, bounded by the available screen.
        fixed = self.heading.winfo_reqheight() + self.actions.winfo_reqheight() + 66
        min_height = min(self.winfo_screenheight() - 100, max(340, fixed + 64))
        minimum = (min(380, self.winfo_screenwidth() - 48), min_height)
        if self.minsize() != minimum:
            self.minsize(*minimum)
        height = max(32, min(128, self.info_view.content.winfo_reqheight(), self.body.winfo_height() - fixed - 32))
        if int(self.info_view.canvas.cget("height")) != height:
            self.info_view.canvas.configure(height=height)

    def _resized(self, _event=None):
        if not self._destroyed and self._frame_image is not None and self._render_timer is None:
            self._render_timer = self.after(16, self._render)

    def _render(self):
        if self._render_timer is not None:
            self.after_cancel(self._render_timer)
            self._render_timer = None
        if self._destroyed or self._frame_image is None:
            return
        width, height = max(1, self.canvas.winfo_width()), max(1, self.canvas.winfo_height())
        bounds = (min(width, REVIEW_SIZE[0]), min(height, REVIEW_SIZE[1]))
        image = (self._frame_image if self._frame_image.size == bounds else
                 ImageOps.contain(self._frame_image, bounds, Image.Resampling.LANCZOS))
        surface = image
        try:
            if image.mode != "RGB":
                # Compose alpha once instead of asking Tk to blend every
                # exposed pixel. Keep the source for later theme changes.
                surface = Image.new("RGB", image.size, self.canvas.cget("background"))
                surface.paste(image, mask=image if "A" in image.getbands() else None)
            if (self._photo is not None and
                    (self._photo.width(), self._photo.height()) == surface.size):
                self._photo.paste(surface)
            else:
                self._photo = ImageTk.PhotoImage(surface, master=self)
                self.canvas.itemconfigure(self._image_item, image=self._photo)
            self.canvas.coords(self._image_item, width // 2, height // 2)
        finally:
            if surface is not image:
                surface.close()
            if image is not self._frame_image:
                image.close()

    def _poll(self):
        if self._poll_timer is not None:
            self.after_cancel(self._poll_timer)
        self._poll_timer = None
        if self._destroyed or self._decision is not None or self.preview is None:
            return
        now = time.monotonic()
        if now >= self._next_frame:
            try:
                frame = self.preview.frames.get_nowait()
            except queue.Empty:
                frame = None
                # A newly arriving frame starts a fresh timeline. A decoder
                # stall must not make queued frames burst to catch up later.
                self._next_frame = 0.0
            if frame is not None:
                self.note.configure(text=frame.note)
                if frame.image is not None:
                    previous, self._frame_image = self._frame_image, frame.image
                    self._render()
                    if previous is not None:
                        previous.close()
                duration = max(0, frame.duration_ms) / 1000
                # Preserve cadence through small timer delays. Beyond half a
                # frame, start anew so this frame cannot flash briefly while
                # playback tries to catch up with an old deadline.
                base = self._next_frame
                if not base or now - base >= duration / 2:
                    base = now
                self._next_frame = base + duration
                rendered = time.monotonic()
                if duration and self._next_frame <= rendered:
                    self._next_frame = rendered + duration
        if self.preview.finished.is_set() and self.preview.frames.empty() and now >= self._next_frame:
            return
        remaining = self._next_frame - time.monotonic()
        # Wake at the frame's deadline; a fixed 20ms poll quantized an 83ms
        # frame to around 100ms, depending on how much rendering cost that day.
        delay = max(1, math.ceil(remaining * 1000)) if remaining > 0 else 8
        self._poll_timer = self.after(delay, self._poll)

    def cancel(self):
        self.choose("stop")
        return "break"

    def choose(self, action):
        if self._destroyed or self._decision is not None or action not in self.buttons:
            return
        if action == "save" and self.buttons["save"].instate(["disabled"]):
            return
        self._decision = action
        for button in self.buttons.values():
            button.configure(state="disabled")
        self.note.configure(text="다음 작업을 준비하고 있습니다…")
        if self.preview is not None:
            self.preview.close()
        self._finish_choice()

    def _finish_choice(self):
        self._choice_timer = None
        if self._destroyed:
            return
        if self.preview is not None and not self.preview.finished.is_set():
            self._choice_timer = self.after(25, self._finish_choice)
            return
        callback, action = self._on_decision, self._decision
        if self.preview is not None and getattr(self.preview, "release_error", False):
            action = "stop"
        self.destroy()
        if callback is not None:
            callback(action)

    def destroy(self):
        if self._destroyed:
            return
        self._destroyed = True
        if self.preview is not None:
            self.preview.close()
        for name in ("_poll_timer", "_choice_timer", "_layout_timer", "_render_timer"):
            timer = getattr(self, name)
            if timer is not None:
                self.after_cancel(timer)
                setattr(self, name, None)
        self._on_decision = None
        self.canvas.theme_redraw = None
        self._photo = None
        if self._frame_image is not None:
            self._frame_image.close()
            self._frame_image = None
        theme.release_variables(self)
        super().destroy()

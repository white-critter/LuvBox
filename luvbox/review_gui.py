"""Non-modal save/skip chooser with local photo and muted animation previews."""

from __future__ import annotations

from pathlib import Path
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
        body = ttk.Frame(self, padding=(20, 18))
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(3, weight=1)
        ttk.Label(body, text="이 파일을 저장할까요?", style="Section.TLabel").grid(row=0, column=0, sticky="w")
        author = str(candidate.get("author") or "작성자 미상")[:100]
        posted = str(candidate.get("posted_at") or "")[:60]
        filename = str(candidate.get("filename") or Path(candidate.get("path") or "").name)[:300]
        self.details = ttk.Label(body, text=f"@{author.lstrip('@')}  {posted}\n{filename}".strip(),
                                 style="Muted.TLabel", wraplength=max(260, width - 40))
        self.details.grid(row=1, column=0, sticky="ew", pady=(7, 8))
        self.explanation = ttk.Label(body, text="미리보기를 위해 파일을 먼저 받습니다. ‘이번엔 건너뛰기’는 이번 실행에만 "
                                                "적용하며, ‘저장 안 함’은 다음에도 받지 않습니다.",
                                     style="Muted.TLabel", wraplength=max(260, width - 40))
        self.explanation.grid(row=2, column=0, sticky="ew", pady=(0, 12))
        self.canvas = tk.Canvas(body, width=min(REVIEW_SIZE[0], width - 40),
                                height=min(REVIEW_SIZE[1], max(80, height - 265)),
                                highlightthickness=0, background=theme.CARD)
        self.canvas.grid(row=3, column=0, sticky="nsew")
        self.canvas.bind("<Configure>", self._resized)
        self.note = ttk.Label(body, text="미리보기를 준비하고 있습니다…", style="Muted.TLabel",
                              wraplength=max(260, width - 40))
        self.note.grid(row=4, column=0, sticky="ew", pady=(9, 12))
        self.actions = ttk.Frame(body)
        self.actions.grid(row=5, column=0, sticky="ew")
        self.buttons = {}
        # This file's three choices first, then the choices for the whole run.
        for row, choices in enumerate(((("save", "저장"), ("skip", "이번엔 건너뛰기"), ("never", "저장 안 함")),
                                       (("auto", "이후 자동 저장"), ("stop", "전체 작업 중단")))):
            line = ttk.Frame(self.actions)
            line.pack(fill="x", pady=(0, 8) if row == 0 else (0, 0))
            line.columnconfigure(tuple(range(len(choices))), weight=1, uniform=f"review-action-{row}")
            for column, (action, label) in enumerate(choices):
                button = ttk.Button(line, text=label, command=lambda action=action: self.choose(action),
                                    style="Primary.TButton" if action == "save" else "TButton")
                button.grid(row=0, column=column, sticky="ew", padx=(0 if column == 0 else 6, 0))
                self.buttons[action] = button

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

    def _resized(self, _event=None):
        if not self._destroyed and self._frame_image is not None:
            self._render()

    def _render(self):
        width, height = max(1, self.canvas.winfo_width()), max(1, self.canvas.winfo_height())
        bounds = (min(width, REVIEW_SIZE[0]), min(height, REVIEW_SIZE[1]))
        image = ImageOps.contain(self._frame_image, bounds, Image.Resampling.LANCZOS)
        self._photo = ImageTk.PhotoImage(image, master=self)
        self.canvas.delete("all")
        self.canvas.create_image(width // 2, height // 2, image=self._photo)
        if image is not self._frame_image:
            image.close()

    def _poll(self):
        self._poll_timer = None
        if self._destroyed or self._decision is not None or self.preview is None:
            return
        now = time.monotonic()
        if now >= self._next_frame:
            try:
                frame = self.preview.frames.get_nowait()
            except queue.Empty:
                frame = None
            if frame is not None:
                self.note.configure(text=frame.note)
                if frame.image is not None:
                    previous, self._frame_image = self._frame_image, frame.image
                    self._render()
                    if previous is not None:
                        previous.close()
                self._next_frame = now + max(0, frame.duration_ms) / 1000
        self._poll_timer = self.after(20, self._poll)

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
        for name in ("_poll_timer", "_choice_timer"):
            timer = getattr(self, name)
            if timer is not None:
                self.after_cancel(timer)
                setattr(self, name, None)
        self._on_decision = None
        self._photo = None
        if self._frame_image is not None:
            self._frame_image.close()
            self._frame_image = None
        theme.release_variables(self)
        super().destroy()

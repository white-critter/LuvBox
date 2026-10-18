"""Themed stand-ins for tkinter.messagebox.

Native Windows message boxes always use the system's light colors. These keep
messagebox's calls (title, message, parent=...) and results, follow the app's
light/dark appearance, and stay modal to their parent window.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from . import theme


_ICONS = {"info": ("i", "BLUE"), "warning": ("!", "ORANGE"), "error": ("×", "RED"), "question": ("?", "BLUE")}


class Dialog(tk.Toplevel):
    """One message with buttons; ``result`` is the chosen button's value."""

    def __init__(self, parent, title, message, *, icon="info", buttons=(("확인", "ok"),),
                 default=0, cancel=None):
        owner = (parent or tk._get_default_root()).winfo_toplevel()
        super().__init__(owner)
        self.withdraw()
        self.title(title or "러브박스")
        self.resizable(False, False)
        # A dialog of a hidden window would be hidden with it on Windows.
        if owner.winfo_viewable():
            self.transient(owner)
        self.configure(bg=theme.CARD)
        self.owner = owner
        self.message = str(message or "")
        self.result = cancel
        self._cancel = cancel
        self.body = body = ttk.Frame(self, style="Plain.TFrame", padding=(28, 26, 28, 22))
        body.pack(fill="both", expand=True)
        heading = ttk.Frame(body, style="Plain.TFrame")
        heading.pack(fill="x")
        heading.columnconfigure(1, weight=1)
        glyph, color = _ICONS.get(icon, _ICONS["info"])
        badge = tk.Canvas(heading, width=38, height=38, highlightthickness=0, background=theme.CARD)
        def redraw_badge():
            badge.delete("all")
            badge.configure(background=theme.CARD)
            badge.create_oval(1, 1, 37, 37, fill=getattr(theme, color), outline="")
            badge.create_text(19, 19, text=glyph, fill=theme.CARD, font=(theme.FONT, 15, "bold"))
        badge.theme_redraw = redraw_badge
        redraw_badge()
        badge.grid(row=0, column=0, sticky="n", padx=(0, 14))
        self.title_label = ttk.Label(heading, text=title or "", style="CardHeading.TLabel",
                                     wraplength=460, justify="left")
        self.title_label.grid(row=0, column=1, sticky="w")

        # The message may contain many restore/error details. Keep the actions
        # outside its scroll area so a long message never hides its buttons.
        self.message_view = theme.ScrollableContent(body, padding=(0, 0))
        self.message_view.configure(style="Plain.TFrame")
        self.message_view.canvas.configure(background=theme.CARD, width=460)
        self.message_view.content.configure(style="Plain.TFrame")
        self.message_view.pack(fill="both", expand=True, pady=(16, 0))
        self.message_label = ttk.Label(self.message_view.content, text=self.message,
                                       style="Card.TLabel", wraplength=460, justify="left")
        self.message_label.pack(fill="x", anchor="w")
        self.separator = ttk.Separator(body)
        self.separator.pack(fill="x", pady=(20, 16))
        self.actions = row = ttk.Frame(body, style="Plain.TFrame")
        row.pack(fill="x")
        self.buttons = []
        for index, (label, value) in enumerate(buttons):
            row.columnconfigure(index * 2, weight=1, uniform="dialog-action")
            if index:
                row.columnconfigure(index * 2 - 1, minsize=8)
            button = ttk.Button(row, text=label, command=lambda value=value: self.choose(value),
                                style="DialogPrimary.TButton" if index == default else "TButton")
            button.grid(row=0, column=index * 2, sticky="nsew")
            self.buttons.append(button)
        # Align the group with the right edge while preserving button order.
        row.pack_configure(anchor="e", fill="none")
        self.default_button = self.buttons[default]
        self.protocol("WM_DELETE_WINDOW", self.cancel)
        self.bind("<Escape>", self._escape)
        # Return activates the focused button, as in native dialogs.
        self.bind("<Return>", self._return)
        self.bind("<Control-c>", lambda _: self._copy())
        self._fit()

    def _return(self, event):
        (event.widget if event.widget in self.buttons else self.default_button).invoke()
        return "break"

    def _escape(self, _event):
        self.cancel()
        return "break"

    def _copy(self):
        self.clipboard_clear()
        self.clipboard_append(self.message)

    def choose(self, value):
        self.result = value
        self.destroy()

    def cancel(self):
        self.choose(self._cancel)

    def _fit(self):
        """Measure the message after wrapping and bound the whole dialog."""
        width = min(580, max(240, self.winfo_screenwidth() - 48))
        max_height = min(760, max(180, self.winfo_screenheight() - 100))
        content_width = max(100, width - 56)
        self.title_label.configure(wraplength=max(80, content_width - 52))
        # Reserve scrollbar space before measuring to avoid height changes when
        # scrolling becomes necessary. Long unbroken paths wrap here as well.
        self.message_label.configure(wraplength=max(80, content_width - 20))
        # Canvas is packed before its conditional scrollbar. Keep its requested
        # width below the available width so pack reserves room for the bar.
        self.message_view.canvas.configure(width=max(100, content_width - 20), height=1)
        self.update_idletasks()
        chrome = self.winfo_reqheight() - self.message_view.winfo_reqheight()
        message_height = self.message_view.content.winfo_reqheight()
        self.message_view.canvas.configure(height=max(40, min(message_height, max_height - chrome)))
        self.update_idletasks()
        height = min(max_height, self.winfo_reqheight())
        self.geometry(f"{width}x{height}")
        self.message_view._schedule_layout()

    def _place(self):
        self._fit()
        self.update_idletasks()
        width, height = self.winfo_reqwidth(), self.winfo_reqheight()
        if self.owner.winfo_viewable():
            x = self.owner.winfo_rootx() + (self.owner.winfo_width() - width) // 2
            y = self.owner.winfo_rooty() + (self.owner.winfo_height() - height) // 3
        else:
            x = (self.winfo_screenwidth() - width) // 2
            y = (self.winfo_screenheight() - height) // 3
        # Tk positions the outer frame; the client starts below its title bar.
        # Keep the same screen reserve used by _fit for the frame and taskbar.
        x = max(0, min(x, self.winfo_screenwidth() - width - 16))
        y = max(0, min(y, self.winfo_screenheight() - height - 100))
        self.geometry(f"+{x}+{y}")

    def run(self):
        """Show modally and wait for a choice, like tkinter.messagebox."""
        previous_grab = self.grab_current()
        previous_focus = self.focus_get()
        previous_grab_kind = previous_grab.grab_status() if previous_grab is not None else None
        self._place()
        self.deiconify()
        self.lift()
        self.default_button.focus_set()
        try:
            self.grab_set()
        except tk.TclError:
            pass
        try:
            self.wait_window()
        finally:
            # A child question temporarily replaces its parent's modal grab.
            # Closing it must restore that parent instead of enabling the app
            # behind both dialogs; focus also returns to the prior control.
            try:
                if previous_grab is not None and previous_grab.winfo_exists():
                    if previous_grab_kind == "global":
                        previous_grab.grab_set_global()
                    else:
                        previous_grab.grab_set()
                if previous_focus is not None and previous_focus.winfo_exists():
                    previous_focus.focus_set()
            except tk.TclError:
                pass
        return self.result


def showinfo(title=None, message=None, *, parent=None, **_):
    return Dialog(parent, title, message, icon="info").run()


def showwarning(title=None, message=None, *, parent=None, **_):
    return Dialog(parent, title, message, icon="warning").run()


def showerror(title=None, message=None, *, parent=None, **_):
    return Dialog(parent, title, message, icon="error").run()


def askyesno(title=None, message=None, *, parent=None, default="yes", **_) -> bool:
    return bool(Dialog(parent, title, message, icon="question",
                       buttons=(("예", True), ("아니요", False)),
                       default=1 if str(default).lower() == "no" else 0, cancel=False).run())


class DeleteDialog(Dialog):
    """Confirm a physical-file summary and its separate redownload choice."""

    def __init__(self, parent, count: int, total_bytes: int, filenames):
        from .history_gui import format_bytes
        names = list(filenames)
        preview = "\n".join("• " + str(name)[:160] for name in names[:5])
        if len(names) > 5:
            preview += f"\n외 {len(names) - 5:,}개"
        message = (f"선택한 파일 {count:,}개 · {format_bytes(total_bytes)}\n\n{preview}\n\n"
                   "Windows 휴지통으로 옮깁니다. 옮길 수 없는 파일은 그대로 둡니다.\n"
                   "동기화 폴더라면 클라우드에서도 지워질 수 있습니다.")
        super().__init__(parent, "선택한 파일을 휴지통으로 보낼까요?", message, icon="warning",
                         buttons=(("취소", None), ("휴지통으로 보내기", True)), default=0, cancel=None)
        self.keep_deleted = tk.BooleanVar(self, value=True)
        option = ttk.Frame(self.body, style="Plain.TFrame")
        option.pack(fill="x", before=self.separator, pady=(18, 0))
        ttk.Checkbutton(option, text="이 계정에서 다시 받지 않기", variable=self.keep_deleted,
                        style="TCheckbutton").pack(anchor="w")
        ttk.Label(option, text="다시 받고 싶다면 ‘사라진 파일’에서 ‘다시 받기’를 누르세요.",
                  style="CardMuted.TLabel", wraplength=440).pack(anchor="w", pady=(6, 0))
        self.buttons[0].configure(style="TButton")
        self.buttons[1].configure(style="DialogPrimary.TButton")
        self._fit()

    def choose(self, value):
        # False is confirmation with redownload allowed; None is cancellation.
        super().choose(bool(self.keep_deleted.get()) if value is True else None)

    def destroy(self):
        super().destroy()
        # The next delete starts a worker immediately. Release this dialog's
        # Tcl variable here so a later worker GC cannot finalize it off-thread.
        theme.release_variables(self)


def confirm_delete(count: int, total_bytes: int, filenames, *, parent=None) -> bool | None:
    return DeleteDialog(parent, count, total_bytes, filenames).run()

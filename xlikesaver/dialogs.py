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
        self.title(title or "XlikeSaver")
        self.resizable(False, False)
        # A dialog of a hidden window would be hidden with it on Windows.
        if owner.winfo_viewable():
            self.transient(owner)
        self.configure(bg=theme.CARD)
        self.owner = owner
        self.message = str(message or "")
        self.result = cancel
        self._cancel = cancel
        body = ttk.Frame(self, style="Plain.TFrame", padding=(24, 22, 24, 18))
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)
        glyph, color = _ICONS.get(icon, _ICONS["info"])
        badge = tk.Canvas(body, width=34, height=34, highlightthickness=0, background=theme.CARD)
        badge.create_oval(1, 1, 33, 33, fill=getattr(theme, color), outline="")
        badge.create_text(17, 17, text=glyph, fill=theme.CARD, font=(theme.FONT, 15, "bold"))
        badge.grid(row=0, column=0, rowspan=2, sticky="n", padx=(0, 16))
        ttk.Label(body, text=title or "", style="CardBold.TLabel").grid(row=0, column=1, sticky="w")
        ttk.Label(body, text=self.message, style="Card.TLabel", wraplength=460,
                  justify="left").grid(row=1, column=1, sticky="w", pady=(6, 0))
        row = ttk.Frame(body, style="Plain.TFrame")
        row.grid(row=2, column=0, columnspan=2, sticky="e", pady=(20, 0))
        self.buttons = []
        for index, (label, value) in enumerate(buttons):
            button = ttk.Button(row, text=label, command=lambda value=value: self.choose(value),
                                style="DialogPrimary.TButton" if index == default else "TButton")
            button.pack(side="left", padx=(8 if index else 0, 0))
            self.buttons.append(button)
        self.default_button = self.buttons[default]
        self.protocol("WM_DELETE_WINDOW", self.cancel)
        self.bind("<Escape>", lambda _: self.cancel())
        # Return activates the focused button, as in native dialogs.
        self.bind("<Return>", lambda event: (event.widget.invoke() if isinstance(event.widget, ttk.Button)
                                             else self.default_button.invoke()))
        self.bind("<Control-c>", lambda _: self._copy())

    def _copy(self):
        self.clipboard_clear()
        self.clipboard_append(self.message)

    def choose(self, value):
        self.result = value
        self.destroy()

    def cancel(self):
        self.choose(self._cancel)

    def _place(self):
        self.update_idletasks()
        width, height = self.winfo_reqwidth(), self.winfo_reqheight()
        if self.owner.winfo_viewable():
            x = self.owner.winfo_rootx() + (self.owner.winfo_width() - width) // 2
            y = self.owner.winfo_rooty() + (self.owner.winfo_height() - height) // 3
        else:
            x = (self.winfo_screenwidth() - width) // 2
            y = (self.winfo_screenheight() - height) // 3
        x = max(0, min(x, self.winfo_screenwidth() - width))
        y = max(0, min(y, self.winfo_screenheight() - height - 40))
        self.geometry(f"+{x}+{y}")

    def run(self):
        """Show modally and wait for a choice, like tkinter.messagebox."""
        self._place()
        self.deiconify()
        self.lift()
        self.default_button.focus_set()
        try:
            self.grab_set()
        except tk.TclError:
            pass
        self.wait_window()
        return self.result


def showinfo(title=None, message=None, *, parent=None, **_):
    return Dialog(parent, title, message, icon="info").run()


def showwarning(title=None, message=None, *, parent=None, **_):
    return Dialog(parent, title, message, icon="warning").run()


def showerror(title=None, message=None, *, parent=None, **_):
    return Dialog(parent, title, message, icon="error").run()


def askyesno(title=None, message=None, *, parent=None, **_) -> bool:
    return bool(Dialog(parent, title, message, icon="question",
                       buttons=(("예", True), ("아니요", False)), cancel=False).run())

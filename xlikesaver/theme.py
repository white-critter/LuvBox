"""A soft, readable ttk theme shared by every window.

Small RGBA images give native ttk controls rounded edges without changing
their keyboard, selection or command behavior. Images stretch with ttk's
nine-slice element support and stay alive for the whole Tk interpreter.
"""

import math
import struct
import tkinter as tk
from tkinter import ttk
import zlib


FONT = "맑은 고딕"
BG = "#f4f6fb"
CARD = "#ffffff"
LINE = "#e0e6f0"
INK = "#243047"
MUTED = "#66748b"
BLUE = "#526fe8"
BLUE_ACTIVE = "#435ed0"
GREEN = "#23845c"
ORANGE = "#9b621a"
RED = "#b6424e"
LOG_BG = "#f8faff"

TITLE = (FONT, 22, "bold")
HEADING = (FONT, 14, "bold")
STATUS = (FONT, 15, "bold")
BODY = (FONT, 12)
BOLD = (FONT, 12, "bold")
SMALL = (FONT, 11)
NUMBER = (FONT, 17, "bold")


def _rgb(color):
    return tuple(int(color[index:index + 2], 16) for index in (1, 3, 5))


def _png(width, height, pixels):
    """Encode generated RGBA control assets using only the standard library."""
    def chunk(kind, data):
        return struct.pack("!I", len(data)) + kind + data + struct.pack("!I", zlib.crc32(kind + data) & 0xffffffff)

    rows = b"".join(b"\0" + bytes(pixels[row * width * 4:(row + 1) * width * 4]) for row in range(height))
    header = struct.pack("!IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")


def _round_rect(pixels, canvas_width, canvas_height, bounds, radius, fill, outline=None, stroke=1):
    """Paint an antialiased rounded shape with transparent outside pixels."""
    left, top, right, bottom = bounds
    center_x, center_y = (left + right) / 2, (top + bottom) / 2
    half_x, half_y = (right - left) / 2, (bottom - top) / 2
    radius = min(radius, half_x, half_y)
    fill_rgb, line_rgb = _rgb(fill), _rgb(outline or fill)
    border = stroke if outline else 0
    for y in range(max(0, math.floor(top)), min(canvas_height, math.ceil(bottom))):
        for x in range(max(0, math.floor(left)), min(canvas_width, math.ceil(right))):
            dx = abs(x + 0.5 - center_x) - (half_x - radius)
            dy = abs(y + 0.5 - center_y) - (half_y - radius)
            distance = math.hypot(max(dx, 0), max(dy, 0)) + min(max(dx, dy), 0) - radius
            coverage = min(1, max(0, 0.5 - distance))
            if not coverage:
                continue
            inside = min(coverage, min(1, max(0, 0.5 - distance - border))) if border else coverage
            alpha = round(coverage * 255)
            color = tuple(round((inside * fill_rgb[i] + (coverage - inside) * line_rgb[i]) / coverage) for i in range(3))
            index = (y * canvas_width + x) * 4
            old_alpha = pixels[index + 3] / 255
            combined = coverage + old_alpha * (1 - coverage)
            if old_alpha and combined:
                color = tuple(round((color[i] * coverage + pixels[index + i] * old_alpha * (1 - coverage)) / combined) for i in range(3))
                alpha = round(combined * 255)
            pixels[index:index + 4] = bytes((*color, alpha))


def _panel_png(fill, outline, *, radius=12, stroke=1, width=40, height=40):
    pixels = bytearray(width * height * 4)
    _round_rect(pixels, width, height, (0, 0, width, height), radius, fill, outline, stroke)
    return _png(width, height, pixels)


def _switch_png(*, selected=False, disabled=False, active=False, focus=False):
    width, height = 56, 30
    pixels = bytearray(width * height * 4)
    if focus:
        _round_rect(pixels, width, height, (0, 0, 48, 30), 15, "#dfe6ff", "#93a7f5", 1)
    track = "#bbc8eb" if selected and disabled else BLUE_ACTIVE if selected and active else BLUE if selected else "#e5eaf2" if disabled else "#becbdf" if active else "#cdd7e6"
    _round_rect(pixels, width, height, (2, 3, 46, 27), 12, track)
    start = 25 if selected else 5
    _round_rect(pixels, width, height, (start, 6, start + 18, 24), 9, "#f5f7fc" if disabled else CARD)
    return _png(width, height, pixels)


def _radio_png(*, selected=False, disabled=False, active=False, focus=False):
    width, height = 32, 28
    pixels = bytearray(width * height * 4)
    if focus:
        _round_rect(pixels, width, height, (0, 0, 28, 28), 14, "#e2e8ff", "#93a7f5", 1)
    border = "#d5ddeb" if disabled else BLUE_ACTIVE if active else BLUE if selected else "#b8c5d9"
    _round_rect(pixels, width, height, (3, 3, 25, 25), 11, "#f5f7fc" if disabled else CARD, border, 1.5)
    if selected:
        _round_rect(pixels, width, height, (8, 8, 20, 20), 6, "#a9b8e3" if disabled else BLUE_ACTIVE if active else BLUE)
    return _png(width, height, pixels)


def _image_element(style, name, images, *, border=0, minimum=(0, 0)):
    default, *states = images
    style.element_create(name, "image", default, *states, border=border, padding=0,
                         width=minimum[0], height=minimum[1], sticky="nsew")


def _control_images(root, style):
    images = {}
    def panel(name, fill, outline, **kwargs):
        images[name] = tk.PhotoImage(master=root, data=_panel_png(fill, outline, **kwargs))
        return images[name]

    # A generous solid center avoids hundreds of tiny image tiles when cards
    # stretch across a wide window on Windows.
    panel("card", CARD, LINE, radius=15, width=160, height=96)
    panel("button", "#edf1f8", "#e2e8f2")
    panel("button_active", "#e4ebf7", "#d3def2")
    panel("button_pressed", "#d8e2f3", "#c7d5ed")
    panel("button_focus", "#edf1f8", "#859ded", stroke=2)
    panel("button_focus_active", "#e4ebf7", "#859ded", stroke=2)
    panel("button_disabled", "#f0f3f8", "#e8edf5")
    panel("primary", BLUE, BLUE)
    panel("primary_active", BLUE_ACTIVE, BLUE_ACTIVE)
    panel("primary_pressed", "#354fb9", "#354fb9")
    panel("primary_focus", BLUE, "#b8c7ff", stroke=2)
    panel("primary_focus_active", BLUE_ACTIVE, "#b8c7ff", stroke=2)
    panel("primary_disabled", "#c6d1ee", "#c6d1ee")
    panel("entry", CARD, "#d6dfed", radius=10)
    panel("entry_focus", CARD, "#8299ef", radius=10, stroke=2)
    panel("entry_disabled", "#f0f3f8", "#e2e8f2", radius=10)
    panel("entry_readonly", "#f8faff", "#d6dfed", radius=10)
    panel("tab", "#e9eef7", "#e9eef7", radius=10)
    panel("tab_active", "#e1e8f8", "#e1e8f8", radius=10)
    panel("tab_selected", "#e3eaff", "#e3eaff", radius=10)
    panel("tab_focus", "#e9eef7", "#91a7f3", radius=10, stroke=2)
    panel("tab_selected_focus", "#e3eaff", "#91a7f3", radius=10, stroke=2)
    panel("tab_disabled", "#f0f3f8", "#f0f3f8", radius=10)
    panel("progress_trough", "#e6ecf7", "#e6ecf7", radius=7, width=28, height=14)
    panel("progress_bar", BLUE, BLUE, radius=7, width=28, height=14)
    for kind, generator in (("switch", _switch_png), ("radio", _radio_png)):
        for name, states in (("off", {}), ("on", {"selected": True}),
                             ("off_active", {"active": True}), ("on_active", {"selected": True, "active": True}),
                             ("off_focus", {"focus": True}), ("on_focus", {"selected": True, "focus": True}),
                             ("off_disabled", {"disabled": True}), ("on_disabled", {"selected": True, "disabled": True})):
            images[kind + "_" + name] = tk.PhotoImage(master=root, data=generator(**states))

    for name, prefix in (("Soft.Button", "button"), ("Soft.Primary", "primary")):
        _image_element(style, name, [images[prefix],
                       ("disabled", images[prefix + "_disabled"]),
                       ("pressed", images[prefix + "_pressed"]),
                       ("active", "focus", images[prefix + "_focus_active"]),
                       ("focus", images[prefix + "_focus"]),
                       ("active", images[prefix + "_active"])], border=12)
    _image_element(style, "Soft.Card", [images["card"]], border=15)
    _image_element(style, "Soft.Entry", [images["entry"], ("disabled", images["entry_disabled"]),
                   ("focus", images["entry_focus"]), ("readonly", images["entry_readonly"])], border=10)
    for name, prefix, size in (("Soft.Switch", "switch", (56, 30)), ("Soft.Radio", "radio", (32, 28))):
        _image_element(style, name, [images[prefix + "_off"],
                       ("disabled", "selected", images[prefix + "_on_disabled"]),
                       ("disabled", images[prefix + "_off_disabled"]),
                       ("focus", "selected", images[prefix + "_on_focus"]),
                       ("focus", images[prefix + "_off_focus"]),
                       ("active", "selected", images[prefix + "_on_active"]),
                       ("active", images[prefix + "_off_active"]),
                       ("selected", images[prefix + "_on"])], minimum=size)
    _image_element(style, "Soft.Tab", [images["tab"], ("disabled", images["tab_disabled"]),
                   ("selected", "focus", images["tab_selected_focus"]), ("focus", images["tab_focus"]),
                   ("selected", images["tab_selected"]), ("active", images["tab_active"])], border=10)
    _image_element(style, "Soft.Progress.trough", [images["progress_trough"]], border=6, minimum=(0, 14))
    _image_element(style, "Soft.Progress.pbar", [images["progress_bar"]], border=6, minimum=(0, 14))
    # A PhotoImage is deleted by Tk when its Python object is collected.
    root._root()._xlikesaver_theme_images = images


def apply_style(root: tk.Misc) -> None:
    """Configure the theme once per Tk interpreter, including state images."""
    if root.tk.call("info", "exists", "::xlikesaver_style") == 1:
        return
    root.option_add("*TCombobox*Listbox.font", BODY)
    style = ttk.Style(root)
    style.theme_use("clam")
    _control_images(root, style)
    style.configure(".", font=BODY, background=BG, foreground=INK)
    style.configure("TFrame", background=BG)
    style.configure("Card.TFrame", background=BG)
    style.layout("Card.TFrame", [("Soft.Card", {"sticky": "nsew"})])
    style.configure("Plain.TFrame", background=CARD)
    style.configure("TLabel", background=BG, foreground=INK, font=BODY)
    for name, values in {
        "Title.TLabel": {"font": TITLE}, "Subtitle.TLabel": {"foreground": MUTED, "font": BODY},
        "Muted.TLabel": {"foreground": MUTED, "font": SMALL}, "Status.TLabel": {"font": STATUS},
        "Warn.TLabel": {"foreground": ORANGE, "font": SMALL}, "Card.TLabel": {"background": CARD, "font": BODY},
        "CardBold.TLabel": {"background": CARD, "font": BOLD}, "CardHeading.TLabel": {"background": CARD, "font": HEADING},
        "CardMuted.TLabel": {"background": CARD, "foreground": MUTED, "font": SMALL},
        "Connected.TLabel": {"background": CARD, "foreground": GREEN, "font": BOLD},
        "Disconnected.TLabel": {"background": CARD, "foreground": ORANGE, "font": BOLD},
        "TileNumber.TLabel": {"background": CARD, "font": NUMBER},
        "TileCaption.TLabel": {"background": CARD, "foreground": MUTED, "font": SMALL},
    }.items():
        style.configure(name, **values)

    for name, element in (("TButton", "Soft.Button"), ("Primary.TButton", "Soft.Primary")):
        style.layout(name, [(element, {"sticky": "nsew", "children": [
            ("Button.padding", {"sticky": "nsew", "children": [("Button.label", {"sticky": "nsew"})]})]})])
    style.configure("TButton", font=BODY, padding=(14, 8), relief="flat", borderwidth=0, foreground=INK)
    style.map("TButton", foreground=[("disabled", "#99a5b9"), ("!disabled", INK)])
    style.configure("Primary.TButton", font=(FONT, 14, "bold"), padding=(30, 11), foreground=CARD)
    style.map("Primary.TButton", foreground=[("disabled", "#f8faff"), ("!disabled", CARD)])
    style.configure("Big.TButton", font=(FONT, 13), padding=(20, 10))
    style.layout("TEntry", [("Soft.Entry", {"sticky": "nsew", "children": [
        ("Entry.padding", {"sticky": "nsew", "children": [("Entry.textarea", {"sticky": "nsew"})]})]})])
    style.layout("TSpinbox", [("Soft.Entry", {"sticky": "nsew", "children": [
        ("Spinbox.padding", {"sticky": "nsew", "children": [
            ("null", {"side": "right", "children": [("Spinbox.uparrow", {"side": "top", "sticky": "e"}),
                                                       ("Spinbox.downarrow", {"side": "bottom", "sticky": "e"})]}),
            ("Spinbox.textarea", {"sticky": "nsew"})]})]})])
    style.layout("TCombobox", [("Soft.Entry", {"sticky": "nsew", "children": [
        ("Combobox.padding", {"sticky": "nsew", "children": [("Combobox.downarrow", {"side": "right"}),
                                                               ("Combobox.textarea", {"sticky": "nsew"})]})]})])
    for name in ("TEntry", "TSpinbox", "TCombobox"):
        style.configure(name, padding=(10, 6), fieldbackground=CARD, foreground=INK,
                        borderwidth=0, relief="flat", arrowcolor=MUTED, arrowsize=13,
                        selectbackground=BLUE, selectforeground=CARD)
        style.map(name, foreground=[("disabled", "#99a5b9")], fieldbackground=[("disabled", "#f0f3f8"), ("readonly", "#f8faff")])
    for name, prefix, element in (("TCheckbutton", "Checkbutton", "Soft.Switch"), ("TRadiobutton", "Radiobutton", "Soft.Radio")):
        style.layout(name, [(prefix + ".padding", {"sticky": "nsew", "children": [
            (element, {"side": "left"}), (prefix + ".label", {"side": "left", "sticky": "nsew"})]})])
        style.configure(name, background=CARD, foreground=INK, font=BODY, padding=(0, 1), focuscolor=BLUE)
        style.map(name, foreground=[("disabled", "#99a5b9")])
    style.configure("Plain.TCheckbutton", background=BG, font=SMALL)
    style.layout("Horizontal.TProgressbar", [("Soft.Progress.trough", {"sticky": "nsew", "children": [
        ("Soft.Progress.pbar", {"side": "left", "sticky": "ns"})]})])
    style.configure("Horizontal.TProgressbar", background=BLUE, troughcolor="#e6ecf7", borderwidth=0, thickness=14)
    style.configure("TNotebook", background=BG, borderwidth=0, tabmargins=(0, 0, 0, 8))
    style.layout("TNotebook.Tab", [("Soft.Tab", {"sticky": "nsew", "children": [
        ("Notebook.padding", {"sticky": "nsew", "children": [("Notebook.label", {"sticky": "nsew"})]})]})])
    style.configure("TNotebook.Tab", font=BODY, padding=(18, 8), foreground=MUTED)
    style.map("TNotebook.Tab", foreground=[("disabled", "#99a5b9"), ("selected", BLUE_ACTIVE), ("!selected", MUTED)])
    style.configure("Treeview", font=SMALL, rowheight=32, background=CARD, fieldbackground=CARD,
                    foreground=INK, bordercolor=LINE, lightcolor=LINE, darkcolor=LINE, relief="flat", borderwidth=1)
    style.map("Treeview", background=[("selected", "#e3eaff")], foreground=[("selected", "#314fbb")])
    style.configure("Treeview.Heading", font=(FONT, 11, "bold"), padding=(8, 7), background="#f1f5fc",
                    foreground=MUTED, borderwidth=0, relief="flat", bordercolor="#f1f5fc", lightcolor="#f1f5fc", darkcolor="#f1f5fc")
    style.map("Treeview.Heading", background=[("active", "#e8eef9")], relief=[("pressed", "flat")])
    root.tk.call("set", "::xlikesaver_style", 1)


def card(parent, **pack) -> ttk.Frame:
    """A rounded white panel with quiet edges and room around its content."""
    frame = ttk.Frame(parent, style="Card.TFrame", padding=(18, 14))
    if pack:
        frame.pack(**pack)
    return frame


def tile(parent, variable: tk.StringVar, caption: str) -> ttk.Frame:
    frame = ttk.Frame(parent, style="Card.TFrame", padding=(14, 8))
    ttk.Label(frame, textvariable=variable, style="TileNumber.TLabel").pack(anchor="w")
    ttk.Label(frame, text=caption, style="TileCaption.TLabel").pack(anchor="w")
    return frame


class ScrollableContent(ttk.Frame):
    """Keep full-sized controls reachable when a window is shorter than them."""

    def __init__(self, parent, *, padding=(24, 16)):
        super().__init__(parent)
        self.canvas = tk.Canvas(self, background=BG, highlightthickness=0, borderwidth=0,
                                width=1, height=1, yscrollincrement=24)
        self.scrollbar = ttk.Scrollbar(self, command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.content = ttk.Frame(self.canvas, padding=padding)
        self.window = self.canvas.create_window(0, 0, anchor="nw", window=self.content)
        self.layout_pending = False
        self.canvas.bind("<Configure>", self._schedule_layout)
        self.content.bind("<Configure>", self._schedule_layout)
        # Child height requests can change while the canvas keeps its frame's
        # actual height fixed, so the parent alone would miss expanded text.
        parent.bind("<Configure>", self._child_configured, add="+")
        parent.bind("<MouseWheel>", self._mousewheel, add="+")
        parent.bind("<FocusIn>", self._focus_visible, add="+")

    def _child_configured(self, event):
        if str(event.widget).startswith(str(self.content) + "."):
            self._schedule_layout()

    def _schedule_layout(self, _event=None):
        if not self.layout_pending:
            self.layout_pending = True
            self.after_idle(self._layout)

    def _layout(self):
        self.layout_pending = False
        width, height = self.canvas.winfo_width(), self.canvas.winfo_height()
        if width <= 1 or height <= 1:
            return
        content_height = max(height, self.content.winfo_reqheight())
        self.canvas.itemconfigure(self.window, width=width, height=content_height)
        self.canvas.configure(scrollregion=(0, 0, width, content_height))
        if content_height > height:
            if not self.scrollbar.winfo_manager():
                self.scrollbar.pack(side="right", fill="y")
        else:
            self.scrollbar.pack_forget()
            self.canvas.yview_moveto(0)

    def _mousewheel(self, event):
        if event.widget.winfo_class() in {"Text", "Treeview", "TScrollbar", "Scrollbar"}:
            return
        if self.scrollbar.winfo_manager() and event.delta:
            steps = -int(event.delta / 120) or (-1 if event.delta > 0 else 1)
            self.canvas.yview_scroll(steps, "units")
            return "break"

    def _focus_visible(self, event):
        widget = event.widget
        if str(widget).startswith(str(self.content) + "."):
            # Focus can follow a scroll before screen coordinates are updated.
            self.after_idle(lambda: self._ensure_visible(widget))

    def _ensure_visible(self, widget):
        if not widget.winfo_exists() or not self.scrollbar.winfo_manager():
            return
        top = widget.winfo_rooty() - self.canvas.winfo_rooty()
        bottom = top + widget.winfo_height()
        height = self.canvas.winfo_height()
        delta = top - 24 if top < 0 else bottom - height + 24 if bottom > height else 0
        if delta:
            offset = self.canvas.canvasy(0) + delta
            self.canvas.yview_moveto(max(0, offset) / self.content.winfo_height())

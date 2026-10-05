"""A soft, readable ttk theme shared by every window.

Small RGBA images give native ttk controls rounded edges without changing
their keyboard, selection or command behavior. Images stretch with ttk's
nine-slice element support and stay alive for the whole Tk interpreter.
"""

import math
import struct
import sys
import time
import tkinter as tk
from tkinter import ttk
from types import SimpleNamespace
import zlib

from .fonts import load_family


FONT = load_family()
# Stable roles make existing and newly opened windows switch together.
LIGHT = {
    "BG": "#f4f6fb", "CARD": "#ffffff", "LINE": "#e0e6f0", "INK": "#243047",
    "MUTED": "#66748b", "BLUE": "#526fe8", "BLUE_ACTIVE": "#435ed0",
    "GREEN": "#23845c", "ORANGE": "#9b621a", "RED": "#b6424e", "LOG_BG": "#f8faff",
    "KNOB": "#ffffff", "PRIMARY_INK": "#ffffff",
}
DARK = {
    "BG": "#171c28", "CARD": "#232b3b", "LINE": "#354158", "INK": "#e8edf8",
    "MUTED": "#a8b5cc", "BLUE": "#839bff", "BLUE_ACTIVE": "#9aafff",
    "GREEN": "#79d7ad", "ORANGE": "#edbc77", "RED": "#ef949e", "LOG_BG": "#202838",
    "KNOB": "#f3f6ff", "PRIMARY_INK": "#18243d",
}
# Legacy image/style shades have a corresponding dark shade. Keeping these
# explicit also recolors native Text tags and popups which specify a color.
_DARK_SHADES = {
    "#314fbb": "#c4d0ff", "#354fb9": "#aebfff", "#8299ef": "#aebfff",
    "#859ded": "#aebfff", "#91a7f3": "#aebfff", "#93a7f5": "#aebfff",
    "#99a5b9": "#78879f", "#a9b8e3": "#607196", "#b8c5d9": "#62738e",
    "#b8c7ff": "#cbd6ff", "#bbc8eb": "#576891", "#becbdf": "#5b6c87",
    "#c6d1ee": "#455372", "#c7d5ed": "#526482", "#cdd7e6": "#4a5a74",
    "#d3def2": "#4c5f80", "#d5ddeb": "#43526a", "#d6dfed": "#4b5a74",
    "#d6dff6": "#526586", "#d8e2f3": "#435575", "#dfe6ff": "#35456a",
    "#e2e8f2": "#40506b", "#e2e8ff": "#35456a", "#e3eaff": "#354871",
    "#e4ebf7": "#3b4c69", "#e5eaf2": "#354158", "#e6ecf7": "#343f55",
    "#e8edf5": "#354158", "#e8eef9": "#384762", "#e9eef9": "#303d55",
    "#edf1f8": "#303d55", "#f0f3f8": "#293345", "#f1f5fc": "#2b374d",
    "#f5f7fc": "#293345",
}
_DARK_SHADES.update({LIGHT[key]: value for key, value in DARK.items() if key not in {"KNOB", "PRIMARY_INK"}})
globals().update(LIGHT)
_active_mode = "light"


def _tone(value):
    return _DARK_SHADES.get(value.lower(), value) if _active_mode == "dark" and isinstance(value, str) else value


def _recolor(value, target):
    """Translate recognized app colors, preserving unrelated custom colors."""
    if not isinstance(value, str):
        return value
    colors = _DARK_SHADES if target == "dark" else {v: k for k, v in _DARK_SHADES.items()}
    return colors.get(value.lower(), value)


def mode(root):
    """Return the appearance of the window's Tk interpreter."""
    return getattr(root._root(), "_xlikesaver_theme_mode", "light")


def apply(root, mode="light"):
    """Apply light/dark appearance immediately to this window and its popups."""
    apply_style(root, mode=mode)


def release_variables(owner):
    """Release a closed window's own variables on its Tk thread.

    Tcl traces can keep Python callbacks and windows in reference cycles. A
    later decoder thread must not be the one to collect those Tcl resources.
    Externally retained variables remain valid, with the window traces removed.
    """
    variables = []
    for name, value in list(vars(owner).items()):
        if isinstance(value, tk.Variable):
            variables.append(value)
            setattr(owner, name, None)
        elif isinstance(value, dict):
            for key, item in list(value.items()):
                if isinstance(item, tk.Variable):
                    variables.append(item)
                    del value[key]
    for variable in variables:
        try:
            for modes, callback in variable.trace_info():
                variable.trace_remove(" ".join(modes), callback)
        except tk.TclError:
            pass


TITLE = (FONT, 22, "bold")
HEADING = (FONT, 14, "bold")
STATUS = (FONT, 15, "bold")
BODY = (FONT, 12)
BOLD = (FONT, 12, "bold")
SMALL = (FONT, 11)
NUMBER = (FONT, 17, "bold")


def _rgb(color):
    color = _tone(color)
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
    _round_rect(pixels, width, height, (start, 6, start + 18, 24), 9, "#f5f7fc" if disabled else KNOB)
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
    if name in style.element_names():
        return
    default, *states = images
    style.element_create(name, "image", default, *states, border=border, padding=0,
                         width=minimum[0], height=minimum[1], sticky="nsew")


def _control_images(root, style):
    images = getattr(root._root(), "_xlikesaver_theme_images", {})
    def asset(name, data):
        if name in images:
            images[name].configure(data=data)
        else:
            images[name] = tk.PhotoImage(master=root, data=data)
        return images[name]

    def panel(name, fill, outline, **kwargs):
        return asset(name, _panel_png(fill, outline, **kwargs))

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
    panel("tab", BG, BG, radius=11)
    panel("tab_active", "#e9eef9", "#e9eef9", radius=11)
    panel("tab_selected", CARD, "#d6dff6", radius=11)
    panel("tab_focus", BG, "#91a7f3", radius=11, stroke=2)
    panel("tab_selected_focus", CARD, "#91a7f3", radius=11, stroke=2)
    panel("tab_disabled", "#f0f3f8", "#f0f3f8", radius=10)
    panel("progress_trough", "#e6ecf7", "#e6ecf7", radius=7, width=28, height=14)
    panel("progress_bar", BLUE, BLUE, radius=7, width=28, height=14)
    for kind, generator in (("switch", _switch_png), ("radio", _radio_png)):
        for name, states in (("off", {}), ("on", {"selected": True}),
                             ("off_active", {"active": True}), ("on_active", {"selected": True, "active": True}),
                             ("off_focus", {"focus": True}), ("on_focus", {"selected": True, "focus": True}),
                             ("off_disabled", {"disabled": True}), ("on_disabled", {"selected": True, "disabled": True})):
            asset(kind + "_" + name, generator(**states))

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
    owner = root._root()
    if not getattr(owner, "_xlikesaver_image_cleanup_bound", False):
        def release(event):
            if event.widget is owner:
                owner._xlikesaver_theme_images.clear()
                owner._xlikesaver_surface_binding = None
        owner.bind("<Destroy>", release, add="+")
        owner._xlikesaver_image_cleanup_bound = True


def apply_style(root: tk.Misc, mode=None) -> None:
    """Configure or recolor a Tk interpreter while keeping native semantics."""
    global _active_mode
    owner = root._root()
    selected = mode if mode is not None else getattr(owner, "_xlikesaver_theme_mode", "light")
    if selected not in {"light", "dark"}:
        raise ValueError("appearance must be 'light' or 'dark'")
    _active_mode = selected
    globals().update(DARK if selected == "dark" else LIGHT)
    if root.tk.call("info", "exists", "::xlikesaver_style") == 1 and getattr(owner, "_xlikesaver_theme_mode", None) == selected:
        return
    owner._xlikesaver_theme_mode = selected
    root.option_add("*TCombobox*Listbox.font", BODY)
    root.option_add("*TCombobox*Listbox.background", CARD)
    root.option_add("*TCombobox*Listbox.foreground", INK)
    root.option_add("*TCombobox*Listbox.selectBackground", BLUE)
    root.option_add("*TCombobox*Listbox.selectForeground", PRIMARY_INK)
    style = ttk.Style(owner)
    if style.theme_use() != "clam":
        style.theme_use("clam")

    def configure(name, **values):
        style.configure(name, **{key: _tone(value) for key, value in values.items()})

    def mapping(name, **values):
        style.map(name, **{key: [(*row[:-1], _tone(row[-1])) for row in rows]
                           for key, rows in values.items()})
    _control_images(root, style)
    configure(".", font=BODY, background=BG, foreground=INK)
    configure("TFrame", background=BG)
    configure("Card.TFrame", background=BG)
    style.layout("Card.TFrame", [("Soft.Card", {"sticky": "nsew"})])
    configure("Plain.TFrame", background=CARD)
    configure("TLabel", background=BG, foreground=INK, font=BODY)
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
        configure(name, **values)

    for name, element in (("TButton", "Soft.Button"), ("Primary.TButton", "Soft.Primary")):
        style.layout(name, [(element, {"sticky": "nsew", "children": [
            ("Button.padding", {"sticky": "nsew", "children": [("Button.label", {"sticky": "nsew"})]})]})])
    configure("TButton", font=BODY, padding=(14, 8), relief="flat", borderwidth=0, foreground=INK)
    # Clam's inherited pressed/active grey backgrounds remain rectangular even
    # when its border is replaced by our transparent rounded image element.
    mapping("TButton", background=[("disabled", BG), ("!disabled", BG)],
              foreground=[("disabled", "#99a5b9"), ("!disabled", INK)])
    configure("Primary.TButton", font=(FONT, 14, "bold"), padding=(30, 11), foreground=PRIMARY_INK)
    style.map("Primary.TButton", foreground=[("disabled", PRIMARY_INK), ("!disabled", PRIMARY_INK)])
    configure("Big.TButton", font=(FONT, 13), padding=(20, 10))
    configure("Compact.TButton", font=SMALL, padding=(8, 5))
    configure("CompactPrimary.TButton", font=SMALL, padding=(8, 5))
    style.layout("CompactPrimary.TButton", style.layout("Primary.TButton"))
    style.map("CompactPrimary.TButton", foreground=[("disabled", PRIMARY_INK), ("!disabled", PRIMARY_INK)])
    configure("DialogPrimary.TButton", font=BOLD, padding=(18, 8), foreground=PRIMARY_INK)
    style.layout("DialogPrimary.TButton", style.layout("Primary.TButton"))
    style.map("DialogPrimary.TButton", foreground=[("disabled", PRIMARY_INK), ("!disabled", PRIMARY_INK)])
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
        configure(name, padding=(10, 6), fieldbackground=CARD, foreground=INK,
                        borderwidth=0, relief="flat", arrowcolor=MUTED, arrowsize=13,
                        selectbackground=BLUE, selectforeground="#ffffff")
        mapping(name, background=[("disabled", BG), ("!disabled", BG)],
                  foreground=[("disabled", "#99a5b9")], fieldbackground=[("disabled", "#f0f3f8"), ("readonly", "#f8faff")])
    for name, prefix, element in (("TCheckbutton", "Checkbutton", "Soft.Switch"), ("TRadiobutton", "Radiobutton", "Soft.Radio")):
        style.layout(name, [(prefix + ".padding", {"sticky": "nsew", "children": [
            (element, {"side": "left"}), (prefix + ".label", {"side": "left", "sticky": "nsew"})]})])
        configure(name, background=CARD, foreground=INK, font=BODY, padding=(0, 1), focuscolor=BLUE)
        mapping(name, foreground=[("disabled", "#99a5b9")])
    configure("Plain.TCheckbutton", background=BG, font=SMALL)
    style.layout("Horizontal.TProgressbar", [("Soft.Progress.trough", {"sticky": "nsew", "children": [
        ("Soft.Progress.pbar", {"side": "left", "sticky": "ns"})]})])
    configure("Horizontal.TProgressbar", background=BG, troughcolor="#e6ecf7", borderwidth=0, thickness=14,
              bordercolor=BG, lightcolor=BG, darkcolor=BG, relief="flat")
    mapping("Horizontal.TProgressbar", background=[("disabled", BG), ("!disabled", BG)])
    configure("TNotebook", background=BG, borderwidth=0, tabmargins=(0, 0, 0, 10),
                    bordercolor=BG, lightcolor=BG, darkcolor=BG, relief="flat")
    style.layout("TNotebook.Tab", [("Soft.Tab", {"sticky": "nsew", "children": [
        ("Notebook.padding", {"sticky": "nsew", "children": [("Notebook.label", {"sticky": "nsew"})]})]})])
    configure("TNotebook.Tab", font=BOLD, padding=(20, 9), foreground=MUTED, background=BG)
    # Selected tabs inherited a smaller padding from clam and visibly jumped
    # in size. Keep every tab's geometry and transparent backdrop identical.
    mapping("TNotebook.Tab", padding=[], expand=[],
              background=[("disabled", BG), ("!disabled", BG)],
              foreground=[("disabled", "#99a5b9"), ("selected", BLUE_ACTIVE), ("!selected", MUTED)])
    configure("Treeview", font=SMALL, rowheight=32, background=CARD, fieldbackground=CARD,
                    foreground=INK, bordercolor=LINE, lightcolor=LINE, darkcolor=LINE, relief="flat", borderwidth=1)
    mapping("Treeview", background=[("selected", "#e3eaff")], foreground=[("selected", "#314fbb")])
    configure("Treeview.Heading", font=(FONT, 11, "bold"), padding=(8, 7), background="#f1f5fc",
                    foreground=MUTED, borderwidth=0, relief="flat", bordercolor="#f1f5fc", lightcolor="#f1f5fc", darkcolor="#f1f5fc")
    mapping("Treeview.Heading", background=[("active", "#e8eef9")], relief=[("pressed", "flat")])
    configure("TScrollbar", background="#edf1f8", troughcolor=BG,
              bordercolor=BG, lightcolor=BG, darkcolor=BG, arrowcolor=MUTED,
              relief="flat", borderwidth=0)
    mapping("TScrollbar", background=[("pressed", "#d8e2f3"), ("active", "#e4ebf7")])
    _bind_surfaces(root, style)
    root.tk.call("set", "::xlikesaver_style", 1)
    _refresh_widgets(owner, selected)


def _bind_surfaces(root, style):
    """Match rounded control backdrops to their actual card/window surface.

    A Map binding covers controls created later in popups as well as the main
    form. Derived styles retain native behavior and the chosen base style.
    """
    owner = root._root()
    if hasattr(owner, "_xlikesaver_surface_binding"):
        return
    controls = {"TButton", "TEntry", "TSpinbox", "TCombobox", "TCheckbutton", "TRadiobutton", "TProgressbar"}

    def surface(parent):
        if isinstance(parent, ttk.Widget):
            name = parent.cget("style") or parent.winfo_class()
            # Card.TFrame's exterior is BG; content lies on its white center.
            if str(name).endswith("Card.TFrame"):
                return CARD
            return style.lookup(name, "background") or BG
        try:
            return parent.cget("background")
        except tk.TclError:
            return BG

    def mapped(event):
        widget = event.widget
        if isinstance(widget, (tk.Tk, tk.Toplevel)):
            # Windows creates the title bar when a window is first shown.
            title_bar(widget, mode(owner))
        _refresh_classic(widget, mode(owner))
        if not isinstance(widget, ttk.Widget):
            return
        klass = widget.winfo_class()
        base = str(widget.cget("style") or klass)
        if klass == "TProgressbar" and base == klass:
            base = str(widget.cget("orient")).capitalize() + "." + klass
        for prefix in ("SurfaceCard.", "SurfaceBg."):
            if base.startswith(prefix):
                base = base[len(prefix):]
                break
        if klass not in controls and not base.endswith("Card.TFrame"):
            return
        color = surface(widget.master)
        # The application has two surfaces; don't reinterpret custom colors.
        if color not in {CARD, BG}:
            return
        name = ("SurfaceCard." if color == CARD else "SurfaceBg.") + base
        style.configure(name, background=color)
        style.map(name, background=[("disabled", color), ("!disabled", color)])
        if widget.cget("style") != name:
            widget.configure(style=name)

    owner._xlikesaver_surface_binding = mapped
    root.bind_all("<Map>", mapped, add="+")


def title_bar(window, selected) -> bool:
    """Ask Windows 10/11 to draw this window's title bar dark or light.

    Returns False where the system has no such option (older Windows, other
    platforms) or the window has no frame yet; it is applied again when shown.
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        frame = int(window.wm_frame(), 16)
        if not frame:
            return False
        hwnd = ctypes.c_void_p(frame)
        value = ctypes.c_int(1 if selected == "dark" else 0)
        set_attribute = ctypes.windll.dwmapi.DwmSetWindowAttribute
        # DWMWA_USE_IMMERSIVE_DARK_MODE is 20, or 19 before Windows 10 20H1.
        if all(set_attribute(hwnd, key, ctypes.byref(value), ctypes.sizeof(value)) != 0 for key in (20, 19)):
            return False
        # Repaint the frame now rather than at the next focus change.
        nosize, nomove, nozorder, noactivate, framechanged = 0x1, 0x2, 0x4, 0x10, 0x20
        ctypes.windll.user32.SetWindowPos(hwnd, None, 0, 0, 0, 0,
                                          nosize | nomove | nozorder | noactivate | framechanged)
        return True
    except (tk.TclError, OSError, AttributeError, ValueError):
        return False


def _refresh_classic(widget, selected):
    if isinstance(widget, ttk.Widget):
        if isinstance(widget, ttk.Combobox):
            # ttk caches its Tcl-only popup, so option_add affects new popups
            # but a menu opened before switching must also be recolored.
            listbox = str(widget) + ".popdown.f.l"
            if widget.tk.call("winfo", "exists", listbox):
                palette = DARK if selected == "dark" else LIGHT
                widget.tk.call(listbox, "configure", "-background", palette["CARD"],
                               "-foreground", palette["INK"], "-selectbackground", palette["BLUE"],
                               "-selectforeground", palette["PRIMARY_INK"])
        # Explicit colors override ttk styles, e.g. an explanatory warning.
        for key in ("foreground", "background"):
            try:
                value = widget.cget(key)
                updated = _recolor(value, selected)
                if updated != value:
                    widget.configure(**{key: updated})
            except tk.TclError:
                pass
        return
    for key in ("background", "foreground", "insertbackground", "selectbackground", "selectforeground"):
        try:
            value = widget.cget(key)
            updated = _recolor(value, selected)
            if updated != value:
                widget.configure(**{key: updated})
        except tk.TclError:
            pass
    if isinstance(widget, tk.Text):
        for tag in widget.tag_names():
            for key in ("foreground", "background"):
                value = widget.tag_cget(tag, key)
                updated = _recolor(value, selected)
                if updated != value:
                    widget.tag_configure(tag, **{key: updated})
        widget.configure(insertbackground=INK, selectbackground=_tone("#e3eaff"), selectforeground=INK)
    if isinstance(widget, Progressbar):
        widget._draw()


def _refresh_widgets(owner, selected):
    binding = getattr(owner, "_xlikesaver_surface_binding", None)
    pending = [owner]
    while pending:
        widget = pending.pop()
        if isinstance(widget, (tk.Tk, tk.Toplevel)):
            title_bar(widget, selected)
        _refresh_classic(widget, selected)
        if binding and isinstance(widget, ttk.Widget):
            binding(SimpleNamespace(widget=widget))
        pending.extend(widget.winfo_children())
    owner.event_generate("<<AppearanceChanged>>", when="tail")


class Progressbar(tk.Canvas):
    """A border-free rounded progress bar with gently animated state changes.

    The small API used by the main form matches ttk.Progressbar. Determinate
    values always report the real worker percentage; only their visual change
    is eased. A quiet sliding pill represents work without a known percentage.
    """

    def __init__(self, parent, *, mode="determinate", value=0, maximum=100,
                 length=200, height=14, variable=None, **kwargs):
        self._progress = {"mode": self._check_mode(mode), "value": float(value),
                          "maximum": self._check_maximum(maximum), "length": length}
        self._display_value = max(0., min(float(value) / float(maximum), 1.))
        self._timer = None
        self._running = False
        self._phase_start = time.monotonic()
        self._last_frame = self._phase_start
        self._variable = variable
        self._variable_trace = None
        background = BG
        if isinstance(parent, ttk.Widget):
            parent_style = parent.cget("style") or parent.winfo_class()
            background = CARD if str(parent_style).endswith("Card.TFrame") else ttk.Style(parent).lookup(parent_style, "background") or BG
        else:
            try:
                background = parent.cget("background")
            except tk.TclError:
                pass
        kwargs.setdefault("background", background)
        kwargs.setdefault("highlightthickness", 0)
        kwargs.setdefault("borderwidth", 0)
        kwargs.setdefault("takefocus", False)
        super().__init__(parent, width=length, height=height, **kwargs)
        self._track = self.create_line(0, 0, 1, 0, capstyle="butt")
        self._track_caps = [self.create_image(0, 0) for _ in range(2)]
        self._fill = self.create_line(0, 0, 1, 0, capstyle="butt")
        self._fill_caps = [self.create_image(0, 0) for _ in range(2)]
        self._cap_images = {}
        self._cap_signature = None
        self.bind("<Configure>", lambda event: self._draw())
        self.bind("<Destroy>", self._destroyed, add="+")
        if variable is not None:
            self._progress["value"] = float(variable.get())
            self._display_value = self._target()
            self._variable_trace = variable.trace_add("write", self._variable_changed)
        self._draw()

    @staticmethod
    def _check_mode(value):
        if value not in {"determinate", "indeterminate"}:
            raise tk.TclError("mode must be determinate or indeterminate")
        return value

    @staticmethod
    def _check_maximum(value):
        number = float(value)
        if not math.isfinite(number) or number <= 0:
            raise tk.TclError("maximum must be a positive finite number")
        return number

    def _target(self):
        return max(0., min(self._progress["value"] / self._progress["maximum"], 1.))

    def configure(self, cnf=None, **kwargs):
        if cnf is not None and not isinstance(cnf, dict):
            if cnf in self._progress:
                return (cnf, cnf, cnf, self._progress[cnf], self._progress[cnf])
            return super().configure(cnf, **kwargs)
        values = dict(cnf or {})
        values.update(kwargs)
        changes = {}
        for key in self._progress:
            if key in values:
                changes[key] = values.pop(key)
        if "mode" in changes:
            changes["mode"] = self._check_mode(changes["mode"])
        if "maximum" in changes:
            changes["maximum"] = self._check_maximum(changes["maximum"])
        if "value" in changes:
            changes["value"] = float(changes["value"])
            if not math.isfinite(changes["value"]):
                raise tk.TclError("value must be finite")
        self._progress.update(changes)
        if "length" in changes:
            values["width"] = changes["length"]
        result = super().configure(**values) if values or not changes else None
        if changes:
            self._last_frame = time.monotonic()
            self._draw()
            self._schedule()
        return result

    config = configure

    def cget(self, key):
        if key in self._progress:
            return self._progress[key]
        return super().cget(key)

    def __getitem__(self, key):
        return self.cget(key)

    def __setitem__(self, key, value):
        self.configure(**{key: value})

    def start(self, interval=None):
        self._running = True
        self._phase_start = self._last_frame = time.monotonic()
        self._schedule()

    def stop(self):
        self._running = False
        if self._timer is not None:
            self.after_cancel(self._timer)
            self._timer = None
        self._draw()

    def step(self, amount=1):
        self.configure(value=self._progress["value"] + amount)

    def _variable_changed(self, *_):
        try:
            self.configure(value=self._variable.get())
        except (tk.TclError, ValueError):
            pass

    def _schedule(self):
        if self._timer is None and (self._running or
                self._progress["mode"] == "determinate" and abs(self._display_value - self._target()) > .0005):
            self._timer = self.after(20, self._tick)

    def _tick(self):
        self._timer = None
        now = time.monotonic()
        elapsed = min(.1, max(.001, now - self._last_frame))
        self._last_frame = now
        if self._progress["mode"] == "determinate":
            target = self._target()
            self._display_value += (target - self._display_value) * (1 - math.exp(-elapsed / .075))
            if abs(self._display_value - target) <= .0005:
                self._display_value = target
        self._draw(now)
        self._schedule()

    def _draw(self, now=None):
        if not hasattr(self, "_track"):
            return
        width, height = self.winfo_width(), self.winfo_height()
        if width <= 1 or height <= 1:
            return
        palette = DARK if mode(self) == "dark" else LIGHT
        radius = min(height / 2, width / 2)
        center = height / 2
        track_color = _DARK_SHADES["#e6ecf7"] if mode(self) == "dark" else "#e6ecf7"
        self.coords(self._track, radius, center, width - radius, center)
        self.itemconfigure(self._track, width=height, fill=track_color)
        signature = (height, track_color, palette["BLUE"])
        if self._cap_signature != signature:
            for name, color in (("track", track_color), ("fill", palette["BLUE"])):
                data = _panel_png(color, color, radius=height / 2, width=height, height=height)
                if name in self._cap_images:
                    self._cap_images[name].configure(data=data)
                else:
                    self._cap_images[name] = tk.PhotoImage(master=self, data=data)
            self._cap_signature = signature
        for item, x in zip(self._track_caps, (radius, width - radius)):
            self.coords(item, x, center)
            self.itemconfigure(item, image=self._cap_images["track"])
        if self._progress["mode"] == "indeterminate":
            # Eased direction changes keep the pill completely inside its track.
            phase = ((now or time.monotonic()) - self._phase_start) / 1.8 * 2 * math.pi
            fraction = (1 - math.cos(phase)) / 2
            segment = max(height, width * .26)
            left = (width - segment) * fraction
            right = left + segment
            visible = self._running
        else:
            left, right = 0, width * self._display_value
            visible = self._display_value > 0
        self.coords(self._fill, left + radius, center, max(left + radius, right - radius), center)
        state = "normal" if visible else "hidden"
        self.itemconfigure(self._fill, width=height, fill=palette["BLUE"], state=state)
        for item, x in zip(self._fill_caps, (left + radius, max(left + radius, right - radius))):
            self.coords(item, x, center)
            self.itemconfigure(item, image=self._cap_images["fill"], state=state)

    def _destroyed(self, event):
        if event.widget is not self:
            return
        self._running = False
        if self._timer is not None:
            self.after_cancel(self._timer)
            self._timer = None
        if self._variable is not None and self._variable_trace is not None:
            self._variable.trace_remove("write", self._variable_trace)
            self._variable_trace = None
        self._variable = None
        self._cap_images.clear()


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
        self._layout_timer = None
        self._focus_timers = set()
        self.canvas.bind("<Configure>", self._schedule_layout)
        self.content.bind("<Configure>", self._schedule_layout)
        # Child height requests can change while the canvas keeps its frame's
        # actual height fixed, so the parent alone would miss expanded text.
        self._parent_bindings = [(event, parent.bind(event, callback, add="+")) for event, callback in (
            ("<Configure>", self._child_configured), ("<MouseWheel>", self._mousewheel),
            ("<FocusIn>", self._focus_visible))]

    def _child_configured(self, event):
        if str(event.widget).startswith(str(self.content) + "."):
            self._schedule_layout()

    def _schedule_layout(self, _event=None):
        if not self.layout_pending:
            self.layout_pending = True
            self._layout_timer = self.after_idle(self._layout)

    def _layout(self):
        self._layout_timer = None
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
            def reveal():
                self._focus_timers.discard(timer)
                self._ensure_visible(widget)
            timer = self.after_idle(reveal)
            self._focus_timers.add(timer)

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

    def destroy(self):
        # Child .master links and our convenience attributes form Python
        # cycles. Break only the closed tree, on the owning Tk thread.
        children = []
        pending = list(self.winfo_children())
        while pending:
            child = pending.pop()
            children.append(child)
            pending.extend(child.winfo_children())
        for event, callback in self._parent_bindings:
            self.master.unbind(event, callback)
        self._parent_bindings.clear()
        for timer in self._focus_timers | ({self._layout_timer} if self._layout_timer else set()):
            self.after_cancel(timer)
        self._focus_timers.clear()
        self._layout_timer = None
        self.layout_pending = False
        super().destroy()
        for child in children:
            child.master = None
        self.canvas = self.scrollbar = self.content = None

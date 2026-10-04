"""Shared look for every window: larger type, plain cards, one accent color.

Windows runs this app without DPI scaling, so sizes are chosen to stay
readable on a 100%-scaled QHD monitor (12 pt body text is about 16 px).
"""

import tkinter as tk
from tkinter import ttk

FONT = "맑은 고딕"
BG = "#f3f5f8"
CARD = "#ffffff"
LINE = "#d9e0e8"
INK = "#18212e"
MUTED = "#5b6879"
BLUE = "#1a6fd6"
BLUE_ACTIVE = "#1559ad"
GREEN = "#1d7f46"
ORANGE = "#a14f0b"
RED = "#b3261e"
LOG_BG = "#f7f9fc"

TITLE = (FONT, 22, "bold")
HEADING = (FONT, 14, "bold")
STATUS = (FONT, 15, "bold")
BODY = (FONT, 12)
BOLD = (FONT, 12, "bold")
SMALL = (FONT, 11)
NUMBER = (FONT, 17, "bold")


def apply_style(root: tk.Misc) -> None:
    """Configure ttk styles once per Tk interpreter."""
    if root.tk.call("info", "exists", "::xlikesaver_style") == 1:
        return
    root.tk.call("set", "::xlikesaver_style", 1)
    # Not "*Font": ttk.Label has its own -font option, and an option-database
    # font would override every label style (titles, numbers, bold labels).
    root.option_add("*TCombobox*Listbox.font", BODY)
    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure(".", font=BODY, background=BG, foreground=INK)
    style.configure("TFrame", background=BG)
    style.configure("Card.TFrame", background=CARD, bordercolor=LINE, relief="solid", borderwidth=1)
    style.configure("Plain.TFrame", background=CARD)
    style.configure("TLabel", background=BG, foreground=INK, font=BODY)
    style.configure("Title.TLabel", font=TITLE)
    style.configure("Subtitle.TLabel", foreground=MUTED, font=BODY)
    style.configure("Muted.TLabel", foreground=MUTED, font=SMALL)
    style.configure("Status.TLabel", font=STATUS)
    style.configure("Warn.TLabel", foreground=ORANGE, font=SMALL)
    style.configure("Card.TLabel", background=CARD, font=BODY)
    style.configure("CardBold.TLabel", background=CARD, font=BOLD)
    style.configure("CardHeading.TLabel", background=CARD, font=HEADING)
    style.configure("CardMuted.TLabel", background=CARD, foreground=MUTED, font=SMALL)
    style.configure("Connected.TLabel", background=CARD, foreground=GREEN, font=BOLD)
    style.configure("Disconnected.TLabel", background=CARD, foreground=ORANGE, font=BOLD)
    style.configure("TileNumber.TLabel", background=CARD, font=NUMBER)
    style.configure("TileCaption.TLabel", background=CARD, foreground=MUTED, font=SMALL)
    style.configure("TButton", font=BODY, padding=(14, 7))
    style.configure("Primary.TButton", font=(FONT, 14, "bold"), padding=(30, 10),
                    background=BLUE, foreground="white", bordercolor=BLUE)
    style.map("Primary.TButton", background=[("disabled", "#a9c0dd"), ("active", BLUE_ACTIVE)],
              foreground=[("disabled", "#eef3fa")])
    style.configure("Big.TButton", font=(FONT, 13), padding=(20, 9))
    style.configure("TEntry", padding=6, fieldbackground="white")
    style.configure("TSpinbox", padding=5, fieldbackground="white")
    style.configure("TCombobox", padding=5)
    style.configure("TCheckbutton", background=CARD, font=BODY)
    style.configure("TRadiobutton", background=CARD, font=BODY)
    style.configure("Plain.TCheckbutton", background=BG, font=SMALL)
    style.configure("Horizontal.TProgressbar", background=BLUE, troughcolor="#e4eaf2", thickness=12)
    style.configure("TNotebook", background=BG)
    style.configure("TNotebook.Tab", font=BODY, padding=(18, 7))
    style.configure("Treeview", font=SMALL, rowheight=30, background="white", fieldbackground="white")
    style.configure("Treeview.Heading", font=(FONT, 11, "bold"), padding=(6, 6))


def card(parent, **pack) -> ttk.Frame:
    """A white panel with a thin border and inner padding."""
    frame = ttk.Frame(parent, style="Card.TFrame", padding=(18, 14))
    if pack:
        frame.pack(**pack)
    return frame


def tile(parent, variable: tk.StringVar, caption: str) -> ttk.Frame:
    """A small statistic: a large number over a short caption."""
    frame = ttk.Frame(parent, style="Card.TFrame", padding=(14, 6))
    ttk.Label(frame, textvariable=variable, style="TileNumber.TLabel").pack(anchor="w")
    ttk.Label(frame, text=caption, style="TileCaption.TLabel").pack(anchor="w")
    return frame

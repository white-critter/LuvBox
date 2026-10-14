"""A small bar chart for counts per month, drawn on a Tk canvas.

One series, one hue (validated against both surfaces), thin bars with a 2px
gap and rounded data ends, a recessive grid, the largest bar labelled, and the
exact value on hover. Text uses the theme's text colors, never the bar color.
"""

from __future__ import annotations

import math
import tkinter as tk

from . import theme


BAR = {"light": "#526fe8", "dark": "#6f88f2"}
BAR_ACTIVE = {"light": "#3d58c9", "dark": "#93a9ff"}
# More months than this would make unreadable slivers; show years instead.
MAX_MONTH_BARS = 36


def bins(months: list[tuple[str, int]]) -> tuple[list[tuple[str, int]], str]:
    """Fill empty months between the first and last; years for long spans."""
    if not months:
        return [], "month"
    first, last = months[0][0], months[-1][0]
    year, month = int(first[:4]), int(first[5:7])
    end = (int(last[:4]), int(last[5:7]))
    counts = dict(months)
    filled = []
    while (year, month) <= end:
        key = f"{year:04d}-{month:02d}"
        filled.append((key, counts.get(key, 0)))
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    if len(filled) <= MAX_MONTH_BARS:
        return filled, "month"
    years = {}
    for key, count in filled:
        years[key[:4]] = years.get(key[:4], 0) + count
    return sorted(years.items()), "year"


def _nice(value: int) -> int:
    """A round axis maximum (1, 2 or 5 × 10^n) at or above value."""
    if value <= 0:
        return 1
    power = 10 ** int(math.floor(math.log10(value)))
    for step in (1, 2, 5, 10):
        if step * power >= value:
            return step * power
    return 10 * power


class MonthlyBars(tk.Canvas):
    def __init__(self, parent, *, height=230):
        super().__init__(parent, height=height, width=1, background=theme.CARD,
                         highlightthickness=0, borderwidth=0)
        self.data: list[tuple[str, int]] = []
        self.unit = "month"
        self.empty_text = "아직 기록이 없습니다."
        self._bars: list[tuple[float, float, int]] = []   # x0, x1, data index
        self._hover = None
        self.bind("<Configure>", lambda _: self.draw())
        self.bind("<Motion>", self._move)
        self.bind("<Leave>", lambda _: self._set_hover(None))

    def show(self, months: list[tuple[str, int]], *, empty_text: str = "아직 기록이 없습니다."):
        self.data, self.unit = bins(months)
        self.empty_text = empty_text
        self._hover = None
        self.draw()

    def theme_redraw(self):
        self.configure(background=theme.CARD)
        self.draw()

    def _mode(self):
        return theme.mode(self)

    def draw(self):
        self.delete("all")
        self._bars = []
        width, height = self.winfo_width(), self.winfo_height()
        if width <= 1 or height <= 1:
            return
        small = (theme.FONT, 10)
        if not self.data or not any(count for _, count in self.data):
            self.create_text(width / 2, height / 2, text=self.empty_text, fill=theme.MUTED,
                             font=theme.SMALL, width=max(120, width - 40), justify="center")
            return
        left, right, top, bottom = 46, 10, 22, 30
        plot_width, plot_height = max(1, width - left - right), max(1, height - top - bottom)
        peak = max(count for _, count in self.data)
        axis = _nice(peak)
        # Recessive grid: baseline, half and top, labelled in muted text.
        for fraction in (0, 0.5, 1):
            y = top + plot_height * (1 - fraction)
            self.create_line(left, y, left + plot_width, y, fill=theme.LINE, width=1)
            value = axis * fraction
            label = f"{value:,.0f}" if value == int(value) else f"{value:,.1f}"
            self.create_text(left - 8, y, text=label, anchor="e", fill=theme.MUTED, font=small)
        slot = plot_width / len(self.data)
        gap = 2 if slot > 4 else 0
        color = BAR[self._mode()]
        label_width = 54 if self.unit == "month" else 34
        step = max(1, math.ceil(label_width / slot))
        peak_index = max(range(len(self.data)), key=lambda index: self.data[index][1])
        for index, (key, count) in enumerate(self.data):
            x0 = left + slot * index + gap / 2
            x1 = left + slot * (index + 1) - gap / 2
            self._bars.append((left + slot * index, left + slot * (index + 1), index))
            if count:
                y = top + plot_height * (1 - count / axis)
                self._bar(x0, y, x1, top + plot_height, BAR_ACTIVE[self._mode()] if index == self._hover else color)
            # Label every n-th period counting back from the latest one.
            if (len(self.data) - 1 - index) % step == 0:
                text = key if self.unit == "year" else key[2:].replace("-", ".")
                self.create_text((x0 + x1) / 2, top + plot_height + 6, text=text, anchor="n",
                                 fill=theme.MUTED, font=small)
        key, count = self.data[peak_index]
        x = left + slot * (peak_index + 0.5)
        y = top + plot_height * (1 - count / axis)
        self.create_text(x, y - 4, text=f"{count:,}", anchor="s", fill=theme.INK, font=small)
        if self._hover is not None:
            self._tooltip(self._hover)

    def _bar(self, x0, y0, x1, y1, color):
        """A bar with 4px rounded top corners anchored to the baseline."""
        radius = min(4, (x1 - x0) / 2, (y1 - y0))
        if radius < 1:
            self.create_rectangle(x0, y0, x1, y1, fill=color, outline="")
            return
        self.create_rectangle(x0, y0 + radius, x1, y1, fill=color, outline="")
        self.create_rectangle(x0 + radius, y0, x1 - radius, y0 + radius, fill=color, outline="")
        self.create_oval(x0, y0, x0 + 2 * radius, y0 + 2 * radius, fill=color, outline="")
        self.create_oval(x1 - 2 * radius, y0, x1, y0 + 2 * radius, fill=color, outline="")

    def _tooltip(self, index):
        key, count = self.data[index]
        label = (f"{key}년" if self.unit == "year" else f"{key[:4]}년 {int(key[5:])}월") + f" · {count:,}개"
        x0, x1, _ = self._bars[index]
        x = min(max((x0 + x1) / 2, 70), self.winfo_width() - 70)
        text = self.create_text(x, 4, text=label, anchor="n", fill=theme.INK, font=theme.SMALL)
        bx0, by0, bx1, by1 = self.bbox(text)
        box = self.create_rectangle(bx0 - 8, by0 - 3, bx1 + 8, by1 + 3, fill=theme.BG, outline=theme.LINE)
        self.tag_lower(box, text)

    def _move(self, event):
        index = next((item for x0, x1, item in self._bars if x0 <= event.x < x1), None)
        self._set_hover(index)

    def _set_hover(self, index):
        if index != self._hover:
            self._hover = index
            self.draw()

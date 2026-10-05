"""Native control behavior and image lifetime for the rounded ttk theme."""

import gc
import os
import tkinter as tk
from tkinter import font, ttk
import unittest

from xlikesaver import theme
from xlikesaver import fonts


class ThemeTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.attributes("-alpha", 0)
        self.root.geometry("420x280")
        self.root.configure(background=theme.BG)
        theme.apply_style(self.root)
        self.addCleanup(self.destroy_root)

    def destroy_root(self):
        for timer in self.root.tk.call("after", "info"):
            self.root.tk.call("after", "cancel", timer)
        self.root.destroy()

    def test_rounded_progress_bar_renders_the_actual_download_fraction(self):
        bar = ttk.Progressbar(self.root, length=300, maximum=100)
        bar.pack()
        self.root.update()
        middle = bar.winfo_height() // 2
        bar.configure(value=0)
        self.root.update()
        self.assertTrue(bar.identify(25, middle).endswith("trough"))
        bar.configure(value=50)
        self.root.update()
        self.assertTrue(bar.identify(25, middle).endswith("pbar"))
        self.assertTrue(bar.identify(275, middle).endswith("trough"))
        bar.configure(value=100)
        self.root.update()
        self.assertTrue(bar.identify(275, middle).endswith("pbar"))

    def test_switch_and_radio_preserve_keyboard_and_disabled_behavior(self):
        enabled = tk.BooleanVar(value=False)
        choice = tk.StringVar(value="first")
        switch = ttk.Checkbutton(self.root, text="동영상", variable=enabled)
        radio = ttk.Radiobutton(self.root, text="작성자별", variable=choice, value="author")
        switch.pack()
        radio.pack()
        self.root.update()
        for widget in (switch, radio):
            widget.focus_force()
            self.root.update()
            widget.event_generate("<KeyPress-space>")
            widget.event_generate("<KeyRelease-space>")
            self.root.update()
        self.assertTrue(enabled.get())
        self.assertEqual(choice.get(), "author")
        switch.state(["disabled"])
        radio.state(["disabled"])
        switch.invoke()
        radio.invoke()
        self.assertTrue(enabled.get())
        self.assertEqual(choice.get(), "author")

    def test_rounded_button_preserves_commands_and_disabled_state(self):
        calls = []
        button = ttk.Button(self.root, text="저장 시작", style="Primary.TButton", command=lambda: calls.append("start"))
        button.pack()
        self.root.update()
        button.invoke()
        button.state(["disabled"])
        button.invoke()
        self.assertEqual(calls, ["start"])
        # The rounded image must not add its own 40px size to the text padding.
        self.assertLessEqual(button.winfo_height(), 60)

    def test_rounded_fields_keep_native_text_and_arrow_hit_targets(self):
        entry = ttk.Entry(self.root, font=theme.BODY)
        spin = ttk.Spinbox(self.root, from_=0, to=100, font=theme.BODY)
        combo = ttk.Combobox(self.root, values=("사진", "동영상"), state="readonly", font=theme.BODY)
        for widget in (entry, spin, combo):
            widget.pack(fill="x", padx=20, pady=4)
        entry.insert(0, "C:/한글 저장 폴더/파일.png")
        spin.set("20")
        combo.current(1)
        self.root.update()
        self.assertEqual(entry.get(), "C:/한글 저장 폴더/파일.png")
        self.assertEqual(spin.get(), "20")
        self.assertEqual(combo.get(), "동영상")
        combo_targets = {combo.identify(x, combo.winfo_height() // 2) for x in range(combo.winfo_width())}
        spin_targets = {spin.identify(x, y) for x in range(spin.winfo_width() - 35, spin.winfo_width())
                        for y in range(spin.winfo_height())}
        self.assertTrue(any(target.endswith("downarrow") for target in combo_targets))
        self.assertTrue(any(target.endswith("uparrow") for target in spin_targets))
        self.assertTrue(any(target.endswith("downarrow") for target in spin_targets))
        self.assertLessEqual(entry.winfo_height(), 50)

    def test_theme_images_outlive_popup_and_repeated_apply(self):
        names = set(map(str, self.root.tk.call("image", "names")))
        self.assertGreater(len(names), 20)
        popup = tk.Toplevel(self.root)
        theme.apply_style(popup)
        popup.destroy()
        gc.collect()
        self.assertTrue(names.issubset(set(map(str, self.root.tk.call("image", "names")))))
        self.assertEqual(len(names), len(self.root.tk.call("image", "names")))

    def test_rounded_controls_match_their_parent_background_in_every_state(self):
        # These state backgrounds caused the visible square outside the PNG's
        # transparent rounded corners, particularly on white cards.
        self.root.geometry("640x700")
        card = theme.card(self.root, fill="x")
        for parent, background in ((self.root, theme.BG), (card, theme.CARD)):
            widgets = [ttk.Button(parent, text="열기"),
                       ttk.Button(parent, text="저장 시작", style="Primary.TButton"),
                       ttk.Entry(parent), ttk.Combobox(parent, state="readonly")]
            for widget in widgets:
                widget.pack()
            self.root.update()
            style = ttk.Style(self.root)
            for widget in widgets:
                self.assertTrue(widget.winfo_ismapped())
                for state in ((), ("active",), ("pressed",), ("disabled",), ("focus",), ("readonly",)):
                    with self.subTest(widget=widget.winfo_class(), background=background, state=state):
                        self.assertEqual(style.lookup(widget.cget("style"), "background", state), background)
        self.assertTrue(self.root._xlikesaver_theme_images["button"].transparency_get(0, 0))

    def test_popup_controls_created_later_use_the_white_surface(self):
        popup = tk.Toplevel(self.root)
        popup.configure(background=theme.CARD)
        self.addCleanup(popup.destroy)
        button = ttk.Button(popup, text="닫기", style="Big.TButton")
        button.pack()
        self.root.update()
        style = ttk.Style(self.root)
        self.assertEqual(style.lookup(button.cget("style"), "background", ("pressed",)), theme.CARD)

    def test_notebook_tab_headers_keep_their_size_when_selection_changes(self):
        self.root.geometry("600x250")
        notebook = ttk.Notebook(self.root)
        notebook.pack(fill="both", expand=True)
        for label in ("저장한 파일", "실행 기록"):
            notebook.add(ttk.Frame(notebook), text=label)
        self.root.update()

        def bounds():
            boxes = {}
            for y in range(min(60, notebook.winfo_height())):
                for x in range(notebook.winfo_width()):
                    try:
                        index = notebook.index(f"@{x},{y}")
                    except tk.TclError:
                        continue
                    box = boxes.setdefault(index, [x, y, x, y])
                    box[2], box[3] = x, y
            return boxes

        first = bounds()
        self.assertEqual(set(first), {0, 1})
        notebook.select(1)
        self.root.update()
        self.assertEqual(first, bounds())
        self.assertEqual(first[0][3] - first[0][1], first[1][3] - first[1][1])

    @unittest.skipUnless(os.name == "nt", "Windows private font registration")
    def test_bundled_pretendard_renders_normal_and_bold_korean_text(self):
        self.assertEqual(theme.FONT, fonts.FAMILY)
        for specification, weight in ((theme.BODY, "normal"), (theme.BOLD, "bold")):
            actual = font.Font(root=self.root, font=specification)
            self.assertEqual(actual.actual("family"), fonts.FAMILY)
            self.assertEqual(actual.actual("weight"), weight)
            self.assertGreater(actual.measure("기록과 통계 · 사진 123"), 0)


if __name__ == "__main__":
    unittest.main()

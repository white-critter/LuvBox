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
        theme.apply_style(self.root, mode="light")
        self.addCleanup(self.destroy_root)

    def destroy_root(self):
        self.root.update_idletasks()
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

    def test_dark_theme_switch_updates_popups_plain_text_and_control_states(self):
        self.root.geometry("600x500")
        card = theme.card(self.root, fill="x")
        button = ttk.Button(card, text="저장", style="CompactPrimary.TButton")
        button.pack()
        text = tk.Text(card, background=theme.CARD, foreground=theme.INK, height=2)
        text.tag_configure("note", foreground=theme.MUTED)
        text.pack(fill="x")
        view = theme.ScrollableContent(self.root)
        view.pack(fill="both", expand=True)
        popup = tk.Toplevel(self.root, background=theme.CARD)
        self.addCleanup(popup.destroy)
        popup_button = ttk.Button(popup, text="닫기")
        popup_button.pack()
        self.root.update()
        image_names = set(map(str, self.root.tk.call("image", "names")))
        image = self.root._xlikesaver_theme_images["card"]
        original_center = image.get(80, 48)
        style = ttk.Style(self.root)
        for appearance in ("dark", "light", "dark", "light"):
            with self.subTest(appearance=appearance):
                theme.apply(self.root, appearance)
                self.root.update()
                palette = theme.DARK if appearance == "dark" else theme.LIGHT
                self.assertEqual(theme.mode(self.root), appearance)
                self.assertEqual(self.root.cget("background"), palette["BG"])
                self.assertEqual(text.cget("background"), palette["CARD"])
                self.assertEqual(text.cget("foreground"), palette["INK"])
                self.assertEqual(text.tag_cget("note", "foreground"), palette["MUTED"])
                self.assertEqual(view.canvas.cget("background"), palette["BG"])
                self.assertEqual(popup.cget("background"), palette["CARD"])
                self.assertEqual(style.lookup("Treeview", "background"), palette["CARD"])
                self.assertEqual(style.lookup("Treeview", "foreground"), palette["INK"])
                for widget in (button, popup_button):
                    for state in ((), ("active",), ("pressed",), ("disabled",), ("focus",)):
                        self.assertEqual(style.lookup(widget.cget("style"), "background", state), palette["CARD"])
                self.assertEqual(style.lookup(button.cget("style"), "foreground"), palette["PRIMARY_INK"])
                # Regenerating colors must update existing assets without
                # losing references or creating hundreds of leaked images.
                self.assertIs(self.root._xlikesaver_theme_images["card"], image)
                self.assertEqual(image_names, set(map(str, self.root.tk.call("image", "names"))))
                self.assertEqual(image.get(80, 48) == original_center, appearance == "light")
        theme.apply(self.root, "dark")
        theme.apply_style(popup)
        self.assertEqual(theme.mode(popup), "dark")
        theme.apply(self.root, "light")

    def test_popup_created_after_dark_switch_uses_dark_surface(self):
        theme.apply(self.root, "dark")
        popup = tk.Toplevel(self.root, background=theme.LIGHT["CARD"])
        self.addCleanup(popup.destroy)
        text = tk.Text(popup, height=2, background=theme.LIGHT["CARD"], foreground=theme.LIGHT["INK"])
        text.pack()
        button = ttk.Button(popup, text="닫기")
        button.pack()
        self.root.update()
        self.assertEqual(text.cget("background"), theme.DARK["CARD"])
        self.assertEqual(text.cget("foreground"), theme.DARK["INK"])
        self.assertEqual(ttk.Style(self.root).lookup(button.cget("style"), "background", ("pressed",)), theme.DARK["CARD"])
        theme.apply(self.root, "light")

    def test_smooth_progress_reports_real_value_and_eases_only_the_display(self):
        bar = theme.Progressbar(self.root, maximum=100, value=0, length=300)
        bar.pack()
        self.root.update()
        self.assertEqual(int(bar.cget("highlightthickness")), 0)
        self.assertEqual(int(bar.cget("borderwidth")), 0)
        bar.configure(value=50)
        self.assertEqual(bar.cget("value"), 50)
        self.assertEqual(bar._display_value, 0)
        wait = tk.BooleanVar(self.root, False)
        self.root.after(350, lambda: wait.set(True))
        self.root.wait_variable(wait)
        self.assertGreater(bar._display_value, .49)
        self.assertLessEqual(bar._display_value, .5)
        coordinates = bar.coords(bar._fill)
        radius = bar.winfo_height() / 2
        self.assertAlmostEqual(coordinates[2] + radius, bar.winfo_width() * .5, delta=2)
        bar.configure(value=100)
        wait.set(False)
        self.root.after(550, lambda: wait.set(True))
        self.root.wait_variable(wait)
        self.assertEqual(bar._display_value, 1)
        self.assertIsNone(bar._timer)
        bar.configure(value=0)
        bar.destroy()
        self.root.update()
        self.assertIsNone(bar._timer)

    def test_indeterminate_progress_stays_in_track_and_stops_without_timer(self):
        card = theme.card(self.root, fill="x")
        bar = theme.Progressbar(card, mode="indeterminate", length=300)
        bar.pack()
        self.root.update()
        self.assertEqual(bar.cget("background"), theme.CARD)
        bar.start(12)
        initial = bar.coords(bar._fill)
        wait = tk.BooleanVar(self.root, False)
        self.root.after(260, lambda: wait.set(True))
        self.root.wait_variable(wait)
        self.assertNotEqual(initial, bar.coords(bar._fill))
        for x in bar.coords(bar._fill)[::2]:
            self.assertGreaterEqual(x, bar.winfo_height() / 2)
            self.assertLessEqual(x, bar.winfo_width() - bar.winfo_height() / 2)
        theme.apply(self.root, "dark")
        self.assertEqual(bar.cget("background"), theme.DARK["CARD"])
        self.assertEqual(bar.itemcget(bar._fill, "fill"), theme.DARK["BLUE"])
        bar.stop()
        self.assertIsNone(bar._timer)
        self.assertEqual(bar.itemcget(bar._fill, "state"), "hidden")
        bar.destroy()
        theme.apply(self.root, "light")

    def test_legacy_rounded_progress_uses_parent_color_in_all_states(self):
        card = theme.card(self.root, fill="x")
        bar = ttk.Progressbar(card, value=40)
        bar.pack()
        self.root.update()
        style = ttk.Style(self.root)
        for appearance in ("dark", "light"):
            theme.apply(self.root, appearance)
            self.root.update()
            for state in ((), ("active",), ("disabled",)):
                self.assertEqual(style.lookup(bar.cget("style"), "background", state), theme.CARD)

    def test_combo_menu_opened_before_switch_keeps_readable_dark_colors(self):
        combo = ttk.Combobox(self.root, values=("사진", "동영상"), state="readonly")
        combo.pack()
        self.root.update()
        popup = str(self.root.tk.call("ttk::combobox::PopdownWindow", str(combo)))
        listbox = popup + ".f.l"
        for appearance in ("dark", "light"):
            theme.apply(self.root, appearance)
            palette = theme.DARK if appearance == "dark" else theme.LIGHT
            self.assertEqual(self.root.tk.call(listbox, "cget", "-background"), palette["CARD"])
            self.assertEqual(self.root.tk.call(listbox, "cget", "-foreground"), palette["INK"])
            self.assertEqual(self.root.tk.call(listbox, "cget", "-selectforeground"), palette["PRIMARY_INK"])

    def test_progress_variable_trace_and_animation_are_removed_on_close(self):
        value = tk.DoubleVar(self.root, 0)
        bar = theme.Progressbar(self.root, variable=value)
        bar.pack()
        self.root.update()
        value.set(70)
        self.assertEqual(bar["value"], 70)
        self.assertEqual(len(value.trace_info()), 1)
        bar.destroy()
        self.assertEqual(value.trace_info(), [])
        self.assertIsNone(bar._timer)
        # A subsequent worker-side value update must not call a dead canvas.
        value.set(80)
        self.root.update()

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

"""Native control behavior and image lifetime for the rounded ttk theme."""

import gc
import tkinter as tk
from tkinter import ttk
import unittest

from xlikesaver import theme


class ThemeTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.attributes("-alpha", 0)
        self.root.geometry("420x280")
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


if __name__ == "__main__":
    unittest.main()

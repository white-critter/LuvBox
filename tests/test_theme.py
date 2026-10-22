"""Native control behavior and image lifetime for the rounded ttk theme."""

import gc
import json
import math
import os
import sys
import tempfile
import tkinter as tk
from pathlib import Path
from tkinter import font, ttk
import unittest
from unittest.mock import patch
import zipfile

from luvbox import theme
from luvbox import fonts


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
        # Collect on the main thread; see tests.test_gui.release_tk.
        del self.root
        gc.collect()

    @unittest.skipUnless(os.name == "nt", "Windows draws the title bar")
    def test_title_bar_follows_dark_and_light_appearance(self):
        import ctypes
        window = tk.Toplevel(self.root)
        window.attributes("-alpha", 0)
        window.update()

        def dark():
            value = ctypes.c_int(-1)
            ctypes.windll.dwmapi.DwmGetWindowAttribute(ctypes.c_void_p(int(window.wm_frame(), 16)), 20,
                                                       ctypes.byref(value), ctypes.sizeof(value))
            return value.value

        # A window shown after switching gets the dark frame automatically.
        theme.apply(self.root, "dark")
        self.addCleanup(theme.apply, self.root, "light")
        window.update()
        self.assertEqual(dark(), 1)
        later = tk.Toplevel(self.root)
        later.attributes("-alpha", 0)
        later.update()
        self.assertTrue(theme.title_bar(later, "dark"))
        theme.apply(self.root, "light")
        self.assertEqual(dark(), 0)
        later.destroy()
        window.destroy()
        self.assertFalse(theme.title_bar(window, "dark"))

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

    def test_buttons_have_a_visible_fill_and_border_before_hover(self):
        for appearance, palette in (("light", theme.LIGHT), ("dark", theme.DARK)):
            theme.apply(self.root, appearance)
            for suffix, surface in (("", palette["BG"]), ("_card", palette["CARD"])):
                for name, fill, line in (
                        ("button", "BUTTON", "BUTTON_LINE"),
                        ("ghost", "BUTTON", "BUTTON_LINE"),
                        ("selected", "ACCENT_SOFT", "BLUE"),
                        ("button_disabled", "FIELD_DISABLED", "DISABLED_LINE"),
                        ("ghost_disabled", "FIELD_DISABLED", "DISABLED_LINE")):
                    with self.subTest(appearance=appearance, surface=surface, kind=name):
                        image = self.root._luvbox_theme_images[name + suffix]
                        color = lambda x, y: "#%02x%02x%02x" % tuple(image.get(x, y))
                        center = color(image.width() // 2, image.height() // 2)
                        edge = color(image.width() // 2, 0)
                        self.assertEqual(center, palette[fill])
                        self.assertEqual(edge, palette[line])
                        self.assertNotEqual(center, surface)
                        self.assertNotEqual(edge, center)
                        self.assertEqual(color(0, 0), surface)

    def test_regular_and_view_switch_buttons_share_the_same_height(self):
        buttons = [ttk.Button(self.root, text="보기", style=style) for style in
                   ("TButton", "Ghost.TButton", "Segment.TButton", "SegmentSelected.TButton")]
        for button in buttons:
            button.pack()
        for appearance in ("light", "dark"):
            theme.apply(self.root, appearance)
            self.root.update()
            heights = {button.winfo_height() for button in buttons}
            self.assertEqual(len(heights), 1)
            self.assertNotIn(1, heights)

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

    def test_installed_app_reuses_drawn_images_for_both_appearances(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(sys, "frozen", True, create=True), \
                patch("luvbox.settings.app_directory", return_value=Path(folder)), \
                patch.object(theme, "_read_bundled_assets", return_value=None), \
                patch.dict(theme._ASSET_CACHE, clear=True):
            cache = Path(folder) / "cache"
            cache.mkdir()
            (cache / "theme-0.0.1-light-old.zip").write_bytes(b"older version")
            light = theme._asset_cache_path(theme._asset_key())
            theme._ASSET_CACHE.clear()
            theme.apply_style(self.root, mode="dark")
            theme.apply_style(self.root, mode="light")
            self.assertEqual(len(list(cache.glob("theme-*.zip"))), 2)
            self.assertTrue(light.is_file())
            self.assertFalse((cache / "theme-0.0.1-light-old.zip").exists())
            drawn = theme._read_asset_cache(theme._asset_key())
            self.assertEqual(set(drawn), set(theme._ASSET_CACHE[theme._asset_key()]))
            theme._ASSET_CACHE.clear()
            with patch.object(theme, "_draw_assets", side_effect=AssertionError("drawn again")):
                theme.apply_style(self.root, mode="dark")
                theme.apply_style(self.root, mode="light")
            # A damaged file is drawn again and replaced.
            light.write_bytes(b"not a zip")
            theme._ASSET_CACHE.clear()
            self.assertIsNone(theme._read_asset_cache(theme._asset_key()))
            theme.apply_style(self.root, mode="dark")
            theme.apply_style(self.root, mode="light")
            self.assertIsNotNone(theme._read_asset_cache(theme._asset_key()))
            self.assertFalse(list(cache.glob("*.partial")))

    def test_renderer_revision_invalidates_installed_cache_without_a_version_change(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(sys, "frozen", True, create=True), \
                patch("luvbox.settings.app_directory", return_value=Path(folder)), \
                patch.object(theme, "_read_bundled_assets", return_value=None), \
                patch.dict(theme._ASSET_CACHE, clear=True):
            cache = Path(folder) / "cache"
            theme.apply_style(self.root, mode="dark")
            theme.apply_style(self.root, mode="light")
            first = set(cache.glob("theme-*.zip"))
            self.assertEqual(len(first), 2)
            theme._ASSET_CACHE.clear()
            with patch.object(theme, "_draw_assets", side_effect=AssertionError("drawn again")):
                theme.apply_style(self.root, mode="dark")
                theme.apply_style(self.root, mode="light")
            theme._ASSET_CACHE.clear()
            with patch.object(theme, "_RENDERER_REVISION", theme._RENDERER_REVISION + 1):
                with patch.object(theme, "_draw_assets", wraps=theme._draw_assets) as draw:
                    theme.apply_style(self.root, mode="dark")
                    theme.apply_style(self.root, mode="light")
                self.assertEqual(draw.call_count, 2)
                second = set(cache.glob("theme-*.zip"))
                self.assertEqual(len(second), 2)
                self.assertFalse(first & second)

    def test_installed_app_recovers_incomplete_or_undecodable_cache_at_start(self):
        original = dict(theme._ASSET_CACHE[theme._asset_key()])
        without_button = dict(original)
        without_button.pop("button")
        damaged = {**original, "card": original["card"][:16]}
        for case, assets in (("single-image", {"card": original["card"]}),
                             ("missing-image", without_button), ("broken-png", damaged)):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as folder, \
                    patch.object(sys, "frozen", True, create=True), \
                    patch("luvbox.settings.app_directory", return_value=Path(folder)), \
                    patch.object(theme, "_read_bundled_assets", return_value=None), \
                    patch.dict(theme._ASSET_CACHE, clear=True):
                location = theme._asset_cache_path(theme._asset_key())
                location.parent.mkdir()
                with zipfile.ZipFile(location, "w", zipfile.ZIP_STORED) as archive:
                    for name, data in assets.items():
                        archive.writestr(name, data)
                for attempt in ("recovery", "next-start"):
                    window = tk.Tk()
                    window.withdraw()
                    try:
                        theme._ASSET_CACHE.clear()
                        with patch.object(theme, "_draw_assets", wraps=theme._draw_assets) as draw:
                            theme.apply_style(window, mode="light")
                        self.assertEqual(draw.call_count, int(attempt == "recovery"))
                        button = ttk.Button(window, text="Save", style="Primary.TButton")
                        self.assertIn("Soft.Primary", ttk.Style(window).layout("Primary.TButton")[0][0])
                        self.assertEqual(set(window._luvbox_theme_images), set(original))
                        self.assertEqual(theme._read_asset_cache(theme._asset_key()), original)
                        button.destroy()
                    finally:
                        window.update_idletasks()
                        window.destroy()

    def test_installed_app_recovers_broken_png_during_theme_switch(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(sys, "frozen", True, create=True), \
                patch("luvbox.settings.app_directory", return_value=Path(folder)), \
                patch.object(theme, "_read_bundled_assets", return_value=None), \
                patch.dict(theme._ASSET_CACHE, clear=True):
            theme.apply_style(self.root, mode="dark")
            location = theme._asset_cache_path(theme._asset_key())
            original = dict(theme._ASSET_CACHE[theme._asset_key()])
            image_names = set(map(str, self.root.tk.call("image", "names")))
            theme.apply_style(self.root, mode="light")
            # This late image fails after the button/card images were recolored.
            with zipfile.ZipFile(location, "w", zipfile.ZIP_STORED) as archive:
                for name, data in original.items():
                    archive.writestr(name, data[:16] if name == "radio_on_disabled_card" else data)
            theme._ASSET_CACHE.clear()
            with patch.object(theme, "_draw_assets", wraps=theme._draw_assets) as draw:
                theme.apply_style(self.root, mode="dark")
            self.assertEqual(draw.call_count, 1)
            self.assertEqual(theme._read_asset_cache(theme._asset_key()), original)
            self.assertEqual(set(map(str, self.root.tk.call("image", "names"))), image_names)
            images = self.root._luvbox_theme_images
            for name, color in (("button", theme.DARK["BG"]), ("button_card", theme.DARK["CARD"]),
                                ("radio_on_disabled_card", theme.DARK["CARD"])):
                self.assertEqual("#%02x%02x%02x" % tuple(images[name].get(0, 0)), color)
            theme._ASSET_CACHE.clear()
            with patch.object(theme, "_draw_assets", side_effect=AssertionError("drawn again")):
                theme.apply_style(self.root, mode="light")
                theme.apply_style(self.root, mode="dark")

    def test_first_use_of_each_appearance_uses_shipped_images_in_source_and_frozen_builds(self):
        for frozen in (False, True):
            with self.subTest(frozen=frozen), \
                    patch.object(sys, "frozen", frozen, create=True), \
                    patch.object(theme, "_asset_cache_path", return_value=None), \
                    patch.dict(theme._ASSET_CACHE, clear=True), \
                    patch.object(theme, "_draw_assets", side_effect=AssertionError("synchronous draw")):
                for appearance in ("light", "dark"):
                    window = tk.Tk()
                    window.withdraw()
                    try:
                        theme.apply_style(window, mode=appearance)
                        self.assertEqual(set(window._luvbox_theme_images), theme._ASSET_NAMES)
                        self.assertEqual(theme._read_bundled_assets(theme._asset_key()),
                                         theme._ASSET_CACHE[theme._asset_key()])
                    finally:
                        window.destroy()

    def test_frozen_self_test_checks_shipped_images_without_requiring_a_disk_cache(self):
        from entry import _check_theme_images
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(sys, "frozen", True, create=True), \
                patch.object(theme, "_asset_cache_path", side_effect=lambda key: Path(folder) / (key + ".zip")), \
                patch.dict(theme._ASSET_CACHE, clear=True), \
                patch.object(theme, "_draw_assets", side_effect=AssertionError("unexpected theme generation")):
            for appearance in ("dark", "light"):
                theme.apply_style(self.root, mode=appearance)
                self.assertEqual(_check_theme_images(self.root), "bundled")
            self.assertEqual(list(Path(folder).iterdir()), [])

    def test_frozen_self_test_rejects_missing_bundles_or_incomplete_tk_images(self):
        from entry import _check_theme_images
        with patch.object(sys, "frozen", True, create=True):
            with patch.object(theme, "_read_bundled_assets", return_value=None):
                with self.assertRaisesRegex(AssertionError, "bundled theme"):
                    _check_theme_images(self.root)
            images = dict(self.root._luvbox_theme_images)
            images.pop("card")
            with patch.object(self.root, "_luvbox_theme_images", images):
                with self.assertRaisesRegex(AssertionError, "incomplete theme"):
                    _check_theme_images(self.root)

    def test_shipped_archives_match_current_renderer_and_are_reproducible(self):
        from tools import build_theme
        previous = theme._active_mode
        with tempfile.TemporaryDirectory() as folder:
            build_theme.build(folder)
            self.assertEqual(theme._active_mode, previous)
            for appearance in ("light", "dark"):
                name = f"{appearance}.zip"
                self.assertEqual((Path(folder) / name).read_bytes(),
                                 (build_theme.ROOT / "assets" / "theme" / name).read_bytes())
            build_theme.build(folder, check=True)

    def test_bundle_key_is_stable_when_frozen_and_invalidates_palette_specs_or_renderer_changes(self):
        original = theme._asset_key()
        with patch.object(sys, "frozen", True, create=True), \
                patch.object(theme, "__file__", "/unavailable/frozen/theme.pyc"):
            self.assertEqual(theme._asset_key(), original)
        with patch.dict(theme.LIGHT, {"BLUE": "#010203"}):
            self.assertNotEqual(theme._asset_key(), original)
        specs = list(theme._asset_specs())
        name, generator, options = specs[0]
        specs[0] = name, generator, {**options, "radius": options["radius"] + 1}
        with patch.object(theme, "_asset_specs", return_value=iter(specs)):
            self.assertNotEqual(theme._asset_key(), original)
        with patch.object(theme, "_RENDERER_REVISION", theme._RENDERER_REVISION + 1):
            self.assertNotEqual(theme._asset_key(), original)
            self.assertIsNone(theme._read_bundled_assets(theme._asset_key()))

    def test_missing_stale_incomplete_and_undecodable_bundles_fall_back_to_drawing(self):
        original = theme._read_bundled_assets(theme._asset_key())
        manifest = theme._asset_manifest(theme._asset_key())
        for case in ("missing", "stale", "incomplete", "broken-png", "broken-json"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as folder, \
                    patch.object(theme, "_asset_cache_path", return_value=None), \
                    patch.object(theme, "_bundled_asset_path", return_value=Path(folder) / "light.zip"), \
                    patch.dict(theme._ASSET_CACHE, clear=True):
                if case != "missing":
                    with zipfile.ZipFile(Path(folder) / "light.zip", "w") as archive:
                        archive.writestr("manifest.json", b"invalid json" if case == "broken-json" else
                                         json.dumps({**manifest, "key": "old"} if case == "stale" else manifest))
                        for name, data in original.items():
                            if case == "incomplete" and name == "button":
                                continue
                            archive.writestr(name, data[:16] if case == "broken-png" and name == "card" else data)
                window = tk.Tk()
                window.withdraw()
                try:
                    with patch.object(theme, "_draw_assets", wraps=theme._draw_assets) as draw:
                        theme.apply_style(window, mode="light")
                    self.assertEqual(draw.call_count, 1)
                    self.assertEqual(theme._ASSET_CACHE[theme._asset_key()], original)
                finally:
                    window.destroy()

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
        # Images are opaque (fast to draw on Windows); each surface has its
        # own copy whose rounded corners are blended onto that surface.
        images = self.root._luvbox_theme_images
        for name, surface in (("button", theme.BG), ("button_card", theme.CARD)):
            self.assertFalse(images[name].transparency_get(0, 0))
            self.assertEqual("#%02x%02x%02x" % tuple(images[name].get(0, 0)), surface)
        self.assertEqual(style.layout("SurfaceCard.TButton")[0][0], "Soft.Button.card")
        self.assertGreaterEqual(images["card"].width(), 640)

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

    def test_card_notebook_has_rounded_client_and_inset_native_pages_in_both_themes(self):
        self.root.geometry("600x320")
        self.root.configure(background=theme.CARD)
        notebook = ttk.Notebook(self.root, style="Card.TNotebook")
        notebook.pack(fill="both", expand=True, padx=20, pady=12)
        pages = []
        for caption in ("저장 위치", "미디어"):
            page = ttk.Frame(notebook, style="Plain.TFrame")
            notebook.add(page, text=caption)
            pages.append(page)
        style = ttk.Style(self.root)
        for appearance in ("dark", "light"):
            theme.apply(self.root, appearance)
            self.root.update()
            palette = theme.DARK if appearance == "dark" else theme.LIGHT
            self.assertEqual(style.lookup("Card.TNotebook", "background"), palette["CARD"])
            self.assertEqual(style.layout("Card.TNotebook")[0][0], "Soft.Notebook.client")
            self.assertEqual(style.layout("Card.TNotebook.Tab")[0][0], "Soft.Tab.card")
            for state in ((), ("selected",), ("active",), ("focus",), ("disabled",)):
                self.assertEqual(style.lookup("Card.TNotebook.Tab", "background", state), palette["CARD"])
            image = self.root._luvbox_theme_images["notebook_card"]
            color = lambda x, y: "#%02x%02x%02x" % tuple(image.get(x, y))
            self.assertEqual(color(0, 0), palette["CARD"])
            self.assertEqual(color(20, 0), palette["LINE"])
            # Ttk locates the '.client' node to reserve its image padding. If
            # that node is renamed incorrectly, the rectangular page starts
            # at x=0 and paints over every rounded corner of the new frame.
            for index, page in enumerate(pages):
                notebook.select(index)
                self.root.update()
                self.assertGreaterEqual(page.winfo_x(), 12)
                self.assertGreaterEqual(notebook.winfo_width() - page.winfo_x() - page.winfo_width(), 12)
                self.assertGreaterEqual(notebook.winfo_height() - page.winfo_y() - page.winfo_height(), 12)
            for name in ("tab_card", "tab_selected_card", "tab_active_card", "tab_focus_card"):
                tab = self.root._luvbox_theme_images[name]
                self.assertEqual("#%02x%02x%02x" % tuple(tab.get(0, 0)), palette["CARD"])

    def test_library_notebook_removes_extra_gap_below_the_tab_buttons(self):
        self.root.geometry("600x320")
        notebook = ttk.Notebook(self.root)
        notebook.pack(fill="both", expand=True)
        page = ttk.Frame(notebook)
        notebook.add(page, text="저장한 파일")
        for appearance in ("dark", "light"):
            theme.apply(self.root, appearance)
            self.root.update()
            tab_bottom = 0
            for y in range(min(100, notebook.winfo_height())):
                try:
                    notebook.index(f"@20,{y}")
                except tk.TclError:
                    continue
                tab_bottom = y + 1
            self.assertGreater(tab_bottom, 0)
            self.assertGreaterEqual(page.winfo_y() - tab_bottom, 0)
            self.assertLessEqual(page.winfo_y() - tab_bottom, 4)

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
        image = self.root._luvbox_theme_images["card"]
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
                self.assertIs(self.root._luvbox_theme_images["card"], image)
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
        self.assertIsNone(bar._next_frame)
        self.assertEqual(bar.itemcget(bar._fill, "state"), "hidden")
        bar.destroy()
        theme.apply(self.root, "light")

    def test_progress_start_honors_interval_without_restarting_the_same_animation(self):
        bar = theme.Progressbar(self.root, mode="indeterminate")
        clock = [10.]
        with patch("luvbox.theme.time.monotonic", side_effect=lambda: clock[0]), \
                patch.object(bar, "after", side_effect=("first", "second")) as after, \
                patch.object(bar, "after_cancel") as cancel:
            bar.start(12)
            self.assertEqual(after.call_args.args[0], 12)
            phase = bar._phase_start
            clock[0] += .005
            bar.start(12)
            self.assertEqual(after.call_count, 1)
            cancel.assert_not_called()
            self.assertEqual(bar._phase_start, phase)
            bar.start(40)
            cancel.assert_called_once_with("first")
            self.assertEqual(after.call_args.args[0], 40)
            self.assertEqual(bar._phase_start, phase)
            bar.stop()
            self.assertIsNone(bar._timer)
            self.assertIsNone(bar._next_frame)
        bar.destroy()

    def test_progress_frame_deadlines_exclude_render_time_and_skip_missed_frames(self):
        bar = theme.Progressbar(self.root, mode="indeterminate")
        clock = [0.]
        def render(*_):
            clock[0] += .004
        with patch("luvbox.theme.time.monotonic", side_effect=lambda: clock[0]), \
                patch.object(bar, "after", return_value="frame") as after, \
                patch.object(bar, "after_cancel"), patch.object(bar, "_draw", side_effect=render):
            bar.start(12)
            clock[0] = .013
            bar._tick()
            # The next deadline remains 24ms, rather than 12ms after rendering.
            self.assertGreaterEqual(after.call_args.args[0], 6)
            self.assertLessEqual(after.call_args.args[0], 8)
            clock[0] = .2
            bar._tick()
            self.assertGreater(bar._next_frame, clock[0])
            self.assertGreaterEqual(after.call_args.args[0], 1)
            self.assertLessEqual(after.call_args.args[0], 12)
            bar.stop()
        bar.destroy()

    def test_progress_target_updates_keep_the_frame_and_catch_up_after_a_pause(self):
        bar = theme.Progressbar(self.root, value=0)
        clock = [0.]
        with patch("luvbox.theme.time.monotonic", side_effect=lambda: clock[0]), \
                patch.object(bar, "after", return_value="frame") as after, \
                patch.object(bar, "after_cancel") as cancel, patch.object(bar, "_draw"):
            bar.configure(value=50)
            self.assertLessEqual(after.call_args.args[0], 16)
            clock[0] = .004
            bar.configure(value=75)
            clock[0] = .008
            bar.configure(value=100)
            self.assertEqual(bar["value"], 100)
            self.assertEqual(after.call_count, 1)
            cancel.assert_not_called()
            clock[0] = .016
            bar._tick()
            self.assertAlmostEqual(bar._display_value, 1 - math.exp(-.016 / .075))
            clock[0] = 1.
            bar._tick()
            self.assertEqual(bar._display_value, 1)
            self.assertIsNone(bar._timer)
            self.assertIsNone(bar._next_frame)
        bar.destroy()

    def test_progress_rejects_invalid_animation_intervals_without_starting(self):
        bar = theme.Progressbar(self.root, mode="indeterminate")
        for interval in (0, -1, float("nan"), float("inf"), "invalid"):
            with self.subTest(interval=interval), self.assertRaises(tk.TclError):
                bar.start(interval)
            self.assertFalse(bar._running)
            self.assertIsNone(bar._timer)
        bar.destroy()

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
        self.assertIsNone(bar._next_frame)
        # A subsequent worker-side value update must not call a dead canvas.
        value.set(80)
        self.root.update()

    def test_navigation_controls_keep_native_focus_and_match_both_surfaces(self):
        self.root.geometry("640x600")
        sidebar = ttk.Frame(self.root, style="Sidebar.TFrame", padding=12)
        sidebar.pack(fill="x")
        calls = []
        controls = []
        for parent in (self.root, sidebar):
            for name in ("Ghost.TButton", "Nav.TButton", "NavSelected.TButton",
                         "Segment.TButton", "SegmentSelected.TButton"):
                button = ttk.Button(parent, text="저장한 파일", style=name,
                                    command=lambda: calls.append("selected"))
                button.pack()
                controls.append(button)
        self.root.update()
        controls[0].focus_force()
        self.root.update()
        controls[0].event_generate("<KeyPress-space>")
        controls[0].event_generate("<KeyRelease-space>")
        self.root.update()
        self.assertEqual(calls, ["selected"])
        controls[0].state(["disabled"])
        controls[0].invoke()
        self.assertEqual(calls, ["selected"])
        style = ttk.Style(self.root)
        for appearance in ("dark", "light"):
            theme.apply(self.root, appearance)
            self.root.update()
            palette = theme.DARK if appearance == "dark" else theme.LIGHT
            for button in controls:
                surface = palette["CARD"] if button.master is sidebar else palette["BG"]
                for state in ((), ("active",), ("pressed",), ("focus",), ("disabled",)):
                    self.assertEqual(style.lookup(button.cget("style"), "background", state), surface)
            for name, surface in (("ghost", palette["BG"]), ("ghost_card", palette["CARD"]),
                                  ("selected", palette["BG"]), ("selected_card", palette["CARD"])):
                image = self.root._luvbox_theme_images[name]
                self.assertEqual("#%02x%02x%02x" % tuple(image.get(0, 0)), surface)

    def test_progress_caps_use_their_window_theme_when_another_interpreter_switches(self):
        other = tk.Tk()
        other.attributes("-alpha", 0)
        try:
            theme.apply_style(other, mode="dark")
            card = theme.card(self.root, fill="x")
            button = ttk.Button(card, text="저장한 파일", style="NavSelected.TButton")
            button.pack()
            bar = theme.Progressbar(card, length=300, value=50)
            bar.pack()
            self.root.update()
            expected = theme.LIGHT
            self.assertEqual(ttk.Style(self.root).lookup(button.cget("style"), "background"), expected["CARD"])
            self.assertEqual(bar.itemcget(bar._fill, "fill"), expected["BLUE"])
            color = "#%02x%02x%02x" % tuple(bar._cap_images["fill"].get(7, 7))
            self.assertEqual(color, expected["BLUE"])
            self.assertEqual(theme.mode(self.root), "light")
        finally:
            other.update_idletasks()
            other.destroy()
            theme.apply(self.root, "light")

    def test_dark_fields_select_text_with_a_readable_foreground(self):
        theme.apply(self.root, "dark")
        style = ttk.Style(self.root)
        for name in ("TEntry", "TSpinbox", "TCombobox"):
            self.assertEqual(style.lookup(name, "selectforeground"), theme.DARK["PRIMARY_INK"])
            self.assertEqual(style.lookup(name, "selectbackground"), theme.DARK["BLUE"])
        theme.apply(self.root, "light")

    def test_progress_rejects_nonfinite_initial_values_without_leaving_a_canvas(self):
        existing = self.root.winfo_children()
        for value in (float("nan"), float("inf"), "invalid"):
            with self.subTest(value=value), self.assertRaises(tk.TclError):
                theme.Progressbar(self.root, value=value)
        self.assertEqual(existing, self.root.winfo_children())

    def _nested_scroll_views(self):
        self.root.geometry("720x360")
        outside = ttk.Button(self.root, text="사이드바 버튼")
        outside.pack(side="bottom", fill="x")
        workspace = ttk.Frame(self.root)
        workspace.pack(fill="both", expand=True)
        workspace.rowconfigure(0, weight=1)
        views, buttons = [], []
        for column in range(2):
            workspace.columnconfigure(column, weight=1, uniform="pane")
            pane = ttk.Frame(workspace)
            pane.grid(row=0, column=column, sticky="nsew")
            view = theme.ScrollableContent(pane)
            view.pack(fill="both", expand=True)
            items = []
            for number in range(30):
                button = ttk.Button(view.content, text=f"설정 {number + 1}")
                button.pack(fill="x", pady=3)
                items.append(button)
            views.append(view)
            buttons.append(items)
        self.root.update()
        self.assertTrue(all(view.scrollbar.winfo_manager() for view in views))
        return views, buttons, outside

    def test_nested_scroll_view_routes_wheel_and_focus_to_its_own_content(self):
        (left, right), buttons, outside = self._nested_scroll_views()
        start_left, start_right = left.canvas.yview(), right.canvas.yview()
        buttons[0][0].event_generate("<MouseWheel>", delta=-120)
        self.root.update()
        self.assertGreater(left.canvas.yview()[0], start_left[0])
        self.assertEqual(right.canvas.yview(), start_right)
        current_left = left.canvas.yview()
        outside.event_generate("<MouseWheel>", delta=-120)
        self.root.update()
        self.assertEqual(left.canvas.yview(), current_left)
        self.assertEqual(right.canvas.yview(), start_right)

        buttons[1][0].event_generate("<MouseWheel>", delta=-120)
        self.root.update()
        self.assertGreater(right.canvas.yview()[0], start_right[0])
        self.assertEqual(left.canvas.yview(), current_left)

        left.canvas.yview_moveto(0)
        right_position = right.canvas.yview()
        buttons[0][-1].focus_force()
        self.root.update()
        self.assertGreater(left.canvas.yview()[0], 0)
        top = buttons[0][-1].winfo_rooty() - left.canvas.winfo_rooty()
        self.assertGreaterEqual(top, 0)
        self.assertLessEqual(top + buttons[0][-1].winfo_height(), left.canvas.winfo_height())
        self.assertEqual(right.canvas.yview(), right_position)
        left_position = left.canvas.yview()
        outside.focus_force()
        self.root.update()
        self.assertEqual(left.canvas.yview(), left_position)
        self.assertEqual(right.canvas.yview(), right_position)

    def test_nested_scroll_view_preserves_text_and_table_wheel_behavior(self):
        (view, other), buttons, outside = self._nested_scroll_views()
        text = tk.Text(view.content, height=3)
        text.insert("1.0", "\n".join(str(number) for number in range(60)))
        text.pack(before=buttons[0][0], fill="x")
        tree = ttk.Treeview(view.content, height=3)
        for number in range(40):
            tree.insert("", "end", text=str(number))
        tree.pack(before=buttons[0][0], fill="x")
        self.root.update()
        view.canvas.yview_moveto(0)
        positions = (view.canvas.yview(), other.canvas.yview())
        for widget in (text, tree):
            previous = widget.yview()
            widget.event_generate("<MouseWheel>", delta=-120)
            self.root.update()
            self.assertGreater(widget.yview()[0], previous[0])
            self.assertEqual((view.canvas.yview(), other.canvas.yview()), positions)
        view.scrollbar.event_generate("<MouseWheel>", delta=-120)
        self.root.update()
        # The native scrollbar is supposed to scroll the canvas it controls.
        # It must keep that behavior without also moving the other view.
        self.assertGreater(view.canvas.yview()[0], positions[0][0])
        self.assertEqual(other.canvas.yview(), positions[1])

    def test_focus_reveal_uses_the_current_canvas_offset_before_native_windows_move(self):
        (view, other), buttons, outside = self._nested_scroll_views()
        target = buttons[0][0]
        view.canvas.yview_moveto(0)
        self.root.update()
        # The logical offset changes immediately; native child positions are
        # still from the old viewport until the next event-loop pass.
        view.canvas.yview_moveto(1)
        view._ensure_visible(target)
        self.root.update()
        self.assertGreaterEqual(target.winfo_rooty(), view.canvas.winfo_rooty())
        self.assertLessEqual(target.winfo_rooty() + target.winfo_height(),
                             view.canvas.winfo_rooty() + view.canvas.winfo_height())

    def test_scroll_view_destroy_releases_only_its_own_toplevel_callbacks(self):
        (view, survivor), buttons, outside = self._nested_scroll_views()
        received = []
        external = self.root.bind("<MouseWheel>", lambda event: received.append(event.widget), add="+")
        self.addCleanup(self.root.unbind, "<MouseWheel>", external)
        own_bindings = list(view._view_bindings)
        surviving_bindings = list(survivor._view_bindings)
        buttons[0][-1].focus_force()
        buttons[0][-1].event_generate("<FocusIn>")
        view._schedule_layout()
        timers = set(view._focus_timers) | {view._layout_timer}
        self.assertTrue(view._focus_timers)
        self.assertEqual(view._event_owner, self.root)
        view.destroy()
        pending = set(self.root.tk.call("after", "info"))
        self.assertTrue(timers.isdisjoint(pending))
        self.assertFalse(view._focus_timers)
        self.assertIsNone(view._event_owner)
        for event, callback in own_bindings:
            self.assertNotIn(callback, self.root.bind(event))
        for event, callback in surviving_bindings:
            self.assertIn(callback, self.root.bind(event))
        self.assertIn(external, self.root.bind("<MouseWheel>"))
        outside.event_generate("<MouseWheel>", delta=-120)
        self.root.update()
        self.assertEqual(received, [outside])
        previous = survivor.canvas.yview()
        buttons[1][0].event_generate("<MouseWheel>", delta=-120)
        self.root.update()
        self.assertGreater(survivor.canvas.yview()[0], previous[0])

    def test_main_text_and_selected_actions_have_readable_contrast_in_each_theme(self):
        def luminance(color):
            channels = [int(color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
            linear = [value / 12.92 if value <= .04045 else ((value + .055) / 1.055) ** 2.4
                      for value in channels]
            return sum(value * weight for value, weight in zip(linear, (.2126, .7152, .0722)))

        for palette in (theme.LIGHT, theme.DARK):
            for text, surface in (("INK", "BG"), ("MUTED", "BG"), ("MUTED", "CARD"),
                                  ("INK", "BUTTON"), ("INK", "BUTTON_HOVER"),
                                  ("PRIMARY_INK", "BLUE"), ("BLUE_ACTIVE", "ACCENT_SOFT")):
                low, high = sorted((luminance(palette[text]), luminance(palette[surface])))
                with self.subTest(text=text, surface=surface, palette=palette["BG"]):
                    self.assertGreaterEqual((high + .05) / (low + .05), 4.5)

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

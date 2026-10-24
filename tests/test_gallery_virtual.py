"""Viewport behavior with libraries much larger than the native widget set."""

from pathlib import Path
import tempfile
import time
import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PIL import Image

from luvbox.gallery import TILE, ThumbnailGrid
from tests import test_gallery as gallery_fixtures
from tests.legacy_ui import previous_window


@previous_window
class GalleryVirtualizationTests(unittest.TestCase):
    release = gallery_fixtures.GalleryLayoutTests.release

    def setUp(self):
        gallery_fixtures.GalleryLayoutTests.setUp(self)
        self.callback_errors = []
        self.root.report_callback_exception = lambda *error: self.callback_errors.append(error)
        self.addCleanup(self.assertFalse, self.callback_errors)

    @staticmethod
    def rows(count):
        return [(str(index), {"author": "offline", "posted_at": "2026-10-01"})
                for index in range(count)]

    def show(self, count):
        self.grid.show(self.rows(count), set)
        self.root.update()

    def test_widget_count_stays_bounded_across_ten_thousand_records_scroll_and_resize(self):
        counts = {}
        for count in (1000, 10000):
            self.show(count)
            largest = 0
            for geometry in ("1050x400", "390x400", "1440x700", "1050x400"):
                self.root.geometry(geometry)
                self.root.update()
                for fraction in (0.0, 0.5, 1.0):
                    self.grid.canvas.yview_moveto(fraction)
                    self.root.update()
                    labels = [child for child in self.grid.inner.winfo_children()
                              if isinstance(child, tk.Label) and child is not self.grid.empty]
                    largest = max(largest, len(labels))
                    # A viewport can be large, but a large catalog must not
                    # become thousands of native children or a huge OS window.
                    self.assertLess(len(labels), 150)
                    self.assertLess(self.grid.inner.winfo_height(), 3000)
                    self.assertEqual(len(self.grid.order), count)
                    if fraction == 1.0:
                        tile = self.grid.tiles[str(count - 1)]
                        self.assertLessEqual(tile.winfo_rooty() + tile.winfo_height(),
                                             self.grid.canvas.winfo_rooty() + self.grid.canvas.winfo_height())
            counts[count] = largest
        self.assertLessEqual(counts[10000], counts[1000] + 10)

    def test_selecting_last_record_mounts_and_reveals_it_without_losing_other_selections(self):
        self.show(10000)
        self.assertNotIn("9999", self.grid.tiles)
        self.grid.select_many(("1", "5000", "9999"), focus="9999")
        self.root.update()
        self.assertEqual(self.grid.selected_keys, ("1", "5000", "9999"))
        tile = self.grid.tiles["9999"]
        self.assertTrue(tile.cget("text").startswith("✓ "))
        self.assertGreaterEqual(tile.winfo_rooty(), self.grid.canvas.winfo_rooty())
        self.assertLessEqual(tile.winfo_rooty() + tile.winfo_height(),
                             self.grid.canvas.winfo_rooty() + self.grid.canvas.winfo_height())

    def test_selection_before_hidden_notebook_page_is_mapped_is_revealed_on_first_map(self):
        self.grid.destroy()
        notebook = ttk.Notebook(self.root)
        notebook.pack(fill="both", expand=True)
        visible, hidden = ttk.Frame(notebook), ttk.Frame(notebook)
        notebook.add(visible, text="First")
        notebook.add(hidden, text="Gallery")
        self.grid = ThumbnailGrid(hidden, on_select=lambda _: None, on_open=lambda _: None)
        self.grid.pack(fill="both", expand=True)
        self.root.update()
        self.grid.show(self.rows(10000), set)
        self.grid.select("9999")
        self.root.update()
        self.assertEqual(self.grid.columns, 0)
        self.assertEqual(self.grid.selected_keys, ("9999",))
        notebook.select(hidden)
        self.root.update()
        tile = self.grid.tiles["9999"]
        self.assertGreaterEqual(tile.winfo_rooty(), self.grid.canvas.winfo_rooty())
        self.assertLessEqual(tile.winfo_rooty() + tile.winfo_height(),
                             self.grid.canvas.winfo_rooty() + self.grid.canvas.winfo_height())

    def test_column_change_keeps_the_current_first_visible_record_near_the_top(self):
        self.show(10000)
        self.grid.canvas.yview_moveto((200 * self.grid._cell_height + 17) / self.grid._content_height)
        self.root.update()
        old_anchor = int(self.grid.canvas.canvasy(0) // self.grid._cell_height) * self.grid.columns
        self.root.geometry("390x400")
        self.root.update()
        view_top = self.grid.canvas.winfo_rooty()
        first = min(int(key) for key, tile in self.grid.tiles.items()
                    if tile.winfo_rooty() + tile.winfo_height() > view_top)
        self.assertLessEqual(first, old_anchor)
        self.assertLess(old_anchor, first + self.grid.columns)

    def test_scaling_change_while_scrolled_keeps_the_same_first_visible_record(self):
        original_scaling = self.root.tk.call("tk", "scaling")
        self.addCleanup(lambda: self.root.tk.call("tk", "scaling", original_scaling))
        self.show(10000)
        self.grid.canvas.yview_moveto((200 * self.grid._cell_height + 17) / self.grid._content_height)
        self.root.update()
        old_columns, old_height = self.grid.columns, self.grid._cell_height
        old_anchor = int(self.grid.canvas.canvasy(0) // old_height) * old_columns
        self.root.tk.call("tk", "scaling", 2.666)
        self.root.geometry("1040x400")  # Trigger Configure without changing the column count.
        self.root.update()
        self.assertEqual(self.grid.columns, old_columns)
        self.assertGreater(self.grid._cell_height, old_height)
        view_top = self.grid.canvas.winfo_rooty()
        first = min(int(key) for key, tile in self.grid.tiles.items()
                    if tile.winfo_rooty() + tile.winfo_height() > view_top)
        self.assertEqual(first, old_anchor)

    def test_release_removes_tcl_font_and_cancels_pending_view_callbacks(self):
        self.show(1000)
        font_name = str(self.grid._font)
        default_anchor = self.grid._default_font_anchor
        self.assertEqual(self.grid.canvas.type(default_anchor), "text")
        self.assertIn(font_name, self.root.tk.call("font", "names"))
        self.grid.select("999")
        self.grid._schedule_layout()
        self.grid._schedule_region()
        self.grid._view_changed(*self.grid.canvas.yview())
        timer_names = ("_layout_timer", "_region_timer", "_selection_timer", "_viewport_timer")
        timers = {getattr(self.grid, name) for name in timer_names}
        self.assertNotIn(None, timers)
        self.grid.release()
        self.assertNotIn(font_name, self.root.tk.call("font", "names"))
        self.assertIsNone(self.grid._font)
        self.assertFalse(self.grid.canvas.type(default_anchor))
        self.assertIsNone(self.grid._default_font_anchor)
        self.assertTrue(timers.isdisjoint(self.root.tk.call("after", "info")))
        self.assertTrue(all(getattr(self.grid, name) is None for name in timer_names))
        self.root.update()
        self.assertFalse(self.grid.tiles)
        self.grid.release()

    def test_hidden_decoder_result_is_available_on_reentry_and_path_change_discards_it(self):
        rows = self.rows(1000)
        rows[-1][1]["path"] = "old-local-fixture.jpg"
        with patch.object(self.grid._decoder, "request"), \
                Image.new("RGB", (4, 4), "red") as red, \
                Image.new("RGB", (4, 4), "blue") as blue:
            self.grid.show(rows, set)
            self.root.update()
            self.assertNotIn("999", self.grid.tiles)
            old_token = self.grid._token
            self.grid._results.put((old_token, "999", red))
            self.grid.poll()
            self.assertIs(self.grid._source_images["999"], red)
            self.assertNotIn("999", self.grid.images)
            self.grid.select("999")
            self.root.update()
            self.assertEqual(tuple(self.grid.tk.call(str(self.grid.images["999"]), "get", 75, 56)),
                             (255, 0, 0))
            self.grid.canvas.yview_moveto(0)
            self.root.update()
            self.assertNotIn("999", self.grid.tiles)
            self.assertNotIn("999", self.grid.images)

            rows[-1][1]["path"] = "different-local-fixture.jpg"
            self.grid.show(rows, set)
            self.assertNotIn("999", self.grid._source_images)
            self.grid._results.put((old_token, "999", red))
            self.grid.poll()
            self.assertNotIn("999", self.grid._source_images)
            self.grid._results.put((self.grid._token, "999", blue))
            self.grid.poll()
            self.grid.select("999")
            self.root.update()
            self.assertEqual(tuple(self.grid.tk.call(str(self.grid.images["999"]), "get", 75, 56)),
                             (0, 0, 255))

            self.grid.canvas.yview_moveto(0)
            self.root.update()
            self.grid.show(rows[:-1], set)
            self.assertNotIn("999", self.grid._source_images)
            self.assertNotIn("999", self.grid.selected_keys)

    def test_permission_refresh_revokes_hidden_preview_before_it_is_mounted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "local.jpg"
            Image.new("RGB", (4, 4), "red").save(path)
            rows = self.rows(1000)
            rows[-1][1]["path"] = str(path)
            self.grid.show(rows, set)
            self.root.update()
            self.assertNotIn("999", self.grid.tiles)
            self.assertNotIn("999", self.grid._source_images)
            self.grid.select("999")
            deadline = time.monotonic() + 3
            while "999" not in self.grid._valid_sources and time.monotonic() < deadline:
                self.root.update()
                time.sleep(.005)
            self.assertIsNotNone(self.grid._source_images["999"])
            self.grid.canvas.yview_moveto(0)
            self.root.update()
            self.assertNotIn("999", self.grid.tiles)

            self.grid.show(rows, lambda paths: set())
            self.assertNotIn("999", self.grid._valid_sources)
            self.grid.select("999")
            deadline = time.monotonic() + 3
            while "999" not in self.grid._valid_sources and time.monotonic() < deadline:
                self.root.update()
                time.sleep(.005)
            self.assertIn("999", self.grid._valid_sources)
            self.assertIsNone(self.grid._source_images["999"])
            self.assertNotIn("999", self.grid.images)
            self.assertEqual(str(self.grid.tiles["999"].cget("image")),
                             str(self.grid.unavailable_placeholder))
            self.assertIn("미리보기 없음", self.grid.tiles["999"].cget("text"))

    def test_shift_range_keeps_anchor_after_its_widget_has_been_recycled(self):
        self.show(1000)
        self.grid._click(SimpleNamespace(state=0), "1")
        self.root.update()
        self.grid.canvas.yview_moveto(0.8)
        self.root.update()
        self.assertNotIn("1", self.grid.tiles)
        top = self.grid.canvas.winfo_rooty()
        target = next(key for key, tile in self.grid.tiles.items()
                      if top <= tile.winfo_rooty() < top + self.grid.canvas.winfo_height())
        self.grid._click(SimpleNamespace(state=0x0001), target)
        self.assertEqual(self.grid.selected_keys,
                         tuple(str(index) for index in range(1, int(target) + 1)))
        self.assertEqual(self.grid._anchor, "1")

    def test_drag_origin_survives_real_geometry_updates_while_scrolling_out_of_view(self):
        self.root.geometry("1050x220")
        self.show(1000)
        self.root.focus_force()
        origin = self.grid.tiles["0"]
        x, y = origin.winfo_width() // 2, origin.winfo_height() // 2
        origin.event_generate("<ButtonPress-1>", x=x, y=y)
        self.root.update()
        bottom = self.grid.canvas.winfo_rooty() + self.grid.canvas.winfo_height() + 30
        below = SimpleNamespace(x_root=origin.winfo_rootx() + x, y_root=bottom, state=0x0100)
        self.grid._drag_motion(below)
        for _ in range(35):
            self.grid.after_cancel(self.grid._drag_scroll)
            self.grid._drag_scroll = None
            self.grid._scroll_drag()
            self.root.update()
            self.assertIs(self.grid.tiles["0"], origin)
        self.assertGreater(self.grid.canvas.canvasy(0), 400)
        self.assertIn("0", self.grid.selected_keys)
        self.assertGreater(max(map(int, self.grid.selected_keys)), 12)
        origin.event_generate("<ButtonRelease-1>", x=x, y=bottom - origin.winfo_rooty(), state=0x0100)
        self.root.update()
        self.assertIsNone(self.grid._drag)
        self.assertIsNone(self.grid._drag_scroll)
        self.assertNotIn("0", self.grid.tiles)

    def test_upward_drag_keeps_late_origin_alive_with_bounded_native_coordinates(self):
        self.show(10000)
        self.grid.select("9999")
        self.root.update()
        origin = self.grid.tiles["9999"]
        origin.event_generate("<ButtonPress-1>", x=10, y=10)
        self.root.update()
        # Fast-forward a long upward autoscroll while keeping the same drag
        # capture; its original tile is now far below the current viewport.
        self.grid.canvas.yview_moveto(0)
        self.root.update()
        self.assertIs(self.grid.tiles["9999"], origin)
        self.assertLessEqual(origin.winfo_y(), self.grid.inner.winfo_height() + origin.winfo_height())
        origin.event_generate("<ButtonRelease-1>", x=10, y=10)
        self.root.update()
        self.assertIsNone(self.grid._drag)
        self.assertNotIn("9999", self.grid.tiles)

    def test_recycled_widget_dispatches_current_key_and_retains_keyboard_focus(self):
        opened, contexts = [], []
        self.grid.on_open = opened.append
        self.grid.on_context = lambda event, key: contexts.append(key)
        self.show(1000)
        self.root.focus_force()
        originals = set(self.grid.tiles.values())
        self.grid.tiles["0"].focus_set()
        self.root.update()
        self.grid.canvas.yview_moveto(1)
        self.root.update()
        self.assertEqual(self.root.focus_get(), self.grid.canvas)
        top = self.grid.canvas.winfo_rooty()
        bottom = top + self.grid.canvas.winfo_height()
        key, tile = next((key, tile) for key, tile in self.grid.tiles.items()
                         if tile in originals and top <= tile.winfo_rooty()
                         and tile.winfo_rooty() + tile.winfo_height() <= bottom)
        self.assertNotEqual(key, "0")
        self.assertTrue(self.root.tk.getboolean(tile.cget("takefocus")))
        # Dispatch through Tcl bindings, which must resolve the recycled key
        # rather than a closure carrying the widget's former record.
        tile.event_generate("<ButtonPress-1>", x=10, y=10, time=1000)
        tile.event_generate("<ButtonRelease-1>", x=10, y=10, time=1010)
        self.root.update()
        self.assertEqual(self.grid.selected_keys, (key,))
        self.assertEqual(self.root.focus_get(), tile)
        self.assertTrue(self.root.tk.getboolean(tile.cget("takefocus")))
        tile.event_generate("<ButtonPress-1>", x=10, y=10, time=1100)
        tile.event_generate("<ButtonRelease-1>", x=10, y=10, time=1110)
        tile.event_generate("<Button-3>", x=10, y=10, time=1200)
        self.root.update()
        self.assertEqual(opened, [key])
        self.assertEqual(contexts, [key])

    def test_large_text_scaling_keeps_captions_inside_nonoverlapping_rows(self):
        old_scaling = self.root.tk.call("tk", "scaling")
        self.addCleanup(lambda: self.root.tk.call("tk", "scaling", old_scaling))
        self.root.tk.call("tk", "scaling", 2.666)
        self.grid.destroy()
        self.grid = ThumbnailGrid(self.root, on_select=lambda _: None, on_open=lambda _: None)
        self.grid.pack(fill="both", expand=True)
        rows = [(str(index), {"author": "abcdefghijklmno", "posted_at": "2026-10-01"})
                for index in range(1000)]
        self.grid.show(rows, set)
        self.grid.select_many(("0", "6"), focus="0", scroll=False)
        self.root.update()
        for tile in self.grid.tiles.values():
            display_font = tkfont.Font(root=self.root, font=tile.cget("font"))
            self.assertEqual(len(tile.cget("text").splitlines()), 2)
            self.assertLessEqual(tile.winfo_reqheight(), tile.winfo_height())
            for line in tile.cget("text").splitlines():
                self.assertLessEqual(display_font.measure(line), TILE[0])
        first, next_row = self.grid.tiles["0"], self.grid.tiles[str(self.grid.columns)]
        self.assertLess(first.winfo_rooty() + first.winfo_height(), next_row.winfo_rooty())


if __name__ == "__main__":
    unittest.main()

"""Local thumbnail paging, resize and shutdown without catalog or X access."""

import gc
from pathlib import Path
import queue
import tempfile
import threading
import tkinter as tk
from types import SimpleNamespace
import unittest
import weakref
from unittest.mock import patch

from PIL import Image

from xlikesaver import theme
from xlikesaver.gallery import TILE, ThumbnailGrid, _GridDecoder
from xlikesaver.preview import Thumbnail


class GalleryDecoderTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.results = queue.Queue(maxsize=240)
        self.directory = Path(self.temporary.name)

    def path(self, name):
        path = self.directory / name
        path.write_bytes(b"local fixture")
        return str(path)

    def stop(self, decoder):
        decoder.close()
        if decoder.thread is not None:
            decoder.thread.join(3)
            self.assertFalse(decoder.thread.is_alive())

    def test_new_pages_replace_pending_work_and_keep_only_one_decoder(self):
        started, resume = threading.Event(), threading.Event()
        paths = [self.path(f"{index}.jpg") for index in range(4)]
        decoded = []

        def decode(path, size):
            decoded.append(str(path))
            if len(decoded) == 1:
                started.set()
                resume.wait(3)
            return Thumbnail(Image.new("RGB", (2, 2)), "ready")

        decoder = _GridDecoder(self.results, decoder=decode)
        self.addCleanup(self.stop, decoder)
        self.addCleanup(resume.set)
        decoder.request(1, [("old", paths[0]), ("old-tail", paths[1])], set)
        self.assertTrue(started.wait(2))
        worker = decoder.thread
        decoder.request(2, [("superseded", paths[2])], set)
        decoder.request(3, [("latest", paths[3])], set)
        self.assertIs(decoder.thread, worker)
        resume.set()
        token, key, image = self.results.get(timeout=3)
        self.assertEqual((token, key), (3, "latest"))
        self.assertIsNotNone(image)
        self.assertEqual(decoded, [paths[0], paths[3]])

    def test_close_discards_an_inflight_result_and_cache(self):
        started, resume = threading.Event(), threading.Event()
        path = self.path("one.jpg")

        def decode(path, size):
            started.set()
            resume.wait(3)
            return Thumbnail(Image.new("RGB", (2, 2)), "ready")

        decoder = _GridDecoder(self.results, decoder=decode)
        self.addCleanup(self.stop, decoder)
        self.addCleanup(resume.set)
        decoder.request(1, [("one", path)], set)
        self.assertTrue(started.wait(2))
        decoder.close()
        resume.set()
        decoder.thread.join(3)
        self.assertFalse(decoder.thread.is_alive())
        self.assertTrue(self.results.empty())
        self.assertEqual(decoder.cache, {})

    def test_cached_file_still_requires_current_page_permission(self):
        path = self.path("one.jpg")
        calls = []

        def decode(path, size):
            calls.append(path)
            return Thumbnail(Image.new("RGB", (2, 2)), "ready")

        decoder = _GridDecoder(self.results, decoder=decode)
        self.addCleanup(self.stop, decoder)
        decoder.request(1, [("one", path)], set)
        self.assertIsNotNone(self.results.get(timeout=3)[2])
        decoder.request(2, [("one", path)], lambda paths: set())
        self.assertIsNone(self.results.get(timeout=3)[2])
        self.assertEqual(len(calls), 1)
        decoder.request(3, [("one", path)], set)
        self.assertIsNotNone(self.results.get(timeout=3)[2])
        self.assertEqual(len(calls), 1)

    def test_one_bad_decode_does_not_stop_the_rest_of_the_page(self):
        paths = [self.path(f"{index}.jpg") for index in range(2)]

        def decode(path, size):
            if str(path) == paths[0]:
                raise OSError("unreadable local fixture")
            return Thumbnail(Image.new("RGB", (2, 2)), "ready")

        decoder = _GridDecoder(self.results, decoder=decode)
        self.addCleanup(self.stop, decoder)
        decoder.request(1, list(enumerate(paths)), set)
        self.assertIsNone(self.results.get(timeout=3)[2])
        self.assertIsNotNone(self.results.get(timeout=3)[2])

    def test_failed_unchanged_file_retries_then_caches_success(self):
        path = self.path("retry.jpg")
        stamp = (Path(path).stat().st_size, Path(path).stat().st_mtime_ns)
        for failure in (Thumbnail(None, "temporary failure"), OSError("temporarily busy")):
            with self.subTest(failure=type(failure).__name__):
                image = Image.new("RGB", (2, 2))
                results = queue.Queue(maxsize=240)
                with patch("xlikesaver.gallery.thumbnail", side_effect=[failure, Thumbnail(image, "ready")]) as decode:
                    decoder = _GridDecoder(results, decoder=decode)
                    try:
                        decoder.request(1, [("one", path)], set)
                        self.assertIsNone(results.get(timeout=3)[2])
                        self.assertFalse(decoder.cache)
                        decoder.request(2, [("one", path)], set)
                        self.assertIs(results.get(timeout=3)[2], image)
                        decoder.request(3, [("one", path)], set)
                        self.assertIs(results.get(timeout=3)[2], image)
                        self.assertEqual(decode.call_count, 2)
                        self.assertEqual(len(decoder.cache), 1)
                        self.assertEqual((Path(path).stat().st_size, Path(path).stat().st_mtime_ns), stamp)
                    finally:
                        self.stop(decoder)


class GalleryLayoutTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.attributes("-alpha", 0)
        self.root.geometry("1050x400")
        theme.apply(self.root)
        self.grid = ThumbnailGrid(self.root, on_select=lambda key: None, on_open=lambda key: None)
        self.grid.pack(fill="both", expand=True)
        self.addCleanup(self.release)

    def release(self):
        if self.root is not None:
            self.root.destroy()
        self.root = self.grid = None
        gc.collect()

    def test_shrinking_and_expanding_does_not_keep_invisible_columns(self):
        rows = [(str(index), {"author": "offline", "posted_at": "2026-10-01"}) for index in range(20)]
        self.grid.show(rows, set)
        self.root.update()
        wide_columns = self.grid.columns
        self.assertGreater(wide_columns, 2)
        self.root.geometry("390x400")
        self.root.update()
        self.assertEqual(self.grid.columns, 2)
        for index in range(2, wide_columns):
            self.assertEqual(self.grid.inner.grid_columnconfigure(index)["weight"], 0)
            self.assertFalse(self.grid.inner.grid_columnconfigure(index)["uniform"])
        self.assertGreaterEqual(self.grid.tiles["0"].winfo_width(), TILE[0])
        self.assertGreaterEqual(self.grid.tiles["1"].winfo_width(), TILE[0])
        self.root.geometry("1050x400")
        self.root.update()
        self.assertEqual(self.grid.columns, wide_columns)
        for index in range(wide_columns):
            self.assertEqual(self.grid.inner.grid_columnconfigure(index)["weight"], 1)

    def test_destroyed_grid_dies_while_decode_remains_inflight(self):
        started, resume = threading.Event(), threading.Event()

        def permitted(paths):
            started.set()
            resume.wait(3)
            return set()

        self.grid.show([("one", {"path": "local-fixture.jpg"})], permitted)
        self.assertTrue(started.wait(2))
        decoder = self.grid._decoder
        self.addCleanup(resume.set)
        reference = weakref.ref(self.grid)
        self.grid.destroy()
        self.grid = None
        # No cyclic collection, and no need to wait for a slow MP4 preview.
        self.assertIsNone(reference())
        resume.set()
        decoder.thread.join(3)
        self.assertFalse(decoder.thread.is_alive())

    def test_unchanged_refresh_keeps_tiles_images_and_updates_changed_captions(self):
        row = {"path": "offline-fixture.jpg", "author": "offline", "posted_at": "2026-10-01"}
        with patch.object(self.grid._decoder, "request"):
            self.grid.show([("one", row)], set)
            source = Image.new("RGB", (2, 2), "red")
            self.grid._results.put((self.grid._token, "one", source))
            self.grid.poll()
            tile, image = self.grid.tiles["one"], self.grid.images["one"]
            self.grid.show([("one", dict(row))], set)
            self.grid._results.put((self.grid._token, "one", source))
            self.grid.poll()
            self.assertIs(self.grid.tiles["one"], tile)
            self.assertIs(self.grid.images["one"], image)
            self.grid.show([("one", dict(row, author="updated", posted_at="2026-10-02"))], set)
            self.assertEqual(tile.cget("text"), "@updated\n2026-10-02")
            self.assertIs(self.grid.images["one"], image)
            self.grid.show([("one", dict(row, path="different-fixture.jpg"))], set)
            self.assertIsNot(self.grid.tiles["one"], tile)
            self.assertNotIn("one", self.grid.images)

    def test_refreshed_permission_revokes_an_already_displayed_thumbnail(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "one.jpg"
            Image.new("RGB", (4, 4), "red").save(path)
            row = {"path": str(path), "author": "offline", "posted_at": "2026-10-01"}
            self.grid.show([("one", row)], set)
            self.grid._results.put(self.grid._results.get(timeout=3))
            self.grid.poll()
            self.assertIn("one", self.grid.images)
            tile = self.grid.tiles["one"]
            self.grid.show([("one", row)], lambda paths: set())
            self.grid._results.put(self.grid._results.get(timeout=3))
            self.grid.poll()
            self.assertIs(self.grid.tiles["one"], tile)
            self.assertNotIn("one", self.grid.images)
            self.assertIsNone(self.grid._source_images["one"])
            self.assertIn("미리보기 없음", tile.cget("text"))

    def selection_rows(self):
        self.grid.show([(str(index), {"author": "offline"}) for index in range(6)], set)
        self.root.update()

    def click(self, key, state=0):
        return self.grid._click(SimpleNamespace(state=state), key)

    def test_control_click_toggles_multiple_records_and_preview_focus(self):
        self.selection_rows()
        changes = []
        self.grid.on_selection = lambda keys, focus: changes.append((keys, focus))
        self.click("1")
        self.click("3", 0x0004)
        self.assertEqual(self.grid.selected_keys, ("1", "3"))
        self.assertEqual(self.grid.selected, "3")
        self.assertEqual(self.grid.tiles["1"].cget("background"), self.grid.tiles["3"].cget("background"))
        self.assertNotEqual(self.grid.tiles["2"].cget("background"), self.grid.tiles["3"].cget("background"))
        self.click("3", 0x0004)
        self.assertEqual((self.grid.selected_keys, self.grid.selected), (("1",), "1"))
        self.assertEqual(self.grid.tiles["3"].cget("background"), theme.CARD)
        self.click("1", 0x0004)
        self.assertEqual((self.grid.selected_keys, self.grid.selected), ((), None))
        self.assertEqual(changes[-1], ((), None))

    def test_shift_selects_a_range_from_stable_anchor_and_control_adds_range(self):
        self.selection_rows()
        self.click("1")
        self.click("4", 0x0001)
        self.assertEqual(self.grid.selected_keys, ("1", "2", "3", "4"))
        self.click("2", 0x0001)
        self.assertEqual(self.grid.selected_keys, ("1", "2"))
        self.click("5", 0x0004)
        self.click("3", 0x0005)
        self.assertEqual(self.grid.selected_keys, ("1", "2", "3", "4", "5"))
        self.assertEqual(self.grid.selected, "3")
        self.click("0")
        self.assertEqual((self.grid.selected_keys, self.grid.selected), (("0",), "0"))

    def test_external_selection_sync_does_not_reenter_callbacks(self):
        self.selection_rows()
        self.grid.on_selection = lambda *_: self.fail("external selection invoked callback")
        self.grid.on_select = lambda *_: self.fail("external selection invoked legacy callback")
        self.grid.select_many(("4", "1", "1", "absent"), focus="4")
        self.assertEqual((self.grid.selected_keys, self.grid.selected), (("1", "4"), "4"))
        self.grid.select("2")
        self.assertEqual((self.grid.selected_keys, self.grid.selected), (("2",), "2"))
        self.grid.select(None)
        self.assertEqual((self.grid.selected_keys, self.grid.selected), ((), None))

    def test_external_selection_sets_range_anchor_but_callback_sync_preserves_it(self):
        self.selection_rows()
        self.click("0")
        self.grid.select_many(("3", "5"), focus="3")
        self.grid.on_selection = lambda keys, focus: self.grid.select_many(keys, focus)
        self.click("4", 0x0001)
        self.assertEqual(self.grid.selected_keys, ("3", "4"))
        self.click("1", 0x0001)
        self.assertEqual(self.grid.selected_keys, ("1", "2", "3"))

    def test_refresh_preserves_surviving_keys_but_new_page_does_not_inherit_selection(self):
        self.selection_rows()
        self.grid.select_many(("1", "3"), focus="3")
        self.grid.show([("1", {"author": "offline"}), ("4", {"author": "offline"})], set)
        self.assertEqual((self.grid.selected_keys, self.grid.selected), (("1",), "1"))
        self.assertNotEqual(self.grid.tiles["1"].cget("background"), theme.CARD)
        self.grid.show([("new-1", {"author": "offline"}), ("new-4", {"author": "offline"})], set)
        self.assertEqual((self.grid.selected_keys, self.grid.selected), ((), None))
        for tile in self.grid.tiles.values():
            self.assertEqual(tile.cget("background"), theme.CARD)

    def test_click_passes_legacy_key_or_new_selection_callback_and_focuses_tile(self):
        self.selection_rows()
        legacy, changes = [], []
        self.grid.on_select = legacy.append
        self.click("2")
        self.assertEqual(legacy, ["2"])
        self.grid.on_selection = lambda keys, focus: changes.append((keys, focus))
        with patch.object(self.grid.tiles["4"], "focus_set") as focus:
            self.assertEqual(self.click("4", 0x0004), "break")
            focus.assert_called_once_with()
        self.assertEqual(legacy, ["2"])
        self.assertEqual(changes, [(("2", "4"), "4")])


    def pointer(self, key, dx=0, dy=0, state=0):
        tile = self.grid.tiles[key]
        return SimpleNamespace(x_root=tile.winfo_rootx() + tile.winfo_width() // 2 + dx,
                               y_root=tile.winfo_rooty() + tile.winfo_height() // 2 + dy, state=state)

    def drag(self, start, end, state=0, start_key=None):
        self.grid._press(start, start_key)
        self.grid._drag_motion(end)
        boxed = self.grid._band.area if self.grid._band.visible else None
        self.grid._release_drag(end)
        return boxed

    def test_drag_box_selects_every_touched_tile_across_rows(self):
        self.grid.show([(str(index), {"author": "offline"}) for index in range(12)], set)
        self.root.update()
        self.assertEqual(self.grid.columns, 6)
        changes = []
        self.grid.on_selection = lambda keys, focus: changes.append(keys)
        start, end = self.pointer("1"), self.pointer("8")
        boxed = self.drag(start, end, start_key="1")
        # The box is drawn while dragging, exactly between the two points.
        self.assertEqual(boxed, (start.x_root, start.y_root, end.x_root - start.x_root, end.y_root - start.y_root))
        self.assertEqual(self.grid.selected_keys, ("1", "2", "7", "8"))
        self.assertEqual(changes[-1], ("1", "2", "7", "8"))
        self.assertFalse(self.grid._band.visible)
        self.assertFalse(self.grid._band.window.winfo_viewable())
        self.assertTrue(self.grid.tiles["7"].cget("text").startswith("✓ "))
        self.assertFalse(self.grid.tiles["3"].cget("text").startswith("✓ "))

    def test_small_moves_click_and_empty_space_clears_but_control_drag_adds(self):
        self.selection_rows()
        self.drag(self.pointer("0"), self.pointer("0", dx=3), start_key="0")
        self.assertEqual(self.grid.selected_keys, ("0",))
        empty = SimpleNamespace(x_root=self.grid.canvas.winfo_rootx() + 5,
                                y_root=self.grid.canvas.winfo_rooty() + self.grid.canvas.winfo_height() - 5, state=0)
        self.grid._press(empty, None)
        self.grid._release_drag(empty)
        self.assertEqual(self.grid.selected_keys, ())
        self.click("5")
        self.drag(self.pointer("1", state=0x0004), self.pointer("2", state=0x0004), start_key="1")
        self.assertEqual(self.grid.selected_keys, ("1", "2", "5"))

    def test_select_several_mode_adds_and_removes_with_plain_clicks(self):
        self.selection_rows()
        self.grid.toggle_mode = True
        self.click("1")
        self.click("3")
        self.assertEqual(self.grid.selected_keys, ("1", "3"))
        self.click("1")
        self.assertEqual(self.grid.selected_keys, ("3",))
        self.assertTrue(self.grid.tiles["3"].cget("text").startswith("✓ @offline"))
        self.drag(self.pointer("4"), self.pointer("5"), start_key="4")
        self.assertEqual(self.grid.selected_keys, ("3", "4", "5"))

    def test_dragging_past_the_edge_scrolls_and_keeps_extending(self):
        self.root.geometry("1050x220")
        self.grid.show([(str(index), {"author": "offline"}) for index in range(60)], set)
        self.root.update()
        start = self.pointer("0")
        self.grid._press(start, "0")
        below = SimpleNamespace(x_root=start.x_root, y_root=self.grid.canvas.winfo_rooty() + self.grid.canvas.winfo_height() + 30,
                                state=0)
        self.grid._drag_motion(below)
        self.assertIsNotNone(self.grid._drag_scroll)
        # The box stops at the gallery's visible edge; that side has no line.
        left, top, width, height = self.grid._band.area
        self.assertEqual(top + height, self.grid.canvas.winfo_rooty() + self.grid.canvas.winfo_height())
        lines = [self.grid._band.canvas.coords(item) for item in self.grid._band.canvas.find_all()]
        self.assertEqual(len(lines), 3)
        self.assertNotIn([0.0, float(height - 2), float(width), float(height)], lines)
        first, chosen = self.grid.canvas.yview()[0], len(self.grid.selected_keys)
        for _ in range(10):
            self.grid._scroll_drag()
        self.assertGreater(self.grid.canvas.yview()[0], first)
        self.assertGreater(len(self.grid.selected_keys), chosen)  # rows scrolled into the box join it
        self.grid._release_drag(below)
        self.assertIsNone(self.grid._drag_scroll)

    def test_fast_drag_box_is_one_window_that_never_takes_focus_or_leaves_lines(self):
        self.grid.show([(str(index), {"author": "offline"}) for index in range(12)], set)
        self.root.update()
        children = set(self.grid.inner.winfo_children())
        focus = self.root.focus_get()
        self.grid._press(self.pointer("0"), "0")
        for key in ("1", "7", "2", "8", "3", "9", "0"):
            self.grid._drag_motion(self.pointer(key))
        self.root.update()
        # Nothing is placed among the tiles, so moving the box repaints none.
        self.assertEqual(set(self.grid.inner.winfo_children()), children)
        self.assertEqual(self.grid._band.window.wm_overrideredirect(), True)
        self.assertEqual(self.root.focus_get(), focus)
        self.grid._release_drag(self.pointer("0"))
        self.grid.release()
        self.assertIsNone(self.grid._band.window)

if __name__ == "__main__":
    unittest.main()

"""Local thumbnail paging, resize and shutdown without catalog or X access."""

import gc
from contextlib import ExitStack
import os
from pathlib import Path
import queue
import tempfile
import threading
import tkinter as tk
from types import SimpleNamespace
import unittest

from tests.legacy_ui import previous_window
import weakref
from unittest.mock import patch

from PIL import Image

from luvbox import theme
from luvbox.gallery import TILE, ThumbnailGrid, _GridDecoder
from luvbox.preview import Thumbnail


class _FullQueue(queue.Queue):
    """Signal when the worker encounters a full queue, without sleeps."""

    def __init__(self, maxsize):
        super().__init__(maxsize=maxsize)
        self.blocked = threading.Event()

    def put_nowait(self, item):
        try:
            return super().put_nowait(item)
        except queue.Full:
            self.blocked.set()
            raise


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

    def test_request_superseded_during_stat_skips_the_old_decode(self):
        paths = [self.path(f"{index}.jpg") for index in range(2)]
        started, resume = threading.Event(), threading.Event()
        original_stat = os.stat
        decoded = []

        def stat(path, *args, **kwargs):
            if str(path) == paths[0]:
                started.set()
                resume.wait(3)
            return original_stat(path, *args, **kwargs)

        def decode(path, size):
            decoded.append(str(path))
            return Thumbnail(Image.new("RGB", (2, 2)), "ready")

        decoder = _GridDecoder(self.results, decoder=decode)
        self.addCleanup(self.stop, decoder)
        self.addCleanup(resume.set)
        with patch("luvbox.gallery.os.stat", side_effect=stat):
            decoder.request(1, [("old", paths[0])], set)
            self.assertTrue(started.wait(2))
            decoder.request(2, [("latest", paths[1])], set)
            resume.set()
            token, key, image = self.results.get(timeout=3)
        self.assertEqual((token, key), (2, "latest"))
        self.assertIsNotNone(image)
        self.assertEqual(decoded, [paths[1]])

    def test_request_superseded_during_decode_skips_optional_cache_write(self):
        paths = [self.path(f"{index}.jpg") for index in range(2)]
        started, resume = threading.Event(), threading.Event()
        remembered = []

        def decode(path, size):
            if str(path) == paths[0]:
                started.set()
                resume.wait(3)
            return Thumbnail(Image.new("RGB", (2, 2)), "ready")

        decoder = _GridDecoder(self.results, decoder=decode)
        self.addCleanup(self.stop, decoder)
        self.addCleanup(resume.set)
        decoder.request(1, [("old", paths[0], ("123", 1, "photo"))], set,
                        remember=lambda key, image: remembered.append(key))
        self.assertTrue(started.wait(2))
        decoder.request(2, [("latest", paths[1])], set)
        resume.set()
        token, key, image = self.results.get(timeout=3)
        self.assertEqual((token, key), (2, "latest"))
        self.assertIsNotNone(image)
        self.assertEqual(remembered, [])

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

    def test_changed_file_is_not_reused_from_the_retained_cache(self):
        path = self.path("changed.jpg")
        pictures = [Image.new("RGB", (2, 2), color) for color in ("red", "blue")]
        with patch("luvbox.gallery.thumbnail", side_effect=[Thumbnail(image, "ready") for image in pictures]) as decode:
            decoder = _GridDecoder(self.results, decoder=decode)
            self.addCleanup(self.stop, decoder)
            decoder.request(1, [("one", path)], set)
            self.assertIs(self.results.get(timeout=3)[2], pictures[0])
            Path(path).write_bytes(b"changed local fixture")
            decoder.request(2, [("one", path)], set)
            self.assertIs(self.results.get(timeout=3)[2], pictures[1])
            self.assertEqual(decode.call_count, 2)

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
                with patch("luvbox.gallery.thumbnail", side_effect=[failure, Thumbnail(image, "ready")]) as decode:
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

    def test_gallery_larger_than_result_queue_delivers_every_thumbnail(self):
        paths = [self.path(f"{index}.jpg") for index in range(300)]
        results = _FullQueue(240)
        image = Image.new("RGB", (2, 2))
        decoder = _GridDecoder(results, decoder=lambda path, size: Thumbnail(image, "ready"))
        self.addCleanup(self.stop, decoder)
        decoder.request(1, list(enumerate(paths)), set)
        # Deliberately leave the UI idle until the queue is full.
        self.assertTrue(results.blocked.wait(3))
        self.assertEqual(results.qsize(), 240)
        delivered = [results.get(timeout=3) for _ in paths]
        self.assertEqual([(token, key) for token, key, _ in delivered], [(1, key) for key in range(300)])
        self.assertTrue(all(picture is image for _, _, picture in delivered))
        self.assertEqual(len(decoder.cache), 240)

    def test_new_request_and_close_interrupt_a_full_result_queue(self):
        paths = [self.path(f"{index}.jpg") for index in range(4)]
        for action in ("request", "close"):
            with self.subTest(action=action):
                results = _FullQueue(1)
                decoded = []

                def decode(path, size):
                    decoded.append(str(path))
                    return Thumbnail(Image.new("RGB", (2, 2)), "ready")

                decoder = _GridDecoder(results, decoder=decode)
                try:
                    decoder.request(1, list(enumerate(paths[:3])), set)
                    self.assertTrue(results.blocked.wait(3))
                    if action == "request":
                        decoder.request(2, [("latest", paths[3])], set)
                        token, key, image = results.get(timeout=3)
                        self.assertEqual((token, key), (2, "latest"))
                        self.assertIsNotNone(image)
                        self.assertEqual(decoded, [*paths[:2], paths[3]])
                    else:
                        decoder.close()
                        decoder.thread.join(3)
                        self.assertFalse(decoder.thread.is_alive())
                        self.assertEqual(decoded, paths[:2])
                        self.assertFalse(decoder.cache)
                    self.assertTrue(results.empty())
                finally:
                    self.stop(decoder)

    def test_appending_large_gallery_reuses_bounded_cache_without_cyclic_eviction(self):
        paths = [self.path(f"{index}.jpg") for index in range(400)]
        image = Image.new("RGB", (2, 2))
        with patch("luvbox.gallery.thumbnail", return_value=Thumbnail(image, "ready")) as decode:
            decoder = _GridDecoder(self.results, decoder=decode)
            self.addCleanup(self.stop, decoder)
            decoder.request(1, list(enumerate(paths[:300])), set)
            for key in range(300):
                self.assertEqual(self.results.get(timeout=3)[:2], (1, key))
            self.assertEqual(decode.call_count, 300)
            self.assertEqual(len(decoder.cache), 240)
            decode.reset_mock()
            decoder.request(2, list(enumerate(paths)), set)
            for key in range(400):
                self.assertEqual(self.results.get(timeout=3)[:2], (2, key))
            # 60 previous tiles fell outside the cache; 100 are new. The 240
            # retained tiles remain reusable even if the first 60 evict them.
            self.assertEqual(decode.call_count, 160)
            self.assertEqual(len(decoder.cache), 240)


@previous_window
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
        for key in self.grid.order[:wide_columns]:
            tile = self.grid.tiles[key]
            self.assertGreaterEqual(tile.winfo_rootx(), self.grid.canvas.winfo_rootx())
            self.assertLessEqual(tile.winfo_rootx() + tile.winfo_width(),
                                 self.grid.canvas.winfo_rootx() + self.grid.canvas.winfo_width())

    def test_resize_within_same_column_count_keeps_tile_coordinates_and_skips_reflow(self):
        self.grid.show([(str(index), {"author": "offline"}) for index in range(200)], set)
        self.root.update()
        columns = self.grid.columns
        before = {key: (tile.winfo_x(), tile.winfo_y()) for key, tile in self.grid.tiles.items()}
        with ExitStack() as stack:
            moves = [stack.enter_context(patch.object(tile, "grid", wraps=tile.grid))
                     for tile in self.grid.tiles.values()]
            flush = stack.enter_context(patch.object(self.grid, "update_idletasks"))
            self.root.geometry("1040x400")
            self.root.update()
            self.assertEqual(self.grid.columns, columns)
            self.assertEqual({key: (tile.winfo_x(), tile.winfo_y()) for key, tile in self.grid.tiles.items()}, before)
            for move in moves:
                move.assert_not_called()
            flush.assert_not_called()

    def test_resize_burst_is_coalesced_and_pending_layout_is_cancelled_on_release(self):
        self.root.update()
        with patch.object(self.grid, "_layout", wraps=self.grid._layout) as layout:
            for _ in range(30):
                self.grid._schedule_layout()
            timer = self.grid._layout_timer
            self.root.update()
            self.assertEqual(layout.call_count, 1)
            self.assertNotIn(timer, self.root.tk.call("after", "info"))
        self.grid._schedule_layout()
        self.grid._schedule_region()
        timers = {self.grid._layout_timer, self.grid._region_timer}
        self.grid.release()
        self.assertTrue(timers.isdisjoint(self.root.tk.call("after", "info")))
        self.root.update()

    def test_appending_tiles_preserves_existing_positions_and_scrolls_to_new_selection(self):
        rows = [(str(index), {"author": "offline"}) for index in range(100)]
        self.grid.show(rows[:60], set)
        self.root.update()
        first = self.grid.tiles["0"]
        with patch.object(first, "place", wraps=first.place) as move:
            self.grid.show(rows, set)
            self.root.update()
            move.assert_not_called()
            self.grid.select("99")
            self.root.update()
        tile = self.grid.tiles["99"]
        self.assertGreaterEqual(tile.winfo_rooty(), self.grid.canvas.winfo_rooty())
        self.assertLessEqual(tile.winfo_rooty() + tile.winfo_height(),
                             self.grid.canvas.winfo_rooty() + self.grid.canvas.winfo_height())

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

    def test_pending_missing_and_restored_previews_have_distinct_finished_states(self):
        row = {"path": "offline-fixture.jpg", "author": "offline", "posted_at": "2026-10-01"}
        with patch.object(self.grid._decoder, "request"):
            self.grid.show([("one", row)], set)
            self.grid.select("one")
            tile = self.grid.tiles["one"]
            self.assertEqual(tile.cget("text"), "✓ @offline\n불러오는 중…")
            self.assertEqual(str(tile.cget("image")), str(self.grid.placeholder))
            # An old page's completion must not make this request look finished.
            self.grid._results.put((self.grid._token - 1, "one", None))
            self.grid.poll()
            self.assertNotIn("one", self.grid._source_images)
            self.assertIn("불러오는 중", tile.cget("text"))

            self.grid._results.put((self.grid._token, "one", None))
            self.grid.poll()
            self.assertEqual(tile.cget("text"), "✓ @offline\n미리보기 없음")
            self.assertEqual(str(tile.cget("image")), str(self.grid.unavailable_placeholder))
            self.assertNotEqual(str(self.grid.placeholder), str(self.grid.unavailable_placeholder))
            # Retrying preserves a settled tile until there is a new result.
            self.grid.show([("one", row)], set)
            self.assertIn("미리보기 없음", tile.cget("text"))
            with Image.new("RGB", (4, 4), "red") as source:
                self.grid._results.put((self.grid._token, "one", source))
                self.grid.poll()
                self.assertEqual(tile.cget("text"), "✓ @offline\n2026-10-01")
                self.assertEqual(str(tile.cget("image")), str(self.grid.images["one"]))
                self.assertNotIn("one", self.grid._unavailable)
                self.assertEqual(self.grid.selected_keys, ("one",))

    def test_record_without_a_path_is_settled_without_waiting_for_a_decoder(self):
        with patch.object(self.grid._decoder, "request") as request:
            self.grid.show([("one", {"author": "offline"})], set)
        self.assertEqual(request.call_args.args[1], [])
        self.assertEqual(self.grid.tiles["one"].cget("text"), "@offline\n미리보기 없음")
        self.assertEqual(str(self.grid.tiles["one"].cget("image")), str(self.grid.unavailable_placeholder))

    def test_placeholder_panels_follow_theme_without_replacing_finished_images(self):
        rows = [("pending", {"path": "offline-fixture.jpg", "author": "offline"}),
                ("missing", {"author": "offline"}),
                ("image", {"path": "other-fixture.jpg", "author": "offline"})]
        with patch.object(self.grid._decoder, "request"), Image.new("RGB", (4, 4), "red") as source:
            self.grid.show(rows, set)
            self.grid._results.put((self.grid._token, "image", source))
            self.grid.poll()
            finished = self.grid.images["image"]
            self.grid.select("missing")
            for appearance in ("dark", "light"):
                with self.subTest(appearance=appearance):
                    theme.apply(self.root, appearance)
                    expected = tuple(channel // 256 for channel in self.root.winfo_rgb(theme.HEADER))
                    for key, placeholder in (("pending", self.grid.placeholder),
                                             ("missing", self.grid.unavailable_placeholder)):
                        self.assertEqual(str(self.grid.tiles[key].cget("image")), str(placeholder))
                        pixel = self.grid.tk.call(str(placeholder), "get", 10, 10)
                        self.assertEqual(tuple(pixel), expected)
                    self.assertIs(self.grid.images["image"], finished)
                    self.assertEqual(str(self.grid.tiles["image"].cget("image")), str(finished))
                    self.assertEqual(self.grid.selected_keys, ("missing",))
                    self.assertTrue(self.grid.tiles["missing"].cget("text").startswith("✓ "))

    def test_placeholder_panels_use_their_own_interpreters_palette(self):
        other = tk.Tk()
        other.attributes("-alpha", 0)
        another_grid = None
        try:
            theme.apply(other, "dark")
            self.assertEqual(theme.mode(self.root), "light")
            self.assertEqual(theme.mode(other), "dark")
            self.grid._refresh_placeholders()
            another_grid = ThumbnailGrid(self.root, on_select=lambda _: None, on_open=lambda _: None)
            expected = tuple(channel // 256 for channel in self.root.winfo_rgb(theme.LIGHT["HEADER"]))
            for grid in (self.grid, another_grid):
                for placeholder in (grid.placeholder, grid.unavailable_placeholder):
                    self.assertEqual(tuple(grid.tk.call(str(placeholder), "get", 10, 10)), expected)
        finally:
            if another_grid is not None:
                another_grid.destroy()
            other.destroy()
            theme.apply(self.root, "light")

    def test_wide_image_and_placeholder_keep_the_same_tile_height(self):
        row = {"path": "offline-fixture.jpg", "author": "offline", "posted_at": "2026-10-01"}
        with patch.object(self.grid._decoder, "request"):
            self.grid.show([("one", row)], set)
            self.root.update_idletasks()
            tile = self.grid.tiles["one"]
            height = tile.winfo_reqheight()
            with Image.new("RGB", (150, 30), "red") as source:
                self.grid._results.put((self.grid._token, "one", source))
                self.grid.poll()
                self.root.update_idletasks()
                self.assertEqual((self.grid.images["one"].width(), self.grid.images["one"].height()), TILE)
                self.assertEqual(tile.winfo_reqheight(), height)
                self.assertEqual(source.size, (150, 30))
            self.grid._results.put((self.grid._token, "one", None))
            self.grid.poll()
            self.root.update_idletasks()
            self.assertEqual(tile.winfo_reqheight(), height)

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

    def test_selection_sync_repaints_only_tiles_that_change_selection(self):
        self.selection_rows()
        self.grid.select_many(("0", "2", "4"), focus="0", scroll=False)
        with ExitStack() as stack:
            repaint = {key: stack.enter_context(patch.object(tile, "configure", wraps=tile.configure))
                       for key, tile in self.grid.tiles.items()}
            # A focus-only synchronization does not change any tile's color
            # or check mark, even when the requested keys arrive out of order.
            self.grid.select_many(("4", "2", "0", "2"), focus="2", scroll=False)
            self.assertEqual((self.grid.selected_keys, self.grid.selected), (("0", "2", "4"), "2"))
            for call in repaint.values():
                call.assert_not_called()
            self.grid.select_many(("5", "4", "0"), focus="5", scroll=False)
            self.assertEqual((self.grid.selected_keys, self.grid.selected), (("0", "4", "5"), "5"))
            for key, call in repaint.items():
                self.assertEqual(call.call_count, int(key in {"2", "5"}))
            self.assertFalse(self.grid.tiles["2"].cget("text").startswith("✓ "))
            self.assertTrue(self.grid.tiles["5"].cget("text").startswith("✓ "))

    def test_recreated_selected_tile_keeps_selection_appearance_in_each_theme(self):
        with patch.object(self.grid._decoder, "request"):
            for appearance in ("light", "dark"):
                with self.subTest(appearance=appearance):
                    theme.apply(self.root, appearance)
                    row = {"path": "first-fixture.jpg", "author": "offline", "posted_at": "2026-10-01"}
                    self.grid.show([("one", row), ("two", row)], set)
                    self.grid.select("one")
                    original = self.grid.tiles["one"]
                    colors = tuple(original.cget(key) for key in ("background", "foreground"))
                    self.grid.show([("one", dict(row, path="second-fixture.jpg", author="updated")), ("two", row)], set)
                    replacement = self.grid.tiles["one"]
                    self.assertIsNot(replacement, original)
                    self.assertEqual((self.grid.selected_keys, self.grid.selected), (("one",), "one"))
                    self.assertEqual(tuple(replacement.cget(key) for key in ("background", "foreground")), colors)
                    self.assertEqual(replacement.cget("text"), "✓ @updated\n불러오는 중…")
                    self.assertNotEqual(replacement.cget("background"), self.grid.tiles["two"].cget("background"))

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
        band = self.grid._band
        shown = [line for line in band.lines if band.canvas.itemcget(line, "state") != "hidden"]
        self.assertEqual(len(shown), 3)
        self.assertEqual(band.canvas.itemcget(band.lines[3], "state"), "hidden")
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
        self.grid.tiles["0"].focus_force()
        self.root.update()
        self.assertEqual(self.root.focus_get(), self.grid.tiles["0"])
        children = set(self.grid.inner.winfo_children())
        self.grid._press(self.pointer("0"), "0")
        for key in ("1", "7", "2", "8", "3", "9", "0"):
            self.grid._drag_motion(self.pointer(key))
        self.root.update()
        # Nothing is placed among the tiles, so moving the box repaints none.
        self.assertEqual(set(self.grid.inner.winfo_children()), children)
        self.assertEqual(self.grid._band.window.wm_overrideredirect(), True)
        self.assertEqual(self.root.focus_get(), self.grid.tiles["0"])
        # One window the size of the visible gallery: moving the box never
        # resizes it (a new size first shows up blank, which looked like a
        # blinking white box).
        band = self.grid._band
        size = (band.window.winfo_width(), band.window.winfo_height())
        self.assertEqual(size, (self.grid.canvas.winfo_width(), self.grid.canvas.winfo_height()))
        with patch.object(band.window, "geometry", wraps=band.window.geometry) as geometry:
            for key in ("4", "10", "1", "6"):
                self.grid._drag_motion(self.pointer(key))
        geometry.assert_not_called()
        self.assertTrue(band.revealed or band._reveal_job is not None)
        self.grid._release_drag(self.pointer("0"))
        self.assertFalse(band.visible or band.revealed)
        self.assertEqual(band.window.state(), "withdrawn")
        if os.name == "nt":  # transparentcolor and the conceal alpha are Windows features.
            self.assertEqual(float(band.window.attributes("-alpha")), 0.0)
        self.grid.release()
        self.assertIsNone(self.grid._band.window)

if __name__ == "__main__":
    unittest.main()

"""Bounded preview memory and viewport-driven reading with real Tk geometry."""

from collections import Counter
from pathlib import Path
import tempfile
import threading
import time
import tkinter as tk
import unittest
from unittest.mock import patch

from PIL import Image

from luvbox import theme
from luvbox.gallery import TILE
from luvbox.preview import Thumbnail
from tests import test_gallery as gallery_fixtures


class GalleryCacheTests(unittest.TestCase):
    release = gallery_fixtures.GalleryLayoutTests.release

    def setUp(self):
        gallery_fixtures.GalleryLayoutTests.setUp(self)
        self.callback_errors = []
        self.root.report_callback_exception = lambda *error: self.callback_errors.append(error)
        self.addCleanup(self.assertFalse, self.callback_errors)
        self.folder = tempfile.TemporaryDirectory(prefix="luvbox-gallery-cache-test-")
        self.addCleanup(self.folder.cleanup)
        self.addCleanup(self.grid.release)
        self.decoded = Counter()
        self.permission_batches = []
        self.allowed = True

        def decode(path, size):
            self.decoded[str(path)] += 1
            number = int(Path(path).stem)
            return Thumbnail(Image.new("RGBA", (150, 84), (number % 255, 60, 180, 255)), "ready")

        self.grid._decoder.decoder = decode
        self.root.update()

    def rows(self, count=1000, *, identities=False):
        result = []
        for index in range(count):
            path = Path(self.folder.name) / f"{index}.jpg"
            path.write_bytes(b"local synthetic fixture")
            row = {"path": str(path), "author": "offline", "posted_at": "2026-10-01"}
            if identities:
                row.update(tweet_id=str(10000 + index), num=1, media_type="photo")
            result.append((str(index), row))
        return result

    def permitted(self, paths):
        self.permission_batches.append(tuple(paths))
        return set(paths) if self.allowed else set()

    def wait(self, predicate, *, timeout=4):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.root.update()
            self.grid.poll(limit=24)
            if predicate():
                self.assertFalse(self.callback_errors)
                return
            time.sleep(.001)
        self.fail("gallery did not settle within the test deadline")

    def settle(self):
        self.wait(lambda: bool(self.grid.tiles) and all(key in self.grid.images for key in self.visible_keys()))

    def visible_keys(self):
        top = self.grid.canvas.winfo_rooty()
        bottom = top + self.grid.canvas.winfo_height()
        return {key for key, tile in self.grid.tiles.items()
                if tile.winfo_rooty() < bottom and tile.winfo_rooty() + tile.winfo_height() > top}

    def show(self, rows, **hooks):
        self.grid.show(rows, self.permitted, **hooks)
        self.root.update()
        self.settle()

    def move_to(self, fraction):
        self.grid.canvas.yview_moveto(fraction)
        self.root.update()
        self.settle()

    def test_initial_page_reads_only_visible_and_nearby_paths(self):
        rows = self.rows()
        self.show(rows)
        observed = {path for batch in self.permission_batches for path in batch}
        self.assertGreater(len(observed), 0)
        self.assertLess(len(observed), 100)
        self.assertNotIn(rows[-1][1]["path"], observed)
        self.assertLess(sum(self.decoded.values()), 100)
        self.assertEqual(len(self.grid.order), 1000)

    def test_visible_previews_finish_on_the_grids_timer_without_host_polling(self):
        self.grid.show(self.rows(), self.permitted)
        deadline = time.monotonic() + 4
        ready = False
        # No explicit grid.poll(): a hidden/slow host's 120ms backup poll
        # must not be required for normal thumbnail completion.
        while time.monotonic() < deadline:
            self.root.update()
            visible = self.visible_keys()
            ready = (bool(visible) and all(key in self.grid.images for key in visible)
                     and not self.grid._pending_keys and self.grid._poll_timer is None)
            if ready:
                break
            time.sleep(.001)
        self.assertTrue(ready, "thumbnail delivery still depends on an external poll")
        self.assertLess(sum(self.decoded.values()), 100)
        self.assertFalse(self.callback_errors)

    def test_reopening_the_same_theme_retains_photos_without_recomposing_them(self):
        self.show(self.rows(100))
        self.grid.select_many(("0", "1"), focus="0", scroll=False)
        photos = dict(self.grid.images)
        placeholders = self.grid.placeholder, self.grid.unavailable_placeholder
        tile = self.grid.tiles["0"]
        pixel = tuple(self.grid.tk.call(str(photos["0"]), "get", 0, 0))
        with patch.object(type(photos["0"]), "paste", side_effect=AssertionError("redundant photo redraw")):
            self.grid.pack_forget()
            self.root.update()
            self.grid.pack(fill="both", expand=True)
            self.root.update()
            self.grid._refresh_placeholders()
        self.assertEqual(self.grid.images, photos)
        self.assertEqual((self.grid.placeholder, self.grid.unavailable_placeholder), placeholders)
        self.assertEqual(self.grid.selected_keys, ("0", "1"))
        self.assertEqual(tuple(self.grid.tk.call(str(photos["0"]), "get", 0, 0)), pixel)
        self.assertTrue(tile.cget("text").startswith("✓ "))
        # A real appearance change still updates the selected transparent
        # margins using the same Tcl photo buffer.
        for appearance in ("dark", "light"):
            theme.apply(self.root, appearance)
            self.root.update()
            self.assertIs(self.grid.images["0"], photos["0"])
            background = tuple(channel // 256 for channel in self.root.winfo_rgb(tile.cget("background")))
            self.assertEqual(tuple(self.grid.tk.call(str(photos["0"]), "get", 0, 0)), background)

    def test_visible_rows_are_requested_before_overscan_rows_after_scroll(self):
        self.show(self.rows())
        with patch.object(self.grid._decoder, "request", wraps=self.grid._decoder.request) as request:
            self.move_to(.5)
        jobs = request.call_args.args[1]
        keys = [job[0] for job in jobs]
        visible = self.visible_keys()
        requested_visible = [key for key in keys if key in visible]
        self.assertTrue(requested_visible)
        self.assertEqual(keys[:len(requested_visible)], requested_visible)
        self.assertTrue(any(key not in visible for key in keys))

    def test_unique_source_images_stay_bounded_and_evicted_items_reload(self):
        rows = self.rows(1500)
        self.show(rows)
        # Distinct viewports exceed both 240-item memory caches while keeping
        # all source-file identities valid and every result image independent.
        first_path = rows[0][1]["path"]
        for fraction in (index / 24 for index in range(1, 25)):
            self.move_to(fraction)
            self.assertLessEqual(len(self.grid._source_images), max(240, len(self.grid.tiles)))
        self.assertGreater(len(self.decoded), 240)
        self.assertNotIn("0", self.grid._source_images)
        reads_before = self.decoded[first_path]
        self.move_to(0)
        self.assertGreater(self.decoded[first_path], reads_before)
        self.assertIn("0", self.grid.images)
        self.assertNotIn("0", self.grid._unavailable)
        self.assertNotIn("미리보기 없음", self.grid.tiles["0"].cget("text"))
        self.assertEqual((self.grid.images["0"].width(), self.grid.images["0"].height()), TILE)

    def test_recent_cached_viewport_reentry_does_not_decode_again(self):
        self.show(self.rows())
        first_keys = self.visible_keys()
        before = dict(self.decoded)
        self.move_to(.02)
        self.move_to(0)
        for key in first_keys:
            self.assertEqual(self.decoded[self.grid._paths[key]], before[self.grid._paths[key]])
            self.assertIn(key, self.grid.images)

    def test_stale_inflight_viewport_cannot_replace_latest_jump_result(self):
        rows = self.rows()
        started, resume = threading.Event(), threading.Event()
        old_path = rows[0][1]["path"]
        original = self.grid._decoder.decoder

        def slow(path, size):
            if str(path) == old_path:
                started.set()
                resume.wait(4)
            return original(path, size)

        self.grid._decoder.decoder = slow
        self.addCleanup(resume.set)
        self.grid.show(rows, self.permitted)
        self.root.update()
        self.assertTrue(started.wait(2))
        self.grid.canvas.yview_moveto(1)
        self.root.update()
        self.grid.canvas.yview_moveto(.5)
        self.root.update()
        self.grid.canvas.yview_moveto(1)
        self.root.update()
        resume.set()
        self.settle()
        self.assertIn("999", self.grid.images)
        self.assertNotIn("0", self.grid.images)
        # Superseded middle and first-page tails never need to be decoded.
        self.assertLess(sum(self.decoded.values()), 100)
        self.assertFalse(self.callback_errors)

    def test_refresh_revokes_hidden_hot_cache_before_reentry(self):
        rows = self.rows()
        self.show(rows)
        self.move_to(.02)
        self.assertNotIn("0", self.grid.tiles)
        self.assertIn("0", self.grid._source_images)
        self.allowed = False
        self.grid.show(rows, self.permitted)
        self.root.update()
        self.grid.canvas.yview_moveto(0)
        self.root.update()
        self.wait(lambda: "0" in self.grid._unavailable and "0" not in self.grid.images)
        self.assertIn("미리보기 없음", self.grid.tiles["0"].cget("text"))
        self.assertEqual(str(self.grid.tiles["0"].cget("image")), str(self.grid.unavailable_placeholder))

    def test_saved_preview_fallback_survives_deferred_permission_failure(self):
        rows = self.rows(1000, identities=True)
        self.allowed = False
        recalled = []
        kept = Image.new("RGBA", (150, 84), (17, 90, 130, 255))

        def recall(identity):
            recalled.append(identity)
            return kept

        remembered = []
        self.show(rows, recall=recall, remember=lambda *args: remembered.append(args))
        self.assertFalse(self.decoded)
        self.assertFalse(remembered)
        self.assertLess(len(recalled), 100)
        self.assertNotIn(("10999", 1, "photo"), recalled)
        self.move_to(1)
        self.assertIn(("10999", 1, "photo"), recalled)
        self.assertIn("999", self.grid.images)
        self.assertEqual(tuple(self.grid.tk.call(str(self.grid.images["999"]), "get", 75, 56)),
                         (17, 90, 130))

    def test_recycled_preview_buffer_replaces_pixels_caption_and_selection(self):
        rows = self.rows()
        for _, row in rows[900:]:
            row["author"] = "fresh"

        def decode(path, size):
            if int(Path(path).stem) < 900:
                image = Image.new("RGBA", TILE, (230, 20, 30, 255))
            else:
                # A tall preview introduces side margins and a transparent
                # pixel where the previous full red photo was opaque.
                image = Image.new("RGBA", (40, 112), (10, 40, 220, 255))
                image.putpixel((20, 56), (0, 0, 0, 0))
            return Thumbnail(image, "ready")

        self.grid._decoder.decoder = decode
        self.show(rows)
        self.wait(lambda: not self.grid._pending_keys)
        old_photos = {tile: self.grid.images[key] for key, tile in self.grid.tiles.items()}
        self.grid.select_many(tuple(self.grid.tiles), focus="0", scroll=False)
        self.move_to(1)
        self.wait(lambda: not self.grid._pending_keys)
        reused = [(key, tile) for key, tile in self.grid.tiles.items() if tile in old_photos]
        self.assertTrue(reused)
        for key, tile in reused:
            photo = self.grid.images[key]
            self.assertIs(photo, old_photos[tile])
            self.assertEqual(str(tile.cget("image")), str(photo))
            self.assertEqual(tile.cget("text"), "@fresh\n2026-10-01")
            self.assertEqual(tile.cget("background"), theme.CARD)
            self.assertEqual(tile.cget("foreground"), theme.INK)
            self.assertEqual(tuple(self.grid.tk.call(str(photo), "get", 60, 56)), (10, 40, 220))
            background = tuple(channel // 256 for channel in self.root.winfo_rgb(tile.cget("background")))
            self.assertEqual(tuple(self.grid.tk.call(str(photo), "get", 0, 0)), background)
            self.assertEqual(tuple(self.grid.tk.call(str(photo), "get", 75, 56)), background)
            self.assertFalse(self.grid.tk.call(str(photo), "transparency", "get", 0, 0))

    def test_preview_alpha_blends_with_selection_and_each_interpreters_theme(self):
        rows = self.rows(1)
        source_color = (210, 30, 80)

        def decode(path, size):
            image = Image.new("RGBA", (40, 84), (*source_color, 128))
            image.putpixel((20, 42), (0, 0, 0, 0))
            return Thumbnail(image, "ready")

        self.grid._decoder.decoder = decode
        self.show(rows)
        tile = self.grid.tiles["0"]
        photo = self.grid.images["0"]
        other = tk.Tk()
        other.withdraw()
        self.addCleanup(other.destroy)
        # A separate interpreter's theme must not choose this tile's colors.
        theme.apply(other, "dark")
        self.assertEqual(theme.mode(self.root), "light")
        self.grid.select_many(("0",), focus="0", scroll=False)
        self.assertEqual(tile.cget("background"), "#e3eaff")
        self.assertEqual(tuple(self.grid.tk.call(str(photo), "get", 0, 0)), (227, 234, 255))
        self.grid.select_many((), scroll=False)
        self.assertEqual(tile.cget("background"), theme.LIGHT["CARD"])

        for appearance in ("light", "dark", "light"):
            theme.apply(self.root, appearance)
            palette = theme.LIGHT if appearance == "light" else theme.DARK
            for selected in (False, True, False):
                with self.subTest(appearance=appearance, selected=selected):
                    self.grid.select_many(("0",) if selected else (), focus="0", scroll=False)
                    self.root.update()
                    expected_background = theme._tone("#e3eaff", appearance) if selected else palette["CARD"]
                    self.assertEqual(tile.cget("background"), expected_background)
                    self.assertIs(self.grid.images["0"], photo)
                    background = tuple(channel // 256 for channel in self.root.winfo_rgb(expected_background))
                    # Both letterbox padding and a fully transparent source
                    # pixel must follow selection/theme, with no old pixels.
                    for x, y in ((0, 0), (75, 56)):
                        self.assertEqual(tuple(self.grid.tk.call(str(photo), "get", x, y)), background)
                    blended = tuple((front * 128 + back * 127 + 127) // 255
                                    for front, back in zip(source_color, background))
                    self.assertEqual(tuple(self.grid.tk.call(str(photo), "get", 60, 30)), blended)
                    self.assertEqual(tile.cget("text").startswith("✓ "), selected)

    def test_failed_preview_hides_reusable_buffer_and_release_drops_retained_tile_image(self):
        rows = self.rows(50)
        self.show(rows)
        tile = self.grid.tiles["0"]
        photo = tile._preview_image
        image_name = str(photo)
        self.assertEqual(str(tile.cget("image")), image_name)
        # Hold the widget past release, but let it own the only Python photo
        # reference. Tcl image deletion must happen on the current UI thread.
        del photo
        self.allowed = False
        self.grid.show(rows, self.permitted)
        self.root.update()
        self.wait(lambda: "0" in self.grid._unavailable and "0" not in self.grid.images)
        self.assertIsNotNone(tile._preview_image)
        self.assertEqual(str(tile.cget("image")), str(self.grid.unavailable_placeholder))
        self.assertNotEqual(str(tile.cget("image")), image_name)
        self.grid.release()
        self.assertIsNone(tile._preview_image)
        self.assertNotIn(image_name, self.root.tk.call("image", "names"))
        self.root.update()
        self.assertFalse(self.callback_errors)

    def test_close_cancels_scheduled_result_poll_and_drops_preview_references(self):
        rows = self.rows(50)
        self.show(rows)
        self.assertTrue(self.grid._source_images)
        started, resume = threading.Event(), threading.Event()
        self.addCleanup(resume.set)

        def slow_permission(paths):
            started.set()
            resume.wait(4)
            return set(paths)

        self.grid.show(rows, slow_permission)
        self.root.update()
        self.assertTrue(started.wait(2))
        self.assertIsNotNone(self.grid._poll_timer)
        pending = set(self.root.tk.call("after", "info"))
        self.assertIn(self.grid._poll_timer, pending)
        self.grid.release()
        self.assertFalse(self.grid._source_images)
        self.assertFalse(self.grid.images)
        self.assertFalse(self.grid._pending_keys)
        self.assertIsNone(self.grid._poll_timer)
        self.assertTrue(pending.isdisjoint(self.root.tk.call("after", "info")))
        resume.set()
        self.grid._decoder.thread.join(3)
        self.assertFalse(self.grid._decoder.thread.is_alive())
        self.root.update()
        self.assertFalse(self.callback_errors)


if __name__ == "__main__":
    unittest.main()

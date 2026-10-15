"""Preview cache bounds and cloud/link safety, using private local fixtures."""

import os
import queue
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image

from luvbox import thumbcache
from luvbox.gallery import _GridDecoder
from luvbox.preview import PreviewLoader, Thumbnail, thumbnail


class ThumbCacheStabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="luvbox-preview-regression-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.directory = self.root / "thumbnails"
        location = patch.object(thumbcache, "directory", return_value=self.directory)
        location.start()
        self.addCleanup(location.stop)
        self.image = Image.new("RGB", thumbcache.SIZE, "red")
        self.key = ("100", 1, "photo")

    def cached(self):
        with closing(thumbcache.Cache("offline", create=False)) as cache:
            return cache.total_bytes(), cache.stored([(str(index), 1, "photo") for index in range(100, 108)])

    def test_gallery_callbacks_share_pruning_across_reopened_connections(self):
        size = len(thumbcache.encode(self.image))
        with patch.object(thumbcache, "LIMIT_BYTES", size * 2), patch.object(thumbcache, "_PRUNE_EVERY", 2):
            for index in range(100, 108):
                thumbcache.remember("offline", (str(index), 1, "photo"), self.image)
            total, keys = self.cached()
        self.assertLessEqual(total, size * 2)
        self.assertNotIn(self.key, keys)
        self.assertIn(("107", 1, "photo"), keys)

    def test_short_save_runs_also_share_the_pruning_interval(self):
        path = self.root / "saved.jpg"
        self.image.save(path)
        size = len(thumbcache.encode(thumbcache.make(path)))
        with patch.object(thumbcache, "LIMIT_BYTES", size * 2), patch.object(thumbcache, "_PRUNE_EVERY", 2):
            for index in range(100, 108):
                recorder = thumbcache.Recorder("offline")
                recorder.add((str(index), 1, "photo"), path)
                recorder.close()
            total, keys = self.cached()
        self.assertLessEqual(total, size * 2)
        self.assertNotIn(self.key, keys)
        self.assertIn(("107", 1, "photo"), keys)

    def test_first_write_checks_an_oversized_cache_from_a_previous_process(self):
        size = len(thumbcache.encode(self.image))
        with closing(thumbcache.Cache("offline")) as cache:
            for index in range(100, 106):
                cache.put((str(index), 1, "photo"), self.image)
        # A restarted process has no shared write count for the existing DB.
        thumbcache._prune_writes.pop(str(self.directory / "offline.sqlite3"), None)
        with patch.object(thumbcache, "LIMIT_BYTES", size * 2):
            thumbcache.remember("offline", ("107", 1, "photo"), self.image)
            total, keys = self.cached()
        self.assertLessEqual(total, size * 2)
        self.assertIn(("107", 1, "photo"), keys)

    def test_deleted_cache_is_rebuilt_for_a_known_identity(self):
        thumbcache.remember("offline", self.key, self.image)
        self.assertTrue(thumbcache.known("offline", self.key))
        (self.directory / "offline.sqlite3").unlink()
        thumbcache.remember("offline", self.key, self.image)
        self.assertIsNotNone(thumbcache.recall("offline", self.key))
        self.assertTrue(thumbcache.known("offline", self.key))

    def test_replaced_cache_invalidates_known_identities(self):
        thumbcache.remember("offline", self.key, self.image)
        with closing(thumbcache.Cache("empty")):
            pass
        (self.directory / "empty.sqlite3").replace(self.directory / "offline.sqlite3")
        self.assertFalse(thumbcache.known("offline", self.key))
        thumbcache.remember("offline", self.key, self.image)
        self.assertIsNotNone(thumbcache.recall("offline", self.key))

    def test_worker_process_pruning_invalidates_known_rows_and_allows_remembering_again(self):
        thumbcache.remember("offline", self.key, self.image)
        self.assertTrue(thumbcache.known("offline", self.key))
        path = self.directory / "offline.sqlite3"
        before = path.stat().st_ino
        command = ("import sys; from pathlib import Path; from luvbox import thumbcache; "
                   "thumbcache.directory=lambda: Path(sys.argv[1]); "
                   "cache=thumbcache.Cache('offline'); cache.prune(1); cache.close()")
        subprocess.run([sys.executable, "-c", command, str(self.directory)], capture_output=True,
                       timeout=10, check=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(path.stat().st_ino, before)
        self.assertFalse(thumbcache.known("offline", self.key))
        self.assertIsNone(thumbcache.recall("offline", self.key))
        thumbcache.remember("offline", self.key, self.image)
        self.assertIsNotNone(thumbcache.recall("offline", self.key))

    def test_reading_a_preview_uses_a_read_only_database(self):
        thumbcache.remember("offline", self.key, self.image)
        with closing(thumbcache.Cache("offline", create=False)) as cache:
            self.assertIsNotNone(cache.get(self.key))
            with self.assertRaises(sqlite3.OperationalError) as raised:
                cache.put(("101", 1, "photo"), self.image)
        self.assertIn("readonly", str(raised.exception).lower())
        self.assertIsNone(thumbcache.recall("offline", ("101", 1, "photo")))

    def test_failed_recorder_batch_is_bounded_and_releases_every_owned_image(self):
        images = []

        def make(*_args, **_kwargs):
            image = self.image.copy()
            images.append(image)
            return image

        recorder = thumbcache.Recorder("offline")
        with patch.object(thumbcache, "_RECORDER_PENDING", 3), \
                patch.object(thumbcache, "make", side_effect=make), \
                patch.object(thumbcache.Cache, "put", side_effect=sqlite3.OperationalError("offline locked cache")):
            for index in range(100, 106):
                recorder.add((str(index), 1, "photo"), self.root / "fixture.jpg")
            self.assertEqual(len(recorder._pending), 3)
            self.assertEqual(recorder.failed, 6)
            recorder.close()
        recorder.close()
        self.assertFalse(recorder._pending)
        self.assertEqual(recorder.failed, 6)
        for image in images:
            with self.assertRaises(ValueError):
                image.getpixel((0, 0))

    def test_cache_decode_rejects_eps_without_invoking_its_external_renderer(self):
        from PIL import EpsImagePlugin

        payload = b"%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 16 16\n%%EndComments\nshowpage\n"
        with patch.object(EpsImagePlugin, "Ghostscript") as renderer:
            self.assertIsNone(thumbcache.decode(payload))
        renderer.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows junction fixture")
    def test_linked_cache_directory_and_ancestor_are_refused(self):
        destination = self.root / "outside"
        destination.mkdir()
        linked = self.root / "junction"
        completed = subprocess.run(["cmd.exe", "/c", "mklink", "/J", str(linked), str(destination)],
                                   capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if completed.returncode:
            self.skipTest("Cannot create private junction fixture")
        self.addCleanup(os.rmdir, linked)
        for directory in (linked, linked / "thumbnails"):
            with self.subTest(directory=directory), patch.object(thumbcache, "directory", return_value=directory):
                with self.assertRaises(OSError):
                    thumbcache.Cache("offline")
                thumbcache.remember("offline", self.key, self.image)
                self.assertIsNone(thumbcache.recall("offline", self.key))
        self.assertEqual(list(destination.iterdir()), [])

    @unittest.skipUnless(os.name == "nt", "Windows junction fixture")
    def test_appdata_moved_with_a_junction_above_the_app_folder_still_works(self):
        destination = self.root / "moved-appdata"
        destination.mkdir()
        linked = self.root / "appdata-junction"
        completed = subprocess.run(["cmd.exe", "/c", "mklink", "/J", str(linked), str(destination)],
                                   capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if completed.returncode:
            self.skipTest("Cannot create private junction fixture")
        self.addCleanup(os.rmdir, linked)
        directory = linked / "LuvBox" / "thumbnails"
        with patch.object(thumbcache, "directory", return_value=directory):
            thumbcache.remember("offline", self.key, self.image)
            self.assertIsNotNone(thumbcache.recall("offline", self.key))

    def test_hardlinked_database_and_sidecars_are_not_written(self):
        self.directory.mkdir()
        outside = self.root / "outside.sqlite3"
        outside.write_bytes(b"private unchanged fixture")
        before = outside.read_bytes(), outside.stat().st_mtime_ns
        for suffix in ("", "-wal", "-shm", "-journal", ".epoch"):
            linked = self.directory / ("offline.sqlite3" + suffix)
            try:
                os.link(outside, linked)
            except OSError:
                self.skipTest("Hard links unavailable for private fixture")
            try:
                with self.subTest(suffix=suffix):
                    with self.assertRaises(OSError):
                        thumbcache.Cache("offline")
                    thumbcache.remember("offline", self.key, self.image)
                    self.assertIsNone(thumbcache.recall("offline", self.key))
                    self.assertEqual((outside.read_bytes(), outside.stat().st_mtime_ns), before)
            finally:
                linked.unlink()
        self.assertEqual(list(self.directory.iterdir()), [])


class CloudPreviewSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="luvbox-cloud-preview-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "photo.jpg"
        Image.new("RGB", (320, 240), "red").save(self.path)
        self.status = self.path.stat()
        self.cloud = True
        real_stat = Path.stat

        def marked(path, *args, **kwargs):
            if path == self.path:
                fields = {name: getattr(self.status, name) for name in (
                    "st_dev", "st_ino", "st_size", "st_mtime_ns", "st_mode", "st_nlink")}
                return SimpleNamespace(**fields, st_file_attributes=0x400000 if self.cloud else 0)
            return real_stat(path, *args, **kwargs)

        flagged = patch.object(Path, "stat", marked)
        flagged.start()
        self.addCleanup(flagged.stop)
        self.kept = Image.new("RGBA", (12, 9), "blue")
        self.original = Image.new("RGBA", (100, 75), "red")

    def stop(self, decoder):
        decoder.close()
        if decoder.thread is not None:
            decoder.thread.join(timeout=5)
            self.assertFalse(decoder.thread.is_alive())

    def test_direct_image_and_video_thumbnail_never_open_online_only_data(self):
        for suffix in (".jpg", ".mp4"):
            self.path = self.path.with_suffix(suffix)
            with self.subTest(suffix=suffix), patch("luvbox.preview.Image.open") as open_image, \
                    patch("luvbox.preview.subprocess.run") as ffmpeg:
                result = thumbnail(self.path)
            self.assertIsNone(result.image)
            self.assertIn("클라우드", result.note)
            open_image.assert_not_called()
            ffmpeg.assert_not_called()

    def test_selected_preview_uses_kept_image_then_retries_when_local(self):
        results = queue.Queue()
        decode = Mock(return_value=Thumbnail(self.original, "local image"))
        remember = Mock()
        loader = PreviewLoader(results, decoder=decode)
        self.addCleanup(self.stop, loader)
        loader.request(1, self.path, lambda _: True, remember=remember, recall=lambda: self.kept)
        _, result = results.get(timeout=5)
        self.assertIs(result.image, self.kept)
        self.assertIn("클라우드", result.note)
        decode.assert_not_called()
        remember.assert_not_called()
        self.assertFalse(loader.cache)
        self.cloud = False
        for token in (2, 3):
            loader.request(token, self.path, lambda _: True, remember=remember)
            self.assertIs(results.get(timeout=5)[1].image, self.original)
        self.assertEqual(decode.call_count, 1)
        self.cloud = True
        loader.request(4, self.path, lambda _: True, remember=remember)
        self.assertIs(results.get(timeout=5)[1].image, self.original)
        self.assertEqual(decode.call_count, 1)

    def test_gallery_uses_kept_image_then_retries_when_local(self):
        results = queue.Queue()
        decode = Mock(return_value=Thumbnail(self.original, "local image"))
        remember = Mock()
        grid = _GridDecoder(results, decoder=decode)
        self.addCleanup(self.stop, grid)
        jobs = [("row", str(self.path), ("100", 1, "photo"))]
        # Grid reads os.stat directly; inject the same Windows attributes.
        real_stat = os.stat

        def marked(path, *args, **kwargs):
            if str(path) == str(self.path):
                return SimpleNamespace(st_dev=self.status.st_dev, st_ino=self.status.st_ino,
                                       st_size=self.status.st_size, st_mtime_ns=self.status.st_mtime_ns,
                                       st_file_attributes=0x400000 if self.cloud else 0)
            return real_stat(path, *args, **kwargs)

        with patch("luvbox.gallery.os.stat", marked):
            grid.request(1, jobs, set, remember=remember, recall=lambda _: self.kept)
            self.assertIs(results.get(timeout=5)[2], self.kept)
            decode.assert_not_called()
            remember.assert_not_called()
            self.assertFalse(grid.cache)
            self.cloud = False
            for token in (2, 3):
                grid.request(token, jobs, set, remember=remember)
                self.assertIs(results.get(timeout=5)[2], self.original)
            self.assertEqual(decode.call_count, 1)
            self.assertEqual(remember.call_count, 1)


if __name__ == "__main__":
    unittest.main()

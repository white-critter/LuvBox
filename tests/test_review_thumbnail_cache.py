"""Declined local .part media retain a bounded preview after removal."""

from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from luvbox import thumbcache
from luvbox.media import ffmpeg_path


class ReviewThumbnailCacheTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="luvbox-declined-preview-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.directory = self.root / "thumbnails"
        location = patch.object(thumbcache, "directory", return_value=self.directory)
        location.start()
        self.addCleanup(location.stop)

    def remember_then_remove(self, path, media_type, suffix):
        before = path.read_bytes(), path.stat().st_mtime_ns
        key = ("100", 1, media_type)
        recorder = thumbcache.Recorder("offline")
        try:
            with patch("requests.Session.request") as network:
                recorder.add(key, path, suffix=suffix)
            network.assert_not_called()
        finally:
            recorder.close()
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)
        # The decoder must release its source before the declined file goes.
        path.unlink()
        kept = thumbcache.recall("offline", key)
        self.assertIsNotNone(kept)
        self.assertLessEqual(kept.width, thumbcache.SIZE[0])
        self.assertLessEqual(kept.height, thumbcache.SIZE[1])
        self.assertFalse(path.exists())
        return kept

    def test_photo_part_remains_visible_without_preserving_the_original(self):
        path = self.root / "photo.jpg.part"
        Image.new("RGB", (480, 320), "red").save(path, "JPEG")
        kept = self.remember_then_remove(path, "photo", ".JPG")
        red, green, blue, _ = kept.getpixel((10, 10))
        self.assertGreater(red, green + 100)
        self.assertGreater(red, blue + 100)

    def test_gif_part_keeps_the_first_frame(self):
        path = self.root / "animation.gif.part"
        Image.new("RGB", (300, 100), "red").save(
            path, "GIF", save_all=True,
            append_images=[Image.new("RGB", (300, 100), "blue")], duration=100, loop=0)
        kept = self.remember_then_remove(path, "animated_gif", ".gif")
        red, _, blue, _ = kept.getpixel((10, 10))
        self.assertGreater(red, blue + 100)

    def test_video_and_unconverted_gif_mp4_parts_keep_a_frame(self):
        executable = ffmpeg_path()
        if executable is None:
            self.skipTest("Bundled local video decoder is unavailable")
        for media_type in ("video", "animated_gif"):
            with self.subTest(media_type=media_type):
                path = self.root / (media_type + ".mp4.part")
                generated = subprocess.run(
                    [str(executable), "-nostdin", "-v", "error", "-f", "lavfi", "-i",
                     "testsrc2=size=320x180:rate=10", "-t", "0.1", "-an", "-c:v",
                     "mpeg4", "-f", "mp4", str(path)],
                    capture_output=True, timeout=10,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                self.assertEqual(generated.returncode, 0,
                                 generated.stderr.decode("utf8", errors="replace"))
                self.remember_then_remove(path, media_type, ".mp4")

    def test_cloud_part_is_not_hydrated_or_recorded(self):
        path = self.root / "cloud.mp4.part"
        path.write_bytes(b"private local placeholder fixture")
        recorder = thumbcache.Recorder("offline")
        try:
            with patch.object(thumbcache, "cloud_only", return_value=True), \
                    patch("luvbox.preview.thumbnail") as decoder:
                recorder.add(("100", 1, "video"), path, suffix=".mp4")
            decoder.assert_not_called()
        finally:
            recorder.close()
        self.assertFalse(self.directory.exists())
        self.assertEqual(path.read_bytes(), b"private local placeholder fixture")

    def test_temporary_cache_lock_retries_after_the_declined_original_is_removed(self):
        path = self.root / "photo.jpg.part"
        with Image.new("RGB", (480, 320), "red") as image:
            image.save(path, "JPEG")
        key = ("100", 1, "photo")
        recorder = thumbcache.Recorder("offline")
        with patch.object(thumbcache.Cache, "put", side_effect=sqlite3.OperationalError("offline locked cache")):
            self.assertFalse(recorder.add(key, path, suffix=".jpg"))
        path.unlink()
        recorder.close()
        self.assertEqual(recorder.failed, 0)
        kept = thumbcache.recall("offline", key)
        self.assertIsNotNone(kept)
        kept.close()


if __name__ == "__main__":
    unittest.main()

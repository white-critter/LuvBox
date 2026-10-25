"""The optional saved card is local, static, bounded and latest-only."""

import base64
from contextlib import contextmanager
import hashlib
from io import BytesIO
from pathlib import Path
import os
import subprocess
import tempfile
from threading import Event, get_ident
import time
import unittest
from unittest.mock import patch

from PIL import Image
from PySide6.QtCore import QCoreApplication

from luvbox.core import _saved_post_details
from luvbox.preview import Thumbnail
from luvbox.qt_download import DownloadController, _saved_item
from luvbox.qt_saved_preview import SavedPreviewLoader, decode_saved_preview

try:
    from . import test_core as support
except ImportError:
    import test_core as support


class SavedPreviewDecodeTests(unittest.TestCase):
    def test_gif_uses_only_first_frame_and_keeps_original_unchanged(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "still.gif"
            with Image.new("RGB", (720, 480), "red") as first, Image.new("RGB", (720, 480), "blue") as second:
                first.save(path, save_all=True, append_images=[second], duration=[50, 50], loop=0)
            before = hashlib.sha256(path.read_bytes()).hexdigest()
            result = decode_saved_preview(path)
            self.assertIn("첫 프레임", result["preview_note"])
            self.assertTrue(result["preview_url"].startswith("data:image/png;base64,"))
            with Image.open(BytesIO(base64.b64decode(result["preview_url"].split(",", 1)[1]))) as image:
                self.assertLessEqual(image.width, 384)
                self.assertLessEqual(image.height, 288)
                self.assertEqual(image.n_frames if hasattr(image, "n_frames") else 1, 1)
                self.assertGreater(image.convert("RGB").getpixel((10, 10))[0], 200)
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)

    def test_mp4_preview_is_one_bounded_png_not_video_playback(self):
        from luvbox.media import ffmpeg_path
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "still.mp4"
            subprocess.run([str(ffmpeg_path()), "-nostdin", "-v", "error", "-f", "lavfi", "-i",
                "testsrc2=size=64x48:rate=8", "-t", "0.5", "-an", "-c:v", "mpeg4", str(path)],
                capture_output=True, check=True, timeout=20,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            before = hashlib.sha256(path.read_bytes()).hexdigest()
            result = decode_saved_preview(path)
            self.assertIn("첫 프레임", result["preview_note"])
            with Image.open(BytesIO(base64.b64decode(result["preview_url"].split(",", 1)[1]))) as image:
                self.assertLessEqual(image.width, 384)
                self.assertLessEqual(image.height, 288)
                self.assertEqual(image.format, "PNG")
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)

    def test_cloud_only_hardlinked_and_relative_files_are_not_decoded(self):
        decoder = lambda *_args: self.fail("unsafe file reached decoder")
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "photo.png"
            with Image.new("RGB", (40, 20), "red") as image:
                image.save(path)
            with patch("luvbox.qt_saved_preview.cloud_only", return_value=True):
                self.assertEqual(decode_saved_preview(path, decoder=decoder)["preview_url"], "")
            self.assertEqual(decode_saved_preview(Path("relative.png"), decoder=decoder)["preview_url"], "")
            linked = path.with_name("linked.png")
            os.link(path, linked)
            self.assertEqual(decode_saved_preview(path, decoder=decoder)["preview_url"], "")

    def test_file_changed_during_decode_is_discarded_and_image_is_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "photo.png"
            with Image.new("RGB", (40, 20), "red") as image:
                image.save(path)
            detached = Image.new("RGBA", (20, 10), "red")
            def decoder(*_args):
                path.write_bytes(b"replacement with another size")
                return Thumbnail(detached, "synthetic")
            result = decode_saved_preview(path, decoder=decoder)
            self.assertEqual(result["preview_url"], "")
            self.assertIn("바뀌어", result["preview_note"])
            with self.assertRaises(ValueError):
                detached.getpixel((0, 0))

    def test_decoder_failures_do_not_disclose_exception_text(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "photo.png"
            path.write_bytes(b"local file")
            def decoder(*_args):
                raise RuntimeError("private-cookie-and-file-content")
            result = decode_saved_preview(path, decoder=decoder)
            self.assertEqual(result["preview_url"], "")
            self.assertNotIn("private", result["preview_note"])

    def test_saved_post_fields_are_bounded_and_never_use_the_liking_user(self):
        result = _saved_post_details({"author": {"nick": "A\0" + "B" * 300},
                                      "user": {"nick": "wrong account"},
                                      "content": "line 1\nline 2\ttext\x1b" + "x" * 6000})
        self.assertLessEqual(len(result["display_name"]), 200)
        self.assertNotIn("wrong", str(result))
        self.assertNotIn("\0", result["display_name"])
        self.assertIn("\n", result["body"])
        self.assertIn("\t", result["body"])
        self.assertNotIn("\x1b", result["body"])
        self.assertLessEqual(len(result["body"]), 5000)


class SavedPreviewWorkerTests(unittest.TestCase):
    def test_latest_pending_request_wins_without_blocking_the_caller(self):
        entered, release = Event(), Event()
        calls, replies = [], []
        def decode(path):
            calls.append((path, get_ident()))
            if path == "first":
                entered.set()
                release.wait(4)
            return {"preview_url": path, "preview_note": ""}
        worker = SavedPreviewLoader(lambda gen, result: replies.append((gen, result)), decoder=decode)
        try:
            self.assertTrue(worker.request(1, "first"))
            self.assertTrue(entered.wait(2))
            worker.request(2, "middle")
            worker.request(3, "latest")
            release.set()
            deadline = time.monotonic() + 3
            while not replies and time.monotonic() < deadline:
                time.sleep(.002)
            self.assertEqual([path for path, _thread in calls], ["first", "latest"])
            self.assertTrue(all(thread != get_ident() for _path, thread in calls))
            self.assertEqual([gen for gen, _result in replies], [3])
        finally:
            release.set()
            worker.close()
            deadline = time.monotonic() + 3
            while worker.alive() and time.monotonic() < deadline:
                time.sleep(.002)
            self.assertFalse(worker.alive())


class SavedPostEventTests(unittest.TestCase):
    def test_real_worker_adds_bounded_post_fields_only_to_newly_saved_media(self):
        from gallery_dl.extractor.message import Message
        fixture = support.CoreTests(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.addCleanup(fixture.tearDown)
        original_items = support.FakeLikesExtractor.items
        def items(extractor):
            for event in original_items(extractor):
                if event[0] in {Message.Directory, Message.Url}:
                    event[-1]["author"]["nick"] = "Alice\0" + "A" * 300
                yield event
        rows = [("123", 1, "photo", "alice", None, "456", "line1\nline2\t" + "x" * 6000)]
        with patch.object(support.FakeLikesExtractor, "items", items):
            result, events = fixture.worker(rows)
            self.assertEqual(result, 0)
            saved = next(event for event in events if event["type"] == "saved")
            self.assertEqual(saved["author"], "alice")
            self.assertEqual(saved["source_url"], "https://x.com/i/web/status/123")
            self.assertEqual(saved["display_name"][:5], "Alice")
            self.assertNotIn("\0", saved["display_name"])
            self.assertLessEqual(len(saved["display_name"]), 200)
            self.assertLessEqual(len(saved["body"]), 5000)
            self.assertIn("\n", saved["body"])
            result, events = fixture.worker(rows)
            self.assertEqual(result, 0)
            skipped = next(event for event in events if event["type"] == "skipped")
            self.assertNotIn("body", skipped)
            self.assertNotIn("display_name", skipped)


class _Service:
    job_id = 7
    def __init__(self): self.pending = []
    def start(self, _options): return True
    def drain(self, _limit): return [self.pending.pop(0)] if self.pending else []
    def close(self): pass
    def stop(self): pass
    def set_review_enabled(self, _enabled): pass


class SavedPreviewControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def wait(self, predicate, timeout=4):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate(): return
            time.sleep(.002)
        self.fail("saved preview did not settle")

    @contextmanager
    def controller(self):
        service = _Service()
        controller = DownloadController(service=service)
        controller.start(object())
        controller._timer.stop()
        try:
            yield controller, service
        finally:
            controller._running = False
            controller.shutdown()
            self.wait(lambda: controller._shutdown_emitted)

    def event(self, path, **fields):
        return {"type": "saved", "job_id": 7, "path": str(path), "filename": "untrusted-name",
                "author": "example", "display_name": "Example", "body": "body\nline two",
                "posted_at": "2026-10-25T12:30:00+09:00", "media_type": "photo",
                "source_url": "https://x.com/i/web/status/123", **fields}

    def test_hidden_card_tracks_latest_saved_metadata_without_decoding(self):
        with tempfile.TemporaryDirectory() as folder, self.controller() as (controller, service):
            path = Path(folder) / "last.png"
            with patch.object(controller._preview_loader, "request") as request:
                service.pending.extend([self.event(path), {"type": "skipped", "job_id": 7, "path": "other"},
                                        self.event(path.with_name("wrong.png"), job_id=1)])
                controller._pump()
                request.assert_not_called()
            self.assertEqual(controller.currentSavedItem["filename"], "last.png")
            self.assertEqual(controller.currentSavedItem["body"], "body\nline two")
            self.assertEqual(controller.savedCount, 1)
            self.assertEqual(controller.currentSavedItem["preview_url"], "")

    def test_progress_updates_do_not_republish_the_large_preview_map(self):
        with tempfile.TemporaryDirectory() as folder, self.controller() as (controller, service):
            service.pending.append(self.event(Path(folder) / "last.png"))
            controller._pump()
            notifications = []
            controller.savedItemChanged.connect(lambda: notifications.append(True))
            service.pending.extend({"type": "progress", "job_id": 7, "downloaded": value, "total": 100}
                                   for value in range(100))
            while service.pending:
                controller._pump()
            self.assertEqual(notifications, [])
            self.assertEqual(controller.progress, .99)

    def test_enable_decodes_static_preview_and_hiding_releases_it(self):
        with tempfile.TemporaryDirectory() as folder, self.controller() as (controller, service):
            path = Path(folder) / "last.png"
            with Image.new("RGB", (600, 400), "green") as image:
                image.save(path)
            service.pending.append(self.event(path))
            controller._pump()
            controller.setDetailsEnabled(True)
            self.wait(lambda: bool(controller.currentSavedItem["preview_url"]))
            self.assertFalse(controller.currentSavedItem["preview_loading"])
            self.assertEqual(controller.savedCount, 1)
            service.pending.extend([{"type": "finished", "job_id": 7, "status": "finished", "saved": 1},
                                    {"type": "process_exit", "job_id": 7, "code": 0}])
            controller._pump()
            self.assertEqual(controller.currentSavedItem["filename"], "last.png")
            controller.setDetailsEnabled(False)
            self.assertEqual(controller.currentSavedItem["preview_url"], "")
            self.assertEqual(controller.status, "저장했습니다")

    def test_hiding_during_decode_discards_late_result_and_close_is_async(self):
        entered, release = Event(), Event()
        with tempfile.TemporaryDirectory() as folder, self.controller() as (controller, service):
            service.pending.append(self.event(Path(folder) / "last.png"))
            controller._pump()
            def decode(_path):
                entered.set()
                release.wait(3)
                return {"preview_url": "stale", "preview_note": "old"}
            controller._preview_loader._decoder = decode
            controller.setDetailsEnabled(True)
            self.assertTrue(entered.wait(1))
            controller.setDetailsEnabled(False)
            started = time.monotonic()
            controller._running = False
            controller.shutdown()
            self.assertLess(time.monotonic() - started, .1)
            self.assertFalse(controller._shutdown_emitted)
            release.set()
            self.wait(lambda: controller._shutdown_emitted)
            self.assertEqual(controller.currentSavedItem["preview_url"], "")

    def test_new_job_clears_previous_saved_card_and_malformed_fields_are_safe(self):
        with tempfile.TemporaryDirectory() as folder, self.controller() as (controller, service):
            service.pending.append(self.event(Path(folder) / "last.png", body="x" * 10000,
                                              media_type={"unsafe": "type"}, source_url="https://example.invalid"))
            controller._pump()
            self.assertEqual(len(controller.currentSavedItem["body"]), 5000)
            self.assertEqual(controller.currentSavedItem["media_type"], "photo")
            self.assertEqual(controller.currentSavedItem["source_url"], "")
            controller._running = False
            controller.start(object())
            self.assertEqual(controller.currentSavedItem, {})
        self.assertEqual(_saved_item({"path": "bad\0path"}), {})


if __name__ == "__main__":
    unittest.main()

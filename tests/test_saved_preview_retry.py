"""Brief read locks recover without accepting a replaced saved file."""

from pathlib import Path
import os
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PIL import Image
from PySide6.QtCore import QCoreApplication

from luvbox import qt_saved_preview
from luvbox.preview import Thumbnail
from tests import test_qt_saved_preview as support
from tests import test_preview_retries as native


class SavedPreviewRetryTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "saved.png"
        with Image.new("RGB", (80, 40), "red") as image:
            image.save(self.path)

    @staticmethod
    def locked():
        return SimpleNamespace(image=None, note="temporary read lock", retryable=True)

    def test_transient_failure_recovers_with_original_identity(self):
        calls = []
        def decode(path, size):
            calls.append(path)
            return self.locked() if len(calls) == 1 else Thumbnail(Image.new("RGBA", (20, 10)), "photo")
        before = self.path.read_bytes()
        with patch.object(qt_saved_preview.time, "sleep") as wait:
            result = qt_saved_preview.decode_saved_preview(self.path, decoder=decode)
        self.assertTrue(result["preview_url"].startswith("data:image/png;base64,"))
        self.assertEqual(len(calls), 2)
        wait.assert_called_once_with(.1)
        self.assertEqual(self.path.read_bytes(), before)

    def test_replacement_between_attempts_is_not_decoded(self):
        def replace(_delay):
            replacement = self.path.with_name("new.png")
            with Image.new("RGB", (80, 40), "blue") as image:
                image.save(replacement)
            replacement.replace(self.path)
        with patch.object(qt_saved_preview.time, "sleep", side_effect=replace), \
                patch.object(qt_saved_preview, "thumbnail", return_value=self.locked()) as decode:
            result = qt_saved_preview.decode_saved_preview(self.path, decoder=decode)
        self.assertEqual(decode.call_count, 1)
        self.assertEqual(result["preview_url"], "")
        self.assertIn("바뀌어", result["preview_note"])

    def test_hardlink_added_while_waiting_is_rejected(self):
        with patch.object(qt_saved_preview.time, "sleep", side_effect=lambda _delay:
                          os.link(self.path, self.path.with_name("linked.png"))), \
                patch.object(qt_saved_preview, "thumbnail", return_value=self.locked()) as decode:
            result = qt_saved_preview.decode_saved_preview(self.path, decoder=decode)
        self.assertEqual(decode.call_count, 1)
        self.assertEqual(result["preview_url"], "")

    def test_permanent_failure_is_not_retried(self):
        with patch.object(qt_saved_preview.time, "sleep") as wait, \
                patch.object(qt_saved_preview, "thumbnail", return_value=Thumbnail(None, "invalid file")) as decode:
            result = qt_saved_preview.decode_saved_preview(self.path, decoder=decode)
        self.assertEqual(decode.call_count, 1)
        wait.assert_not_called()
        self.assertEqual(result, {"preview_url": "", "preview_note": "invalid file"})

    def test_persistent_lock_has_a_small_fixed_retry_limit(self):
        with patch.object(qt_saved_preview.time, "sleep") as wait, \
                patch.object(qt_saved_preview, "thumbnail", return_value=self.locked()) as decode:
            result = qt_saved_preview.decode_saved_preview(self.path, decoder=decode)
        self.assertEqual(decode.call_count, 4)
        self.assertEqual([call.args[0] for call in wait.call_args_list], [.1, .2, .4])
        self.assertEqual(result["preview_url"], "")

    def test_unrelated_directory_named_like_drive_upload_is_not_trusted(self):
        os.link(self.path, self.path.with_name("linked.png"))
        own = os.path.splitdrive(os.path.abspath(self.path))[1]
        with patch.object(qt_saved_preview, "_volume_hard_links",
                          return_value=[own, r"\unrelated\.tmp.driveupload\anything"]), \
                patch.object(qt_saved_preview, "thumbnail") as decoder:
            result = qt_saved_preview.decode_saved_preview(self.path, decoder=decoder)
        decoder.assert_not_called()
        self.assertEqual(result["preview_url"], "")

    @unittest.skipUnless(os.name == "nt", "Windows hard link names")
    def test_same_tree_upload_alias_works_with_real_link_names(self):
        upload = self.path.parent / ".tmp.driveupload" / "upload-fixture"
        upload.parent.mkdir()
        os.link(self.path, upload)
        before = self.path.read_bytes()
        result = qt_saved_preview.decode_saved_preview(self.path)
        self.assertTrue(result["preview_url"].startswith("data:image/png;base64,"))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(upload.read_bytes(), before)

    @unittest.skipUnless(os.name == "nt", "Windows sharing modes")
    def test_actual_sharing_lock_recovers_without_changing_the_saved_file(self):
        from threading import Event, Thread
        ready, release = Event(), Event()
        errors = []
        def hold():
            try:
                with native.NativePreviewLockTests.exclusive_reader(self, self.path):
                    ready.set()
                    release.wait(3)
            except BaseException as error:
                errors.append(error)
                ready.set()
        before = self.path.read_bytes()
        owner = Thread(target=hold)
        owner.start()
        try:
            self.assertTrue(ready.wait(2))
            self.assertEqual(errors, [])
            def unlock(_delay):
                release.set()
                owner.join(2)
            with patch.object(qt_saved_preview.time, "sleep", side_effect=unlock) as wait:
                result = qt_saved_preview.decode_saved_preview(self.path)
            self.assertTrue(result["preview_url"].startswith("data:image/png;base64,"))
            wait.assert_called_once_with(.1)
            self.assertEqual(self.path.read_bytes(), before)
        finally:
            release.set()
            owner.join(3)
        self.assertFalse(owner.is_alive())
        self.assertEqual(errors, [])


class SavedPreviewManualRetryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    wait = support.SavedPreviewControllerTests.wait
    controller = support.SavedPreviewControllerTests.controller
    event = support.SavedPreviewControllerTests.event

    def test_retry_preserves_post_and_ignores_repeat_clicks_while_loading(self):
        with tempfile.TemporaryDirectory() as folder, self.controller() as (controller, service):
            service.pending.append(self.event(Path(folder) / "saved.png"))
            controller._pump()
            with patch.object(controller._preview_loader, "request", return_value=True) as request:
                controller.setDetailsEnabled(True)
                generation = controller._preview_generation
                controller._accept_preview((generation, {"preview_url": "", "preview_note": "locked"}))
                before = {key: controller.currentSavedItem[key] for key in
                          ("filename", "author", "display_name", "body", "posted_at", "source_url")}
                controller.retrySavedPreview()
                self.assertTrue(controller.currentSavedItem["preview_loading"])
                self.assertGreater(controller._preview_generation, generation)
                self.assertEqual(request.call_count, 2)
                controller.retrySavedPreview()
                self.assertEqual(request.call_count, 2)
                controller._accept_preview((generation, {"preview_url": "stale", "preview_note": ""}))
                self.assertEqual(controller.currentSavedItem["preview_url"], "")
                controller._accept_preview((controller._preview_generation,
                                            {"preview_url": "new preview", "preview_note": "photo"}))
                self.assertEqual(controller.currentSavedItem["preview_url"], "new preview")
                self.assertEqual({key: controller.currentSavedItem[key] for key in before}, before)

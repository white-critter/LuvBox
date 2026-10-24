"""Qt download status and save-review failure text, without user data."""

from importlib.util import find_spec
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch


_QT_AVAILABLE = find_spec("PySide6") is not None
if _QT_AVAILABLE:
    from PySide6.QtCore import QCoreApplication
    from luvbox.qt_download import DownloadController


class _Events:
    def __init__(self):
        self.job_id = 7
        self.pending = []
        self.stopped = 0

    def start(self, options):
        return True

    def drain(self, limit=1):
        if not self.pending:
            return []
        return [self.pending.pop(0)]

    def stop(self):
        self.stopped += 1

    def close(self):
        return True

    def resolve_review(self, ident, action):
        return True

    def set_review_enabled(self, enabled):
        return None


@unittest.skipUnless(_QT_AVAILABLE, "optional Qt dependencies are not installed")
class DownloadStatusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QCoreApplication.instance() or QCoreApplication([])

    def controller(self):
        service = _Events()
        controller = DownloadController(service=service)
        self.addCleanup(controller.shutdown)
        started = controller.start(object())
        self.assertTrue(started)
        controller._timer.stop()
        return controller, service

    def test_idle_poll_does_not_notify_the_window(self):
        controller, _service = self.controller()
        notices = []
        controller.changed.connect(lambda: notices.append(True))
        controller._pump()
        self.assertEqual(notices, [])
        self.assertTrue(controller.running)

    def test_other_job_events_are_ignored(self):
        controller, service = self.controller()
        service.pending.append({"type": "saved", "job_id": 1})
        notices = []
        controller.changed.connect(lambda: notices.append(True))
        controller._pump()
        self.assertEqual(controller.savedCount, 0)
        self.assertEqual(notices, [])

    def test_finished_summary_stays_visible_after_a_clean_exit(self):
        controller, service = self.controller()
        service.pending.extend((
            {"type": "finished", "status": "finished", "job_id": 7, "saved": 2, "skipped": 1,
             "message": "새 Like 확인을 마쳤습니다. 2개를 저장했습니다."},
            {"type": "process_exit", "job_id": 7, "code": 0},
        ))
        controller._pump()
        self.assertEqual(controller.status, "저장했습니다")
        self.assertEqual(controller.notice, "새 Like 확인을 마쳤습니다. 2개를 저장했습니다.")
        self.assertEqual(controller.savedCount, 2)
        self.assertEqual(controller.skippedCount, 1)
        self.assertFalse(controller.running)
        self.assertEqual(controller.error, "")

    def test_metadata_note_follows_the_summary(self):
        controller, service = self.controller()
        service.pending.append({
            "type": "finished", "status": "finished", "job_id": 7, "saved": 1,
            "metadata_failed": 1, "message": "저장을 마쳤습니다.",
        })
        controller._pump()
        self.assertTrue(controller.notice.startswith("저장을 마쳤습니다. "))
        self.assertIn("작성자·출처 기록", controller.notice)

    def test_error_hides_non_text_worker_details(self):
        controller, service = self.controller()
        service.pending.append({"type": "finished", "status": "error", "job_id": 7,
                                 "message": {"token": "secret"}})
        controller._pump()
        self.assertEqual(controller.error, "저장 작업이 중단됐습니다.")
        self.assertNotIn("secret", controller.error)
        self.assertNotIn("token", controller.error)


@unittest.skipUnless(_QT_AVAILABLE, "optional Qt dependencies are not installed")
class ReviewFailureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QCoreApplication.instance() or QCoreApplication([])

    def wait_for(self, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.application.processEvents()
            if predicate():
                return
            time.sleep(0.01)
        self.fail("Qt controller did not settle")

    def test_unreadable_preview_reports_the_file_change_not_folder_permission(self):
        with tempfile.TemporaryDirectory(prefix="luvbox-qt-review-") as folder, \
                patch.dict(os.environ, {"LOCALAPPDATA": folder}), \
                patch("luvbox.qt_schedule.task_status", return_value={"registered": False}):
            from luvbox.qt_app import ApplicationController
            controller = ApplicationController()
            try:
                missing = Path(folder) / "missing.jpg"
                controller._commands.put(("review", {
                    "id": "preview-1", "path": str(missing), "signature": [1, 2, 3, 4],
                    "media_type": "photo",
                }))
                self.wait_for(lambda: controller.error != "")
                self.assertEqual(controller.error, "저장 확인 중 파일이 바뀌었습니다.")
                self.assertNotIn("권한", controller.error)
                self.assertNotIn(str(missing), controller.error)
            finally:
                controller.requestClose()
                self.wait_for(lambda: controller.shutdownReady)

    def test_review_request_with_an_ntfs_file_id_above_int64_reaches_the_window(self):
        # NTFS file IDs carry a sequence number in their top bits and can exceed
        # the signed 64-bit range; such a review must not vanish in transit.
        with tempfile.TemporaryDirectory(prefix="luvbox-qt-review-") as folder, \
                patch.dict(os.environ, {"LOCALAPPDATA": folder}), \
                patch("luvbox.qt_schedule.task_status", return_value={"registered": False}):
            from luvbox.qt_app import ApplicationController
            controller = ApplicationController()
            try:
                large_id = 13300401179940104648
                event = {"type": "review-request", "id": "preview-2", "job_id": 1, "media_type": "photo",
                         "path": str(Path(folder) / "missing.jpg"),
                         "signature": [8444249301665953, large_id, 676, 1792857244290150500]}
                received = []
                controller.download.eventReceived.connect(received.append)
                controller.download.eventReceived.emit(event)
                controller.download.reviewRequested.emit(event)
                self.assertEqual(received[0]["signature"][1], large_id)
                # The window checks the file; this one is gone, so it reports that.
                self.wait_for(lambda: controller.error != "")
                self.assertEqual(controller.error, "저장 확인 중 파일이 바뀌었습니다.")
            finally:
                controller.requestClose()
                self.wait_for(lambda: controller.shutdownReady)

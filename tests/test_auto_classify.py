"""Newly saved photos get their AI category without being asked.

The run that starts on its own is quiet when there is nothing to do, waits for
a running job, and never downloads the model: it only works once the model is
on this PC, which the first classification the owner starts takes care of.
"""

from contextlib import closing
from importlib.util import find_spec
import os
from pathlib import Path
import tempfile
from threading import Event
import time
import unittest
from unittest.mock import Mock, patch

from tests import app_data_isolation  # noqa: F401  Never open the user's app data.
from luvbox import clip_classifier, clip_model, scheduled
from luvbox.app_settings import default_settings
from luvbox.core import JobOptions
from luvbox.duplicates import ScanCancelled
from luvbox.history import HistoryStore
from luvbox.storage import resolve_storage

_QT = find_spec("PySide6") is not None
if _QT:
    from PySide6.QtCore import QCoreApplication
    from tests.test_qt_preview_navigation import PagedRepository
    from luvbox import qt_app
    from luvbox.qt_library import LibraryController, LibraryRepository, _Request, _store_labels

ACCOUNT = "offline"


class ModelFileTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="luvbox-auto-model-")
        self.addCleanup(folder.cleanup)
        environment = patch.dict(os.environ, {"LOCALAPPDATA": folder.name})
        environment.start()
        self.addCleanup(environment.stop)
        self.target = Path(folder.name) / "LuvBox" / "models" / "clip" / clip_model.MODEL_FILENAME

    def test_no_file_means_no_model(self):
        self.assertEqual(clip_model.default_cache_dir() / clip_model.MODEL_FILENAME, self.target)
        self.assertFalse(clip_model.model_file_present())

    def test_only_a_file_of_the_expected_size_counts(self):
        self.target.parent.mkdir(parents=True)
        with patch.object(clip_model, "MODEL_SIZE", 12):
            self.target.write_bytes(b"x" * 11)
            self.assertFalse(clip_model.model_file_present())
            self.target.write_bytes(b"x" * 12)
            self.assertTrue(clip_model.model_file_present())
        # Against the real size the small file is no model.
        self.assertFalse(clip_model.model_file_present())

    def test_the_classifier_uses_the_same_place_and_size(self):
        self.assertIs(clip_classifier.default_cache_dir, clip_model.default_cache_dir)
        self.assertIs(clip_classifier.bundled_model_path, clip_model.bundled_model_path)
        self.assertEqual((clip_classifier.MODEL_FILENAME, clip_classifier.MODEL_SIZE),
                         (clip_model.MODEL_FILENAME, clip_model.MODEL_SIZE))
        self.assertEqual(clip_classifier.resolve_model_path(), self.target)


class SettingTests(unittest.TestCase):
    def test_it_is_on_unless_switched_off(self):
        self.assertIs(default_settings()["auto_classify"], True)

    @unittest.skipUnless(_QT, "Qt dependencies are not installed")
    def test_the_switch_is_a_screen_preference_a_running_save_does_not_lock(self):
        self.assertIn("auto_classify", qt_app._BOOLEANS)
        self.assertIn("auto_classify", qt_app._VIEW_PREFERENCES)
        self.assertIn("auto_classify", qt_app._PUBLIC_PREFERENCES)


@unittest.skipUnless(_QT, "Qt dependencies are not installed")
class PendingTests(unittest.TestCase):
    """What the look before an automatic run counts as work, on a real catalog."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="luvbox-auto-pending-")
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        environment = patch.dict(os.environ, {"LOCALAPPDATA": str(self.root / "appdata")})
        environment.start()
        self.addCleanup(environment.stop)
        self.paths = resolve_storage(self.root / "Likes", ACCOUNT)
        self.files = {}
        with closing(HistoryStore.for_storage(self.paths)) as store:
            for tweet in ("101", "102"):
                path = self.paths.output_dir / f"writer_{tweet}_1.jpg"
                path.write_bytes(b"photo " + tweet.encode())
                self.files[tweet] = path
                store.record_media("flat", {"tweet_id": tweet, "num": 1, "type": "photo",
                                            "author": {"name": "writer"},
                                            "date": "2026-09-01T12:00:00+09:00"}, path)
        self.repository = LibraryRepository()
        self.addCleanup(self.repository.configure, None)
        self.repository.configure(_Request(generation=1, account=ACCOUNT, output_dir=str(self.paths.output_dir),
                                           separate_accounts=False))
        self.model = patch("luvbox.clip_model.model_file_present", return_value=True)
        self.model.start()
        self.addCleanup(self.model.stop)

    def store(self):
        return closing(HistoryStore.for_storage(self.paths, create_media=False))

    def classify(self, store, tweet, label="일러스트"):
        identity = ("flat", tweet, 1, "photo")
        store.save_embeddings([(*identity, b"\0" * 2048, 0.9)])
        if label is not None:
            _store_labels(store, [(*identity, label)])

    def test_a_photo_without_a_category_is_work(self):
        self.assertTrue(self.repository.auto_ai_pending(1))

    def test_without_the_model_nothing_is_work(self):
        self.model.stop()
        try:
            self.assertFalse(self.repository.auto_ai_pending(1))
        finally:
            self.model.start()

    def test_classified_photos_are_no_work(self):
        with self.store() as store:
            self.classify(store, "101")
            self.classify(store, "102")
        self.assertFalse(self.repository.auto_ai_pending(1))

    def test_a_cleared_category_is_work_again(self):
        # The embedding is kept when tags are cleared; scoring it again needs no picture.
        with self.store() as store:
            self.classify(store, "101")
            self.classify(store, "102", label=None)
        self.assertTrue(self.repository.auto_ai_pending(1))

    def test_a_missing_file_and_an_undecodable_photo_are_no_work(self):
        with self.store() as store:
            self.classify(store, "101")
            info = self.files["102"].stat()
            store.remember_unreadable([("flat", "102", 1, "photo", info.st_size, info.st_mtime_ns)])
        self.assertFalse(self.repository.auto_ai_pending(1))
        # The file changed: it is tried again.
        self.files["102"].write_bytes(b"another picture, another size")
        self.assertTrue(self.repository.auto_ai_pending(1))
        self.files["102"].unlink()
        self.assertFalse(self.repository.auto_ai_pending(1))

    def test_another_library_or_a_closed_one_is_no_work(self):
        self.assertFalse(self.repository.auto_ai_pending(2))
        self.repository.configure(None)
        self.assertFalse(self.repository.auto_ai_pending(1))

    def test_an_automatic_run_never_downloads_the_model(self):
        # The file check says "present", but the classifier finds no usable model (as after a damaged download).
        with patch("luvbox.clip_classifier.ensure_model", side_effect=AssertionError("must not download")) as fetch, \
                patch("luvbox.clip_classifier.is_available", return_value=False), \
                patch("luvbox.clip_classifier.get_session", side_effect=AssertionError("no model to load")):
            self.assertEqual(self.repository.scan_ai_tags(1, download=False), 0)
        fetch.assert_not_called()
        with self.store() as store:
            self.assertEqual(len(store.photos_without_embeddings()), 2)

    def test_a_run_the_owner_starts_still_downloads_it(self):
        with patch("luvbox.clip_classifier.ensure_model", side_effect=OSError("offline")) as fetch, \
                patch("luvbox.clip_classifier.is_available", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "AI 분류 모델을 준비하지 못했습니다"):
                self.repository.scan_ai_tags(1)
        fetch.assert_called_once()


if _QT:
    class AutoRepository(PagedRepository):
        """A gallery reader that records the looks and runs the controller asks for."""

        def __init__(self):
            super().__init__()
            self.pending = False
            self.looks = 0
            self.scans = []                       # one entry per run: was a download allowed
            self.result = 3
            self.gate = Event()
            self.gate.set()
            self.duplicates_gate = Event()
            self.duplicates_gate.set()
            self.duplicates_started = Event()

        def catalog_epoch(self):
            return 1

        def same_catalog(self, epoch):
            return self.current() is not None

        def auto_ai_pending(self, generation):
            self.looks += 1
            return self.pending

        def scan_ai_tags(self, generation, progress_callback=None, status_callback=None, epoch=None, download=True):
            self.scans.append(download)
            if progress_callback:
                progress_callback(2, 2)
            self.gate.wait(5)
            self.pending = False
            return self.result

        def scan_duplicates(self, generation, progress_callback=None, epoch=None):
            self.duplicates_started.set()
            self.duplicates_gate.wait(5)
            return 0

        def duplicate_counts(self, generation=None):
            return 0


@unittest.skipUnless(_QT, "Qt dependencies are not installed")
class ControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def setUp(self):
        self.repository = AutoRepository()
        replacement = patch("luvbox.qt_library.LibraryRepository", return_value=self.repository)
        replacement.start()
        self.addCleanup(replacement.stop)
        self.library = LibraryController()
        self.library._auto_ai_timer.setInterval(0)
        self.addCleanup(self.close)

    def open(self):
        self.library.setLibrary(ACCOUNT, str(Path(tempfile.gettempdir()) / "synthetic-auto"), False)
        self.wait(lambda: not self.library.model.busy and self.library.model.count > 0)

    def wait(self, predicate, label="the gallery did not settle"):
        until = time.monotonic() + 5
        while time.monotonic() < until:
            self.app.processEvents()
            if predicate():
                return
            time.sleep(.002)
        self.fail(label)

    def settle(self, milliseconds=60):
        until = time.monotonic() + milliseconds / 1000
        while time.monotonic() < until:
            self.app.processEvents()
            time.sleep(.002)

    def close(self):
        self.repository.gate.set()
        self.repository.duplicates_gate.set()
        self.library.shutdown()
        self.wait(lambda: self.library.shutdownComplete)

    def test_opening_a_library_looks_once_and_stays_quiet_when_nothing_is_new(self):
        self.open()
        self.wait(lambda: self.repository.looks == 1 and not self.library._auto_ai_wanted, "no look at opening")
        self.settle()
        self.assertEqual(self.repository.scans, [])
        self.assertFalse(self.library.aiScanning)
        self.assertEqual(self.library.status, "")
        # The same library again (a reload of the settings) is not another reason to look.
        self.library.setLibrary(ACCOUNT, str(Path(tempfile.gettempdir()) / "synthetic-auto"), False)
        self.settle()
        self.assertEqual(self.repository.looks, 1)

    def test_new_photos_are_classified_without_a_download_and_reported(self):
        self.repository.pending = True
        self.repository.gate.clear()
        self.open()
        self.wait(lambda: self.repository.scans == [False], "the automatic run did not start")
        self.wait(lambda: self.library.aiScanning and self.library.busy, "the run is not shown as running")
        self.repository.gate.set()
        self.wait(lambda: not self.library.busy and "새로 저장한 사진을 AI로 분류했습니다" in self.library.status,
                  "no result line")
        self.assertIn("2장을 확인했고, 3장에 태그를 적용했습니다", self.library.status)
        self.assertFalse(self.library.aiScanning)
        self.assertEqual(self.repository.scans, [False])

    def test_a_finished_save_looks_again_and_a_run_without_new_tags_says_nothing(self):
        self.open()
        self.wait(lambda: self.repository.looks == 1 and not self.library._auto_ai_wanted)
        self.repository.pending = True
        self.repository.result = 0
        self.library.classifyNewPhotos()
        self.wait(lambda: self.repository.scans == [False] and not self.library.busy, "no run after the save")
        self.settle()
        self.assertEqual(self.library.status, "")
        self.assertEqual(self.repository.looks, 2)

    def test_switched_off_nothing_looks_and_switching_on_looks(self):
        self.library.setAutoClassify(False)
        self.repository.pending = True
        self.open()
        self.library.classifyNewPhotos()
        self.settle()
        self.assertEqual((self.repository.looks, self.repository.scans), (0, []))
        self.library.setAutoClassify(True)
        self.wait(lambda: self.repository.scans == [False] and not self.library.busy, "switching on did not look")

    def test_it_waits_for_a_running_job(self):
        self.open()
        self.wait(lambda: self.repository.looks == 1 and not self.library._auto_ai_wanted)
        self.repository.duplicates_gate.clear()
        self.assertTrue(self.library.scanDuplicates())
        self.assertTrue(self.repository.duplicates_started.wait(2))
        self.repository.pending = True
        self.library.classifyNewPhotos()
        self.settle(120)
        self.assertEqual(self.repository.scans, [])            # the duplicate scan still runs
        self.assertTrue(self.library.copiesLocked)
        self.repository.duplicates_gate.set()
        self.wait(lambda: self.repository.scans == [False] and not self.library.busy, "no run after the scan ended")

    def test_a_reload_does_not_lose_the_look(self):
        self.repository.pending = True
        self.open()
        self.library.refresh()
        self.library.refresh()
        self.wait(lambda: self.repository.scans == [False] and not self.library.busy, "the look was lost in a reload")

    def test_a_run_the_owner_starts_is_unchanged(self):
        self.open()
        self.wait(lambda: self.repository.looks == 1 and not self.library._auto_ai_wanted)
        self.assertTrue(self.library.scanAiTags())
        self.wait(lambda: self.repository.scans == [True] and not self.library.busy)
        self.assertIn("AI 자동분류 완료", self.library.status)


class ScheduledTests(unittest.TestCase):
    """An unattended save classifies what it saved, and never fails because of it."""

    def setUp(self):
        self.lines = []
        self.options = JobOptions("offline", str(Path(tempfile.gettempdir()) / "synthetic-auto"),
                                  separate_accounts=True)

    def repository(self, **scan):
        repository = Mock()
        repository._ai_cancel = Event()
        repository.scan_ai_tags = Mock(**scan)
        return repository

    @unittest.skipUnless(_QT, "Qt dependencies are not installed")
    def test_what_was_saved_is_classified_with_the_model_already_here(self):
        repository = self.repository(return_value=4)
        with patch("luvbox.clip_model.model_file_present", return_value=True), \
                patch("luvbox.qt_library.LibraryRepository", return_value=repository):
            scheduled._classify_saved(self.options, self.lines.append)
        repository.scan_ai_tags.assert_called_once_with(1, download=False)
        request = repository.configure.call_args_list[0].args[0]
        self.assertEqual((request.account, request.output_dir, request.separate_accounts),
                         ("offline", self.options.output_dir, True))
        self.assertIsNone(repository.configure.call_args_list[-1].args[0])
        self.assertEqual(self.lines, ["AI 분류 · 새 사진 4장에 분류를 붙였습니다"])
        self.assertFalse(repository._ai_cancel.is_set())

    @unittest.skipUnless(_QT, "Qt dependencies are not installed")
    def test_without_the_model_nothing_is_loaded_or_written(self):
        with patch("luvbox.clip_model.model_file_present", return_value=False), \
                patch("luvbox.qt_library.LibraryRepository", side_effect=AssertionError("not needed")) as made:
            scheduled._classify_saved(self.options, self.lines.append)
        made.assert_not_called()
        self.assertEqual(self.lines, [])

    @unittest.skipUnless(_QT, "Qt dependencies are not installed")
    def test_a_failure_or_a_long_run_is_a_line_in_the_log_not_an_error(self):
        for raised, expected in ((OSError("disk"), "AI 분류를 마치지 못했습니다. 저장한 파일에는 영향이 없습니다."),
                                 (ScanCancelled("stopped"), "AI 분류 · 오래 걸려 멈췄습니다. 남은 사진은 앱을 열면 이어서 분류합니다.")):
            self.lines.clear()
            repository = self.repository(side_effect=raised)
            with patch("luvbox.clip_model.model_file_present", return_value=True), \
                    patch("luvbox.qt_library.LibraryRepository", return_value=repository):
                scheduled._classify_saved(self.options, self.lines.append)
            self.assertEqual(self.lines, [expected])
            self.assertIsNone(repository.configure.call_args_list[-1].args[0])

    @unittest.skipUnless(_QT, "Qt dependencies are not installed")
    def test_a_long_run_is_stopped_after_its_time(self):
        repository = self.repository()
        repository.scan_ai_tags.side_effect = lambda *_args, **_kwargs: (
            repository._ai_cancel.wait(5) and (_ for _ in ()).throw(ScanCancelled("stopped")))
        with patch("luvbox.clip_model.model_file_present", return_value=True), \
                patch("luvbox.qt_library.LibraryRepository", return_value=repository), \
                patch.object(scheduled, "_CLASSIFY_SECONDS", 0.05):
            scheduled._classify_saved(self.options, self.lines.append)
        self.assertTrue(repository._ai_cancel.is_set())
        self.assertIn("오래 걸려 멈췄습니다", self.lines[0])


class ScheduledRunTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.output = self.root / "사진 저장"
        self.settings = {"account": "Offline_Test", "output_dir": str(self.output), "folder_mode": "flat",
                         "include_videos": False, "include_gifs": True, "max_video_mb": 50,
                         "separate_accounts": False, "scan_mode": "quick"}
        for target, value in (("luvbox.scheduled.log_directory", self.root / "logs"),
                              ("luvbox.scheduled.last_run_path", self.root / "last-scheduled.json"),
                              ("luvbox.activity.activity_path", self.root / "activity.sqlite3")):
            patcher = patch(target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        notify = patch("luvbox.scheduled._notify_failure")
        notify.start()
        self.addCleanup(notify.stop)

    def run_saved(self, settings, saved=1, code=0):
        def worker(options, emit, stop_file=None):
            emit({"type": "finished", "status": "finished" if code == 0 else "error", "saved": saved, "skipped": 0,
                  "size_skipped": 0, "message": "끝", "retry_at": ""})
            return code

        with patch("luvbox.scheduled.load_settings", return_value=settings), \
                patch("luvbox.core.run_worker", side_effect=worker), \
                patch("luvbox.scheduled._classify_saved") as classify:
            result = scheduled.run_saved()
        return result, classify

    def test_a_run_that_saved_something_classifies_it(self):
        code, classify = self.run_saved(self.settings)
        self.assertEqual(code, 0)
        classify.assert_called_once()
        options, write = classify.call_args.args
        self.assertEqual(options.account, "offline_test")
        self.assertTrue(callable(write))

    def test_nothing_saved_a_failed_run_or_the_switch_off_classifies_nothing(self):
        for settings, saved, code in ((self.settings, 0, 0), (self.settings, 3, 1),
                                      ({**self.settings, "auto_classify": False}, 3, 0)):
            _result, classify = self.run_saved(settings, saved=saved, code=code)
            classify.assert_not_called()

    def test_the_log_line_of_the_classification_is_in_the_run_log(self):
        def worker(options, emit, stop_file=None):
            emit({"type": "finished", "status": "finished", "saved": 2, "skipped": 0, "size_skipped": 0,
                  "message": "끝", "retry_at": ""})
            return 0

        with patch("luvbox.scheduled.load_settings", return_value=self.settings), \
                patch("luvbox.core.run_worker", side_effect=worker), \
                patch("luvbox.scheduled._classify_saved",
                      side_effect=lambda _options, write: write("AI 분류 · 새 사진 2장에 분류를 붙였습니다")):
            self.assertEqual(scheduled.run_saved(), 0)
        log = sorted((self.root / "logs").glob("scheduled-*.log"))[-1].read_text(encoding="utf-8")
        self.assertIn("AI 분류 · 새 사진 2장에 분류를 붙였습니다", log)
        self.assertLess(log.index("종료 ·"), log.index("AI 분류 ·"))


if __name__ == "__main__":
    unittest.main()

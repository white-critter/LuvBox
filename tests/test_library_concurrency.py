"""Folder-wide scans stay serialized without blocking gallery reads."""

from importlib.util import find_spec
from pathlib import Path
import tempfile
from threading import Event
import time
import unittest
from unittest.mock import patch

_QT = find_spec("PySide6") is not None
if _QT:
    from PySide6.QtCore import QCoreApplication
    from luvbox.preview import Thumbnail
    from luvbox.qt_library import LibraryController, LibraryRepository, _Request
    from luvbox.qt_management import ManagementController


class _BlockingCatalog:
    """Stand-in catalog whose AI scan waits until the test releases it."""

    def __init__(self):
        self.epoch = 1
        self.request = object()
        self.started = Event()
        self.release = Event()
        self.overview_calls = 0

    def catalog_epoch(self):
        return self.epoch

    def same_catalog(self, epoch):
        return self.request is not None and epoch == self.epoch

    def configure(self, request):
        self.request = request

    def current(self, generation=None):
        return self.request

    def read(self, token, offset, limit):
        return 0, []

    def decode(self, path, size):
        return Thumbnail(None, "")

    def overview(self, generation=None):
        self.overview_calls += 1
        return {"files": 4, "posts": 1, "bytes": 10}

    def scan_ai_tags(self, generation, progress_callback=None, status_callback=None, epoch=None):
        self.started.set()
        self.release.wait(5)
        return 2


@unittest.skipUnless(_QT, "Qt dependencies are not installed")
class CatalogEpochTests(unittest.TestCase):
    def test_filter_reload_keeps_the_same_catalog_epoch(self):
        repo = LibraryRepository()
        repo.configure(_Request(1, "offline", "C:/library", False))
        epoch = repo.catalog_epoch()
        repo.configure(_Request(2, "offline", "C:/library", False, query="cat"))
        self.assertEqual(repo.catalog_epoch(), epoch)
        self.assertIsNotNone(repo._catalog_request(1, epoch))
        repo.configure(_Request(3, "other", "C:/library", False))
        self.assertNotEqual(repo.catalog_epoch(), epoch)
        self.assertIsNone(repo._catalog_request(2, epoch))


@unittest.skipUnless(_QT, "Qt dependencies are not installed")
class LibraryScanOverlapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def setUp(self):
        self.repo = _BlockingCatalog()
        self._patch = patch("luvbox.qt_library.LibraryRepository", return_value=self.repo)
        self._patch.start()
        self.library = LibraryController()
        self.library.setLibrary("offline", str(Path(tempfile.gettempdir()) / "concurrency"), False)
        self.stats = []
        self.library.statisticsLoaded.connect(self.stats.append)
        self.management = ManagementController(self.library)
        self.addCleanup(self.close)

    def wait(self, predicate):
        until = time.monotonic() + 4
        while time.monotonic() < until:
            self.app.processEvents()
            if predicate():
                return
            time.sleep(0.002)
        self.fail("library action did not settle")

    def close(self):
        self.repo.release.set()
        self.library.shutdown()
        self.management.shutdown()
        # Drain both workers before the next test creates another controller.
        # Leaving the management thread alive, then pumping events, deleted a
        # Qt object that a queued signal still referenced.
        self.wait(lambda: self.library.shutdownComplete and not self.management._worker.is_alive())
        self.app.processEvents()
        self._patch.stop()

    def test_scan_allows_browsing_reads_and_blocks_conflicting_edits(self):
        self.assertTrue(self.library.scanAiTags())
        self.assertTrue(self.repo.started.wait(3))
        self.assertTrue(self.library.tagsLocked)
        self.assertTrue(self.library.filesLocked)
        self.assertFalse(self.library.copiesLocked)

        self.library.requestStatistics()
        self.wait(lambda: self.stats and self.repo.overview_calls == 1)
        self.assertEqual(self.stats[-1]["files"], 4)
        self.assertFalse(self.repo.release.is_set())

        self.library.search("wind")
        self.assertTrue(self.library.tagsLocked)
        self.assertIn("AI 자동분류", self.library.status)
        self.assertFalse(self.library.addSelectedTag("풍경"))
        self.assertIn("태그", self.library.status)
        self.assertFalse(self.library.scanDuplicates())
        self.assertIn("끝난 뒤", self.library.status)

        self.management.prepareDelete(["g1_flat_1_1_photo"])
        self.assertIn("휴지통", self.management.error)
        self.assertFalse(self.management.mutating)

        self.repo.epoch += 1
        self.repo.release.set()
        self.wait(lambda: not self.library.tagsLocked and not self.library.busy)
        self.assertIn("저장 폴더", self.library.status)
        self.assertTrue(self.library.scanAiTags())
        self.wait(lambda: not self.library.busy)
        self.assertFalse(self.library.tagsLocked)

    def test_ai_progress_reaches_the_library_while_the_scan_is_running(self):
        def scanning(generation, progress_callback=None, status_callback=None, epoch=None):
            self.repo.started.set()
            if progress_callback:
                progress_callback(16, 20)
            self.repo.release.wait(5)
            if progress_callback:
                progress_callback(20, 20)
            return 16

        self.repo.scan_ai_tags = scanning
        self.assertTrue(self.library.scanAiTags())
        self.wait(lambda: self.library.aiProgressCurrent == 16 and self.library.aiProgressTotal == 20)
        self.assertIn("16", self.library.status)
        self.assertIn("20", self.library.status)
        self.assertTrue(self.library.busy)
        self.repo.release.set()
        self.wait(lambda: not self.library.busy)
        self.assertIn("16", self.library.status)
        self.assertIn("태그를 적용", self.library.status)

    def test_a_finished_scan_keeps_the_list_unless_its_filter_depends_on_the_result(self):
        generation = self.library._generation
        self.assertTrue(self.library.scanAiTags())
        self.assertTrue(self.repo.started.wait(3))
        # A scan is not the opened item loading: the preview keeps showing it.
        self.assertTrue(self.library.busy)
        self.assertFalse(self.library.selectionBusy)
        self.repo.release.set()
        self.wait(lambda: not self.library.busy)
        self.assertEqual(self.library._generation, generation)
        self.assertIn("태그를 적용", self.library.status)

        self.repo.started.clear()
        self.repo.release.clear()
        self.library._ai_category = "인물"
        self.assertTrue(self.library.scanAiTags())
        self.assertTrue(self.repo.started.wait(3))
        self.repo.release.set()
        self.wait(lambda: not self.library.busy)
        self.assertEqual(self.library._generation, generation + 1)
        self.assertIn("태그를 적용", self.library.status)

    def test_a_finished_duplicate_scan_reloads_only_the_duplicate_view(self):
        self.repo.scan_duplicates = lambda generation, progress_callback=None, epoch=None: 3
        generation = self.library._generation
        self.assertTrue(self.library.scanDuplicates())
        self.wait(lambda: not self.library.busy)
        self.assertEqual((self.library._generation, self.library.duplicateCount), (generation, 3))
        self.assertIn("3", self.library.status)
        self.library._duplicate_only = True
        self.assertTrue(self.library.scanDuplicates())
        self.wait(lambda: not self.library.busy)
        self.assertEqual(self.library._generation, generation + 1)

    def test_teaching_refuses_tag_edits_until_it_finishes(self):
        self.library._exclusive = "teach"
        self.assertTrue(self.library.tagsLocked)
        self.assertFalse(self.library.addSelectedTag("풍경"))
        self.assertIn("태그", self.library.status)
        self.library._exclusive = ""

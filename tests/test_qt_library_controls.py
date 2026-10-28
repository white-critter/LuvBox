"""Filters and multi-selection retain only the current displayed gallery."""

from pathlib import Path
from importlib.util import find_spec
import tempfile
import time
import unittest
from unittest.mock import patch

_QT = find_spec("PySide6") is not None
if _QT:
    from PySide6.QtCore import QCoreApplication
    from tests.test_qt_preview_navigation import PagedRepository
    from luvbox.qt_library import LibraryController


@unittest.skipUnless(_QT, "Qt dependencies are not installed")
class LibraryControlsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def setUp(self):
        self.repository = PagedRepository()
        replacement = patch("luvbox.qt_library.LibraryRepository", return_value=self.repository)
        replacement.start()
        self.addCleanup(replacement.stop)
        self.library = LibraryController()
        self.library.model._batch_size = 2
        self.library.setLibrary("offline", str(Path(tempfile.gettempdir()) / "synthetic-controls"), False)
        self.addCleanup(self.close)
        self.wait(lambda: not self.library.model.busy and self.library.model.count == 2)

    def wait(self, predicate):
        until = time.monotonic() + 4
        while time.monotonic() < until:
            self.app.processEvents()
            if predicate():
                return
            time.sleep(.002)
        self.fail("Gallery control did not settle")

    def close(self):
        self.library.shutdown()
        self.wait(lambda: self.library.shutdownComplete)

    def test_selection_is_loaded_only_and_cleared_when_filters_or_account_change(self):
        keys = [row["key"] for row in self.library.model._rows]
        self.library.toggleSelection(keys[0])
        self.assertEqual((self.library.selectionMode, self.library.selectedCount), (True, 1))
        self.library.selectLoaded()
        self.assertEqual(self.library.selectedKeys, keys)
        self.assertEqual(self.library.model.count, 2)
        exposed = self.library.selectedKeys
        exposed.clear()
        self.assertEqual(self.library.selectedCount, 2)
        self.library.toggleSelection(keys[0])
        self.assertEqual(self.library.selectedKeys, [keys[1]])
        self.library.search("102")
        self.assertEqual(self.library.selectedKeys, [])
        self.wait(lambda: not self.library.model.busy)
        self.library.toggleSelection(keys[1])
        self.assertEqual(self.library.selectedCount, 0)
        self.library.selectLoaded()
        self.assertEqual(self.library.selectedCount, 1)
        self.library.setLibrary("other", str(Path(tempfile.gettempdir()) / "other-controls"), False)
        self.assertEqual(self.library.selectedKeys, [])

    def test_select_loaded_keeps_present_files_when_an_item_is_missing(self):
        keys = [row["key"] for row in self.library.model._rows]
        self.repository.missing.add(keys[0].split("_")[2])
        self.assertFalse(self.library.canSelect(keys[0]))
        self.library.toggleSelection(keys[0])
        self.assertEqual(self.library.selectedCount, 0)
        self.library.selectLoaded()
        self.assertEqual(self.library.selectedKeys, [keys[1]])
        self.assertIn("제외", self.library.status)

    def test_author_dates_are_validated_before_changing_the_current_request(self):
        self.assertTrue(self.library.setAdvancedFilters(" @ALICE ", "saved", "2026-10-01", "2026-10-27"))
        self.assertEqual((self.library.filterAuthor, self.library.dateField,
                          self.library.dateFrom, self.library.dateTo),
                         ("alice", "saved", "2026-10-01", "2026-10-27"))
        self.assertIn("작성자 @alice", self.library.filterSummary)
        self.assertIn("저장일", self.library.filterSummary)
        request = self.repository.current()
        for author, field, first, last in (("alice2", "liked", "", ""),
                                           ("alice", "posted", "2026-02-30", ""),
                                           ("alice", "posted", "2026-11-01", "2026-10-01"),
                                           ("ali ce", "posted", "", ""),
                                           ("alice", "posted", "20261001", ""),
                                           ("alice", "posted", "", "9999-12-31")):
            with self.subTest(author=author, field=field, first=first, last=last):
                self.assertFalse(self.library.setAdvancedFilters(author, field, first, last))
                self.assertIs(self.repository.current(), request)
        self.library.setFilters("photo", "posted_oldest", self.library.filterAuthor)
        self.assertEqual(self.repository.current().date_from, "2026-10-01")
        self.library.resetFilters()
        request = self.repository.current()
        self.assertEqual((request.author, request.date_from, request.date_to, request.media_type, request.order),
                         ("", "", "", "all", "saved"))
        self.assertEqual(self.library.filterSummary, "")

    def test_exiting_selection_mode_clears_only_the_batch_selection(self):
        key = self.library.model._rows[0]["key"]
        self.library.selectItem(key)
        self.wait(lambda: not self.library.busy)
        self.library.toggleSelection(key)
        self.library.setSelectionMode(False)
        self.assertEqual(self.library.selectedKeys, [])
        self.assertEqual(self.library.selectedKey, key)
        self.assertFalse(self.library.selectionMode)


if __name__ == "__main__":
    unittest.main()

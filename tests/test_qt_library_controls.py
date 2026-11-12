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

    def test_a_page_load_does_not_republish_authors_and_tiles_check_the_selection_set(self):
        self.wait(lambda: self.library._authors_loaded)
        author_updates = []
        page_updates = []
        self.library.authorsChanged.connect(lambda: author_updates.append(1))
        self.library.changed.connect(lambda: page_updates.append(1))
        self.library.model.changed.emit()
        self.app.processEvents()
        self.assertEqual(author_updates, [])
        self.assertGreaterEqual(len(page_updates), 1)
        keys = [row["key"] for row in self.library.model._rows]
        self.library.toggleSelection(keys[0])
        self.assertEqual(self.library.selectionRevision, 1)
        self.assertTrue(self.library.isSelected(keys[0]))
        self.assertFalse(self.library.isSelected(keys[1]))
        copied = self.library.selectedKeys
        copied.append("not-a-real-key")
        self.assertFalse(self.library.isSelected("not-a-real-key"))
        self.assertEqual(self.library.selectedCount, 1)

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

    def test_duplicate_selection_counts_only_new_loaded_items_and_respects_capacity(self):
        keys = [row["key"] for row in self.library.model._rows]
        self.library.toggleSelection(keys[0])
        self.repository.duplicate_copy_keys = lambda generation: [*keys, "unloaded"]
        self.repository.selectable_keys = lambda candidates: (list(candidates), 0, 0)
        with patch("luvbox.qt_library.MAX_SELECTION", 1):
            self.assertEqual(self.library.selectDuplicateCopies(), 0)
            self.assertEqual(self.library.selectedKeys, [keys[0]])
        self.assertEqual(self.library.selectDuplicateCopies(), 1)
        self.assertEqual(set(self.library.selectedKeys), set(keys))
        self.assertEqual(self.library.selectDuplicateCopies(), 0)

    def test_empty_gallery_does_not_select_unloaded_duplicate_copies(self):
        self.library.model._rows.clear()
        self.library.model._keys.clear()
        self.repository.duplicate_copy_keys = lambda generation: ["unloaded"]
        self.repository.selectable_keys = lambda candidates: (list(candidates), 0, 0)
        self.assertEqual(self.library.selectDuplicateCopies(), 0)
        self.assertEqual(self.library.selectedCount, 0)

    def test_failed_favorite_write_preserves_displayed_state(self):
        key = self.library.model._rows[0]["key"]
        self.library.selectItem(key)
        self.wait(lambda: not self.library.busy)
        self.library._selected["favorite"] = True
        self.library.model.setFavorite(key, True)
        self.repository.toggle_favorite = lambda key, generation: None
        self.assertFalse(self.library.toggleSelectedFavorite())
        self.assertTrue(self.library._selected["favorite"])
        self.assertTrue(self.library.model._rows[0]["favorite"])
        self.assertIn("저장하지 못했습니다", self.library.status)
        self.repository.toggle_favorite = lambda key, generation: False
        self.assertFalse(self.library.toggleSelectedFavorite())
        self.assertFalse(self.library._selected["favorite"])
        self.assertFalse(self.library.model._rows[0]["favorite"])
        self.assertNotIn("저장하지 못했습니다", self.library.status)


    def test_thumbnail_decoders_follow_the_cores_within_two_to_four(self):
        from luvbox.qt_library import _thumbnail_workers
        for cores, expected in ((None, 2), (1, 2), (4, 2), (6, 3), (8, 4), (64, 4)):
            with self.subTest(cores=cores), patch("luvbox.qt_library.os.cpu_count", return_value=cores):
                self.assertEqual(_thumbnail_workers(), expected)
        self.assertIn(self.library.provider._worker_count, (2, 3, 4))

    def test_favorite_notice_reports_a_failed_save_instead_of_a_removal(self):
        key = self.library.model._rows[0]["key"]
        self.repository.toggle_favorite = lambda key, generation: True
        self.assertEqual(self.library.favoriteNotice(key), "즐겨찾기에 추가했습니다.")
        self.assertTrue(self.library.model._rows[0]["favorite"])
        self.repository.toggle_favorite = lambda key, generation: None
        self.assertIn("저장하지 못했습니다", self.library.favoriteNotice(key))
        self.assertTrue(self.library.model._rows[0]["favorite"])
        self.repository.toggle_favorite = lambda key, generation: False
        self.assertEqual(self.library.favoriteNotice(key), "즐겨찾기에서 제외했습니다.")
        self.assertFalse(self.library.model._rows[0]["favorite"])
        self.assertEqual(self.library.favoriteNotice(""), "")

    def test_teaching_from_the_preview_keeps_the_opened_photo(self):
        key = self.library.model._rows[0]["key"]
        self.library.selectItem(key)
        self.wait(lambda: not self.library.busy and self.library.selectedKey == key)
        generation = self.library._generation
        seen = []
        original = self.repository.selected

        def reread(key, generation):
            seen.append(key)
            return dict(original(key, generation), ai_category="인물", ai_confirmed=True)

        self.repository.selected = reread
        self.repository.teach_labels = lambda generation, idents, category, epoch=None, progress_callback=None: (1, 0)
        self.assertTrue(self.library.teachSelected("인물"))
        self.wait(lambda: not self.library.busy and self.library.selectedAiConfirmed)
        self.assertEqual((self.library._generation, self.library.selectedKey), (generation, key))
        self.assertEqual(seen, [key])
        self.assertIn("분류를 저장했습니다", self.library.status)
        self.assertEqual(self.library.model.count, 2)

    def test_clear_selection_resets_selected_keys_and_status(self):
        keys = [row["key"] for row in self.library.model._rows]
        self.library.selectLoaded()
        self.assertGreater(self.library.selectedCount, 0)
        self.library.clearSelection()
        self.assertEqual(self.library.selectedCount, 0)
        self.assertEqual(self.library.selectedKeys, [])

    def test_cancel_ai_scan_sets_cancel_event_and_updates_status(self):
        self.library._exclusive = "ai"
        self.library._ai_scanning = True
        self.library.cancelAiScan()
        self.assertTrue(self.repository._ai_cancel.is_set())
        self.assertIn("중단", self.library.status)

    def test_toggle_favorite_filter(self):
        self.assertFalse(self.library.filterFavoriteOnly)
        self.library.setFavoriteOnly(True)
        self.assertTrue(self.library.filterFavoriteOnly)
        self.library.setFavoriteOnly(False)
        self.assertFalse(self.library.filterFavoriteOnly)


if __name__ == "__main__":
    unittest.main()

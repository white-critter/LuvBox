"""Filters and multi-selection retain only the current displayed gallery."""

from pathlib import Path
from importlib.util import find_spec
import tempfile
import time
import unittest
from unittest.mock import patch
try:
    from tests import app_data_isolation  # noqa: F401  (never this PC's real app data)
except ModuleNotFoundError:      # run as a script: the tests folder is the path
    import app_data_isolation  # noqa: F401

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
        self.library._duplicate_only = True        # the duplicate view is on
        with patch("luvbox.qt_library.MAX_SELECTION", 1):
            self.assertEqual(self.library.selectDuplicateCopies(), 0)
            self.assertEqual(self.library.selectedKeys, [keys[0]])
        self.assertEqual(self.library.selectDuplicateCopies(), 1)
        self.assertEqual(set(self.library.selectedKeys), set(keys))
        # A copy further down the list is not picked, and the person is told how to get it.
        self.assertIn("불러오지 않은 사본 1장은 아래로 내린 뒤 한 번 더 누르면 고릅니다", self.library.status)
        self.assertEqual(self.library.selectDuplicateCopies(), 0)

    def test_empty_gallery_does_not_select_unloaded_duplicate_copies(self):
        self.library.model._rows.clear()
        self.library.model._keys.clear()
        self.repository.duplicate_copy_keys = lambda generation: ["unloaded"]
        self.repository.selectable_keys = lambda candidates: (list(candidates), 0, 0)
        self.library._duplicate_only = True
        self.assertEqual(self.library.selectDuplicateCopies(), 0)
        self.assertEqual(self.library.selectedCount, 0)

    def test_counts_dropped_by_an_early_filter_change_are_asked_for_again(self):
        """A filter changed right after opening made the first answers stale, and nothing asked again."""
        from threading import Event

        held, calls = Event(), []

        def counts(generation=None):
            calls.append(generation)
            if len(calls) == 1:
                held.wait(5)                  # the first answer is still on its way when the filter changes
            return [{"category": "인물", "name": "인물", "count": 3}]

        self.repository.ai_counts = counts
        self.library.setLibrary("offline", str(Path(tempfile.gettempdir()) / "synthetic-controls-2"), False)
        self.wait(lambda: len(calls) == 1)
        self.library.search("x")              # a new listing: the answer above belongs to the old one
        held.set()
        self.wait(lambda: len(calls) >= 2 and self.library._counts_loaded)
        self.assertEqual([item["count"] for item in self.library.aiCategoryCounts], [3])

    def test_a_drag_adds_to_the_selection_only_while_selecting_was_switched_on(self):
        """With "여러 개 선택" on, a click that moved a little must not throw away what was picked."""
        first, second = (row["key"] for row in self.library.model._rows)

        def drag_over_the_second_tile():
            self.library.beginBandSelection(False)          # no Ctrl held
            self.library.bandSelect(0, 0, 1, 1, 2)          # row 0, column 1 of two columns
            self.library.endBandSelection()

        self.library.setSelectionMode(True)
        self.library.toggleSelection(first)
        drag_over_the_second_tile()
        self.assertEqual(set(self.library.selectedKeys), {first, second})
        # A selection that began by picking a tile is replaced by a plain drag, as in a file manager.
        self.library.setSelectionMode(False)
        self.library.toggleSelection(first)
        self.assertFalse(self.library._selection_explicit)
        drag_over_the_second_tile()
        self.assertEqual(self.library.selectedKeys, [second])

    def test_copies_are_selected_only_while_the_duplicate_view_is_on(self):
        """The button is hidden in the ordinary gallery, but a key could still reach it (it kept the focus)."""
        keys = [row["key"] for row in self.library.model._rows]
        self.repository.duplicate_copy_keys = lambda generation: list(keys)
        self.repository.selectable_keys = lambda candidates: (list(candidates), 0, 0)
        self.assertFalse(self.library._duplicate_only)
        self.assertEqual(self.library.selectDuplicateCopies(), 0)
        self.assertEqual((self.library.selectedKeys, self.library.selectionMode), ([], False))

    def test_a_shift_range_starts_at_the_highlight_before_its_details_arrive(self):
        first, second = (row["key"] for row in self.library.model._rows)
        self.repository.block_selection = first
        try:
            self.library.selectItem(first)
            self.wait(lambda: self.repository.selection_entered.is_set())
            self.assertEqual(self.library._selection_anchor, "")
            self.assertEqual(self.library.selectedKey, "")
            self.assertEqual(self.library.selectRangeTo(second), 2)
            self.assertEqual(self.library.selectedKeys, [first, second])
        finally:
            self.repository.selection_release.set()

    def test_forgetting_the_opened_photo_leaves_nothing_opened_and_the_batch_as_it_was(self):
        first, second = (row["key"] for row in self.library.model._rows)
        self.library.selectItem(first)
        self.wait(lambda: not self.library.busy)
        self.library.toggleSelection(second)
        self.assertEqual(self.library.selectedKey, first)
        self.library.forgetOpened()
        self.assertEqual((self.library.selectedKey, self.library._selection_key, self.library.busy), ("", "", False))
        self.assertEqual(self.library.selectedKeys, [second])
        # With nothing opened, the favourite key has no photo to act on.
        self.assertFalse(self.library.toggleSelectedFavorite())
        # Forgetting while a photo is still loading drops that request too.
        self.library.selectItem(first)
        self.library.forgetOpened()
        self.wait(lambda: not self.library.busy)
        self.app.processEvents()
        self.assertEqual(self.library.selectedKey, "")

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

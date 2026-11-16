"""Editing a filter's marks must not hide the last page behind stale rows."""
from contextlib import closing
from pathlib import Path
import tempfile
import time
import unittest

from tests import app_data_isolation  # noqa: F401
from PySide6.QtCore import QCoreApplication
from luvbox.history import HistoryStore
from luvbox.qt_library import LibraryController
from luvbox.storage import resolve_storage


class GalleryFilterEditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="luvbox-filter-edit-")
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name)
        self.paths = resolve_storage(self.output, "offline")
        with closing(HistoryStore.for_storage(self.paths)) as store:
            for number in range(101, 108):
                path = self.output / f"offline_{number}_1.jpg"
                path.write_bytes(b"fixture")
                store.record_media("flat", {"tweet_id": str(number), "num": 1,
                                           "type": "photo", "author": "offline"}, path)
                store.toggle_favorite("flat", str(number), 1, "photo")
                store.add_tag("flat", str(number), 1, "photo", "여행")
                store.add_tag("flat", str(number), 1, "photo", "인물", source="ai")
        self.library = LibraryController()
        self.library.model._batch_size = 3
        self.library.setAutoClassify(False)
        self.addCleanup(self.close)
        self.library.setLibrary("offline", str(self.output), False)
        self.wait(lambda: not self.library.model.busy and self.library.model.count == 3)

    def wait(self, predicate):
        until = time.monotonic() + 5
        while time.monotonic() < until:
            self.app.processEvents()
            if predicate():
                return
            time.sleep(.003)
        self.fail("gallery did not settle")

    def close(self):
        self.library.shutdown()
        self.wait(lambda: self.library.shutdownComplete)

    def open_first(self):
        self.wait(lambda: not self.library.model.busy and self.library.model.count == 3)
        key = self.library.model._rows[0]["key"]
        self.library.selectItem(key)
        self.wait(lambda: not self.library.selectionBusy and self.library.selectedKey == key)
        return key.split("_")[2]

    def assert_remaining(self, removed):
        self.wait(lambda: not self.library.model.busy)
        while self.library.model.canFetchMore():
            self.library.model.loadMore()
            self.wait(lambda: not self.library.model.busy)
        actual = {row["key"].split("_")[2] for row in self.library.model._rows}
        self.assertEqual(actual, {str(n) for n in range(101, 108)} - {removed})
        self.assertEqual(self.library.model.total, 6)
        self.assertEqual(len(list(self.output.glob("*.jpg"))), 7)

    def test_unfavorite_updates_the_filtered_list_and_preserves_the_last_page(self):
        self.library.setFavoriteOnly(True)
        removed = self.open_first()
        self.assertFalse(self.library.toggleSelectedFavorite())
        self.assert_remaining(removed)

    def test_removing_the_active_user_tag_updates_the_list_and_last_page(self):
        self.library.setUserTag("여행")
        removed = self.open_first()
        self.assertTrue(self.library.removeSelectedTag("여행"))
        self.assert_remaining(removed)

    def test_replacing_an_ai_label_with_a_user_tag_updates_the_ai_list(self):
        self.library.setAiFilter("인물", "")
        removed = self.open_first()
        self.assertTrue(self.library.addSelectedTag("인물"))
        self.assert_remaining(removed)

    def test_removing_an_ai_label_updates_the_ai_list(self):
        self.library.setAiFilter("인물", "")
        removed = self.open_first()
        self.assertTrue(self.library.removeSelectedTag("인물"))
        self.assert_remaining(removed)

    def test_unrelated_tag_edit_preserves_open_photo_and_selection(self):
        self.library.setUserTag("여행")
        self.open_first()
        key = self.library.selectedKey
        self.library.toggleSelection(key)
        self.assertTrue(self.library.addSelectedTag("동물"))
        self.assertTrue(self.library.removeSelectedTag("동물"))
        self.assertEqual(self.library.selectedKey, key)
        self.assertEqual(self.library.selectedKeys, [key])
        self.assertEqual(self.library.model.total, 7)


if __name__ == "__main__":
    unittest.main()

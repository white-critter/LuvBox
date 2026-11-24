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

    def assert_remaining(self, removed, total=6):
        self.wait(lambda: not self.library.model.busy)
        while self.library.model.canFetchMore():
            self.library.model.loadMore()
            self.wait(lambda: not self.library.model.busy)
        actual = {row["key"].split("_")[2] for row in self.library.model._rows}
        self.assertEqual(actual, {str(n) for n in range(101, 108)} - {removed})
        self.assertEqual(self.library.model.total, total)
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

    def test_typing_an_ai_tags_name_keeps_the_photo_in_its_ai_list(self):
        """The AI list of a tag shows the photos the person tagged that way too (one list per tag)."""
        self.library.setAiFilter("인물", "")
        kept = self.open_first()
        resets = []
        self.library.model.modelReset.connect(lambda: resets.append(1))
        self.assertTrue(self.library.addSelectedTag("인물"))
        self.assert_remaining(None, total=7)
        self.assertEqual(resets, [])
        self.assertIn(kept, {row["key"].split("_")[2] for row in self.library.model._rows})

    def test_removing_an_ai_label_updates_the_ai_list(self):
        self.library.setAiFilter("인물", "")
        removed = self.open_first()
        self.assertTrue(self.library.removeSelectedTag("인물"))
        self.assert_remaining(removed)

    def test_an_edit_that_takes_a_photo_out_of_the_filter_keeps_the_place_in_the_list(self):
        """1.10.5 read the list again: back to the top, the preview closed. Now only that tile goes."""
        edits = (("favourite", lambda: self.library.setFavoriteOnly(True),
                  lambda: self.library.toggleSelectedFavorite()),
                 ("user tag", lambda: self.library.setUserTag("여행"),
                  lambda: self.library.removeSelectedTag("여행")),
                 ("ai label", lambda: self.library.setAiFilter("인물", ""),
                  lambda: self.library.removeSelectedTag("인물")))
        resets = []
        self.library.model.modelReset.connect(lambda: resets.append(1))
        for name, choose, edit in edits:
            with self.subTest(filter=name):
                choose()
                self.wait(lambda: not self.library.model.busy and self.library.model.count == 3)
                self.library.model.loadMore()                      # the person has scrolled on: six tiles
                self.wait(lambda: not self.library.model.busy and self.library.model.count == 6)
                rows = [row["key"] for row in self.library.model._rows]
                self.library.selectItem(rows[4])                    # the fifth photo is open
                self.wait(lambda: not self.library.selectionBusy and self.library.selectedKey == rows[4])
                before = len(resets)
                edit()
                self.wait(lambda: not self.library.selectionBusy and self.library.selectedKey == rows[5])
                self.assertEqual(len(resets), before)               # the list was not read again
                self.assertEqual([row["key"] for row in self.library.model._rows],
                                 rows[:4] + rows[5:])
                self.assert_remaining(rows[4].split("_")[2])
                # Put the mark back for the next filter.
                with closing(HistoryStore.for_storage(self.paths)) as store:
                    identity = ("flat", rows[4].split("_")[2], 1, "photo")
                    if name == "favourite":
                        store.toggle_favorite(*identity)
                    elif name == "user tag":
                        store.add_tag(*identity, "여행")
                    else:
                        store.add_tag(*identity, "인물", source="ai")
                self.library.setFavoriteOnly(False)
                self.library.setUserTag("")
                self.library.setAiFilter("", "")
                self.wait(lambda: not self.library.model.busy)

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

    def test_tag_filters_and_edits_normalize_leading_hash_and_control_chars(self):
        self.open_first()
        self.assertTrue(self.library.addSelectedTag("# \x07밤하늘 "))
        self.library.setUserTag("# 밤하늘")
        self.wait(lambda: not self.library.model.busy and self.library.model.count == 1)
        self.assertEqual(self.library.filterUserTag, "밤하늘")
        self.library.selectLoaded()
        self.assertEqual(self.library.addTagToSelection("#\x08여름"), 1)
        self.library.setUserTag("# 여름")
        self.wait(lambda: not self.library.model.busy and self.library.model.count == 1)
        key = self.library.model._rows[0]["key"]
        self.library.selectItem(key)
        self.wait(lambda: not self.library.selectionBusy and self.library.selectedKey == key)
        self.assertTrue(self.library.removeSelectedTag("#\x07여름"))
        self.wait(lambda: not self.library.model.busy and self.library.model.total == 0)

    def test_removing_user_tag_preserves_taught_confirmed_category_and_ai_sentinel(self):
        with closing(HistoryStore.for_storage(self.paths)) as store:
            store.set_taught_categories([("flat", "101", 1, "photo")], "인물")
            self.assertTrue(store.add_tag("flat", "101", 1, "photo", "인물", source="user"))
            self.assertTrue(store.remove_tag("flat", "101", 1, "photo", "인물"))
            taught = store.classification_of("flat", "101", 1, "photo")
            self.assertEqual((taught["category"], taught["confirmed"]), ("인물", True))
            self.assertIn({"tag": "인물", "source": "confirmed"}, store.item_tags("flat", "101", 1, "photo"))

            # Taking away a tag the person typed is not a "not this" (it may have been a typo).
            # The AI had looked at this photo (its 인물 row became the typed tag), so it stays
            # looked at instead of getting the AI's guess straight back on the next run.
            store.save_embeddings([("flat", "102", 1, "photo", b"\x00" * (768 * 4), 0.9)])
            self.assertTrue(store.add_tag("flat", "102", 1, "photo", "인물", source="user"))
            self.assertTrue(store.remove_tag("flat", "102", 1, "photo", "인물"))
            self.assertEqual(store.tag_feedback([("flat", "102", 1, "photo")]), {})
            self.assertEqual(store.item_tags("flat", "102", 1, "photo"), [{"tag": "여행", "source": "user"}])
            self.assertEqual(store.count_embeddings_without_labels(), 0)

    def test_cancelled_partial_ai_scan_settles_active_ai_filter(self):
        self.library.setAiFilter("풍경·배경", "")
        self.wait(lambda: not self.library.model.busy and self.library.model.total == 0)

        def partial_scan(generation, progress_callback=None, status_callback=None, epoch=None, download=True):
            from luvbox.duplicates import ScanCancelled
            with closing(HistoryStore.for_storage(self.paths)) as store:
                store.save_ai_tags([("flat", "101", 1, "photo", "풍경·배경")])
            self.library._repository._ai_wrote = True
            raise ScanCancelled("AI 자동분류를 중단했습니다. 이미 분류된 1장은 유지됩니다.")

        original = self.library._repository.scan_ai_tags
        self.library._repository.scan_ai_tags = partial_scan
        try:
            self.assertTrue(self.library.scanAiTags())
            self.wait(lambda: not self.library.aiScanning and "유지됩니다" in self.library.status)
            self.wait(lambda: not self.library.model.busy and self.library.model.total == 1)
        finally:
            self.library._repository.scan_ai_tags = original

    def test_near_duplicate_copies_only_allows_subcopy_deletion_with_near_keeper(self):
        with closing(HistoryStore.for_storage(self.paths)) as store:
            rows = store.records()
            p0 = next(r for r in rows if r["tweet_id"] == "103")
            p1 = next(r for r in rows if r["tweet_id"] == "102")
            p2 = next(r for r in rows if r["tweet_id"] == "101")
            for p, content in ((p0["path"], b"alpha" * 30), (p1["path"], b"beta" * 20), (p2["path"], b"beta" * 20)):
                Path(p).write_bytes(content)
            import hashlib
            h_a = hashlib.sha256(b"alpha" * 30).hexdigest()
            h_b = hashlib.sha256(b"beta" * 20).hexdigest()
            store.save_image_hashes([
                ("flat", "103", 1, "photo", 150, h_a, "0" * 16),
                ("flat", "102", 1, "photo", 80, h_b, "0" * 16),
                ("flat", "101", 1, "photo", 80, h_b, "0" * 16),
            ])
            store.set_duplicate_rule("near")
            store.save_clusters([[("flat", "103", 1, "photo"), ("flat", "102", 1, "photo"), ("flat", "101", 1, "photo")]])
        self.library.setDuplicateOnly(True)
        self.wait(lambda: not self.library.model.busy and self.library.model.count == 3)
        subcopy_key = next(r["key"] for r in self.library.model._rows if r["key"].endswith("_101_1_photo"))
        near_key = next(r["key"] for r in self.library.model._rows if r["key"].endswith("_102_1_photo"))
        manifest = self.library._repository.deletion_manifest([subcopy_key, near_key])
        self.assertEqual(manifest["kept"], 0)
        by_id = {m["tweet_id"]: m for m in manifest["rows"]}
        self.assertEqual(by_id["101"]["_copy_near"], False)
        self.assertIn(by_id["102"]["path"], by_id["101"]["_copy_keepers"])
        self.assertEqual(by_id["102"]["_copy_near"], True)


if __name__ == "__main__":
    unittest.main()

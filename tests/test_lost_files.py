"""Saved files that are gone (the gallery's "파일 없음" tiles) can be downloaded again or excluded.

Excluding takes the media out of the gallery and stops later saves from fetching it.
Nothing is deleted: the records stay, each keeps its last location, and the choice can
be released from the excluded list. A file that exists is never touched, and a media
folder that is missing altogether is not mistaken for every file having been deleted.
"""

from contextlib import closing
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from tests import app_data_isolation  # noqa: F401  Never open the user's app data.
from luvbox.history import HistoryStore, archive_key
from luvbox.storage import resolve_storage

from importlib.util import find_spec

_QT = find_spec("PySide6") is not None
if _QT:
    from PySide6.QtCore import QCoreApplication
    from luvbox.qt_records import MISSING, RecordsController

ACCOUNT = "offline"
PRESENT = ("101", "102")
GONE = ("201", "202", "203")          # 203 has a record in both layouts


class LostCatalog(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="luvbox-lost-")
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        environment = patch.dict(os.environ, {"LOCALAPPDATA": str(self.root / "appdata")})
        environment.start()
        self.addCleanup(environment.stop)
        self.paths = resolve_storage(self.root / "Likes", ACCOUNT)
        self.files = {}
        with closing(HistoryStore.for_storage(self.paths)) as store:
            for tweet in PRESENT + GONE:
                path = self.paths.output_dir / f"writer_{tweet}_1.jpg"
                path.write_bytes(b"picture " + tweet.encode())
                self.files[tweet] = path
                details = {"tweet_id": tweet, "num": 1, "type": "photo", "author": {"name": "writer"},
                           "date": "2026-09-01T12:00:00+09:00"}
                if tweet == "203":
                    store.record_media("author", details, path)
                    store.record_media("flat", details, path, status="existing")
                else:
                    store.record_media("flat", details, path)
            store.save_image_hashes([("flat", "201", 1, "photo", 11, "digest-201", "", "")])
        for tweet in GONE:                       # removed in Explorer, not through the app
            self.files[tweet].unlink()

    def store(self):
        return closing(HistoryStore.for_storage(self.paths, create_media=False))

    def identity(self, tweet):
        return (tweet, 1, "photo")

    def listed(self, store):
        return store.record_page(0, 100, include_deleted=False)["total"]


class StoreTests(LostCatalog):
    def test_lost_media_are_the_gallerys_missing_tiles(self):
        with self.store() as store:
            self.assertEqual(self.listed(store), 5)                 # two photos and three "파일 없음" tiles
            self.assertEqual(store.lost_count(), 3)
            page = store.lost_media()
            self.assertEqual((page["total"], page["pages"], page["page"]), (3, 1, 0))
            self.assertEqual(sorted(row["tweet_id"] for row in page["rows"]), list(GONE))
            self.assertEqual({row["author"] for row in page["rows"]}, {"writer"})
            self.assertEqual(sorted(store.lost_archive_keys()), sorted(archive_key(*self.identity(t)) for t in GONE))
            self.assertEqual(len(store.lost_archive_keys(2)), 2)
            self.assertEqual(store.lost_media(page=5, page_size=2)["page"], 1)
            for bad in ({"page": -1}, {"page_size": 0}, {"page_size": 201}, {"page": True}):
                with self.assertRaises(Exception):
                    store.lost_media(**bad)

    def test_excluding_takes_it_out_of_the_gallery_and_keeps_the_record(self):
        with self.store() as store:
            records = store._db.execute("SELECT COUNT(*) FROM media").fetchone()[0]
            self.assertEqual(store.exclude_lost([self.identity("201")]), 1)
            self.assertEqual(self.listed(store), 4)
            self.assertEqual(store.lost_count(), 2)
            self.assertIn(self.identity("201"), store.ignored_media())
            self.assertEqual(store.excluded_media()["total"], 1)
            # Nothing is deleted: the record is there, with where the file was and when it was noticed.
            self.assertEqual(store._db.execute("SELECT COUNT(*) FROM media").fetchone()[0], records)
            row = store._db.execute("SELECT path,last_path,missing_since,filesize FROM media WHERE tweet_id='201'").fetchone()
            self.assertEqual((row["path"], row["last_path"], row["filesize"]), ("", str(self.files["201"]), None))
            self.assertTrue(row["missing_since"])
            # A hash of a file that is gone no longer takes part in duplicate groups.
            self.assertIsNone(store._db.execute("SELECT 1 FROM image_hashes WHERE tweet_id='201'").fetchone())
            # Doing it again changes nothing.
            self.assertEqual(store.exclude_lost([self.identity("201")]), 0)

    def test_every_record_of_the_media_is_put_away(self):
        with self.store() as store:
            self.assertEqual(store.exclude_lost([self.identity("203")]), 1)
            rows = store._db.execute("SELECT folder_mode,path,last_path FROM media WHERE tweet_id='203'").fetchall()
            self.assertEqual(sorted(row["folder_mode"] for row in rows), ["author", "flat"])
            self.assertTrue(all(row["path"] == "" and row["last_path"] == str(self.files["203"]) for row in rows))
            self.assertEqual(self.listed(store), 4)

    def test_a_file_that_exists_is_never_touched(self):
        with self.store() as store:
            before = [tuple(row) for row in store._db.execute("SELECT * FROM media ORDER BY tweet_id,folder_mode")]
            self.assertEqual(store.exclude_lost([self.identity("101"), self.identity("999")]), 0)
            # A file that came back after the list was read is a present file again.
            self.files["202"].write_bytes(b"restored from the Recycle Bin")
            self.assertEqual(store.exclude_lost([self.identity("202")]), 0)
            self.assertEqual([tuple(row) for row in store._db.execute(
                "SELECT * FROM media ORDER BY tweet_id,folder_mode")], before)
            self.assertEqual(store.lost_count(), 2)
            for bad in ([("abc", 1, "photo")], [("201", 0, "photo")], [("201", 1, "sound")], [("201", True, "photo")]):
                with self.assertRaises(Exception):
                    store.exclude_lost(bad)

    def test_all_of_them_at_once_and_the_choice_can_be_released(self):
        with self.store() as store:
            self.assertEqual(store.exclude_lost(), 3)
            self.assertEqual((store.lost_count(), self.listed(store)), (0, 2))
            self.assertEqual(store.excluded_media()["total"], 3)
            self.assertEqual(store.exclude_lost(), 0)
            # Released from the excluded list, it is a "파일 없음" tile again, to download or exclude.
            self.assertEqual(store.release_excluded([self.identity("202")]), 1)
            self.assertEqual((store.lost_count(), self.listed(store)), (1, 3))
            self.assertEqual(store.lost_media()["rows"][0]["tweet_id"], "202")

    def test_a_missing_media_folder_is_not_every_file_deleted(self):
        moved = self.paths.output_dir.with_name("Likes moved")
        self.paths.output_dir.rename(moved)
        try:
            with closing(HistoryStore(self.paths.record_dir, media_dir=self.paths.media_dir,
                                      output_dir=self.paths.output_dir, legacy_dir=self.paths.legacy_dir)) as store:
                self.assertEqual(store.lost_count(), 0)
                self.assertEqual(store.lost_media()["total"], 0)
                self.assertEqual(store.exclude_lost(), 0)
                self.assertEqual(store.ignored_media(), set())
        finally:
            moved.rename(self.paths.output_dir)
        with self.store() as store:
            self.assertEqual(store.lost_count(), 3)

    def test_a_record_in_a_former_folder_keeps_its_place(self):
        with self.store() as store:
            real = store.is_download_path
            with patch.object(store, "is_download_path", side_effect=lambda path: "201" not in str(path) and real(path)):
                self.assertEqual(store.exclude_lost(), 2)
            row = store._db.execute("SELECT path FROM media WHERE tweet_id='201'").fetchone()
            self.assertEqual(row["path"], str(self.files["201"]))
            self.assertNotIn(self.identity("201"), store.ignored_media())

    def test_a_file_that_returns_to_its_place_is_connected_again(self):
        with self.store() as store:
            store.exclude_lost([self.identity("201")])
            self.files["201"].write_bytes(b"picture 201")
            store.import_legacy(refresh=True)
            row = store._db.execute("SELECT path,missing_since FROM media WHERE tweet_id='201'").fetchone()
            self.assertEqual((row["path"], row["missing_since"]), (str(self.files["201"]), ""))
            self.assertEqual(self.listed(store), 5)
            self.assertEqual(store.lost_count(), 2)


@unittest.skipUnless(_QT, "Qt dependencies are not installed")
class ControllerTests(LostCatalog):
    @classmethod
    def setUpClass(cls):
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def setUp(self):
        super().setUp()
        self.records = RecordsController()
        self.addCleanup(self.close)
        self.changes, self.retries = [], []
        self.records.catalogChanged.connect(lambda: self.changes.append(True))
        self.records.retryRequested.connect(self.retries.append)
        self.records.setLibrary(ACCOUNT, str(self.paths.output_dir), False)
        self.wait(lambda: self.records.missingCount == 3, "missing files were not counted")

    def close(self):
        self.records.shutdown()
        finished = []
        self.records.shutdownFinished.connect(lambda: finished.append(True))
        until = time.monotonic() + 5
        while not finished and time.monotonic() < until:
            self.app.processEvents()
            time.sleep(.005)

    def wait(self, predicate, label="the list did not settle"):
        until = time.monotonic() + 6
        while time.monotonic() < until:
            self.app.processEvents()
            if predicate():
                return
            time.sleep(.003)
        self.fail(label)

    def open(self, view="missing"):
        self.records.setActive("unsaved", True)
        self.records.setUnsavedView(view)
        self.wait(lambda: self.records.unsavedView == view and not self.records.unsavedBusy, "view did not load")

    def tokens(self):
        return {row["token"]: row for row in self.records.unsavedRows}

    def test_the_view_lists_the_missing_files(self):
        self.open()
        rows = self.records.unsavedRows
        self.assertEqual((len(rows), self.records.unsavedTotal, self.records.unsavedPages), (3, 3, 1))
        self.assertEqual({row["reason"] for row in rows}, {MISSING})
        self.assertEqual({row["reasonLabel"] for row in rows}, {"파일 없음"})
        self.assertTrue(all(row["canRetry"] and "폴더에 없습니다" in row["detail"] for row in rows))
        self.assertEqual({row["author"] for row in rows}, {"@writer"})
        self.assertEqual(self.records.retryableCount, 3)
        self.assertEqual((self.records.unsavedCount, self.records.excludedCount), (0, 0))

    def test_excluding_one_moves_it_to_the_excluded_list_and_tells_the_gallery(self):
        self.open()
        token = next(iter(self.tokens()))
        self.records.exclude(token)
        self.wait(lambda: self.records.missingCount == 2 and self.records.excludedCount == 1
                  and len(self.records.unsavedRows) == 2, "one file was not excluded")
        self.assertIn("1개를 갤러리에서 뺐습니다", self.records.unsavedStatus)
        self.assertEqual(self.changes, [True])
        with self.store() as store:
            self.assertEqual(self.listed(store), 4)
        # Released there, it comes back as a missing file and the gallery hears of it again.
        self.open("excluded")
        self.assertEqual(len(self.records.unsavedRows), 1)
        self.records.release(next(iter(self.tokens())))
        self.wait(lambda: self.records.missingCount == 3 and self.records.excludedCount == 0, "the release was lost")
        self.assertEqual(self.changes, [True, True])

    def test_chosen_tiles_are_put_away_or_downloaded_together(self):
        self.open()
        tokens = list(self.tokens())
        self.assertEqual(self.records.unsavedSelectedCount, 0)
        self.records.toggleUnsavedSelected(tokens[0])
        self.records.toggleUnsavedSelected(tokens[1])
        self.records.toggleUnsavedSelected("not a token")
        self.assertEqual((self.records.unsavedSelectedCount, sorted(self.records.unsavedSelection)),
                         (2, sorted(tokens[:2])))
        self.records.toggleUnsavedSelected(tokens[1])
        self.assertEqual(self.records.unsavedSelection, [tokens[0]])
        self.records.selectUnsavedPage()
        self.assertEqual(self.records.unsavedSelectedCount, 3)
        self.records.clearUnsavedSelection()
        self.assertEqual(self.records.unsavedSelectedCount, 0)
        # Download the two chosen ones: their keys go to the save, nothing else.
        chosen = [self.tokens()[token] for token in tokens[:2]]
        for token in tokens[:2]:
            self.records.toggleUnsavedSelected(token)
        self.records.retrySelected()
        self.assertEqual(len(self.retries), 1)
        self.assertEqual(len(self.retries[0]), 2)
        self.assertTrue(set(self.retries[0]) <= {archive_key(*self.identity(tweet)) for tweet in GONE})
        self.assertEqual(len(chosen), 2)
        # Exclude the two chosen ones; the selection ends with the page they were on.
        self.records.settleSelected()
        self.wait(lambda: self.records.missingCount == 1 and self.records.excludedCount == 2
                  and len(self.records.unsavedRows) == 1, "the chosen files were not excluded")
        self.assertEqual(self.records.unsavedSelectedCount, 0)
        self.assertIn("2개를 갤러리에서 뺐습니다", self.records.unsavedStatus)
        # In the excluded view the same action releases.
        self.open("excluded")
        self.records.selectUnsavedPage()
        self.records.settleSelected()
        self.wait(lambda: self.records.excludedCount == 0 and self.records.missingCount == 3, "the chosen were not released")

    def test_all_at_once(self):
        self.open()
        self.records.retryAll()
        self.wait(lambda: len(self.retries) == 1, "no download was asked for")
        self.assertEqual(sorted(self.retries[0]), sorted(archive_key(*self.identity(tweet)) for tweet in GONE))
        self.records.excludeAll()
        self.wait(lambda: self.records.missingCount == 0 and self.records.excludedCount == 3
                  and not self.records.unsavedRows, "not everything was excluded")
        self.assertIn("3개를 갤러리에서 뺐습니다", self.records.unsavedStatus)
        # With nothing left the action says so instead of pretending.
        self.records.excludeAll()
        self.wait(lambda: "뺄 항목이 없습니다" in self.records.unsavedStatus, "an empty exclusion gave no answer")

    def test_actions_of_another_view_do_nothing_here(self):
        self.open()
        token = next(iter(self.tokens()))
        self.records.dismiss(token)
        self.records.dismissAll()
        self.records.release(token)
        self.records.releaseAll()
        self.open("unsaved")
        self.records.excludeAll()
        self.open()
        self.assertEqual((self.records.missingCount, len(self.records.unsavedRows), self.changes), (3, 3, []))


if __name__ == "__main__":
    unittest.main()

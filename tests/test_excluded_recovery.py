"""A 'do not save again' choice stays in the catalog, and can be released there.

Deleting or trimming the failure ledger, or moving the save folder, must not
make that choice invisible. Releasing it clears the skip and the archive hit
that would block the next save, and leaves the catalog row and any saved file
alone.
"""
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path

from tests import app_data_isolation  # Never open this PC's real app data.
from luvbox.history import HistoryStore
from luvbox.qt_records import RecordsController
from luvbox.storage import migrate_records, resolve_storage
from luvbox import unsaved
from PySide6.QtCore import QCoreApplication
import time


def decline(paths, tweet="100", kind="photo"):
    store = HistoryStore.for_storage(paths)
    try:
        store.record_declined("flat", {"tweet_id": tweet, "num": 1, "type": kind, "author": {"name": "alice"}},
                              paths.media_dir / f"alice_{tweet}_1.jpg")
        archive = paths.record_dir / ".luvbox-archive.flat.sqlite3"
        with closing(sqlite3.connect(archive)) as database:
            database.execute("CREATE TABLE IF NOT EXISTS archive(entry TEXT PRIMARY KEY)")
            database.execute("INSERT OR IGNORE INTO archive(entry) VALUES (?)", (f"twitter{tweet}_1",))
            database.commit()
    finally:
        store.close()


def ignored(paths):
    store = HistoryStore(paths.record_dir, media_dir=paths.media_dir, output_dir=paths.output_dir,
                         legacy_dir=paths.legacy_dir)
    try:
        return store.ignored_media(), store.excluded_media()["total"]
    finally:
        store.close()


class ExcludedRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def setUp(self):
        import tempfile
        self.temp = tempfile.TemporaryDirectory(prefix="luvbox-excluded-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.output = self.root / "사진"
        self.output.mkdir()
        self.paths = resolve_storage(self.output, "alice")
        decline(self.paths)
        self.records = RecordsController()
        self.addCleanup(self.records.shutdown)

    def wait(self, predicate, seconds=8):
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            self.app.processEvents()
            if predicate():
                return
            time.sleep(0.005)
        self.fail("excluded list did not settle")

    def open_excluded(self):
        self.records.setLibrary("alice", str(self.output), False)
        self.records.setActive("unsaved", True)
        self.records.setUnsavedView("excluded")
        self.wait(lambda: not self.records.unsavedBusy and self.records.excludedCount == 1)

    def test_the_catalog_choice_survives_deleting_the_display_ledger(self):
        unsaved.record([{"tweet_id": "100", "num": 1, "media_type": "photo", "reason": unsaved.EXCLUDED,
                         "author": "alice", "detail": "저장 확인에서 ‘저장 안 함’으로 골랐습니다."}],
                       account="alice", output_dir=str(self.output))
        unsaved.remove("alice", str(self.output), items=[("100", 1, "photo")])
        self.open_excluded()
        self.assertEqual(self.records.unsavedRows[0]["detail"], "저장 확인에서 ‘저장 안 함’으로 골랐습니다.")
        self.assertEqual(ignored(self.paths)[0], {("100", 1, "photo")})

    def test_clearing_the_failure_list_does_not_release_the_skip(self):
        unsaved.record([{"tweet_id": "200", "num": 1, "media_type": "photo", "reason": unsaved.FAILED,
                         "author": "alice", "detail": "실패"}],
                       account="alice", output_dir=str(self.output))
        self.records.setLibrary("alice", str(self.output), False)
        self.records.setActive("unsaved", True)
        self.wait(lambda: not self.records.unsavedBusy and self.records.unsavedTotal == 1)
        self.records.dismissAll()
        self.wait(lambda: self.records.unsavedTotal == 0 and not self.records.unsavedBusy)
        self.assertEqual(self.records.excludedCount, 1)
        self.assertEqual(ignored(self.paths)[0], {("100", 1, "photo")})

    def test_release_clears_the_skip_and_archive_and_keeps_the_catalog_row(self):
        self.open_excluded()
        self.records.release(self.records.unsavedRows[0]["token"])
        self.wait(lambda: self.records.excludedCount == 0 and not self.records.unsavedBusy)
        self.assertIn("제외를 해제했습니다", self.records.unsavedStatus)
        skips, total = ignored(self.paths)
        self.assertEqual((skips, total), (set(), 0))
        store = HistoryStore(self.paths.record_dir, media_dir=self.paths.media_dir,
                             output_dir=self.paths.output_dir, legacy_dir=self.paths.legacy_dir)
        try:
            row = store.media_record("flat", {"tweet_id": "100", "num": 1, "type": "photo"})
        finally:
            store.close()
        self.assertEqual(row["record_source"], "declined")
        with closing(sqlite3.connect(self.paths.record_dir / ".luvbox-archive.flat.sqlite3")) as database:
            self.assertEqual(list(database.execute("SELECT entry FROM archive")), [])

    def test_another_account_keeps_its_own_skip(self):
        other_output = self.root / "다른"
        other_output.mkdir()
        other = resolve_storage(other_output, "bob")
        decline(other, "500")
        self.open_excluded()
        self.records.releaseAll()
        self.wait(lambda: self.records.excludedCount == 0 and not self.records.unsavedBusy)
        self.assertEqual(ignored(other)[0], {("500", 1, "photo")})
        self.assertEqual(ignored(self.paths)[0], set())

    def test_moving_the_save_folder_keeps_the_choice_visible(self):
        destination = resolve_storage(self.root / "옮긴 사진", "alice")
        destination.output_dir.mkdir()
        migrate_records(self.paths, destination)
        self.records.setLibrary("alice", str(destination.output_dir), False)
        self.records.setActive("unsaved", True)
        self.records.setUnsavedView("excluded")
        self.wait(lambda: not self.records.unsavedBusy and self.records.excludedCount == 1)
        self.assertIn("저장 안 함", self.records.unsavedRows[0]["detail"])
        # The move copies the catalog. Both folders still hold the same choice.
        self.assertEqual(ignored(self.paths)[0], {("100", 1, "photo")})
        self.assertEqual(ignored(destination)[0], {("100", 1, "photo")})


if __name__ == "__main__":
    unittest.main()

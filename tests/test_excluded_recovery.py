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
        self.addCleanup(self.close)

    def close(self):
        # Wait for the worker thread. A controller let go while its thread still runs is
        # destroyed on that thread, and the timer shutdown() started then outlives it in
        # the event loop: a later test crashed (access violation) while processing events.
        self.records.shutdown()
        self.wait(lambda: not self.records._worker.is_alive() and not self.records._shutdown_timer.isActive())

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


class ExcludedPageTests(unittest.TestCase):
    def test_one_page_keeps_the_newest_author_without_reading_the_whole_catalog(self):
        import tempfile
        import time

        folder = tempfile.TemporaryDirectory(prefix="luvbox-excluded-page-")
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        output = root / "사진"
        output.mkdir()
        paths = resolve_storage(output, "alice")
        store = HistoryStore.for_storage(paths)
        try:
            media = []
            choices = []
            for index in range(8000):
                tweet = str(10**18 + index)
                stamp = f"2024-{(index % 12) + 1:02d}-{(index % 27) + 1:02d}T12:00:00+09:00"
                media.append(("flat", tweet, 1, "photo", "crowd", tweet + ".jpg", stamp, "metadata", stamp,
                              str(paths.media_dir / (tweet + ".jpg")), 1000, "saved", "saved", stamp,
                              "https://x.com/i/web/status/" + tweet))
            for index in range(120):
                tweet = str(10**18 + index)
                stamp = f"2026-10-01T{index // 60:02d}:{index % 60:02d}:00+09:00"
                choices.append((tweet, 1, "photo", 1, stamp))
            store._db.executemany(
                "INSERT INTO media(folder_mode,tweet_id,num,media_type,author,filename,posted_at,"
                "posted_source,saved_at,path,filesize,status,record_source,updated_at,source_url) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", media)
            store._db.executemany(
                "INSERT INTO missing_choices(tweet_id,num,media_type,ignored,updated_at) VALUES (?,?,?,?,?)",
                choices)
            # A newer row in another layout must win the displayed author.
            store._db.execute(
                "INSERT INTO media(folder_mode,tweet_id,num,media_type,author,filename,posted_at,"
                "posted_source,saved_at,path,filesize,status,record_source,updated_at,source_url) "
                "VALUES ('date',?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (choices[-1][0], 1, "photo", "carol", "carol.jpg", "2026-10-02T00:00:00+09:00", "metadata",
                 None, "", None, "existing", "declined", "2026-11-01T00:00:00+09:00",
                 "https://x.com/i/web/status/" + choices[-1][0]))
            store._db.commit()
            query_started = time.perf_counter()
            page = store.excluded_media(page=0, page_size=50)
            later = store.excluded_media(page=2, page_size=50)
            elapsed = time.perf_counter() - query_started
            self.assertEqual((page["total"], page["pages"], len(page["rows"])), (120, 3, 50))
            self.assertEqual(page["rows"][0]["author"], "carol")
            self.assertIn("저장 안 함", page["rows"][0]["detail"])
            self.assertEqual(page["rows"][1]["author"], "crowd")
            self.assertNotIn("저장 안 함", page["rows"][1]["detail"])
            self.assertEqual(len(later["rows"]), 20)
            self.assertNotEqual(later["rows"][0]["tweet_id"], page["rows"][0]["tweet_id"])
            # The previous query scanned the catalog once per skip and took seconds at this size.
            self.assertLess(elapsed, 0.4, f"excluded pages took {elapsed:.3f}s")
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()

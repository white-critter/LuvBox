"""The media folder is moved or renamed in Explorer and chosen again at its new place.

Through the application's own steps (ApplicationController._prepare_save, then
the real worker with the offline fakes of tests.test_core). Until 1.10.7 the new
place started from the records copy inside the folder: every photo showed as
"파일 없음", no save connected them again, and favourites and tags made after
the last save stayed behind.
"""
from contextlib import closing
from dataclasses import replace
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from tests import app_data_isolation  # noqa: F401  (never this PC's real app data)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from tests import test_core
from luvbox import core
from luvbox.core import JobOptions
from luvbox.history import HistoryStore
from luvbox.qt_app import ApplicationController
from luvbox.qt_library import LibraryRepository, _Request
from luvbox.storage import migrate_records, resolve_storage

ROWS = [(str(1000 + index), 1, "photo", "alice", f"2026-09-{1 + index:02d}T12:00:00") for index in range(12)]


class _Provider:
    def register(self, key, path):
        pass


class MovedFolderTests(unittest.TestCase):
    setUp = test_core.CoreTests.setUp
    tearDown = test_core.CoreTests.tearDown
    set_session = test_core.CoreTests.set_session

    def save(self, options, rows=ROWS):
        test_core.FakeHttpDownloader.downloaded = []
        events = []
        with patch("gallery_dl.extractor.find", side_effect=lambda url: test_core.FakeLikesExtractor(url, rows)), \
                patch("gallery_dl.downloader.find", return_value=test_core.FakeHttpDownloader):
            code = core.run_worker(options, events.append)
        self.assertEqual(events[-1]["type"], "finished")
        return code, events[-1], len(test_core.FakeHttpDownloader.downloaded)

    def gallery(self, output):
        """(tiles, tiles with their file, tiles showing "파일 없음", favourites) as the gallery reads them."""
        repository = LibraryRepository()
        repository.provider = _Provider()
        repository.configure(_Request(generation=1, account="myaccount", output_dir=str(output),
                                      separate_accounts=False))
        try:
            total, rows = repository.read("1", 0, 500)
        finally:
            repository.configure(None)
        return (total, sum(1 for row in rows if not row["missing"]), sum(1 for row in rows if row["missing"]),
                sum(1 for row in rows if row["favorite"]))

    def store(self, output):
        return closing(HistoryStore.for_storage(resolve_storage(output, "myaccount", False), create_media=False))

    def chosen(self, output, consents):
        snapshot = {"account": "myaccount", "output_dir": str(output), "folder_mode": "flat", "scan_mode": "quick",
                    "include_videos": False, "include_gifs": False, "max_video_mb": 200, "separate_accounts": False,
                    "gif_format": "gif", "gif_size": "original", "confirm_each": False,
                    "record_locations": {"myaccount": str(self.output)}}
        return ApplicationController._prepare_save(snapshot, set(consents))

    def library(self):
        """Twelve saved photos, five of them recorded under two folder layouts with one path, as real libraries are."""
        options = JobOptions("@MyAccount", str(self.output), folder_mode="flat", include_videos=False,
                             include_gifs=False, separate_accounts=False)
        code, done, fetched = self.save(options)
        self.assertEqual((code, fetched), (0, 12), done.get("message"))
        with self.store(self.output) as store:
            for row in store.records()[:5]:
                store.record_media("author", {"tweet_id": row["tweet_id"], "num": row["num"], "type": "photo",
                                              "author": {"name": "alice"}}, row["path"], status="existing")
            store.toggle_favorite("flat", "1000", 1, "photo")
            store.toggle_favorite("flat", "1001", 1, "photo")
        # A later ordinary save writes the records copy into the folder: two favourites are in it.
        self.save(replace(options, scan_mode="quick"))
        self.assertEqual(self.gallery(self.output), (12, 12, 0, 2))
        return options

    def move(self):
        moved = self.base / "photos-moved"
        self.output.rename(moved)                       # what the person does in Explorer
        return moved

    def test_the_photos_stay_connected_and_what_was_marked_after_the_last_save_comes_along(self):
        self.library()
        # After the last save: a tag, one more favourite, and one favourite taken back.
        with self.store(self.output) as store:
            store.add_tag("flat", "1002", 1, "photo", "keep")
            store.toggle_favorite("flat", "1003", 1, "photo")
            store.toggle_favorite("flat", "1001", 1, "photo")
        moved = self.move()
        asked = self.chosen(moved, ())
        self.assertEqual(asked.get("choice"), "relocate")
        prepared = self.chosen(moved, {"relocate"})
        self.assertIn("options", prepared)
        # Every photo is there, once, with its file; the favourites are the two of this moment.
        self.assertEqual(self.gallery(moved), (12, 12, 0, 2))
        with self.store(moved) as store:
            rows = store.records()
            self.assertEqual(len(rows), 17)                               # 12 files, 5 of them twice
            self.assertTrue(all(Path(row["path"]).is_relative_to(moved) for row in rows))
            self.assertEqual([(row["tag"], row["count"]) for row in store.user_tags()], [("keep", 1)])
            favourites = {row["tweet_id"] for row in store.records() if store.is_favorite(
                row["folder_mode"], row["tweet_id"], row["num"], row["media_type"])}
            self.assertEqual(favourites, {"1000", "1003"})
            self.assertEqual(store.lost_count(), 0)
        # The next save has nothing to fetch again and changes nothing.
        code, done, fetched = self.save(prepared["options"])
        self.assertEqual((code, fetched, done["saved"]), (0, 0, 0), done.get("message"))
        self.assertEqual(self.gallery(moved), (12, 12, 0, 2))
        self.assertEqual(len(list(moved.glob("*.jpg"))), 12)

    def test_importing_the_snapshot_before_the_save_still_brings_later_marks(self):
        self.library()
        older = self.base / "earlier-location"
        older.mkdir()
        picture = older / "alice_99_1.jpg"
        picture.write_bytes(b"earlier photo")
        with self.store(older) as store:
            store.record_media("flat", {"tweet_id": "99", "num": 1, "type": "photo"}, picture)
            picture.unlink()
            store.import_legacy(refresh=True)
        migrate_records(resolve_storage(older, "myaccount"), resolve_storage(self.output, "myaccount"))
        with self.store(self.output) as store:
            store.add_tag("flat", "1002", 1, "photo", "keep")
            store.toggle_favorite("flat", "1003", 1, "photo")
            store.toggle_favorite("flat", "1001", 1, "photo")
        moved = self.move()
        with self.store(moved) as store:
            earlier = store.media_record("flat", {"tweet_id": "99", "num": 1})
            self.assertEqual((earlier["path"], Path(earlier["last_path"])), ("", picture.resolve()))
        prepared = self.chosen(moved, {"relocate"})
        self.assertIn("options", prepared)
        self.assertEqual(self.gallery(moved), (13, 12, 1, 2))
        with self.store(moved) as store:
            self.assertEqual([(row["tag"], row["count"]) for row in store.user_tags()], [("keep", 1)])
            favourites = {row["tweet_id"] for row in store.records() if store.is_favorite(
                row["folder_mode"], row["tweet_id"], row["num"], row["media_type"])}
            self.assertEqual(favourites, {"1000", "1003"})

    def test_marks_added_and_removed_in_both_locations_survive(self):
        options = self.library()
        with self.store(self.output) as store:
            self.assertTrue(store.add_tag("flat", "1005", 1, "photo", "remove-old"))
            self.assertTrue(store.add_tag("flat", "1006", 1, "photo", "remove-new"))
        self.save(replace(options, scan_mode="quick"))
        with self.store(self.output) as store:
            self.assertFalse(store.toggle_favorite("flat", "1001", 1, "photo"))
            self.assertTrue(store.toggle_favorite("flat", "1003", 1, "photo"))
            self.assertTrue(store.remove_tag("flat", "1005", 1, "photo", "remove-old"))
            self.assertTrue(store.add_tag("flat", "1002", 1, "photo", "old-side"))

        moved = self.move()
        with self.store(moved) as store:
            self.assertEqual(len(store.records()), 17)
        repository = LibraryRepository()
        repository.provider = _Provider()
        repository.configure(_Request(generation=1, account="myaccount", output_dir=str(moved),
                                      separate_accounts=False))
        self.assertFalse(repository.toggle_favorite("g1_flat_1000_1_photo", 1))
        self.assertTrue(repository.toggle_favorite("g1_flat_1004_1_photo", 1))
        self.assertTrue(repository.remove_tag("g1_flat_1006_1_photo", 1, "remove-new"))
        self.assertTrue(repository.add_tag("g1_flat_1004_1_photo", 1, "new-side"))

        self.assertIn("options", self.chosen(moved, {"relocate"}))
        self.assertEqual(self.gallery(moved), (12, 12, 0, 2))
        with self.store(moved) as store:
            self.assertEqual({row["tag"] for row in store.user_tags()}, {"old-side", "new-side"})
            favourites = {row["tweet_id"] for row in store.records() if store.is_favorite(
                row["folder_mode"], row["tweet_id"], row["num"], row["media_type"])}
            self.assertEqual(favourites, {"1003", "1004"})

    def test_a_file_that_is_not_at_the_new_place_keeps_its_old_record(self):
        self.library()
        moved = self.move()
        with self.store(self.output) as store:
            gone = next(row for row in store.records() if row["tweet_id"] == "1007")
        (moved / Path(gone["path"]).name).unlink()
        self.assertIn("options", self.chosen(moved, {"relocate"}))
        self.assertEqual(self.gallery(moved)[:3], (12, 11, 1))
        with self.store(moved) as store:
            left = [row for row in store.records() if not Path(row["path"]).is_relative_to(moved)]
        # Only that photo's records (it may have one per folder layout) still name the old place.
        self.assertEqual({(row["tweet_id"], row["path"]) for row in left}, {("1007", gone["path"])})

    def test_the_records_of_the_old_place_are_left_as_they_were(self):
        self.library()
        before = resolve_storage(self.output, "myaccount", False).record_dir
        names = sorted(path.name for path in before.iterdir())
        moved = self.move()
        self.chosen(moved, {"relocate"})
        self.assertEqual(sorted(path.name for path in before.iterdir()), names)
        with closing(HistoryStore(before, media_dir=self.output, output_dir=self.output)) as store:
            self.assertEqual(len(store.records()), 17)

    def test_a_place_that_already_has_its_own_records_keeps_them(self):
        """Choosing again a folder this PC already keeps records for must not replace those records."""
        self.library()
        other = self.base / "other-place"
        other.mkdir()
        with self.store(other) as store:
            picture = other / "bob_2000_1.jpg"
            picture.write_bytes(b"picture")
            store.record_media("flat", {"tweet_id": "2000", "num": 1, "type": "photo", "author": {"name": "bob"}},
                               picture)
        self.output.rename(self.base / "gone")
        self.assertIn("options", self.chosen(other, {"relocate"}))
        with self.store(other) as store:
            self.assertEqual([row["tweet_id"] for row in store.records()], ["2000"])


if __name__ == "__main__":
    unittest.main()

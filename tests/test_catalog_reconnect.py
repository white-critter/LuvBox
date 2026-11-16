"""A file that comes back, a file moved into another layout's folder, and a released skip.

A photo restored from the Recycle Bin, or a removed file put back where it was,
is connected again at the start of the next save. That used to happen only in
a whole-library rescan the app no longer runs, so the photo stayed out of the
gallery for good. A photo moved in Explorer into another layout's folder counts
once: the old layout's record of a file that is gone no longer stands for it.
Moved back, it counts once again from the start of the next save, and one of
two real copies deleted and put back is shown again beside the other.
And media released from the excluded list are downloaded by the next quick
save, which used to stop at a run of saved files long before older Likes. One
that X no longer serves is tried by that save only and then listed as failed,
so later quick saves do not walk down to it again.

Most of the owner's files have two records with the same path (flat and
author), so the catalogs here have that shape too. The save tests go through
the same worker the app runs, with the offline fakes of ``test_core``.
"""

import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from tests import app_data_isolation  # noqa: F401  (never this PC's real app data)
from tests import test_core
from luvbox import core, unsaved
from luvbox.history import HistoryStore
from luvbox.storage import resolve_storage


def details(tweet, author="alice"):
    return {"tweet_id": tweet, "num": 1, "type": "photo", "author": {"name": author}}


def signature(path):
    value = Path(path).stat()
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns


class Review:
    """Answers the save review: 'never' for the chosen tweets, 'save' for the rest."""

    def __init__(self, never):
        self.never = set(never)

    def enabled(self):
        return True

    def ask(self, path, media, check_stop):
        return "never" if str(media.get("tweet_id")) in self.never else "save"


class SkipReview(Review):
    """Answers 'skip' (this save only) for the chosen tweets, 'save' for the rest."""

    def ask(self, path, media, check_stop):
        return "skip" if str(media.get("tweet_id")) in self.never else "save"


class AskedReview(Review):
    """Answers 'save' to every file, and keeps the tweets it was asked about."""

    def __init__(self):
        super().__init__(())
        self.asked = []

    def ask(self, path, media, check_stop):
        self.asked.append(str(media.get("tweet_id")))
        return "save"


class GoneDownloader(test_core.FakeHttpDownloader):
    """A media X no longer serves (a 404 on an old Like): it fails every time, without a run-ending log."""
    gone = frozenset()
    tried = []

    def download(self, url, pathfmt):
        if any(f"/{tweet}_" in url for tweet in self.gone):
            self.tried.append(url)
            self.job.log.warning("HTTP 404 Not Found")
            return False
        return super().download(url, pathfmt)


class SaveTests(unittest.TestCase):
    setUp = test_core.CoreTests.setUp
    tearDown = test_core.CoreTests.tearDown
    set_session = test_core.CoreTests.set_session

    def save(self, rows, *, review=None, downloader=test_core.FakeHttpDownloader, **changes):
        test_core.FakeHttpDownloader.downloaded = []
        events = []
        with patch("gallery_dl.extractor.find", side_effect=lambda url: test_core.FakeLikesExtractor(url, rows)), \
                patch("gallery_dl.downloader.find", return_value=downloader):
            code = core.run_worker(replace(self.options, **changes), events.append, review=review)
        self.assertNotIn(test_core.SECRET, json.dumps(events))
        self.assertEqual(events[-1]["type"], "finished")
        self.assertEqual(code, 0, events[-1].get("message"))
        return events[-1]

    def store(self):
        return closing(HistoryStore.for_storage(resolve_storage(self.output, "myaccount", True)))

    @property
    def media(self):
        return self.output / "myaccount"

    def rows(self, store, tweet):
        return {row["folder_mode"]: row for row in store.records() if row["tweet_id"] == tweet}

    def gallery(self, store):
        return sorted(row["tweet_id"] for row in store.records(include_deleted=False))

    def owner_shape(self, tweets):
        """A flat record beside each author record, with the same path."""
        with self.store() as store:
            for tweet in tweets:
                store.record_media("flat", details(tweet), self.rows(store, tweet)["author"]["path"], status="existing")
            for tweet in tweets:
                rows = self.rows(store, tweet)
                self.assertEqual(rows["flat"]["path"], rows["author"]["path"])

    def recycle(self, tweet, keep_deleted):
        """What the gallery's delete does, with a folder standing in for the Recycle Bin."""
        with self.store() as store:
            row = self.rows(store, tweet)["author"]
            ident = store.begin_deletion("offline-batch", row, keep_deleted, signature(row["path"]))
            parked = self.base / "fake-bin" / row["filename"]
            parked.parent.mkdir(exist_ok=True)
            Path(row["path"]).rename(parked)
            store.finish_deletion(ident, "recycled", "offline receipt", str(parked))
            self.assertNotIn(tweet, self.gallery(store))
        return Path(row["path"]), parked

    # A file that comes back is connected again by the next save.

    def test_a_photo_restored_from_the_recycle_bin_is_back_after_the_next_save(self):
        likes = [(tweet, 1, "photo", "alice") for tweet in ("103", "102", "101")]
        self.save(likes)
        self.owner_shape(("101", "102", "103"))
        original, parked = self.recycle("102", keep_deleted=False)
        parked.rename(original)                          # restored in Windows

        finished = self.save(likes, scan_mode="quick")

        self.assertEqual((finished["status"], finished["saved"]), ("finished", 0))
        self.assertEqual(test_core.FakeHttpDownloader.downloaded, [])
        with self.store() as store:
            self.assertEqual(self.gallery(store), ["101", "102", "103"])
            self.assertEqual({row["path"] for row in self.rows(store, "102").values()}, {str(original)})
            self.assertEqual({row["missing_since"] for row in self.rows(store, "102").values()}, {""})
            self.assertEqual((store.lost_count(), store.excluded_media()["total"]), (0, 0))
            self.assertEqual(store.overview(verify_files=False)["files"], 3)
            # The deletion history stays as it happened.
            self.assertEqual(store.deletion_page()["rows"][0]["status"], "recycled")

    def test_a_restored_photo_kept_from_downloading_is_no_longer_excluded(self):
        likes = [(tweet, 1, "photo", "alice") for tweet in ("103", "102", "101")]
        self.save(likes)
        self.owner_shape(("101", "102", "103"))
        original, parked = self.recycle("102", keep_deleted=True)
        with self.store() as store:
            self.assertEqual(store.ignored_media(), {("102", 1, "photo")})
        parked.rename(original)

        self.save(likes, scan_mode="quick")

        self.assertEqual(test_core.FakeHttpDownloader.downloaded, [])
        with self.store() as store:
            self.assertEqual(self.gallery(store), ["101", "102", "103"])
            self.assertEqual(store.ignored_media(), set())
            self.assertEqual(store.excluded_media()["total"], 0)
            self.assertEqual({row["path"] for row in self.rows(store, "102").values()}, {str(original)})

    def test_a_lost_photo_put_away_and_then_put_back_is_in_the_gallery_again(self):
        likes = [(tweet, 1, "photo", "alice") for tweet in ("103", "102", "101")]
        self.save(likes)
        self.owner_shape(("101", "102", "103"))
        with self.store() as store:
            original = Path(self.rows(store, "102")["author"]["path"])
        parked = self.base / "elsewhere.jpg"
        original.rename(parked)                          # gone in Explorer
        with self.store() as store:
            with patch.object(HistoryStore, "_lost_items", side_effect=AssertionError("unexpected full scan")):
                self.assertEqual(store.exclude_lost([("102", 1, "photo")]), 1)
            self.assertEqual(self.gallery(store), ["101", "103"])
        parked.rename(original)                          # and back in its place

        self.save(likes, scan_mode="quick")

        self.assertEqual(test_core.FakeHttpDownloader.downloaded, [])
        with self.store() as store:
            self.assertEqual(self.gallery(store), ["101", "102", "103"])
            self.assertEqual(store.ignored_media(), set())
            self.assertEqual(store.excluded_media()["total"], 0)
            self.assertEqual({row["path"] for row in self.rows(store, "102").values()}, {str(original)})
            self.assertEqual(store.lost_count(), 0)

    # A photo moved into the main folder counts once.

    def test_photos_moved_from_the_author_folders_count_once_after_a_save_in_one_folder(self):
        likes = [(tweet, 1, "photo", "alice") for tweet in ("204", "203", "202", "201")]
        self.save(likes)
        # 204 keeps the owner's shape (flat and author records of one file) and stays put.
        self.owner_shape(("204",))
        with closing(sqlite3.connect(self.record_dir / ".luvbox-archive.flat.sqlite3")) as archive:
            archive.execute("CREATE TABLE IF NOT EXISTS archive(entry TEXT PRIMARY KEY)")
            archive.execute("INSERT INTO archive(entry) VALUES ('twitter204_1')")
            archive.commit()
        moved = {}
        for tweet in ("201", "202", "203"):              # moved in Explorer into the main folder
            source = self.media / "alice" / f"alice_{tweet}_1.jpg"
            moved[tweet] = (source, self.media / source.name)
            source.rename(self.media / source.name)

        finished = self.save(likes, folder_mode="flat")

        self.assertEqual((finished["saved"], finished["skipped"]), (0, 4))
        self.assertEqual(test_core.FakeHttpDownloader.downloaded, [])
        with self.store() as store:
            self.assertEqual(self.gallery(store), ["201", "202", "203", "204"])
            self.assertEqual(store.record_page(0, 100, include_deleted=False)["total"], 4)
            overview = store.overview(verify_files=False)
            self.assertEqual((overview["files"], overview["photo"]), (4, 4))
            self.assertEqual(overview["stats"]["authors"][0]["files"], 4)
            self.assertEqual(store.authors(), [{"author": "alice", "count": 4}])
            self.assertEqual(store.summary()["files"], 4)
            self.assertEqual((store.lost_count(), store.missing_media()), (0, []))
            for tweet, (old, new) in moved.items():
                rows = self.rows(store, tweet)
                self.assertEqual(rows["flat"]["path"], str(new.resolve()))
                self.assertEqual((rows["author"]["path"], rows["author"]["last_path"]), ("", str(old.resolve())))
                self.assertTrue(rows["author"]["missing_since"])
                self.assertIsNone(rows["author"]["filesize"])
                # Nothing about the earlier save is lost.
                self.assertTrue(rows["author"]["saved_at"])
            rows = self.rows(store, "204")
            self.assertEqual(rows["flat"]["path"], rows["author"]["path"])
            self.assertTrue(Path(rows["author"]["path"]).is_file())

    def test_photos_moved_out_of_the_author_folder_are_found_where_a_save_in_one_folder_puts_them(self):
        # The owner's shape: each photo's flat record has the author path too, so
        # after the move the flat save's own record still points at the old place.
        likes = [(tweet, 1, "photo", "alice") for tweet in ("203", "202", "201")]
        self.save(likes)
        self.owner_shape(("201", "202", "203"))
        moved = {}
        for tweet in ("201", "202", "203"):              # moved in Explorer into the main folder
            source = self.media / "alice" / f"alice_{tweet}_1.jpg"
            moved[tweet] = (str(source.resolve()), str((self.media / source.name).resolve()))
            source.rename(self.media / source.name)

        finished = self.save(likes, folder_mode="flat")

        self.assertEqual((finished["saved"], finished["skipped"]), (0, 3))
        self.assertEqual(test_core.FakeHttpDownloader.downloaded, [])
        with self.store() as store:
            self.assertEqual(sorted((row["folder_mode"], row["path"]) for row in store.records(include_deleted=False)),
                             sorted(("flat", new) for old, new in moved.values()))
            self.assertEqual((store.lost_count(), store.missing_media()), (0, []))
            self.assertEqual(store.record_page(0, 100, include_deleted=False)["total"], 3)
            self.assertEqual(store.overview(verify_files=False)["files"], 3)
            for tweet, (old, new) in moved.items():
                rows = self.rows(store, tweet)
                self.assertEqual(rows["flat"]["path"], new)
                # The author record keeps where the file was, so a file moved back is connected again.
                self.assertEqual((rows["author"]["path"], rows["author"]["last_path"]), ("", old))

    def test_a_moved_file_is_found_at_destination_even_when_target_layout_has_a_fileless_row(self):
        likes = [("201", 1, "photo", "alice")]
        self.save(likes)
        source = self.media / "alice" / "alice_201_1.jpg"
        destination = self.media / "alice_201_1.jpg"
        with self.store() as store:
            store.record_declined("flat", details("201"), destination)
            with store._db:
                store._db.execute("DELETE FROM missing_choices WHERE tweet_id='201'")
                store._db.execute(
                    "UPDATE media SET last_path='', missing_since='' WHERE folder_mode='flat' AND tweet_id='201'"
                )
        source.rename(destination)

        finished = self.save(likes, folder_mode="flat")

        self.assertEqual((finished["saved"], finished["skipped"]), (0, 1))
        self.assertEqual(test_core.FakeHttpDownloader.downloaded, [])
        with self.store() as store:
            rows = self.rows(store, "201")
            self.assertEqual(rows["flat"]["path"], str(destination.resolve()))
            self.assertEqual(rows["author"]["path"], "")
            self.assertEqual(store.lost_count(), 0)

    def test_a_save_in_one_folder_keeps_a_recorded_place_that_is_there_or_not_known_to_be_gone(self):
        """Today's destination is looked at only for a recorded file verifiably gone from the media folder."""
        likes = [(tweet, 1, "photo", "alice") for tweet in ("203", "202", "201")]
        self.save(likes)
        self.owner_shape(("201", "202", "203"))
        # 201 is still in its place, with another copy where a save in one folder puts it.
        kept = self.media / "alice" / "alice_201_1.jpg"
        (self.media / kept.name).write_bytes(kept.read_bytes())
        for tweet in ("202", "203"):
            source = self.media / "alice" / f"alice_{tweet}_1.jpg"
            source.rename(self.media / source.name)
        # 202's flat record is in a save folder chosen before this one.
        former = self.base / "former-photos"
        with closing(sqlite3.connect(self.record_dir / ".luvbox-history.sqlite3")) as catalog, catalog:
            catalog.execute("INSERT OR REPLACE INTO catalog_meta(key,value) VALUES ('allowed_media_roots',?)",
                            (json.dumps([str(self.output), str(former)]),))
            catalog.execute("UPDATE media SET path=? WHERE folder_mode='flat' AND tweet_id='202'",
                            (str(former / "myaccount" / "alice" / "alice_202_1.jpg"),))
        with self.store() as store:
            before = {tweet: self.rows(store, tweet) for tweet in ("201", "202", "203")}
        # 203's place cannot be checked (an inaccessible or disconnected location): not proof it is gone.
        real = HistoryStore._deletion_path_absent

        def absent(path):
            return False if "alice_203_1" in str(path) else real(path)

        with patch.object(HistoryStore, "_deletion_path_absent", side_effect=absent):
            finished = self.save(likes, folder_mode="flat")

        self.assertEqual((finished["saved"], finished["skipped"]), (0, 3))
        self.assertEqual(test_core.FakeHttpDownloader.downloaded, [])
        with self.store() as store:
            for tweet, rows in before.items():
                after = self.rows(store, tweet)
                for mode in ("author", "flat"):
                    self.assertEqual((after[mode]["path"], after[mode]["last_path"]),
                                     (rows[mode]["path"], rows[mode]["last_path"]), (tweet, mode))

    def move_to_the_main_folder_and_back(self, tweets, newer=()):
        """The owner's shape moved out of alice/ in Explorer, a save in one folder, then the move undone.

        ``newer`` are likes above the moved photos that stay put. Returns the
        likes and, for each moved photo, its place in alice/ and the place in
        the main folder its flat record followed it to.
        """
        likes = [(tweet, 1, "photo", "alice") for tweet in (*newer, *sorted(tweets, reverse=True))]
        self.save(likes)
        self.owner_shape((*newer, *tweets))
        with closing(sqlite3.connect(self.record_dir / ".luvbox-archive.flat.sqlite3")) as archive, archive:
            archive.execute("CREATE TABLE IF NOT EXISTS archive(entry TEXT PRIMARY KEY)")
            archive.executemany("INSERT INTO archive(entry) VALUES (?)", [(f"twitter{tweet}_1",) for tweet in newer])
        places = {}
        for tweet in tweets:
            source = self.media / "alice" / f"alice_{tweet}_1.jpg"
            places[tweet] = (str(source.resolve()), str((self.media / source.name).resolve()))
            source.rename(self.media / source.name)
        self.save(likes, folder_mode="flat")             # the flat records follow the files
        with self.store() as store:
            for tweet, (old, new) in places.items():
                self.assertEqual((self.rows(store, tweet)["flat"]["path"], self.rows(store, tweet)["author"]["path"]),
                                 (new, ""))
        for old, new in places.values():                  # and the move is undone
            Path(new).rename(old)
        return likes, places

    def on_disk(self):
        return sorted(str(path.resolve()) for path in self.media.rglob("*.jpg"))

    def assert_counted_once(self, places):
        """Each photo moved back is one tile at its file, and the record that followed it no longer stands for it."""
        on_disk = self.on_disk()
        with self.store() as store:
            self.assertEqual(sorted(row["path"] for row in store.records(include_deleted=False)), on_disk)
            self.assertEqual((store.lost_count(), store.missing_media()), (0, []))
            self.assertEqual(store.record_page(0, 100, include_deleted=False)["total"], len(on_disk))
            self.assertEqual(store.overview(verify_files=False)["files"], len(on_disk))
            for tweet, (old, new) in places.items():
                rows = self.rows(store, tweet)
                self.assertEqual(rows["author"]["path"], old)
                # The flat record keeps the place it had followed the file to, which is gone now.
                self.assertFalse(Path(new).exists())
                self.assertEqual((rows["flat"]["path"], rows["flat"]["last_path"]), ("", new))
                self.assertTrue(rows["flat"]["missing_since"])
                self.assertIsNone(rows["flat"]["filesize"])

    def test_photos_moved_back_into_the_author_folder_count_once_from_the_next_save_on(self):
        likes, places = self.move_to_the_main_folder_and_back(("201", "202", "203"))
        self.embed_source.reset_mock()

        finished = self.save(likes, folder_mode="flat")

        self.assertEqual((finished["saved"], finished["skipped"]), (0, 3))
        self.assertEqual(test_core.FakeHttpDownloader.downloaded, [])
        # Each file is where its tags were written; nothing is written again.
        self.embed_source.assert_not_called()
        self.assertEqual(len(self.on_disk()), 3)
        self.assert_counted_once(places)
        self.save(likes, folder_mode="flat", scan_mode="quick")    # later saves in one folder keep it so,
        self.assert_counted_once(places)
        self.save(likes)                                            # and so does a save in the author folders
        self.assert_counted_once(places)

    def test_old_photos_moved_back_count_once_even_when_the_quick_save_stops_before_them(self):
        newer = [str(tweet) for tweet in range(1010, 1000, -1)]
        with patch("luvbox.core.QUICK_STOP_AFTER", 3):
            likes, places = self.move_to_the_main_folder_and_back(("201", "202", "203"), newer=newer)

            finished = self.save(likes, folder_mode="flat", scan_mode="quick")

        # The quick save stopped among the newer likes, long before the photos moved back.
        self.assertEqual((finished["scan_mode"], finished["status"], finished["saved"]), ("quick", "finished", 0))
        self.assertLess(finished["visited"], len(newer))
        self.assertEqual(len(self.on_disk()), 13)
        self.assert_counted_once(places)
        with self.store() as store:
            self.assertEqual(store.overview(verify_files=True)["files"], 13)

    def test_the_save_start_clears_another_layouts_place_only_in_the_media_folder_and_for_a_gone_file(self):
        likes, places = self.move_to_the_main_folder_and_back(("201", "202", "203", "204"))
        # 204 is two real files now: a copy was put where its flat record points.
        shutil.copyfile(places["204"][0], places["204"][1])
        # 202's flat record points into a save folder chosen before this one, and its file is not there either.
        former = self.base / "former-photos"
        (former / "myaccount").mkdir(parents=True)
        with closing(sqlite3.connect(self.record_dir / ".luvbox-history.sqlite3")) as catalog, catalog:
            catalog.execute("INSERT OR REPLACE INTO catalog_meta(key,value) VALUES ('allowed_media_roots',?)",
                            (json.dumps([str(self.output), str(former)]),))
            catalog.execute("UPDATE media SET path=? WHERE folder_mode='flat' AND tweet_id='202'",
                            (str(former / "myaccount" / "alice_202_1.jpg"),))
        with self.store() as store:
            before = {tweet: self.rows(store, tweet)["flat"] for tweet in places}
        # 203's place in the main folder cannot be checked (an inaccessible or disconnected location).
        real = HistoryStore._deletion_path_absent

        def absent(path):
            return False if os.path.normcase(str(path)) == os.path.normcase(places["203"][1]) else real(path)

        with patch.object(HistoryStore, "_deletion_path_absent", side_effect=absent):
            self.save(likes, folder_mode="flat")

        self.assertEqual(test_core.FakeHttpDownloader.downloaded, [])
        with self.store() as store:
            for tweet, (old, new) in places.items():
                rows = self.rows(store, tweet)
                self.assertEqual(rows["author"]["path"], old, tweet)    # every file back is connected again
                if tweet == "201":                                     # and the rule does apply here
                    self.assertEqual((rows["flat"]["path"], rows["flat"]["last_path"]), ("", new))
                else:
                    self.assertEqual((rows["flat"]["path"], rows["flat"]["last_path"]),
                                     (before[tweet]["path"], before[tweet]["last_path"]), tweet)
            self.assertEqual(self.rows(store, "204")["flat"]["path"], places["204"][1])
            self.assertEqual(sum(row["tweet_id"] == "204" for row in store.records(include_deleted=False)), 2)

    # One of two real copies deleted in Explorer and put back is shown again.

    def two_copies(self, tweets):
        """Photos saved in the author folders and then again by a save in one folder: two files of each post."""
        likes = [(tweet, 1, "photo", "alice") for tweet in sorted(tweets, reverse=True)]
        self.save(likes)
        self.assertEqual(self.save(likes, folder_mode="flat")["saved"], len(tweets))
        with self.store() as store:
            places = {tweet: {mode: row["path"] for mode, row in self.rows(store, tweet).items()} for tweet in tweets}
        for place in places.values():
            self.assertNotEqual(place["author"], place["flat"])
        self.assertEqual(len(self.on_disk()), 2 * len(tweets))
        return likes, places

    def assert_every_file_shown(self, places):
        """Every file on disk is one tile and counted once, and each record is still at its own file."""
        on_disk = self.on_disk()
        self.assertEqual(len(on_disk), 2 * len(places))
        with self.store() as store:
            self.assertEqual(sorted(row["path"] for row in store.records(include_deleted=False)), on_disk)
            self.assertEqual(store.overview(verify_files=True)["files"], len(on_disk))
            self.assertEqual(store.overview(verify_files=False)["files"], len(on_disk))
            self.assertEqual(store.record_page(0, 100, include_deleted=False)["total"], len(on_disk))
            for tweet, place in places.items():
                self.assertEqual({mode: row["path"] for mode, row in self.rows(store, tweet).items()}, place)

    def test_a_main_folder_copy_deleted_in_explorer_and_put_back_is_shown_again(self):
        likes, places = self.two_copies(("301", "302"))
        parked = self.base / "fake-bin"
        parked.mkdir()
        for tweet, place in places.items():               # deleted in Explorer
            Path(place["flat"]).rename(parked / f"{tweet}.jpg")
        self.save(likes, folder_mode="flat")             # a save in the layout of the deleted copies
        for tweet, place in places.items():               # put back (Ctrl+Z or the Recycle Bin)
            (parked / f"{tweet}.jpg").rename(place["flat"])

        self.save(likes, folder_mode="flat")

        self.assertEqual(test_core.FakeHttpDownloader.downloaded, [])
        self.assert_every_file_shown(places)
        self.save(likes, folder_mode="flat", scan_mode="quick")
        self.save(likes)
        self.assert_every_file_shown(places)

    def test_an_author_folder_copy_deleted_in_explorer_and_put_back_is_shown_again(self):
        likes, places = self.two_copies(("401",))
        parked = self.base / "fake-bin" / "401.jpg"
        parked.parent.mkdir()
        Path(places["401"]["author"]).rename(parked)    # deleted in Explorer
        self.save(likes)                                 # a save in the author folders
        parked.rename(places["401"]["author"])          # put back

        self.save(likes)

        self.assertEqual(test_core.FakeHttpDownloader.downloaded, [])
        self.assert_every_file_shown(places)
        self.save(likes, folder_mode="flat")
        self.assert_every_file_shown(places)

    # A released skip is downloaded by the next quick save.

    def test_a_released_older_like_is_downloaded_by_the_next_quick_save(self):
        older = [(str(tweet), 1, "photo", "alice") for tweet in range(1029, 999, -1)]
        older.insert(20, ("5000", 1, "photo", "alice"))  # an older Like, after 20 saved ones
        with patch("luvbox.core.QUICK_STOP_AFTER", 5):
            first = self.save(older, review=Review({"5000"}), confirm_each=True)
            self.assertEqual(first["review_never"], 1)
            with self.store() as store:
                self.assertIn(("5000", 1, "photo"), store.ignored_media())
                self.assertEqual(store.release_excluded([("5000", 1, "photo")]), 1)
                self.assertEqual(store.ignored_media(), set())

            finished = self.save([("6000", 1, "photo", "bob")] + older, scan_mode="quick")

            self.assertEqual((finished["scan_mode"], finished["status"], finished["saved"]), ("quick", "finished", 2))
            self.assertTrue(any("5000_1" in url for url in test_core.FakeHttpDownloader.downloaded))
            self.assertTrue((self.media / "alice" / "alice_5000_1.jpg").is_file())
            with self.store() as store:
                self.assertIn("5000", self.gallery(store))
                # Reached and saved: nothing is left for the next quick save to look for.
                self.assertEqual(store.review_pending(), set())
            # The quick save after that stops early again.
            again = self.save([("6001", 1, "photo", "bob"), ("6000", 1, "photo", "bob")] + older, scan_mode="quick")
            self.assertEqual((again["scan_mode"], again["saved"]), ("quick", 1))
            self.assertLess(again["visited"], 10)

    def test_a_released_like_that_no_longer_downloads_is_tried_by_one_save(self):
        older = [(str(tweet), 1, "photo", "alice") for tweet in range(1029, 999, -1)]
        older.insert(20, ("5000", 1, "photo", "alice"))
        self.addCleanup(setattr, GoneDownloader, "gone", frozenset())
        with patch("luvbox.core.QUICK_STOP_AFTER", 5):
            self.save(older, review=Review({"5000"}), confirm_each=True)
            with self.store() as store:
                self.assertEqual(store.release_excluded([("5000", 1, "photo")]), 1)
            GoneDownloader.gone, GoneDownloader.tried = frozenset({"5000"}), []

            finished = self.save([("6000", 1, "photo", "bob")] + older, scan_mode="quick", downloader=GoneDownloader)

            # The save after the release went down to it and tried it.
            self.assertEqual((finished["scan_mode"], finished["status"], finished["saved"]), ("quick", "finished", 1))
            self.assertEqual(len(GoneDownloader.tried), 1)
            self.assertGreater(finished["visited"], 20)
            # It failed, so it is listed for '다시 저장', and no later quick save looks for it.
            self.assertIn(("5000", 1, "photo"), unsaved.identities("myaccount", str(self.output)))
            with self.store() as store:
                self.assertEqual(store.review_pending(), set())
            for number, tweet in enumerate(("6001", "6002")):
                newer = [(tweet, 1, "photo", "bob")] + [(str(6000 + earlier), 1, "photo", "bob")
                                                        for earlier in range(number, -1, -1)]
                again = self.save(newer + older, scan_mode="quick", downloader=GoneDownloader)
                self.assertEqual((again["scan_mode"], again["status"], again["saved"]), ("quick", "finished", 1))
                self.assertLess(again["visited"], 10)
            self.assertEqual(len(GoneDownloader.tried), 1)

    def released_and_skipped_again_in_a_review(self, scan_mode):
        """Released from 직접 제외, then '이번엔 건너뛰기' in the review of a save; the next save asks again."""
        older = [(str(tweet), 1, "photo", "alice") for tweet in range(1029, 999, -1)]
        older.insert(20, ("5000", 1, "photo", "alice"))
        with patch("luvbox.core.QUICK_STOP_AFTER", 5):
            self.save(older, review=Review({"5000"}), confirm_each=True)
            with self.store() as store:
                self.assertEqual(store.release_excluded([("5000", 1, "photo")]), 1)

            skipped = self.save([("6000", 1, "photo", "bob")] + older, review=SkipReview({"5000"}),
                                confirm_each=True, scan_mode=scan_mode)

            self.assertEqual((skipped["status"], skipped["review_skipped"]), ("finished", 1))
            self.assertIn("다음 저장 때 다시 보여 드립니다", skipped["message"])
            with self.store() as store:
                self.assertEqual(store.review_pending(), {("5000", 1, "photo")})
            review = AskedReview()
            again = self.save([("6001", 1, "photo", "bob"), ("6000", 1, "photo", "bob")] + older, review=review,
                              confirm_each=True, scan_mode="quick")
            self.assertEqual((again["scan_mode"], again["saved"]), ("quick", 2))
            self.assertEqual(review.asked, ["6001", "5000"])
            self.assertTrue((self.media / "alice" / "alice_5000_1.jpg").is_file())
            with self.store() as store:
                self.assertEqual(store.review_pending(), set())

    def test_a_released_like_skipped_again_in_a_quick_save_is_shown_by_the_next_save(self):
        self.released_and_skipped_again_in_a_review("quick")

    def test_a_released_like_skipped_again_in_a_full_check_is_shown_by_the_next_save(self):
        self.released_and_skipped_again_in_a_review("full")

    def logged_save(self, rows, *, downloader=test_core.FakeHttpDownloader, **changes):
        """An unattended save, like ``save``, that also returns its log lines (the scan note among them)."""
        test_core.FakeHttpDownloader.downloaded = []
        events = []
        with patch("gallery_dl.extractor.find", side_effect=lambda url: test_core.FakeLikesExtractor(url, rows)), \
                patch("gallery_dl.downloader.find", return_value=downloader):
            code = core.run_worker(replace(self.options, **changes), events.append)
        self.assertNotIn(test_core.SECRET, json.dumps(events))
        self.assertEqual((code, events[-1]["type"]), (0, "finished"), events[-1].get("message"))
        return events[-1], " ".join(event.get("message", "") for event in events if event["type"] == "log")

    def test_a_skipped_like_that_no_longer_downloads_lets_later_quick_saves_stop_early(self):
        # Skipped in a save review ('이번엔 건너뛰기'), never excluded or released.
        older = [(str(tweet), 1, "photo", "alice") for tweet in range(1029, 999, -1)]
        older.insert(20, ("5000", 1, "photo", "alice"))
        self.addCleanup(setattr, GoneDownloader, "gone", frozenset())
        with patch("luvbox.core.QUICK_STOP_AFTER", 5):
            first = self.save(older, review=SkipReview({"5000"}), confirm_each=True)
            self.assertEqual(first["review_skipped"], 1)
            with self.store() as store:
                self.assertEqual(store.review_pending(), {("5000", 1, "photo")})
            GoneDownloader.gone, GoneDownloader.tried = frozenset({"5000"}), []

            finished, logs = self.logged_save([("6000", 1, "photo", "bob")] + older, scan_mode="quick",
                                              downloader=GoneDownloader)

            # The next quick save went on until it met the skipped like, and tried it.
            self.assertIn("지난번에 건너뛰었거나 제외를 해제한 파일 1개가 나올 때까지는 이어서 확인합니다.", logs)
            self.assertEqual((finished["scan_mode"], finished["status"], finished["saved"]), ("quick", "finished", 1))
            self.assertEqual(len(GoneDownloader.tried), 1)
            self.assertGreater(finished["visited"], 20)
            # It failed and is listed for '다시 저장', so no later quick save walks down to it again.
            self.assertIn(("5000", 1, "photo"), unsaved.identities("myaccount", str(self.output)))
            with self.store() as store:
                self.assertEqual(store.review_pending(), set())
            for number, tweet in enumerate(("6001", "6002")):
                newer = [(tweet, 1, "photo", "bob")] + [(str(6000 + earlier), 1, "photo", "bob")
                                                        for earlier in range(number, -1, -1)]
                again, logs = self.logged_save(newer + older, scan_mode="quick", downloader=GoneDownloader)
                self.assertEqual((again["scan_mode"], again["status"], again["saved"]), ("quick", "finished", 1))
                self.assertLess(again["visited"], 10)
                self.assertNotIn("나올 때까지는 이어서 확인합니다", logs)
            self.assertEqual(len(GoneDownloader.tried), 1)

    def test_the_scan_note_counts_a_released_like_among_the_files_to_get_again(self):
        older = [(str(tweet), 1, "photo", "alice") for tweet in range(1029, 999, -1)]
        older.insert(20, ("5000", 1, "photo", "alice"))
        with patch("luvbox.core.QUICK_STOP_AFTER", 5):
            self.save(older, review=Review({"5000"}), confirm_each=True)
            with self.store() as store:
                self.assertEqual(store.release_excluded([("5000", 1, "photo")]), 1)

            finished, logs = self.logged_save([("6000", 1, "photo", "bob")] + older, scan_mode="quick")

        # Not a file the last save skipped: one the person asked to get again.
        self.assertIn("지난번에 건너뛰었거나 제외를 해제한 파일 1개가 나올 때까지는 이어서 확인합니다.", logs)
        self.assertNotIn("건너뛴 파일", logs)
        self.assertEqual((finished["scan_mode"], finished["saved"]), ("quick", 2))


class StoreTests(unittest.TestCase):
    """The catalog side of the same three, without a save around it."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="luvbox-reconnect-")
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        isolated = patch("luvbox.protection.state_directory", return_value=self.root / "protection")
        isolated.start()
        self.addCleanup(isolated.stop)
        self.paths = resolve_storage(self.root / "Likes", "offline")
        self.paths.output_dir.mkdir()
        self.store = HistoryStore.for_storage(self.paths)
        self.addCleanup(self.store.close)

    def file(self, relative, payload=b"picture"):
        path = self.paths.media_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        return path

    def rows(self, tweet):
        return {row["folder_mode"]: row for row in self.store.records() if row["tweet_id"] == tweet}

    def moved(self, tweet="300"):
        old = self.file(f"writer/writer_{tweet}_1.jpg")
        self.store.record_media("author", details(tweet, "writer"), old)
        new = self.paths.media_dir / old.name
        old.rename(new)
        return old, new

    def test_the_reconnect_at_save_start_is_not_a_folder_rescan(self):
        present = self.file("writer_100_1.jpg")
        for mode in ("author", "flat"):
            self.store.record_media(mode, details("100", "writer"), present)
        gone = self.file("writer_101_1.jpg")
        self.store.record_media("flat", details("101", "writer"), gone)
        self.store.import_legacy()                       # the first save's import
        gone.unlink()
        with self.store._db:
            self.store._db.execute("UPDATE media SET last_path=path,path='',filesize=NULL WHERE tweet_id='101'")
        self.file("writer_999_1.jpg")                    # never recorded

        result = self.store.import_legacy()

        self.assertEqual(result, {"imported": False, "records": 0, "warnings": []})
        self.assertEqual({row["tweet_id"] for row in self.store.records()}, {"100", "101"})
        self.assertEqual(self.rows("101")["flat"]["path"], "")
        gone.write_bytes(b"picture")
        self.store.import_legacy()
        self.assertEqual(self.rows("101")["flat"]["path"], str(gone))
        self.assertEqual({row["path"] for row in self.rows("100").values()}, {str(present)})

    def test_a_malformed_last_location_does_not_stop_the_save_start(self):
        present = self.file("writer_100_1.jpg")
        self.store.record_media("flat", details("100", "writer"), present)
        self.store.import_legacy()
        # From a copied or merged catalog: a NUL is not a path the system can look at.
        with self.store._db:
            self.store._db.execute(
                "INSERT INTO media(folder_mode,tweet_id,num,media_type,status,record_source,updated_at,path,last_path) "
                "VALUES ('author','101',1,'photo','saved','saved','2026-01-01','',?)",
                (str(self.paths.media_dir / "writer_101_1\x00.jpg"),))
        lost = self.file("writer_102_1.jpg")
        self.store.record_media("flat", details("102", "writer"), lost)
        with self.store._db:
            self.store._db.execute("UPDATE media SET last_path=path,path='',filesize=NULL WHERE tweet_id='102'")

        self.assertEqual(self.store.import_legacy(), {"imported": False, "records": 0, "warnings": []})

        # The other records are still reconnected, and the malformed one is left as it was.
        self.assertEqual(self.rows("102")["flat"]["path"], str(lost))
        self.assertEqual(self.rows("101")["author"]["path"], "")
        self.assertEqual(self.rows("100")["flat"]["path"], str(present))

    def test_a_moved_file_carries_the_persons_star_and_tags(self):
        self.moved()
        self.store.toggle_favorite("author", "300", 1, "photo")
        self.store.add_tag("author", "300", 1, "photo", "좋은 사진")
        self.store.set_taught_categories([("author", "300", 1, "photo")], "풍경·배경")
        # An estimate the AI run may already have made for the new place.
        self.store.add_tag("flat", "300", 1, "photo", "인물", source="ai")
        new = self.paths.media_dir / "writer_300_1.jpg"

        self.store.record_media("flat", details("300", "writer"), new, status="existing")

        [tile] = self.store.records(include_deleted=False)
        self.assertEqual((tile["folder_mode"], tile["path"]), ("flat", str(new)))
        self.assertTrue(self.store.is_favorite("flat", "300", 1, "photo"))
        self.assertEqual(self.store.record_page(favorite_only=True, include_deleted=False)["total"], 1)
        self.assertIn({"tag": "좋은 사진", "source": "user"}, self.store.item_tags("flat", "300", 1, "photo"))
        self.assertEqual(self.store.user_tags(), [{"tag": "좋은 사진", "count": 1}])
        self.assertEqual(self.store.classification_of("flat", "300", 1, "photo")["category"], "풍경·배경")
        self.assertEqual(self.store.record_page(ai_category="풍경·배경", include_deleted=False)["total"], 1)
        self.assertEqual(self.store.record_page(ai_category="인물", include_deleted=False)["total"], 0)
        # The old record keeps what it had; nothing is deleted.
        self.assertTrue(self.store._db.execute(
            "SELECT 1 FROM favorites WHERE folder_mode='author' AND tweet_id='300'").fetchone())
        self.assertEqual(self.store.classification_of("author", "300", 1, "photo")["category"], "풍경·배경")

    def test_the_new_records_own_marks_win(self):
        self.moved()
        self.store.set_taught_categories([("author", "300", 1, "photo")], "풍경·배경")
        self.store.set_taught_categories([("flat", "300", 1, "photo")], "인물")
        self.store.add_tag("flat", "300", 1, "photo", "풍경·배경")
        new = self.paths.media_dir / "writer_300_1.jpg"

        self.store.record_media("flat", details("300", "writer"), new, status="existing")

        self.assertEqual(self.store.classification_of("flat", "300", 1, "photo")["category"], "인물")
        self.assertEqual(self.store.record_page(ai_category="인물", include_deleted=False)["total"], 1)
        self.assertEqual(self.store.record_page(ai_category="풍경·배경", include_deleted=False)["total"], 0)
        self.assertCountEqual(self.store.item_tags("flat", "300", 1, "photo"),
                              [{"tag": "인물", "source": "confirmed"}, {"tag": "풍경·배경", "source": "user"}])

    def test_two_real_copies_stay_two_files(self):
        author = self.file("writer/writer_300_1.jpg", b"author copy")
        self.store.record_media("author", details("300", "writer"), author)
        flat = self.file("writer_300_1.jpg", b"flat copy")

        self.store.record_media("flat", details("300", "writer"), flat)

        rows = self.rows("300")
        self.assertEqual((rows["author"]["path"], rows["flat"]["path"]), (str(author), str(flat)))
        self.assertEqual(self.store.summary()["files"], 2)
        self.assertEqual(self.store.record_page(include_deleted=False)["total"], 2)

    def test_a_drive_that_is_not_connected_or_a_former_folder_keeps_its_place(self):
        old, new = self.moved()
        before = self.rows("300")["author"]
        with patch.object(HistoryStore, "_deletion_path_absent", return_value=False):
            self.store.record_media("flat", details("300", "writer"), new, status="existing")
        self.assertEqual(self.rows("300")["author"]["path"], before["path"])
        real = self.store.is_download_path
        with patch.object(self.store, "is_download_path", side_effect=lambda path: str(old) != str(path) and real(path)):
            self.store.record_media("flat", details("300", "writer"), new, status="existing")
        self.assertEqual(self.rows("300")["author"]["path"], before["path"])
        self.store.record_media("flat", details("300", "writer"), new, status="existing")
        self.assertEqual(self.rows("300")["author"]["path"], "")

    def test_a_moved_file_that_returns_is_connected_again(self):
        old, new = self.moved()
        self.store.record_media("flat", details("300", "writer"), new, status="existing")
        self.store.import_legacy()
        new.rename(old)                                  # moved back into the author folder

        self.store.import_legacy()                       # the next save's start
        self.store.record_media("author", details("300", "writer"), old, status="existing")

        rows = self.rows("300")
        self.assertEqual((rows["author"]["path"], rows["flat"]["path"]), (str(old), ""))
        self.assertEqual([row["folder_mode"] for row in self.store.records(include_deleted=False)], ["author"])
        self.assertEqual(self.store.summary(verify_files=False)["files"], 1)

    def test_a_file_back_in_its_place_leaves_the_place_another_layout_followed_it_to(self):
        old, new = self.moved()
        self.store.record_media("flat", details("300", "writer"), new, status="existing")
        self.store.import_legacy()
        self.store.toggle_favorite("flat", "300", 1, "photo")     # starred while it was in the main folder
        new.rename(old)                                  # the move undone
        # A save changed the record meanwhile: the reconnect does not apply, and nothing else changes.
        real = self.store._write_batches

        def changed_meanwhile(items, apply):
            with self.store._db:
                self.store._db.execute("UPDATE media SET updated_at='changed' WHERE folder_mode='author'")
            real(items, apply)

        with patch.object(self.store, "_write_batches", side_effect=changed_meanwhile):
            self.store.import_legacy()
        rows = self.rows("300")
        self.assertEqual((rows["author"]["path"], rows["flat"]["path"]), ("", str(new)))

        self.store.import_legacy()                       # the next save's start, and nothing else

        rows = self.rows("300")
        self.assertEqual(rows["author"]["path"], str(old))
        self.assertEqual((rows["flat"]["path"], rows["flat"]["last_path"]), ("", str(new)))
        self.assertIsNone(rows["flat"]["filesize"])
        self.assertTrue(rows["flat"]["missing_since"])
        [tile] = self.store.records(include_deleted=False)
        self.assertEqual((tile["folder_mode"], tile["path"]), ("author", str(old)))
        self.assertEqual((self.store.summary(verify_files=False)["files"], self.store.lost_count()), (1, 0))
        # The star belongs to the file and goes with it.
        self.assertTrue(self.store.is_favorite("author", "300", 1, "photo"))
        self.assertEqual(self.store.record_page(favorite_only=True, include_deleted=False)["total"], 1)

    def test_release_reaches_the_next_quick_save_only_for_media_without_a_file(self):
        present = self.file("writer_400_1.jpg")
        self.store.record_media("flat", details("400", "writer"), present)
        self.store.set_missing_ignored([("400", 1, "photo"), ("401", 1, "photo"), ("402", 1, "video")], True)
        self.store.set_review_pending({("99", 1, "photo")})

        self.assertEqual(self.store.release_excluded(None), 3)

        self.assertEqual(self.store.review_pending(), {("99", 1, "photo"), ("401", 1, "photo"), ("402", 1, "video")})
        # A damaged memory already makes the next save check every Like; it is left alone.
        with self.store._db:
            self.store._db.execute("UPDATE catalog_meta SET value='damaged' WHERE key='review_pending'")
        self.store.set_missing_ignored([("403", 1, "photo")], True)
        self.assertEqual(self.store.release_excluded([("403", 1, "photo")]), 1)
        self.assertIsNone(self.store.review_pending())

    def test_released_media_stay_remembered_until_a_save_meets_them(self):
        """The save itself forgets what it met (saved, listed as failed, or excluded); a run's end changes nothing."""
        self.store.set_review_pending({("99", 1, "photo")})          # skipped in a review, never released
        self.store.set_missing_ignored([("401", 1, "photo")], True)
        self.store.release_excluded([("401", 1, "photo")])
        both = {("99", 1, "photo"), ("401", 1, "photo")}
        for mode, status in (("quick", "stopped"), ("quick", "error"), ("retry", "finished"),
                             ("quick", "finished"), ("full", "finished")):
            run = self.store.start_run("flat", scan_mode=mode)
            self.store.finish_run(run, status)
            self.assertEqual(self.store.review_pending(), both, (mode, status))

    def test_release_looks_each_media_up_by_the_primary_key(self):
        statements = []
        self.store._db.set_trace_callback(statements.append)
        self.addCleanup(self.store._db.set_trace_callback, None)
        self.store.set_missing_ignored([("500", 1, "photo")], True)
        self.store.release_excluded([("500", 1, "photo")])
        lookups = [sql for sql in statements if sql.startswith("SELECT 1 FROM media")]
        self.assertTrue(lookups)
        for sql in lookups:
            plan = " ".join(row[3] for row in self.store._db.execute("EXPLAIN QUERY PLAN " + sql))
            self.assertIn("sqlite_autoindex_media_1", plan, sql)


if __name__ == "__main__":
    unittest.main()

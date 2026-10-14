"""Statistics describe existing account files, independently of archive rows."""

import json
import os
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from luvbox.history import HistoryStore


class FileStatisticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name) / "account"
        self.store = HistoryStore(self.output)
        self.addCleanup(self.store.close)

    def file(self, name, payload=b"media"):
        path = self.output / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        return path

    @staticmethod
    def metadata(tweet="100", kind="photo", author="alice", month=9, number=1, embedded=False):
        data = {"tweet_id": tweet, "num": number, "type": kind,
                "author": {"name": author, "id": "12345"},
                "date": datetime(2026, month, 1, tzinfo=timezone.utc)}
        if embedded:
            data["_luvbox_metadata_status"] = "embedded"
            data["_luvbox_author_metadata_status"] = "embedded"
        return data

    def record(self, path, mode="flat", **metadata):
        self.store.record_media(mode, self.metadata(**metadata), path)

    def test_same_file_in_two_layouts_counts_once_without_removing_history(self):
        path = self.file("100_1.jpg", b"one-file")
        self.record(path, "author", embedded=True)
        self.record(path, "flat", embedded=True)
        original = self.store.records()

        summary = self.store.summary()

        self.assertEqual((summary["files"], summary["photo"], summary["bytes"]), (1, 1, 8))
        self.assertEqual((summary["posts"], summary["current_posts"], summary["record_count"]), (1, 1, 2))
        self.assertEqual(summary["duplicate_records"], 1)
        self.assertEqual((summary["embedded_sources"], summary["embedded_authors"]), (1, 1))
        self.assertEqual(self.store.records(), original)

    def test_moved_file_does_not_count_the_missing_previous_layout(self):
        old = self.file("alice/100_1.jpg")
        self.record(old, "author")
        new = self.output / old.name
        old.rename(new)
        self.record(new, "flat")
        original = self.store.records()

        summary = self.store.summary()

        self.assertEqual((summary["files"], summary["bytes"], summary["record_count"]), (1, 5, 2))
        self.assertEqual((summary["posts"], summary["current_posts"]), (1, 1))
        self.assertEqual(summary["duplicate_records"], 0)
        self.assertEqual(self.store.records(), original)

    def test_metadata_success_on_one_duplicate_is_counted_only_once(self):
        path = self.file("100_1.jpg")
        self.record(path, "author", embedded=True)
        self.record(path, "flat")

        summary = self.store.summary()

        self.assertEqual((summary["files"], summary["embedded_sources"], summary["embedded_authors"]), (1, 1, 1))
        self.assertEqual(len(self.store.records()), 2)

    def test_shared_file_author_completion_requires_the_selected_name_and_id(self):
        cases = (("OldName", "12345", "NewName", "12345", False),
                 ("alice", "12345", "alice", "67890", False),
                 ("Writer", "12345", "writer", "12345", False),
                 ("alice", "12345", "alice", "12345", True))
        for index, (old_name, old_id, new_name, new_id, complete) in enumerate(cases):
            with self.subTest(old=(old_name, old_id), new=(new_name, new_id)):
                tweet = str(100 + index)
                path = self.file(f"{tweet}_1.jpg")
                old = self.metadata(tweet=tweet, author=old_name, embedded=True)
                old["author"]["id"] = old_id
                self.store.record_media("author", old, path)
                with self.store._db:
                    self.store._db.execute("UPDATE media SET updated_at='2020-01-01' WHERE tweet_id=?", (tweet,))
                new = self.metadata(tweet=tweet, author=new_name)
                new["author"]["id"] = new_id
                self.store.record_media("flat", new, path, status="existing")
                original = self.store.records()

                for verify in (False, True):
                    _, files, *_ = self.store._file_snapshot(verify_files=verify)
                    [row] = [row for row in files if row["tweet_id"] == tweet]
                    self.assertEqual((row["author"], row["author_id"]), (new_name, new_id))
                    self.assertEqual(row["author_metadata_status"], "embedded" if complete else "pending")
                    summary = self.store.overview(verify_files=verify)
                    self.assertEqual(summary["embedded_authors"], int(complete))
                    self.assertEqual(summary["embedded_sources"], index + 1)
                self.assertEqual(self.store.records(), original)

    def test_shared_file_completion_does_not_follow_replaced_author_information(self):
        path = self.file("100_1.jpg")
        self.record(path, "author")
        with self.store._db:
            self.store._db.execute("UPDATE media SET updated_at='2020-01-01'")
        unknown = self.metadata(embedded=True)
        unknown["author"] = {}
        self.store.record_media("flat", unknown, path, status="existing")
        original = self.store.records()

        for verify in (False, True):
            _, [row], *_ = self.store._file_snapshot(verify_files=verify)
            self.assertEqual((row["author"], row["author_id"], row["author_metadata_status"]),
                             ("alice", "12345", "pending"))
            summary = self.store.summary(verify_files=verify)
            self.assertEqual((summary["files"], summary["embedded_authors"], summary["embedded_sources"]), (1, 0, 1))
        self.assertEqual(self.store.records(), original)

    def test_latest_author_of_the_same_file_is_ranked_once(self):
        path = self.file("100_1.jpg")
        self.record(path, "author", author="old_name")
        time.sleep(0.02)
        self.record(path, "flat", author="current_name")

        [author] = self.store.statistics()["authors"]

        self.assertEqual((author["author"], author["files"], author["posts"]), ("current_name", 1, 1))
        self.assertEqual(len(self.store.records()), 2)

    def test_new_existing_api_metadata_updates_author_but_keeps_original_saved_month(self):
        path = self.file("100_1.jpg")
        self.record(path, "author", author="old_name")
        with closing(sqlite3.connect(self.store.path)) as db:
            db.execute("UPDATE media SET saved_at='2026-08-01T10:00:00+09:00',"
                       "updated_at='2026-08-01T10:00:00+09:00'")
            db.commit()
        self.store.record_media("flat", self.metadata(author="current_name"), path, status="existing")
        original = self.store.records()

        overview = self.store.overview()
        [author] = overview["stats"]["authors"]

        self.assertEqual((author["author"], author["files"], author["posts"]), ("current_name", 1, 1))
        self.assertEqual(overview["stats"]["months"]["saved"], [("2026-08", 1)])
        self.assertEqual((overview["files"], overview["record_count"]), (1, 2))
        self.assertEqual({row["author_id"] for row in original}, {"12345"})
        self.assertEqual(self.store.records(), original)

    def test_shared_output_reconnects_moved_file_across_layouts_without_claiming_other_media(self):
        store = HistoryStore(Path(self.temp.name) / "isolated-records",
                             media_dir=self.output, output_dir=self.output)
        self.addCleanup(store.close)
        old_path = self.file("alice/100_1.jpg")
        store.record_media("author", self.metadata(embedded=True), old_path)
        [original] = store.records()
        moved_path = self.output / old_path.name
        old_path.rename(moved_path)
        # A matching tweet alone is insufficient: the number and media type
        # must also belong to this account's catalog.
        self.file("100_2.jpg")
        self.file("100_video_1.mp4")
        self.file("999_1.jpg")

        store.import_legacy(refresh=True)

        [reconnected] = store.records()
        self.assertEqual(reconnected["path"], str(moved_path.resolve()))
        for field in ("folder_mode", "tweet_id", "num", "media_type", "author", "author_id",
                      "saved_at", "posted_at", "source_url", "metadata_status", "author_metadata_status"):
            self.assertEqual(reconnected[field], original[field], field)
        self.assertEqual((store.summary()["files"], store.summary()["record_count"]), (1, 1))

    def test_shared_output_preserves_both_existing_layout_copies(self):
        store = HistoryStore(Path(self.temp.name) / "isolated-records",
                             media_dir=self.output, output_dir=self.output)
        self.addCleanup(store.close)
        author_path = self.file("alice/100_1.jpg", b"author-copy")
        store.record_media("author", self.metadata(embedded=True), author_path)
        [original] = store.records()
        flat_path = self.file("100_1.jpg", b"flat-copy")
        self.file("999_1.jpg", b"not-this-account")

        store.import_legacy(refresh=True)

        rows = {row["folder_mode"]: row for row in store.records()}
        self.assertEqual(set(rows), {"author", "flat"})
        self.assertEqual(rows["author"], original)
        self.assertEqual(rows["flat"]["path"], str(flat_path.resolve()))
        summary = store.summary()
        self.assertEqual((summary["files"], summary["record_count"], summary["bytes"]), (2, 2, 20))
        self.assertEqual((summary["photo"], summary["posts"], summary["current_posts"]), (2, 1, 1))

    def test_current_copy_does_not_overwrite_disconnected_former_root_location(self):
        former_path = self.file("alice/100_1.jpg")
        self.record(former_path, "author")
        [original] = self.store.records()
        new_output = Path(self.temp.name) / "new-output"
        records = new_output / ".luvbox" / "account"
        records.mkdir(parents=True)
        with closing(sqlite3.connect(self.store.path)) as source, \
                closing(sqlite3.connect(records / ".luvbox-history.sqlite3")) as target:
            source.backup(target)
            target.execute("INSERT OR REPLACE INTO catalog_meta(key,value) VALUES ('allowed_media_roots',?)",
                           (json.dumps([str(self.output), str(new_output)]),))
            target.commit()
        relocated = HistoryStore(records, media_dir=new_output, output_dir=new_output)
        self.addCleanup(relocated.close)
        former_path.unlink()
        current_path = new_output / "100_1.jpg"
        current_path.write_bytes(b"current-copy")

        relocated.import_legacy(refresh=True)

        rows = {row["folder_mode"]: row for row in relocated.records()}
        self.assertEqual(set(rows), {"author", "flat"})
        self.assertEqual((rows["author"]["path"], rows["author"]["saved_at"]),
                         (original["path"], original["saved_at"]))
        self.assertIsNone(rows["author"]["filesize"])
        self.assertEqual(rows["flat"]["path"], str(current_path.resolve()))
        self.assertEqual(relocated.summary()["files"], 1)
        former_path.write_bytes(b"reconnected")
        self.assertEqual(relocated.summary()["files"], 2)

    def test_distinct_existing_copies_of_the_same_media_count_as_two_files(self):
        self.record(self.file("alice/100_1.jpg", b"a"), "author")
        self.record(self.file("100_1.jpg", b"bb"), "flat")

        summary = self.store.summary()

        self.assertEqual((summary["files"], summary["bytes"], summary["current_posts"]), (2, 3, 1))
        self.assertEqual(summary["duplicate_records"], 0)

    def test_deleted_file_is_excluded_before_any_folder_refresh(self):
        path = self.file("100_1.jpg")
        self.record(path)
        original = self.store.records()
        path.unlink()

        summary = self.store.summary()

        self.assertEqual((summary["files"], summary["bytes"], summary["photo"]), (0, 0, 0))
        self.assertEqual((summary["posts"], summary["current_posts"], summary["record_count"]), (1, 0, 1))
        self.assertEqual(self.store.statistics()["authors"], [])
        self.assertEqual(self.store.records(), original)

    def test_resized_file_uses_current_size_without_rewriting_download_history(self):
        path = self.file("100_1.jpg", b"old")
        self.record(path)
        original = self.store.records()
        path.write_bytes(b"new-longer-media")

        self.assertEqual(self.store.summary()["bytes"], len(b"new-longer-media"))
        self.assertEqual(self.store.records(), original)

    def test_archive_only_rows_are_history_rather_than_existing_files(self):
        archive = self.output / ".luvbox-archive.sqlite3"
        with closing(sqlite3.connect(archive)) as db:
            db.execute("CREATE TABLE archive (entry TEXT PRIMARY KEY) WITHOUT ROWID")
            db.execute("INSERT INTO archive(entry) VALUES ('twitter100_1')")
            db.commit()
        self.store.import_legacy()
        before = archive.read_bytes()

        summary = self.store.summary()

        self.assertEqual((summary["files"], summary["bytes"], summary["record_count"]), (0, 0, 1))
        self.assertEqual((summary["posts"], summary["current_posts"]), (1, 0))
        self.assertEqual(self.store.statistics()["months"]["saved"], [])
        self.assertEqual(len(self.store.records()), 1)
        self.assertEqual(archive.read_bytes(), before)

    def test_authors_months_media_and_metadata_counts_share_the_unique_files(self):
        photo = self.file("100_1.jpg", b"aa")
        self.record(photo, "author", embedded=True)
        self.record(photo, "flat", embedded=True)
        gif = self.file("200_gif_1.mp4", b"bbb")
        self.record(gif, "author", tweet="200", kind="animated_gif", month=8, embedded=True)
        self.record(gif, "flat", tweet="200", kind="animated_gif", month=8, embedded=True)
        self.record(self.file("300_video_1.mp4", b"cccc"), tweet="300", kind="video", author="bob")

        overview = self.store.overview()
        stats = overview["stats"]

        self.assertEqual((overview["files"], overview["bytes"], overview["record_count"]), (3, 9, 5))
        self.assertEqual(overview["counts"], {"photo": 1, "video": 1, "animated_gif": 1})
        self.assertEqual((overview["embedded_sources"], overview["embedded_authors"], overview["mp4_gifs"]), (2, 2, 1))
        self.assertEqual([(row["author"], row["files"], row["posts"]) for row in stats["authors"]],
                         [("alice", 2, 2), ("bob", 1, 1)])
        self.assertEqual(stats["months"]["posted"], [("2026-08", 1), ("2026-09", 2)])
        self.assertEqual(sum(count for _, count in stats["months"]["saved"]), overview["files"])
        self.assertEqual(self.store.statistics(), stats)

    def test_cached_overview_preserves_fast_preview_until_files_are_verified(self):
        path = self.file("100_1.jpg")
        self.record(path, "author")
        self.record(path, "flat")
        path.unlink()

        cached = self.store.overview(verify_files=False)
        verified = self.store.overview()

        self.assertEqual((cached["files"], cached["duplicate_records"]), (1, 1))
        self.assertEqual(sum(row["files"] for row in cached["stats"]["authors"]), 1)
        self.assertEqual((verified["files"], verified["stats"]["authors"]), (0, []))

    @unittest.skipUnless(os.name == "nt", "Windows paths ignore letter case")
    def test_windows_case_variants_of_one_path_count_once(self):
        path = self.file("100_1.JPG")
        self.record(path, "author")
        self.record(path, "flat")
        with closing(sqlite3.connect(self.store.path)) as db:
            db.execute("UPDATE media SET path=? WHERE folder_mode='author'", (str(path).upper(),))
            db.commit()

        summary = self.store.summary()

        self.assertEqual((summary["files"], summary["duplicate_records"]), (1, 1))
        self.assertEqual(self.store.overview(verify_files=False)["files"], 1)

    def test_former_output_file_disconnection_and_reconnection_are_reflected(self):
        path = self.file("100_1.jpg")
        self.record(path)
        new_output = Path(self.temp.name) / "new-output"
        new_records = new_output / ".luvbox" / "account"
        new_records.mkdir(parents=True)
        with closing(sqlite3.connect(self.store.path)) as source, \
                closing(sqlite3.connect(new_records / ".luvbox-history.sqlite3")) as target:
            source.backup(target)
            target.execute("INSERT OR REPLACE INTO catalog_meta(key,value) VALUES ('allowed_media_roots',?)",
                           (json.dumps([str(self.output), str(new_output)]),))
            target.commit()
        relocated = HistoryStore(new_records, media_dir=new_output, output_dir=new_output)
        self.addCleanup(relocated.close)
        self.assertEqual(relocated.summary()["files"], 1)
        path.unlink()
        self.assertEqual(relocated.summary()["files"], 0)
        path.write_bytes(b"returned")
        self.assertEqual((relocated.summary()["files"], relocated.summary()["bytes"]), (1, 8))
        self.assertEqual(len(relocated.records()), 1)

    def test_untracked_files_in_shared_output_do_not_belong_to_this_account(self):
        self.record(self.file("100_1.jpg"))
        self.file("200_1.jpg", b"another-account-file")

        self.assertEqual(self.store.summary()["files"], 1)

    def test_a_catalog_path_outside_allowed_output_does_not_count(self):
        self.record(self.file("100_1.jpg"))
        outside = Path(self.temp.name) / "outside.jpg"
        outside.write_bytes(b"outside")
        with closing(sqlite3.connect(self.store.path)) as db:
            db.execute("UPDATE media SET path=?", (str(outside),))
            db.commit()

        summary = self.store.summary()

        self.assertEqual((summary["files"], summary["bytes"], summary["record_count"]), (0, 0, 1))

    def test_file_replaced_by_a_link_is_not_counted(self):
        path = self.file("100_1.jpg")
        self.record(path)
        outside = Path(self.temp.name) / "outside.jpg"
        outside.write_bytes(b"outside")
        path.unlink()
        try:
            path.symlink_to(outside)
        except OSError as error:
            self.skipTest(f"Creating file symlinks is unavailable: {error}")

        summary = self.store.summary()

        self.assertEqual((summary["files"], summary["bytes"], summary["record_count"]), (0, 0, 1))

    def test_like_months_keep_recorded_history_after_all_files_are_deleted(self):
        first_path = self.file("100_1.jpg")
        second_path = self.file("200_1.jpg")
        self.record(first_path)
        self.record(second_path, tweet="200")

        first_run = self.store.start_run("flat")
        order = self.store.like_order(first_run)
        order.visit("100")
        order.flush()
        self.store.finish_run(first_run, "finished")
        time.sleep(0.02)  # Windows has a coarse clock; distinguish the two runs.
        second_run = self.store.start_run("flat")
        order = self.store.like_order(second_run)
        for tweet in ("200", "100"):
            order.visit(tweet)
        order.flush()
        self.store.finish_run(second_run, "finished")
        liked_before = self.store.statistics()["months"]["liked"]
        self.assertEqual(sum(count for _, count in liked_before), 1)
        first_path.unlink()
        second_path.unlink()

        overview = self.store.overview()

        self.assertEqual((overview["files"], overview["posts"], overview["record_count"]), (0, 2, 2))
        self.assertEqual(overview["stats"]["months"]["liked"], liked_before)
        self.assertEqual(overview["stats"]["months"]["saved"], [])
        self.assertEqual(overview["stats"]["months"]["posted"], [])


if __name__ == "__main__":
    unittest.main()

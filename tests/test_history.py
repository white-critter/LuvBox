"""Offline catalog tests with synthetic local archives and media files."""

import csv
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from xlikesaver.history import HistoryError, HistoryStore


SECRET = "offline-cookie-never-store-in-history"
KST = timezone(timedelta(hours=9))


def tweet_id(posted):
    return str((int(posted.timestamp() * 1000) - 1_288_834_974_657) << 22)


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.account = Path(self.temp.name) / "account"
        self.store = HistoryStore(self.account)
        self.addCleanup(self.store.close)

    def file(self, name, content=b"offline-media"):
        path = self.account / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def metadata(self, number=1, kind="photo", *, ident="100", author="alice", posted=None):
        return {"tweet_id": ident, "num": number, "type": kind, "author": {"name": author},
                "date": posted, "url": "https://invalid.example/?token=" + SECRET,
                "auth_token": SECRET, "cookies": {"auth_token": SECRET}}

    def archive(self, entries, mode="author"):
        name = ".xlikesaver-archive.sqlite3" if mode == "author" else f".xlikesaver-archive.{mode}.sqlite3"
        path = self.account / name
        with closing(sqlite3.connect(path)) as db:
            db.execute("CREATE TABLE archive (entry TEXT PRIMARY KEY) WITHOUT ROWID")
            db.executemany("INSERT INTO archive(entry) VALUES (?)", ((entry,) for entry in entries))
            db.commit()
        return path

    def test_source_metadata_status_and_csv_do_not_change_original_save_time(self):
        path = self.file("100_1.jpg")
        data = self.metadata()
        self.store.record_media("flat", data, path)
        original = self.store.records()[0]
        self.assertEqual(original["source_url"], "https://x.com/i/web/status/100")
        self.assertEqual(self.store.get_metadata_status("flat", data), "pending")
        path.write_bytes(b"updated-offline-file-with-tags")
        self.store.mark_metadata(original, "embedded")
        record = self.store.records()[0]
        self.assertEqual(record["saved_at"], original["saved_at"])
        self.assertEqual(record["filesize"], path.stat().st_size)
        self.assertEqual(self.store.get_metadata_status("flat", data), "embedded")
        self.store.import_legacy(refresh=True)
        self.assertEqual(self.store.records()[0]["metadata_status"], "embedded")
        csv_path = Path(self.temp.name) / "sources.csv"
        self.store.export_records(csv_path)
        with csv_path.open(encoding="utf-8-sig", newline="") as file:
            row = next(csv.DictReader(file))
        self.assertEqual(row["source_url"], "https://x.com/i/web/status/100")
        self.assertNotIn(SECRET, csv_path.read_text(encoding="utf-8-sig"))

    def test_copied_catalog_refresh_matches_new_paths_without_accessing_old_folder(self):
        self.store.record_media("flat", self.metadata(), self.file("100_1.jpg"))
        new_account = Path(self.temp.name) / "moved_account"
        new_account.mkdir()
        new_file = new_account / "100_1.jpg"
        new_file.write_bytes(b"moved-local-media")
        with closing(sqlite3.connect(new_account / ".xlikesaver-history.sqlite3")) as copied:
            self.store._db.backup(copied)
        copied = HistoryStore(new_account)
        self.addCleanup(copied.close)
        copied.import_legacy(refresh=True)
        row = copied.records()[0]
        self.assertEqual(row["path"], str(new_file))
        self.assertEqual(row["filesize"], new_file.stat().st_size)

    def relocated_store(self):
        """A verified catalog transfer authorizes its former selected output."""
        new_output = Path(self.temp.name) / "new-output"
        records = new_output / ".xlikesaver" / "account"
        records.mkdir(parents=True)
        with closing(sqlite3.connect(records / ".xlikesaver-history.sqlite3")) as copied:
            self.store._db.backup(copied)
            copied.execute("INSERT OR REPLACE INTO catalog_meta(key,value) VALUES ('allowed_media_roots',?)",
                           (json.dumps([str(self.account), str(new_output)]),))
            copied.commit()
        copied = HistoryStore(records, media_dir=new_output, output_dir=new_output)
        self.addCleanup(copied.close)
        return copied

    def test_relocated_catalog_retains_existing_paths_save_time_and_source_status(self):
        path = self.file("alice/100_1.jpg")
        self.store.record_media("author", self.metadata(), path)
        self.store.mark_metadata(self.store.records()[0], "embedded")
        original = self.store.records()[0]
        relocated = self.relocated_store()
        relocated.import_legacy(refresh=True)
        row = relocated.records()[0]
        for field in ("path", "saved_at", "source_url", "metadata_status", "author", "filesize"):
            self.assertEqual(row[field], original[field])
        self.assertEqual(relocated.media_roots, (relocated.output_dir, self.account))
        self.assertTrue(relocated.is_media_path(path))
        self.assertFalse(relocated.is_download_path(path))
        self.assertTrue(relocated.is_download_path(relocated.output_dir / "100_2.jpg"))
        path.write_bytes(b"updated-tags")
        relocated.mark_metadata(row, "embedded")
        updated = relocated.records()[0]
        self.assertEqual(updated["filesize"], len(b"updated-tags"))
        self.assertEqual(updated["saved_at"], original["saved_at"])

    def test_old_roots_only_authorize_known_media_and_never_records_or_linked_files(self):
        known = self.file("100_1.jpg")
        unrelated = self.file("100_2.jpg")
        self.store.record_media("flat", self.metadata(), known)
        relocated = self.relocated_store()
        self.assertFalse(relocated.is_media_path(unrelated))
        self.assertFalse(relocated.is_media_path(self.account / ".xlikesaver" / "100_1.jpg"))
        self.assertFalse(relocated.is_download_path(known))
        with self.assertRaises(HistoryError):
            relocated.record_media("flat", self.metadata(number=2), unrelated)
        relocated.import_legacy(refresh=True)
        self.assertEqual(len(relocated.records()), 1)
        # A historical root itself can later become a junction/symlink.
        is_symlink = Path.is_symlink
        with patch.object(Path, "is_symlink", lambda candidate: candidate == self.account or is_symlink(candidate)):
            self.assertFalse(relocated.is_media_path(known))

    def test_missing_former_file_keeps_location_until_drive_or_file_returns(self):
        path = self.file("100_1.jpg")
        self.store.record_media("flat", self.metadata(), path)
        relocated = self.relocated_store()
        original = relocated.records()[0]
        path.unlink()
        relocated.import_legacy(refresh=True)
        missing = relocated.records()[0]
        self.assertEqual(missing["path"], str(path))
        self.assertIsNone(missing["filesize"])
        self.assertEqual(missing["saved_at"], original["saved_at"])
        path.write_bytes(b"reconnected")
        relocated.import_legacy(refresh=True)
        self.assertEqual(relocated.records()[0]["filesize"], len(b"reconnected"))

    def test_local_scan_does_not_replace_valid_former_file_with_duplicate(self):
        old = self.file("100_1.jpg")
        self.store.record_media("flat", self.metadata(), old)
        relocated = self.relocated_store()
        (relocated.output_dir / old.name).write_bytes(b"unrelated-duplicate")
        relocated.import_legacy(refresh=True)
        self.assertEqual(relocated.records()[0]["path"], str(old))

    def test_records_three_media_types_without_ordinal_collision(self):
        for kind, name, payload in (("photo", "alice/100_1.jpg", b"aa"),
                                    ("video", "alice/100_video_1.mp4", b"bbb"),
                                    ("animated_gif", "alice/100_gif_1.mp4", b"cccc")):
            self.store.record_media("author", self.metadata(kind=kind), self.file(name, payload))
        summary = self.store.summary()
        self.assertEqual((summary["files"], summary["posts"], summary["bytes"]), (3, 1, 9))
        self.assertEqual(summary["counts"], {"photo": 1, "video": 1, "animated_gif": 1})
        self.assertEqual(summary["unknown_bytes"], 0)
        self.assertEqual(len(self.store.records()), 3)

    def test_exact_media_lookup_separates_layout_type_and_number_without_writing(self):
        identities = (("date", "photo", 1, "2026-10-04/100_1.jpg"),
                      ("date", "video", 1, "2026-10-04/100_video_1.mp4"),
                      ("date", "photo", 2, "2026-10-04/100_2.jpg"),
                      ("flat", "photo", 1, "100_1.jpg"))
        for mode, kind, number, name in identities:
            self.store.record_media(mode, self.metadata(number=number, kind=kind), self.file(name))
        changes = self.store._db.total_changes
        for mode, kind, number, name in identities:
            row = self.store.media_record(mode, self.metadata(number=number, kind=kind))
            self.assertEqual(row["path"], str(self.account / name))
            self.assertTrue(row["saved_at"])
        self.assertIsNone(self.store.media_record("date", self.metadata(kind="animated_gif")))
        self.assertIsNone(self.store.media_record("author", self.metadata()))
        self.assertIsNone(self.store.media_record("date", self.metadata(ident="101")))
        self.assertEqual(self.store._db.total_changes, changes)

    def test_duplicate_skip_enriches_without_replacing_known_saved_time(self):
        path = self.file("alice/100_1.jpg")
        with patch("xlikesaver.history._now", return_value="2026-10-04T22:00:00+09:00"):
            self.store.record_media("author", self.metadata(author="", posted=None), path)
        with patch("xlikesaver.history._now", return_value="2026-10-05T22:00:00+09:00"):
            self.store.record_media("author", self.metadata(posted=datetime(2024, 1, 1)), path, status="existing")
        row = self.store.records()[0]
        self.assertEqual(row["saved_at"], "2026-10-04T22:00:00+09:00")
        self.assertEqual(row["author"], "alice")
        self.assertEqual(row["posted_at"], "2024-01-01T09:00:00+09:00")
        self.assertEqual(row["status"], "saved")
        self.assertEqual(self.store.summary()["files"], 1)

    def test_old_existing_file_save_time_stays_unknown_and_metadata_is_whitelisted(self):
        path = self.file("alice/100_1.jpg")
        os.utime(path, (946684800, 946684800))
        self.store.record_media("author", self.metadata(posted=datetime(2026, 10, 3, 16)), path, status="existing")
        row = self.store.records()[0]
        self.assertIsNone(row["saved_at"])
        self.assertEqual(row["posted_at"], "2026-10-04T01:00:00+09:00")
        self.assertNotIn(SECRET, json.dumps(row))
        self.assertNotIn("url", row)
        self.assertNotIn("cookies", row)

    def test_archive_only_moved_files_have_unknown_path_size_and_saved_time(self):
        posted = datetime(2024, 3, 2, 16, tzinfo=timezone.utc)
        ident = tweet_id(posted)
        archive = self.archive([f"twitter{ident}_1", "twitter100_1", "ignore-unrecognized-entry"])
        before = archive.read_bytes()
        self.store.import_legacy()
        records = {row["tweet_id"]: row for row in self.store.records()}
        row = records[ident]
        self.assertEqual(row["posted_at"], "2024-03-03T01:00:00+09:00")
        self.assertEqual(row["posted_source"], "id_estimate")
        self.assertEqual(row["path"], "")
        self.assertIsNone(row["filesize"])
        self.assertIsNone(row["saved_at"])
        self.assertIsNone(records["100"]["posted_at"])
        self.assertEqual(archive.read_bytes(), before)
        summary = self.store.summary()
        self.assertEqual((summary["files"], summary["unknown_bytes"], summary["estimated_dates"]), (2, 2, 1))
        self.assertEqual(summary["archivepaths"], [str(archive)])

    def test_import_merges_archive_and_file_by_kind_layout_and_number(self):
        entries = ["twitter100_1", "twitter100_video_1", "twitter100_animated_gif_1"]
        archive = self.archive(entries)
        before = archive.read_bytes()
        self.file("alice/100_1.jpg", b"a")
        self.file("alice/100_video_1.mp4", b"bb")
        self.file("alice/100_gif_1.mp4", b"ccc")
        self.file("100_1.png", b"dddd")
        self.file("2026-10-04/100_1.webp", b"eeeee")
        self.file("alice/101_1.gif", b"gif-format-is-not-x-type")
        self.file("alice/100_2.jpg.part", b"partial")
        self.file("alice/random.jpg", b"unrecognized")
        self.file("alice/100_0.jpg", b"invalid-number")
        self.store.import_legacy()
        summary = self.store.summary()
        self.assertEqual(summary["files"], 6)
        self.assertEqual(summary["posts"], 2)
        self.assertEqual(summary["photo"], 4)
        self.assertEqual(summary["video"], 1)
        self.assertEqual(summary["animated_gif"], 1)
        self.assertTrue(all(row["saved_at"] is None for row in self.store.records()))
        self.assertEqual(archive.read_bytes(), before)
        alice_rows = [row for row in self.store.records() if row["folder_mode"] == "author"]
        self.assertTrue(all(row["author"] == "alice" for row in alice_rows))

    def test_default_import_is_cached_and_manual_refresh_finds_new_or_changed_files(self):
        path = self.file("alice/100_1.jpg", b"a")
        first = self.store.import_legacy()
        self.assertTrue(first["imported"])
        self.file("alice/101_1.jpg", b"new")
        with patch.object(self.store, "_files", side_effect=AssertionError("must not rescan")):
            self.assertFalse(self.store.import_legacy()["imported"])
        self.assertEqual(self.store.summary()["files"], 1)
        path.write_bytes(b"changed-length")
        self.store.import_legacy(refresh=True)
        self.assertEqual(self.store.summary()["files"], 2)
        self.assertEqual(next(row for row in self.store.records() if row["tweet_id"] == "100")["filesize"], 14)
        path.unlink()
        self.store.import_legacy(refresh=True)
        row = next(row for row in self.store.records() if row["tweet_id"] == "100")
        self.assertEqual(row["path"], "")
        self.assertIsNone(row["filesize"])

    def test_import_does_not_overwrite_real_metadata_or_actual_save_time(self):
        ident = tweet_id(datetime(2024, 1, 1, tzinfo=timezone.utc))
        path = self.file(f"alice/{ident}_1.jpg")
        self.archive([f"twitter{ident}_1"])
        posted = datetime(2023, 12, 31, 23, tzinfo=timezone.utc)
        with patch("xlikesaver.history._now", return_value="2026-10-04T23:00:00+09:00"):
            self.store.record_media("author", self.metadata(ident=ident, author="Alice", posted=posted), path)
        self.store.import_legacy()
        row = self.store.records()[0]
        self.assertEqual(row["posted_at"], "2024-01-01T08:00:00+09:00")
        self.assertEqual(row["posted_source"], "metadata")
        self.assertEqual(row["saved_at"], "2026-10-04T23:00:00+09:00")
        self.assertEqual(row["author"], "Alice")
        self.assertEqual(row["filename"], path.name)

    def test_runs_preserve_partial_counts_position_and_safe_fields(self):
        ident = tweet_id(datetime(2024, 1, 1, tzinfo=timezone.utc))
        run = self.store.start_run("author")
        self.store.update_run(run, saved=2, skipped=3, size_skipped=1, visited=9,
                              last_tweet_id=ident, last_posted_at=datetime(2024, 1, 1))
        self.store.finish_run(run, "stopped", message="사용자가 중단했습니다.")
        row = self.store.runs()[0]
        self.assertEqual((row["id"], row["status"], row["folder_mode"]), (run, "stopped", "author"))
        self.assertEqual((row["saved"], row["skipped"], row["size_skipped"], row["visited"]), (2, 3, 1, 9))
        self.assertEqual(row["last_tweet_id"], ident)
        self.assertEqual(row["last_posted_at"], "2024-01-01T09:00:00+09:00")
        self.assertTrue(row["finished_at"])
        self.assertEqual(self.store.summary()["last_run"], row)
        later = self.store.start_run("flat")
        self.store.finish_run(later, "error", saved=1, message="저장 오류")
        self.assertEqual(self.store.summary()["last_run"]["status"], "error")
        with self.assertRaises(HistoryError):
            self.store.update_run(later, auth_token=SECRET)

    def test_failed_run_update_is_atomic_and_does_not_destroy_previous_counts(self):
        run = self.store.start_run("author")
        self.store.update_run(run, saved=4)
        with self.assertRaises(HistoryError):
            self.store.finish_run(run, "error", saved=-1)
        row = self.store.runs()[0]
        self.assertEqual((row["saved"], row["status"], row["finished_at"]), (4, "running", None))

    def test_abandoned_run_recovery_keeps_progress_and_unknown_end_time(self):
        finished = self.store.start_run("author")
        self.store.finish_run(finished, "finished", saved=1, message="completed")
        abandoned = self.store.start_run("flat")
        self.store.update_run(abandoned, saved=3, skipped=5, visited=7,
                              last_tweet_id="100", last_posted_at="2026-10-04T10:00:00+09:00")
        original = next(row for row in self.store.runs() if row["id"] == abandoned)
        self.store.close()
        self.store = HistoryStore(self.account)
        self.addCleanup(self.store.close)
        self.assertEqual(self.store.recover_interrupted_runs(), 1)
        recovered = next(row for row in self.store.runs() if row["id"] == abandoned)
        self.assertEqual(recovered["status"], "interrupted")
        self.assertIsNone(recovered["finished_at"])
        for key in ("started_at", "saved", "skipped", "size_skipped", "visited", "last_tweet_id", "last_posted_at"):
            self.assertEqual(recovered[key], original[key])
        self.assertIn("실제 종료 시각은 알 수 없습니다", recovered["message"])
        self.assertEqual(next(row for row in self.store.runs() if row["id"] == finished)["status"], "finished")
        self.assertEqual(self.store.recover_interrupted_runs(), 0)

    def test_record_reader_does_not_mark_active_run_interrupted(self):
        active = self.store.start_run("date")
        observer = HistoryStore(self.account)
        self.addCleanup(observer.close)
        observer.summary()
        self.assertEqual(observer.runs()[0]["id"], active)
        self.assertEqual(observer.runs()[0]["status"], "running")

    def test_abandoned_run_recovery_failure_rolls_back_all_status_changes(self):
        first = self.store.start_run("author")
        second = self.store.start_run("flat")
        self.store.update_run(first, saved=2)
        self.store.update_run(second, saved=3)
        before = self.store.runs()
        self.store._db.execute(f"""CREATE TRIGGER reject_recovery BEFORE UPDATE ON runs
            WHEN NEW.id={second} AND NEW.status='interrupted'
            BEGIN SELECT RAISE(ABORT,'{SECRET}'); END""")
        self.store._db.commit()
        with self.assertRaises(HistoryError) as error:
            self.store.recover_interrupted_runs()
        self.assertNotIn(SECRET, str(error.exception))
        self.assertEqual(self.store.runs(), before)

    def test_wal_reader_observes_autocommitted_writer_progress(self):
        other = HistoryStore(self.account)
        self.addCleanup(other.close)
        run = self.store.start_run("author")
        self.store.update_run(run, visited=3)
        self.store.record_media("author", self.metadata(), self.file("alice/100_1.jpg"))
        self.assertEqual(other.summary()["files"], 1)
        self.assertEqual(other.runs()[0]["visited"], 3)
        self.assertEqual(self.store._db.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(self.store._db.execute("PRAGMA synchronous").fetchone()[0], 2)
        self.assertEqual(other._db.execute("PRAGMA synchronous").fetchone()[0], 2)

    def test_filter_search_pagination_and_formula_safe_csv_export(self):
        for number, kind, author in ((1, "photo", "=danger"), (2, "video", "bob_50%"),
                                     (3, "animated_gif", "@danger")):
            self.store.record_media("flat", self.metadata(number, kind, author=author), self.file(f"100_{number}.mp4"))
        self.assertEqual(len(self.store.records(media_type="video")), 1)
        self.assertEqual(len(self.store.records(query="bob_50%")), 1)
        self.assertEqual(len(self.store.records(query="100_2")), 1)
        self.assertEqual(len(self.store.records(limit=1, offset=1)), 1)
        destination = Path(self.temp.name) / "history.csv"
        self.assertEqual(self.store.export_records(destination), 3)
        with destination.open(encoding="utf-8-sig", newline="") as file:
            rows = list(csv.DictReader(file))
        self.assertIn("'=danger", [row["author"] for row in rows])
        self.assertIn("'@danger", [row["author"] for row in rows])
        self.assertNotIn(SECRET, destination.read_text(encoding="utf-8-sig"))

    def test_outside_paths_and_missing_saved_files_are_rejected_without_catalog_rows(self):
        outside = Path(self.temp.name) / "outside.jpg"
        outside.write_bytes(b"outside")
        for path in (outside, self.account / "missing.jpg"):
            with self.subTest(path=path.name), self.assertRaises(HistoryError) as error:
                self.store.record_media("author", self.metadata(), path)
            self.assertNotIn(str(path), str(error.exception))
        self.assertEqual(self.store.summary()["files"], 0)

    def test_scan_excludes_symlink_targets_and_directory_loops(self):
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        (outside / "100_1.jpg").write_bytes(b"outside")
        link = self.account / "linked"
        try:
            link.symlink_to(outside, target_is_directory=True)
            (self.account / "loop").symlink_to(self.account, target_is_directory=True)
        except OSError:
            self.skipTest("Windows symlink creation is unavailable")
        self.store.import_legacy()
        self.assertEqual(self.store.summary()["files"], 0)

    def test_bad_archive_does_not_prevent_actual_file_import_and_is_not_modified(self):
        archive = self.account / ".xlikesaver-archive.sqlite3"
        archive.write_bytes(b"bad archive " + SECRET.encode())
        self.file("alice/100_1.jpg")
        result = self.store.import_legacy()
        self.assertTrue(result["warnings"])
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertEqual(self.store.summary()["files"], 1)
        self.assertEqual(archive.read_bytes(), b"bad archive " + SECRET.encode())


if __name__ == "__main__":
    unittest.main()

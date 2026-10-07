"""Deletion journals and folder snapshots, using only temporary fake media."""

import itertools
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from xlikesaver.history import _JOINED, HistoryError, HistoryStore
from xlikesaver.recovery import rebuild_history
from xlikesaver.storage import _merge_history, _merge_media


def signature(path):
    value = path.stat()
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns


class DeleteRecordTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        isolated = patch("xlikesaver.protection.state_directory", return_value=self.root / "app" / "protection")
        isolated.start()
        self.addCleanup(isolated.stop)
        self.folder = self.root / "media"
        self.store = HistoryStore(self.folder)
        self.addCleanup(self.store.close)

    def media(self, ident="100", mode="flat", name=None):
        path = self.folder / (name or f"{ident}_1.jpg")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"offline deletion fixture")
        self.store.record_media(mode, {"tweet_id": ident, "num": 1, "type": "photo",
                                      "author": {"name": "alice"}}, path)
        return next(row for row in self.store.records() if row["tweet_id"] == ident and row["folder_mode"] == mode)

    def begin(self, row, keep=True):
        return self.store.begin_deletion("offline-batch", row, keep, signature(Path(row["path"])))

    def recycle(self, row):
        destination = self.root / "fake-bin" / row["filename"]
        destination.parent.mkdir(exist_ok=True)
        Path(row["path"]).rename(destination)
        return str(destination)

    def test_intent_is_durable_without_changing_the_file_or_catalog(self):
        row = self.media()
        ident = self.begin(row)
        self.assertEqual(self.store.records(), [row])
        self.assertTrue(Path(row["path"]).is_file())
        with closing(HistoryStore(self.folder)) as second:
            [pending] = second.pending_deletions()
            self.assertEqual(pending["id"], ident)
            self.assertEqual(json.loads(pending["signature"]), list(signature(Path(row["path"]))))
            self.assertEqual(second.ignored_media(), {("100", 1, "photo")})

    def test_verified_success_updates_actual_counts_but_preserves_history(self):
        row = self.media()
        self.store.record_tweets([("100", "preserved tweet", ["tag"])])
        run = self.store.start_run("flat")
        self.store.finish_run(run, "finished", saved=1)
        ident = self.begin(row)
        receipt = self.recycle(row)
        self.store.finish_deletion(ident, "recycled", "휴지통으로 이동", receipt)
        summary = self.store.overview(verify_files=False)
        self.assertEqual((summary["files"], summary["posts"], summary["ignored_missing"]), (0, 1, 1))
        [after] = self.store.records()
        self.assertEqual((after["path"], after["last_path"], after["content"]), ("", row["path"], "preserved tweet"))
        self.assertEqual((after["saved_at"], after["source_url"]), (row["saved_at"], row["source_url"]))
        self.assertEqual(self.store.runs()[0]["saved"], 1)
        [deletion] = self.store.deletion_page()["rows"]
        self.assertEqual((deletion["status"], deletion["recycle_path"]), ("recycled", receipt))
        self.assertEqual(self.store.pending_deletions(), [])

    def test_unchecked_keep_deleted_explicitly_allows_download_again(self):
        row = self.media()
        self.store.set_missing_ignored([("100", 1, "photo")], True)
        ident = self.begin(row, keep=False)
        self.store.finish_deletion(ident, "recycled", recycle_path=self.recycle(row))
        self.assertEqual(self.store.ignored_media(), set())
        self.assertEqual(self.store.summary(verify_files=False)["missing_files"], 1)

    def test_same_path_layouts_clear_together_while_other_real_copies_remain(self):
        row = self.media()
        self.store.record_media("author", {"tweet_id": "100", "num": 1, "type": "photo"}, row["path"])
        other = self.media(mode="date", name="2026-10-07/100_1.jpg")
        ident = self.begin(row)
        self.store.finish_deletion(ident, "recycled", recycle_path=self.recycle(row))
        records = {item["folder_mode"]: item for item in self.store.records()}
        self.assertEqual((records["flat"]["path"], records["author"]["path"]), ("", ""))
        self.assertEqual(records["date"]["path"], other["path"])
        self.assertEqual(self.store.summary(verify_files=False)["files"], 1)

    @unittest.skipUnless(os.name == "nt", "Windows path casing")
    def test_deleted_layout_aliases_with_different_path_case_are_all_hidden(self):
        row = self.media()
        self.store.record_media("author", {"tweet_id": "100", "num": 1, "type": "photo"}, row["path"].upper())
        ident = self.begin(row)
        self.store.finish_deletion(ident, "recycled", recycle_path=self.recycle(row))
        self.assertEqual(self.store.record_page()["total"], 2)
        self.assertEqual(self.store.record_page(include_deleted=False)["total"], 0)

    def test_failed_or_cancelled_move_preserves_file_counts_and_existing_choice(self):
        for status in ("failed", "cancelled"):
            with self.subTest(status=status):
                row = self.media(ident="100" if status == "failed" else "200")
                before = self.store.records()
                ident = self.begin(row)
                self.store.finish_deletion(ident, status, "offline unavailable bin")
                self.assertEqual(self.store.records(), before)
                self.assertTrue(Path(row["path"]).is_file())
        self.assertEqual(self.store.ignored_media(), set())
        self.assertEqual(self.store.summary(verify_files=False)["files"], 2)

    def test_completion_is_idempotent_and_keeps_a_new_copy_at_the_original_path(self):
        row = self.media()
        ident = self.begin(row)
        receipt = self.recycle(row)
        self.store.finish_deletion(ident, "recycled", recycle_path=receipt)
        Path(row["path"]).write_bytes(b"newly downloaded copy")
        self.store.record_media("flat", {"tweet_id": "100", "num": 1, "type": "photo"}, row["path"])
        self.store.finish_deletion(ident, "recycled", "repeated completion", receipt)
        self.assertEqual(self.store.records()[0]["path"], row["path"])
        self.assertEqual(self.store.summary(verify_files=False)["files"], 1)

    def test_file_recreated_before_journal_commit_still_counts_as_present(self):
        row = self.media()
        ident = self.begin(row)
        receipt = self.recycle(row)
        Path(row["path"]).write_bytes(b"external recreated copy")
        self.store.finish_deletion(ident, "recycled", recycle_path=receipt)
        self.assertEqual(self.store.records()[0]["path"], row["path"])
        self.assertEqual(self.store.summary()["files"], 1)

    def test_interrupted_intents_never_claim_a_missing_file_was_recycled(self):
        existing, missing = self.media(), self.media("200")
        self.begin(existing)
        self.begin(missing)
        self.recycle(missing)
        result = self.store.reconcile_deletions()
        self.assertEqual(result, {"interrupted": 2, "missing": 1})
        self.assertTrue(Path(existing["path"]).is_file())
        rows = {row["tweet_id"]: row for row in self.store.records()}
        self.assertEqual(rows["100"]["path"], existing["path"])
        self.assertEqual((rows["200"]["path"], rows["200"]["last_path"]), ("", missing["path"]))
        self.assertEqual(self.store.ignored_media(), {("200", 1, "photo")})
        self.assertEqual({item["status"] for item in self.store.deletion_page()["rows"]}, {"interrupted"})
        self.assertTrue(all(not item["recycle_path"] for item in self.store.deletion_page()["rows"]))
        self.assertEqual(self.store.summary(verify_files=False)["files"], 1)
        self.assertEqual(self.store.reconcile_deletions(), {"interrupted": 0, "missing": 0})

    def test_inaccessible_original_is_never_cleared_as_missing(self):
        row = self.media()
        self.begin(row)
        original = Path.lstat
        def denied(path, *args, **kwargs):
            if os.path.abspath(path) == row["path"]:
                raise PermissionError("offline permission fixture")
            return original(path, *args, **kwargs)
        with patch.object(Path, "lstat", denied):
            self.assertEqual(self.store.reconcile_deletions()["missing"], 0)
        self.assertEqual(self.store.records()[0]["path"], row["path"])
        self.assertEqual(self.store.ignored_media(), set())

    def test_library_rescan_before_crash_recovery_preserves_original_location_and_tombstone(self):
        row = self.media()
        self.store.record_media("author", {"tweet_id": "100", "num": 1, "type": "photo"}, row["path"])
        self.begin(row)
        self.recycle(row)
        # A read-only library refresh may see the vanished file before the
        # next protected worker settles its unfinished delete intent.
        self.store.import_legacy(refresh=True)
        self.assertTrue(all(item["last_path"] == row["path"] for item in self.store.records()))
        self.assertEqual(self.store.reconcile_deletions(), {"interrupted": 1, "missing": 1})
        self.assertEqual(self.store.record_page(include_deleted=False)["total"], 0)
        self.assertEqual(self.store.ignored_media(), {("100", 1, "photo")})
        self.assertEqual(self.store.deletion_page()["rows"][0]["status"], "interrupted")
        for item in self.store.records():
            self.assertGreater(item["updated_at"], row["updated_at"])
        # A previous full-path folder snapshot cannot refill the deleted path.
        self.assertEqual(_merge_media(row, self.store.records()[0])["path"], "")

    def test_delete_pages_reach_every_entry_and_clamp_after_refresh(self):
        row = self.media()
        for _ in range(7):
            ident = self.begin(row, keep=False)
            self.store.finish_deletion(ident, "cancelled")
        first, last = self.store.deletion_page(0, 3), self.store.deletion_page(99, 3)
        self.assertEqual((first["total"], first["pages"], len(first["rows"])), (7, 3, 3))
        self.assertEqual((last["page"], len(last["rows"])), (2, 1))
        identities = {item["id"] for page in range(3) for item in self.store.deletion_page(page, 3)["rows"]}
        self.assertEqual(len(identities), 7)

    def test_library_filter_hides_only_removed_files_and_restoration_returns_them(self):
        removed, retained, manually_missing = self.media(), self.media("200"), self.media("300")
        ident = self.begin(removed)
        self.store.finish_deletion(ident, "recycled", recycle_path=self.recycle(removed))
        Path(manually_missing["path"]).unlink()
        self.store.import_legacy(refresh=True)
        visible = self.store.record_page(page_size=1, include_deleted=False)
        self.assertEqual((visible["total"], visible["pages"]), (2, 2))
        self.assertEqual({row["tweet_id"] for row in self.store.records(include_deleted=False)}, {"200", "300"})
        self.assertEqual(self.store.record_page()["total"], 3)
        Path(removed["path"]).write_bytes(b"restored offline copy")
        self.store.import_legacy(refresh=True)
        self.assertEqual(self.store.record_page(include_deleted=False)["total"], 3)
        self.assertEqual(self.store.summary()["files"], 2)

    def test_library_filter_keeps_other_present_copies_of_the_deleted_media(self):
        row = self.media()
        other = self.media(mode="author", name="alice/100_1.jpg")
        ident = self.begin(row)
        self.store.finish_deletion(ident, "recycled", recycle_path=self.recycle(row))
        [visible] = self.store.records(include_deleted=False)
        self.assertEqual((visible["folder_mode"], visible["path"]), ("author", other["path"]))

    def test_invalid_or_unrecorded_intents_do_not_create_a_journal(self):
        row = self.media()
        changed = dict(row, path=str(self.root / "elsewhere.jpg"))
        cases = [("bad batch", row, True, signature(Path(row["path"]))),
                 ("batch", changed, True, (1, 2, 3, 4)),
                 ("batch", row, 1, (1, 2, 3, 4)), ("batch", row, True, (1, 2, 3)),
                 ("batch", dict(row, tweet_id="200"), True, (1, 2, 3, 4))]
        for args in cases:
            with self.subTest(args=args), self.assertRaises(HistoryError):
                self.store.begin_deletion(*args)
        self.assertEqual(self.store.deletion_page()["total"], 0)

    def test_snapshot_merge_keeps_finished_journal_and_deleted_path(self):
        row = self.media()
        ident = self.begin(row)
        other_folder = self.root / "other-records"
        other_folder.mkdir()
        other_path = other_folder / ".xlikesaver-history.sqlite3"
        with closing(sqlite3.connect(other_path)) as db:
            self.store._db.backup(db)
        self.store.finish_deletion(ident, "recycled", recycle_path=self.recycle(row))
        with closing(sqlite3.connect(other_path)) as target, closing(sqlite3.connect(self.store.path)) as source:
            with target:
                _merge_history(target, source)
                _merge_history(target, source)
            deletion = target.execute("SELECT status FROM deletion_items WHERE id=?", (ident,)).fetchone()
            saved = target.execute("SELECT path,last_path FROM media").fetchone()
            self.assertEqual((deletion[0], saved), ("recycled", ("", row["path"])))
            self.assertEqual(target.execute("SELECT COUNT(*) FROM deletion_items").fetchone()[0], 1)
        with closing(sqlite3.connect(self.store.path)) as target, closing(sqlite3.connect(other_path)) as source:
            with target:
                _merge_history(target, source)
        self.assertEqual(self.store.deletion_page()["rows"][0]["status"], "recycled")
        self.assertEqual(self.store.records()[0]["path"], "")

    def test_merge_prefers_a_present_copy_over_a_deleted_old_location(self):
        row = self.media()
        ident = self.begin(row)
        self.store.finish_deletion(ident, "recycled", recycle_path=self.recycle(row))
        deleted = self.store.records()[0]
        Path(row["path"]).write_bytes(b"present restored copy")
        self.assertEqual(_merge_media(row, deleted)["path"], row["path"])

    def test_recovery_keeps_latest_receipt_when_fallback_has_a_finished_journal(self):
        row = self.media()
        ident = self.begin(row)
        earlier = self.root / "pending.sqlite3"
        with closing(sqlite3.connect(earlier)) as db:
            self.store._db.backup(db)
        self.store.finish_deletion(ident, "recycled", recycle_path=self.recycle(row))
        rebuilt = self.root / "rebuilt.sqlite3"
        result = rebuild_history(earlier, rebuilt, fallback=self.store.path)
        self.assertEqual(result["counts"]["deletion_items"], 1)
        with closing(sqlite3.connect(rebuilt)) as db:
            self.assertEqual(db.execute("SELECT status FROM deletion_items WHERE id=?", (ident,)).fetchone()[0], "recycled")
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")


    def tick(self):
        # Windows clocks can repeat a time stamp within one test; keep them ordered.
        counter = itertools.count()
        clock = patch("xlikesaver.history._now", side_effect=lambda: "2026-10-07T12:00:00.%06d+09:00" % next(counter))
        clock.start()
        self.addCleanup(clock.stop)

    def test_library_filter_finds_deletion_receipts_by_index(self):
        where, params = HistoryStore._record_filter("all", "", None, "", False)
        plan = " ".join(row[3] for row in self.store._db.execute(
            "EXPLAIN QUERY PLAN SELECT COUNT(*) FROM " + _JOINED + where, params))
        self.assertIn("deletion_identity", plan)

    def test_completion_never_reads_the_whole_library(self):
        rows = [self.media(str(ident)) for ident in range(100, 110)]
        ident = self.begin(rows[0])
        receipt = self.recycle(rows[0])
        statements = []
        self.store._db.set_trace_callback(statements.append)
        try:
            self.store.finish_deletion(ident, "recycled", recycle_path=receipt)
        finally:
            self.store._db.set_trace_callback(None)
        lookups = [sql for sql in statements if sql.lstrip().upper().startswith("SELECT") and " FROM media " in sql]
        self.assertTrue(lookups)
        for sql in lookups:
            plan = " ".join(row[3] for row in self.store._db.execute("EXPLAIN QUERY PLAN " + sql))
            self.assertNotIn("SCAN media", plan, sql)
        self.assertEqual(self.store.summary(verify_files=False)["files"], 9)

    def test_restoring_a_recycled_file_allows_downloading_it_again(self):
        self.tick()
        row = self.media()
        ident = self.begin(row)
        receipt = self.recycle(row)
        self.store.finish_deletion(ident, "recycled", recycle_path=receipt)
        self.assertEqual(self.store.ignored_media(), {("100", 1, "photo")})
        Path(receipt).rename(row["path"])  # Restored from the Recycle Bin.
        self.store.import_legacy(refresh=True)
        self.assertEqual(self.store.ignored_media(), set())
        self.assertEqual(self.store.summary()["files"], 1)
        self.assertEqual(self.store.record_page(include_deleted=False)["total"], 1)
        # The deletion history itself stays as it happened.
        self.assertEqual(self.store.deletion_page()["rows"][0]["status"], "recycled")
        self.store.import_legacy(refresh=True)
        self.assertEqual(self.store.ignored_media(), set())

    def test_restoring_one_copy_keeps_a_later_choice_for_another_deleted_copy(self):
        self.tick()
        flat = self.media()
        self.media(mode="author", name="alice/100_1.jpg")
        first = self.begin(flat)
        flat_receipt = self.recycle(flat)
        self.store.finish_deletion(first, "recycled", recycle_path=flat_receipt)
        author = next(item for item in self.store.records() if item["folder_mode"] == "author")
        second = self.begin(author)
        author_receipt = self.root / "fake-bin" / "author-copy.jpg"
        Path(author["path"]).rename(author_receipt)
        self.store.finish_deletion(second, "recycled", recycle_path=str(author_receipt))
        Path(flat_receipt).rename(flat["path"])  # Only the first copy is restored.
        self.store.import_legacy(refresh=True)
        self.assertEqual(self.store.ignored_media(), {("100", 1, "photo")})
        self.assertEqual({item["folder_mode"] for item in self.store.records(include_deleted=False)}, {"flat"})

    def test_surviving_file_of_an_interrupted_deletion_keeps_an_earlier_choice(self):
        self.tick()
        row = self.media()
        self.store.set_missing_ignored([("100", 1, "photo")], True)
        ident = self.begin(row)
        self.store.finish_deletion(ident, "interrupted", "fixture interruption before any move")
        self.store.import_legacy(refresh=True)
        self.assertEqual(self.store.ignored_media(), {("100", 1, "photo")})


if __name__ == "__main__":
    unittest.main()

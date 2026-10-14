"""Deletion worker contracts using only temporary catalogs and mocked Shell."""

import json
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from luvbox import deletion
from luvbox.history import HistoryError, HistoryStore
from luvbox.protection import AccountGuard, ProtectionError
from luvbox.recycling import RecyclingError, file_signature
from luvbox.storage import OutputGuard, resolve_storage


class DeletionWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="luvbox-deletion-test-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.protection = patch("luvbox.protection.state_directory",
                                return_value=self.base / "app" / "request-protection")
        self.protection.start()
        self.addCleanup(self.protection.stop)
        self.output = self.base / "media"
        self.output.mkdir()
        self.paths = resolve_storage(self.output, "alice")
        self.events = []
        self.stop = threading.Event()
        self.kept = self.base / "mock-recycled-fixtures"
        self.kept.mkdir()

    def row(self, tweet, number=1, mode="flat", path=None, kind="photo"):
        path = path or self.output / (tweet + "_" + str(number) + ".jpg")
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_bytes(("fixture " + tweet + " " + str(number)).encode())
        with closing(HistoryStore.for_storage(self.paths)) as store:
            store.record_media(mode, {"tweet_id": tweet, "num": number, "type": kind,
                                     "author": {"name": "fixture_writer"}, "date": "2026-10-07T12:00:00+09:00"}, path)
            return next(row for row in store.records() if row["tweet_id"] == tweet and row["num"] == number
                        and row["folder_mode"] == mode and row["media_type"] == kind)

    def mock_recycle(self, path, expected=None):
        path = Path(path)
        self.assertEqual(file_signature(path), tuple(expected))
        # Keep test bytes for inspection; the real native module tests Shell receipts.
        destination = self.kept / (str(len(list(self.kept.iterdir()))) + "-" + path.name)
        path.rename(destination)
        return str(destination)

    def run_worker(self, rows, keep=True, native=None):
        with patch.object(deletion, "recycle_file", side_effect=native or self.mock_recycle) as recycle:
            result = deletion.run_deletion(self.paths, rows, keep, self.stop, self.events.append)
        self.last_native = recycle
        return result

    def state(self):
        with closing(HistoryStore.for_storage(self.paths)) as store:
            return {"summary": store.summary(), "records": store.records(), "choices": store.ignored_media(),
                    "journal": store.deletion_page()["rows"], "pending": store.pending_deletions()}

    def test_success_counts_actual_files_and_preserves_source_history(self):
        row = self.row("100")
        result = self.run_worker([row])
        self.assertEqual((result["total"], result["recycled"], result["failed"], result["cancelled"]), (1, 1, 0, 0))
        self.assertEqual(result["paths"], [row["path"]])
        self.assertFalse(Path(row["path"]).exists())
        state = self.state()
        self.assertEqual(state["summary"]["files"], 0)
        self.assertEqual(state["choices"], {("100", 1, "photo")})
        self.assertEqual(state["records"][0]["source_url"], row["source_url"])
        self.assertEqual(state["records"][0]["author"], "fixture_writer")
        self.assertEqual(state["journal"][0]["status"], "recycled")
        self.assertEqual(state["journal"][0]["path"], row["path"])
        self.assertEqual(state["pending"], [])
        self.assertEqual(self.events[-1]["recycled"], 1)

    def test_keep_deleted_unchecked_allows_later_download(self):
        row = self.row("100")
        with closing(HistoryStore.for_storage(self.paths)) as store:
            store.set_missing_ignored([("100", 1, "photo")], True)
        self.run_worker([row], keep=False)
        state = self.state()
        self.assertEqual(state["choices"], set())
        self.assertEqual(state["summary"]["missing_files"], 1)
        self.assertFalse(state["journal"][0]["keep_deleted"])

    def test_converted_gif_deletion_and_restore_track_gif_in_success_history(self):
        path = self.output / "100_gif_1.gif"
        row = self.row("100", path=path, kind="animated_gif")
        result = self.run_worker([row])
        self.assertEqual(result["recycled"], 1)
        state = self.state()
        self.assertEqual(state["records"][0]["last_path"], str(path))
        self.assertEqual(state["records"][0]["media_type"], "animated_gif")
        self.assertTrue(state["records"][0]["saved_at"])
        self.assertEqual(state["choices"], {("100", 1, "animated_gif")})
        next(self.kept.iterdir()).rename(path)
        with closing(HistoryStore.for_storage(self.paths)) as store:
            store.import_legacy(refresh=True)
            restored = store.records()[0]
            self.assertEqual(restored["path"], str(path))
            self.assertEqual(store.ignored_media(), set())

    def test_same_physical_file_is_moved_only_once_across_duplicate_rows(self):
        first = self.row("100")
        second = self.row("100", mode="author", path=Path(first["path"]))
        result = self.run_worker([first, first, second])
        self.assertEqual(self.last_native.call_count, 1)
        self.assertEqual((result["total"], result["recycled"]), (1, 1))
        state = self.state()
        self.assertEqual(state["summary"]["files"], 0)
        self.assertTrue(all(not item["path"] for item in state["records"]))
        self.assertEqual(len(state["journal"]), 1)

    def test_independent_media_in_one_tweet_remain_independent(self):
        first, second = self.row("100", 1), self.row("100", 2)
        result = self.run_worker([first])
        self.assertEqual(result["recycled"], 1)
        self.assertTrue(Path(second["path"]).exists())
        self.assertEqual(self.state()["choices"], {("100", 1, "photo")})

    def test_safe_native_failure_does_not_block_next_file(self):
        rows = [self.row("100"), self.row("200")]

        def recycle(path, expected=None):
            if Path(path) == Path(rows[0]["path"]):
                raise RecyclingError("fixture recycle is unavailable")
            return self.mock_recycle(path, expected)

        result = self.run_worker(rows, native=recycle)
        self.assertEqual((result["recycled"], result["failed"], result["cancelled"]), (1, 1, 0))
        self.assertTrue(Path(rows[0]["path"]).exists())
        self.assertFalse(Path(rows[1]["path"]).exists())
        self.assertEqual({item["status"] for item in self.state()["journal"]}, {"recycled", "failed"})
        self.assertEqual(self.state()["choices"], {("200", 1, "photo")})

    def test_finished_message_names_a_few_failed_files_and_their_reason(self):
        rows = [self.row(str(tweet)) for tweet in range(100, 105)]

        def recycle(path, expected=None):
            if Path(path) != Path(rows[0]["path"]):
                raise RecyclingError("fixture recycle is unavailable")
            return self.mock_recycle(path, expected)

        result = self.run_worker(rows, native=recycle)
        self.assertEqual((result["recycled"], result["failed"]), (1, 4))
        lines = result["message"].splitlines()
        self.assertEqual(lines[0], "휴지통 이동 1개 · 실패 4개")
        self.assertEqual(lines[1:], ["101_1.jpg: fixture recycle is unavailable",
                                     "102_1.jpg: fixture recycle is unavailable",
                                     "103_1.jpg: fixture recycle is unavailable", "외 1개"])
        self.assertEqual(self.events[-1]["message"], result["message"])

    def test_uncertain_native_result_stops_before_next_file(self):
        rows = [self.row("100"), self.row("200")]

        def uncertain(path, expected=None):
            receipt = self.mock_recycle(path, expected)
            raise RecyclingError("fixture missing receipt", uncertain=True, recycled_path=receipt)

        result = self.run_worker(rows, native=uncertain)
        self.assertEqual(self.last_native.call_count, 1)
        self.assertEqual((result["recycled"], result["cancelled"]), (0, 1))
        self.assertTrue(Path(rows[1]["path"]).exists())
        state = self.state()
        self.assertEqual(state["journal"][0]["status"], "interrupted")
        self.assertEqual(state["choices"], {("100", 1, "photo")})
        self.assertEqual(state["pending"], [])

    def test_generic_error_after_file_disappears_is_uncertain_and_stops(self):
        rows = [self.row("100"), self.row("200")]

        def uncertain(path, expected=None):
            self.mock_recycle(path, expected)
            raise OSError("fixture unclassified native error")

        result = self.run_worker(rows, native=uncertain)
        self.assertEqual(self.last_native.call_count, 1)
        self.assertEqual(result["recycled"], 0)
        self.assertTrue(Path(rows[1]["path"]).exists())
        self.assertEqual(self.state()["journal"][0]["status"], "interrupted")

    def test_stop_between_files_preserves_unprocessed_files(self):
        rows = [self.row("100"), self.row("200"), self.row("300")]

        def recycle(path, expected=None):
            destination = self.mock_recycle(path, expected)
            self.stop.set()
            return destination

        result = self.run_worker(rows, native=recycle)
        self.assertEqual((result["recycled"], result["failed"], result["cancelled"]), (1, 0, 2))
        self.assertEqual(self.last_native.call_count, 1)
        self.assertTrue(all(Path(row["path"]).exists() for row in rows[1:]))
        self.assertEqual(len(self.state()["journal"]), 1)

    def test_already_stopped_never_calls_native(self):
        rows = [self.row("100"), self.row("200")]
        self.stop.set()
        result = self.run_worker(rows)
        self.assertEqual((result["recycled"], result["cancelled"]), (0, 2))
        self.last_native.assert_not_called()
        self.assertTrue(all(Path(row["path"]).exists() for row in rows))

    def test_outside_record_is_rejected_without_touching_external_file(self):
        row = self.row("100")
        outside = self.base / "outside.jpg"
        outside.write_bytes(b"fixture must remain")
        row["path"] = str(outside)
        with closing(sqlite3.connect(self.paths.record_dir / ".luvbox-history.sqlite3")) as db:
            db.execute("UPDATE media SET path=?", (str(outside),))
            db.commit()
        result = self.run_worker([row])
        self.assertEqual(result["recycled"], 0)
        self.last_native.assert_not_called()
        self.assertEqual(outside.read_bytes(), b"fixture must remain")

    def test_stale_selected_catalog_path_is_not_redirected_to_current_file(self):
        row = self.row("100")
        changed = self.output / "other.jpg"
        changed.write_bytes(b"new fixture path")
        with closing(sqlite3.connect(self.paths.record_dir / ".luvbox-history.sqlite3")) as db:
            db.execute("UPDATE media SET path=?", (str(changed),))
            db.commit()
        result = self.run_worker([row])
        self.assertEqual(result["recycled"], 0)
        self.last_native.assert_not_called()
        self.assertTrue(changed.exists())
        self.assertTrue(Path(row["path"]).exists())

    def test_missing_catalog_identity_never_deletes_surviving_file(self):
        row = self.row("100")
        with closing(sqlite3.connect(self.paths.record_dir / ".luvbox-history.sqlite3")) as db:
            db.execute("DELETE FROM media")
            db.commit()
        result = self.run_worker([row])
        self.assertEqual(result["recycled"], 0)
        self.last_native.assert_not_called()
        self.assertTrue(Path(row["path"]).exists())

    def test_selected_file_signature_change_is_rejected(self):
        row = self.row("100")
        path = Path(row["path"])
        row["_delete_signature"] = file_signature(path)
        path.write_bytes(b"fixture edited after confirmation")
        result = self.run_worker([row])
        self.assertEqual(result["recycled"], 0)
        self.last_native.assert_not_called()
        self.assertEqual(path.read_bytes(), b"fixture edited after confirmation")

    def test_account_lock_conflict_never_calls_native(self):
        row = self.row("100")
        guard = AccountGuard("alice", sleep=lambda _: None)
        guard.acquire(respect_cooldown=False)
        try:
            result = self.run_worker([row])
        finally:
            guard.close()
        self.assertEqual(result["recycled"], 0)
        self.last_native.assert_not_called()
        self.assertTrue(Path(row["path"]).exists())

    def test_output_lock_conflict_never_calls_native(self):
        row = self.row("100")
        guard = OutputGuard(self.output)
        guard.acquire()
        try:
            result = self.run_worker([row])
        finally:
            guard.close()
        self.assertEqual(result["recycled"], 0)
        self.last_native.assert_not_called()
        self.assertTrue(Path(row["path"]).exists())

    def test_migrated_media_root_is_also_locked_before_native_operation(self):
        self.row("100")
        self.row("200")
        old = self.base / "previous-media"
        old.mkdir()
        media = old / "200_1.jpg"
        media.write_bytes(b"migrated fixture")
        with closing(HistoryStore.for_storage(self.paths)) as store:
            store._db.execute("INSERT OR REPLACE INTO catalog_meta(key,value) VALUES ('allowed_media_roots',?)",
                              (json.dumps([str(old)]),))
            store._db.execute("UPDATE media SET path=?,filesize=? WHERE tweet_id='200'", (str(media), media.stat().st_size))
            store._db.commit()
        with closing(HistoryStore.for_storage(self.paths)) as store:
            row = next(item for item in store.records() if item["tweet_id"] == "200")
        guard = OutputGuard(old)
        guard.acquire()
        try:
            result = self.run_worker([row])
        finally:
            guard.close()
        self.assertEqual(result["recycled"], 0)
        self.last_native.assert_not_called()
        self.assertTrue(media.exists())
        # A failure to acquire the extra root cannot leave the primary root locked.
        guard = OutputGuard(self.output)
        try:
            guard.acquire()
        finally:
            guard.close()

    def test_local_deletion_does_not_modify_account_download_cooldown(self):
        row = self.row("100")
        guard = AccountGuard("alice", sleep=lambda _: None)
        guard.root.mkdir(parents=True, exist_ok=True)
        content = json.dumps({"until": time.time() + 3600}).encode()
        guard.state_path.write_bytes(content)
        self.assertEqual(self.run_worker([row])["recycled"], 1)
        self.assertEqual(guard.state_path.read_bytes(), content)

    def test_missing_former_root_lock_does_not_reconcile_pending_journal(self):
        row = self.row("100")
        old = self.base / "previous-media"
        old.mkdir()
        with closing(HistoryStore.for_storage(self.paths)) as store:
            store.begin_deletion("abandoned-batch", row, True, file_signature(row["path"]))
            store._db.execute("INSERT OR REPLACE INTO catalog_meta(key,value) VALUES ('allowed_media_roots',?)",
                              (json.dumps([str(old)]),))
            store._db.commit()
        guard = OutputGuard(old)
        guard.acquire()
        try:
            with patch.object(HistoryStore, "reconcile_deletions") as reconcile:
                result = self.run_worker([row])
            reconcile.assert_not_called()
        finally:
            guard.close()
        self.last_native.assert_not_called()
        self.assertEqual(result["recycled"], 0)
        self.assertEqual(self.state()["journal"][0]["status"], "pending")

    def test_legacy_missing_catalog_folder_is_not_recreated(self):
        missing = self.base / "never-created-legacy-folder"
        row = {"path": str(missing / "100_1.jpg")}
        result = deletion.run_deletion(missing, [row], True, self.stop, self.events.append)
        self.assertEqual((result["recycled"], result["cancelled"]), (0, 1))
        self.assertFalse(missing.exists())
        self.assertEqual(self.events[-1]["type"], "done")

    def test_native_operation_holds_account_and_output_locks(self):
        row = self.row("100")

        def recycle(path, expected=None):
            for guard in (AccountGuard("alice", sleep=lambda _: None), OutputGuard(self.output)):
                try:
                    with self.assertRaises(ProtectionError):
                        if isinstance(guard, AccountGuard):
                            guard.acquire(respect_cooldown=False)
                        else:
                            guard.acquire()
                finally:
                    guard.close()
            return self.mock_recycle(path, expected)

        self.assertEqual(self.run_worker([row], native=recycle)["recycled"], 1)
        # A completed batch always releases both locks.
        for guard in (AccountGuard("alice", sleep=lambda _: None), OutputGuard(self.output)):
            try:
                if isinstance(guard, AccountGuard):
                    guard.acquire(respect_cooldown=False)
                else:
                    guard.acquire()
            finally:
                guard.close()

    def test_journal_intent_is_committed_before_native_operation(self):
        row = self.row("100")

        def recycle(path, expected=None):
            with closing(sqlite3.connect(self.paths.record_dir / ".luvbox-history.sqlite3")) as db:
                item = db.execute("SELECT status,path,signature FROM deletion_items").fetchone()
            self.assertEqual(item[:2], ("pending", row["path"]))
            self.assertEqual(json.loads(item[2]), list(expected))
            return self.mock_recycle(path, expected)

        self.assertEqual(self.run_worker([row], native=recycle)["recycled"], 1)

    def test_journal_write_error_prevents_native_operation_and_stops_batch(self):
        rows = [self.row("100"), self.row("200")]
        with patch.object(HistoryStore, "begin_deletion", side_effect=HistoryError("fixture journal write error")):
            result = self.run_worker(rows)
        self.assertEqual(result["recycled"], 0)
        self.last_native.assert_not_called()
        self.assertTrue(all(Path(row["path"]).exists() for row in rows))

    def test_journal_completion_error_stops_before_another_file(self):
        rows = [self.row("100"), self.row("200")]
        real_finish = HistoryStore.finish_deletion

        def finish(store, ident, status, *args, **kwargs):
            if status == "recycled":
                raise HistoryError("fixture completion write error")
            return real_finish(store, ident, status, *args, **kwargs)

        with patch.object(HistoryStore, "finish_deletion", finish):
            result = self.run_worker(rows)
        self.assertEqual(self.last_native.call_count, 1)
        self.assertTrue(Path(rows[1]["path"]).exists())
        self.assertEqual((result["recycled"], result["cancelled"]), (1, 1))
        self.assertEqual(result["paths"], [rows[0]["path"]])
        # Interrupted intent remains visible; a missing file is never silently retried.
        state = self.state()
        self.assertTrue(any(item["status"] in {"pending", "interrupted"} for item in state["journal"]))

    def test_abandoned_intent_is_reconciled_without_retrying_it(self):
        first, second = self.row("100"), self.row("200")
        with closing(HistoryStore.for_storage(self.paths)) as store:
            store.begin_deletion("abandoned-batch", first, True, file_signature(first["path"]))
        self.mock_recycle(first["path"], file_signature(first["path"]))
        result = self.run_worker([second])
        self.assertEqual(result["recycled"], 1)
        self.assertEqual(self.last_native.call_count, 1)
        state = self.state()
        self.assertEqual({item["status"] for item in state["journal"]}, {"recycled", "interrupted"})
        self.assertEqual(state["choices"], {("100", 1, "photo"), ("200", 1, "photo")})

    def test_snapshot_contains_completed_journal_and_choices(self):
        row = self.row("100")
        self.run_worker([row])
        snapshot = self.paths.snapshot_dir / ".luvbox-history.sqlite3"
        self.assertTrue(snapshot.exists())
        with closing(sqlite3.connect(f"file:{snapshot.as_posix()}?mode=ro", uri=True)) as db:
            self.assertEqual(db.execute("SELECT status FROM deletion_items").fetchone()[0], "recycled")
            self.assertEqual(db.execute("SELECT ignored FROM missing_choices WHERE tweet_id='100'").fetchone()[0], 1)
        self.assertFalse(Path(str(snapshot) + "-wal").exists())
        self.assertFalse(Path(str(snapshot) + "-shm").exists())

    def test_empty_batch_is_a_controlled_no_op(self):
        result = self.run_worker([])
        self.assertEqual(result["recycled"], 0)
        self.last_native.assert_not_called()
        self.assertFalse(self.paths.record_dir.exists())
        self.assertEqual(self.events[-1]["type"], "done")

    def test_missing_media_folder_is_not_recreated_for_deletion_or_snapshot(self):
        row = self.row("100")
        moved = self.base / "moved-media"
        self.output.rename(moved)
        result = self.run_worker([row])
        self.assertEqual((result["recycled"], result["failed"]), (0, 1))
        self.last_native.assert_not_called()
        self.assertFalse(self.output.exists())
        self.assertEqual((moved / Path(row["path"]).name).read_bytes(), b"fixture 100 1")

    def test_known_former_root_stays_usable_without_recreating_current_output(self):
        row = self.row("100")
        old = self.base / "previous-media"
        old.mkdir()
        original = Path(row["path"])
        moved = old / original.name
        original.rename(moved)
        self.output.rmdir()
        with closing(HistoryStore.for_storage(self.paths, create_media=False)) as store:
            store._db.execute("INSERT OR REPLACE INTO catalog_meta(key,value) VALUES ('allowed_media_roots',?)",
                              (json.dumps([str(old)]),))
            store._db.execute("UPDATE media SET path=?,last_path=?", (str(moved), str(moved)))
            store._db.commit()
            row = store.records()[0]
        result = self.run_worker([row])
        self.assertEqual(result["recycled"], 1)
        self.assertFalse(self.output.exists())
        self.assertFalse(moved.exists())

    def test_stop_after_journal_commit_cancels_before_native_operation(self):
        row = self.row("100")
        begin = HistoryStore.begin_deletion

        def stopped(store, *args, **kwargs):
            ident = begin(store, *args, **kwargs)
            self.stop.set()
            return ident

        with patch.object(HistoryStore, "begin_deletion", stopped):
            result = self.run_worker([row])
        self.last_native.assert_not_called()
        self.assertTrue(Path(row["path"]).exists())
        self.assertEqual((result["recycled"], result["cancelled"]), (0, 1))
        self.assertEqual(self.state()["journal"][0]["status"], "cancelled")
        self.assertEqual(self.state()["pending"], [])

    def test_output_guard_cleanup_error_still_releases_account_and_reports_done(self):
        row = self.row("100")
        real_close = OutputGuard.close

        def close_with_error(guard):
            real_close(guard)
            raise OSError("fixture cleanup error")

        with patch.object(OutputGuard, "close", close_with_error):
            result = self.run_worker([row])
        self.assertEqual(result["recycled"], 1)
        self.assertIn("정리를 마치지 못했습니다", result["message"])
        self.assertEqual(self.events[-1]["type"], "done")
        guard = AccountGuard("alice", sleep=lambda _: None)
        try:
            guard.acquire(respect_cooldown=False)
        finally:
            guard.close()

    def test_store_cleanup_error_still_releases_all_guards_and_reports_done(self):
        row = self.row("100")
        real_close = HistoryStore.close

        def close_with_error(store):
            real_close(store)
            raise OSError("fixture catalog cleanup error")

        with patch.object(HistoryStore, "close", close_with_error), \
                patch.object(deletion, "publish_snapshot") as snapshot:
            result = self.run_worker([row])
        snapshot.assert_not_called()
        self.assertIn("닫지 못했습니다", result["message"])
        self.assertEqual(self.events[-1]["type"], "done")
        for guard in (AccountGuard("alice", sleep=lambda _: None), OutputGuard(self.output)):
            try:
                if isinstance(guard, AccountGuard):
                    guard.acquire(respect_cooldown=False)
                else:
                    guard.acquire()
            finally:
                guard.close()


if __name__ == "__main__":
    unittest.main()

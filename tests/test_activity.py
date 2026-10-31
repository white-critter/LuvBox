"""Terminal outcomes include pre-worker failures without changing old records."""

from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from tests import app_data_isolation  # noqa: F401
from luvbox import activity


class ActivityTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="luvbox-activity-")
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.path = self.root / "activity.sqlite3"
        replacement = patch.object(activity, "activity_path", return_value=self.path)
        replacement.start()
        self.addCleanup(replacement.stop)
        previous = patch("luvbox.scheduled.last_run", return_value=None)
        self.previous = previous.start()
        self.addCleanup(previous.stop)

    def append(self, ident="test-1", **fields):
        return activity.record_outcome({"run_id": ident, "status": "finished", "saved": 3, **fields},
                                       origin="manual", account="fixture", output_dir=str(self.root / "media"),
                                       started_at="2026-10-27T09:00:00+09:00")

    def test_empty_history_read_does_not_create_a_database(self):
        self.assertEqual(activity.outcome_page(), {"rows": [], "total": 0, "page": 0, "pages": 0, "page_size": 100})
        self.assertEqual(list(self.root.iterdir()), [])

    def test_a_ledger_from_before_failed_media_were_counted_is_extended_without_losing_rows(self):
        import sqlite3
        from contextlib import closing
        with closing(sqlite3.connect(self.path)) as old, old:
            old.execute("CREATE TABLE outcomes (run_id TEXT PRIMARY KEY, origin TEXT NOT NULL, account TEXT NOT NULL,"
                        "output_dir TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT NOT NULL,"
                        "status TEXT NOT NULL, saved INTEGER NOT NULL, skipped INTEGER NOT NULL,"
                        "size_skipped INTEGER NOT NULL, visited INTEGER NOT NULL, message TEXT NOT NULL,"
                        "code INTEGER, retry_at TEXT NOT NULL, log TEXT NOT NULL, history_run_id INTEGER,"
                        "legacy INTEGER NOT NULL DEFAULT 0)")
            old.execute("INSERT INTO outcomes VALUES('old-run','manual','fixture','','2026-10-01T09:00:00+09:00',"
                        "'2026-10-01T09:05:00+09:00','finished',4,2,1,9,'',0,'','',NULL,0)")
        before = activity.outcome_page()["rows"]
        self.assertEqual([(row["run_id"], row.get("failed_media", 0)) for row in before], [("old-run", 0)])
        self.append("new-run", failed_media=3, size_skipped=1)
        rows = {row["run_id"]: row for row in activity.outcome_page()["rows"]}
        self.assertEqual((rows["old-run"]["saved"], rows["old-run"]["failed_media"]), (4, 0))
        self.assertEqual((rows["new-run"]["failed_media"], rows["new-run"]["size_skipped"]), (3, 1))

    def test_manual_and_scheduled_outcomes_are_immutable_and_paged(self):
        self.append()
        self.append(status="error", saved=99)
        activity.record_outcome({"run_id": "scheduled-2", "status": "error", "message": "connection needed"},
                                origin="scheduled", account="other")
        page = activity.outcome_page(page_size=1)
        self.assertEqual((page["total"], page["pages"], page["rows"][0]["run_id"]), (2, 2, "scheduled-2"))
        original = activity.outcome_page(1, 1)["rows"][0]
        self.assertEqual((original["status"], original["saved"], original["account"]), ("finished", 3, "fixture"))
        self.assertEqual(activity.outcome_page(account="FIXTURE")["total"], 1)
        self.assertEqual(activity.page_outcomes(origin="scheduled")["total"], 1)
        self.assertEqual(activity.outcome_page(100, 1)["page"], 1)

    def test_legacy_last_result_is_read_without_writes_then_backfilled_once(self):
        legacy = {"run_id": "legacy-run", "status": "error", "message": "old startup failure",
                  "finished_at": "2026-10-26T09:00:00+09:00", "seen": False}
        self.previous.return_value = legacy
        row = activity.outcome_page()["rows"][0]
        self.assertEqual((row["account"], row["origin"], row["legacy"]), ("", "scheduled", 1))
        self.assertFalse(self.path.exists())
        self.append()
        self.previous.return_value = None
        rows = activity.outcome_page()["rows"]
        self.assertEqual([row["run_id"] for row in rows], ["test-1", "legacy-run"])
        self.previous.return_value = dict(legacy, seen=True)
        self.assertEqual(activity.outcome_page()["total"], 2)

    def test_legacy_without_run_id_keeps_one_identity_when_acknowledged(self):
        self.previous.return_value = {"status": "error", "finished_at": "2026-10-26T09:00:00+09:00", "seen": False}
        self.append()
        self.previous.return_value["seen"] = True
        self.assertEqual(activity.outcome_page()["total"], 2)

    def test_existing_history_does_not_drop_a_later_unrecorded_legacy_result(self):
        self.append("manual-before")
        self.previous.return_value = {"run_id": "not-yet-recorded", "status": "error",
                                      "finished_at": "2026-10-26T09:00:00+09:00", "seen": False}
        activity.record_outcome({"run_id": "next-scheduled", "status": "finished"}, origin="scheduled")
        self.previous.return_value = None  # The new scheduler result replaced the old JSON.
        self.assertEqual({row["run_id"] for row in activity.outcome_page()["rows"]},
                         {"manual-before", "not-yet-recorded", "next-scheduled"})

    def test_distinct_terminal_outcomes_survive_ledger_roundtrip(self):
        for status in ("interrupted", "cancelled", "partial"):
            with self.subTest(status=status):
                self.append(status, status=status)
                self.assertEqual(activity.outcome_page()["rows"][0]["status"], status)

    def test_retention_keeps_newest_appends(self):
        with patch.object(activity, "MAX_OUTCOMES", 2):
            for ident in ("one", "two", "three"):
                self.append(ident)
            self.assertEqual([row["run_id"] for row in activity.outcome_page()["rows"]], ["three", "two"])

    def test_pruned_legacy_outcome_does_not_evict_newer_runs_when_last_result_is_unchanged(self):
        self.previous.return_value = {"run_id": "old-scheduled", "status": "finished",
                                      "finished_at": "2026-10-26T09:00:00+09:00", "seen": False}
        with patch.object(activity, "MAX_OUTCOMES", 2):
            for ident in ("first-manual", "second-manual", "third-manual"):
                self.append(ident)
            self.assertEqual([row["run_id"] for row in activity.outcome_page()["rows"]],
                             ["third-manual", "second-manual"])

    def test_read_only_page_preserves_database_bytes(self):
        self.append()
        before = self.path.read_bytes(), self.path.stat().st_mtime_ns
        self.assertEqual(activity.outcome_page()["total"], 1)
        self.assertEqual((self.path.read_bytes(), self.path.stat().st_mtime_ns), before)

    def test_concurrent_writers_keep_distinct_terminal_results(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda number: self.append(f"run-{number}"), range(20)))
        self.assertEqual(activity.outcome_page()["total"], 20)

    def test_reader_waits_for_first_ledger_initialization(self):
        created = threading.Event()
        resume = threading.Event()
        real_connect = activity.sqlite3.connect

        def paused_connect(*args, **kwargs):
            database = real_connect(*args, **kwargs)
            if not kwargs.get("uri"):
                created.set()
                if not resume.wait(5):
                    database.close()
                    raise TimeoutError("writer was not released")
            return database

        with patch.object(activity.sqlite3, "connect", side_effect=paused_connect):
            with ThreadPoolExecutor(max_workers=2) as pool:
                writer = pool.submit(self.append)
                try:
                    self.assertTrue(created.wait(5))
                    self.assertTrue(self.path.exists())
                    reader = pool.submit(activity.outcome_page)
                    with self.assertRaises(FutureTimeout):
                        reader.result(timeout=0.1)
                finally:
                    resume.set()
                writer.result(timeout=5)
                self.assertEqual(reader.result(timeout=5)["total"], 1)

    def test_linked_or_damaged_ledger_is_preserved_and_rejected(self):
        other = self.root / "other.sqlite3"
        other.write_bytes(b"not an activity database")
        try:
            os.link(other, self.path)
        except OSError:
            self.skipTest("hard links are unavailable")
        for operation in (self.append, activity.outcome_page):
            with self.assertRaises(activity.ActivityError):
                operation()
        self.assertEqual(other.read_bytes(), b"not an activity database")
        self.path.unlink()
        self.path.write_bytes(b"damaged database")
        with self.assertRaises(activity.ActivityError) as error:
            activity.outcome_page()
        self.assertNotIn(str(self.path), str(error.exception))
        self.assertEqual(self.path.read_bytes(), b"damaged database")

    def test_unknown_fields_and_credentials_are_not_serialized(self):
        self.append(auth_token="secret", cookies=["secret"], saved=-1, skipped=True, message="ok\0message")
        row = activity.outcome_page()["rows"][0]
        self.assertEqual((row["saved"], row["skipped"], row["message"]), (0, 0, "okmessage"))
        self.assertNotIn("secret", json.dumps(row))
        with self.assertRaises(ValueError):
            activity.outcome_page(page_size=0)


if __name__ == "__main__":
    unittest.main()

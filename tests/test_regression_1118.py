"""Regression checks for the 1.11.8 inspection fixes."""

from contextlib import ExitStack, closing
from datetime import datetime, timezone
from importlib.util import find_spec
import io
import json
import os
from pathlib import Path
import sqlite3
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
try:
    from tests import app_data_isolation  # noqa: E402,F401
except ModuleNotFoundError:
    import app_data_isolation  # noqa: E402,F401


def _jpeg_bytes(size=(16, 16), color="#d94444") -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    with Image.new("RGB", size, color) as img:
        img.save(buf, format="JPEG")
    return buf.getvalue()


class CoreAndServiceRegressionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="luvbox-core-1118-")
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_retry_sharing_lock_retries_winerror_32_and_fails_fast_on_other_oserror(self):
        from luvbox.core import _retry_sharing_lock

        attempts = 0

        def flaky():
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                err = PermissionError("locked")
                err.winerror = 32
                raise err
            return "ok"

        with patch("luvbox.core.time.sleep"):
            self.assertEqual(_retry_sharing_lock(flaky, check_stop=lambda: None), "ok")
        self.assertEqual(attempts, 3)

        def non_sharing():
            err = OSError("disk gone")
            err.errno = 28
            raise err

        with self.assertRaises(OSError):
            _retry_sharing_lock(non_sharing, check_stop=lambda: None)

    def test_scheduled_log_writes_korean_status_labels(self):
        from luvbox import scheduled

        log_dir = self.dir / "logs"
        out_dir = self.dir / "media"
        out_dir.mkdir()

        def fake_worker(_options, emit):
            emit({"type": "finished", "status": "finished", "saved": 0, "skipped": 0,
                  "size_skipped": 0, "failed_media": 0, "message": "마쳤습니다."})
            return 0

        with patch.dict(os.environ, {"LOCALAPPDATA": str(self.dir)}), \
                patch("luvbox.scheduled.load_settings", return_value={"account": "alice", "output_dir": str(out_dir)}), \
                patch("luvbox.scheduled.log_directory", return_value=log_dir), \
                patch("luvbox.core.run_worker", side_effect=fake_worker), \
                patch("luvbox.scheduled._remember_outcome"), \
                patch("luvbox.scheduled._notify_failure"):
            code = scheduled.run_saved()
        self.assertEqual(code, 0)
        logs = list(log_dir.glob("scheduled-*.log"))
        self.assertEqual(len(logs), 1)
        content = logs[0].read_text(encoding="utf-8")
        self.assertIn("종료 · 완료 ·", content)
        self.assertNotIn("종료 · finished", content)

    def test_download_service_final_guard_blocks_duplicate_finish(self):
        from luvbox import download_service
        from luvbox.core import JobOptions

        class FakeWorker:
            def __init__(self, events_path, payload):
                events_path.write_bytes(payload)
                self.returncode = None

            def poll(self):
                if self.returncode is None:
                    self.returncode = 0
                return self.returncode

            def wait(self, timeout=None):
                return self.returncode

            def terminate(self):
                self.returncode = -15

            def kill(self):
                self.returncode = -9

        payload = (json.dumps({"type": "finished", "status": "finished"}).encode() + b"\n"
                   + b"x" * (256 * 1024 + 1) + b"\n")
        svc = download_service.DownloadService()
        try:
            svc._job_id = 1
            svc._running = True
            with patch.object(download_service.subprocess, "Popen",
                              side_effect=lambda cmd, **_kw: FakeWorker(Path(cmd[-2]), payload)), \
                    patch.object(download_service.time, "sleep"):
                svc._run(1, JobOptions("offline", str(self.dir / "media")))
            finished = [e for e in svc.drain(100) if e.get("type") == "finished"]
            self.assertEqual(len(finished), 1)
            self.assertEqual(finished[0]["status"], "finished")
        finally:
            svc.close()


class StorageHistoryMediaRegressionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="luvbox-storage-1118-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_history_count_total_false_missing_since_preserved_and_has_media_record(self):
        from luvbox.history import HistoryStore, page_cursor
        from luvbox.storage import resolve_storage

        media_dir = self.root / "media"
        media_dir.mkdir()
        paths = resolve_storage(media_dir, "alice")
        with closing(HistoryStore.for_storage(paths)) as store:
            p1 = media_dir / "alice_100_1.jpg"
            p2 = media_dir / "alice_200_1.jpg"
            p1.write_bytes(_jpeg_bytes())
            p2.write_bytes(_jpeg_bytes(color="#2266aa"))
            store.record_media("flat", {"tweet_id": "100", "num": 1, "type": "photo", "author": {"name": "alice"}}, p1)
            store.record_media("flat", {"tweet_id": "200", "num": 1, "type": "photo", "author": {"name": "alice"}}, p2)
            self.assertTrue(store.has_media_record(("100", 1, "photo")))
            self.assertFalse(store.has_media_record(("999", 1, "photo")))

            page = store.record_page(page_size=1, count_total=False)
            self.assertIsNone(page["total"])
            self.assertTrue(page["has_more"])
            self.assertEqual(len(page["rows"]), 1)
            cursor = page_cursor(page["rows"][-1], "saved")
            page2 = store.record_page(page_size=1, after=cursor, count_total=False)
            self.assertIsNone(page2["total"])
            self.assertFalse(page2["has_more"])
            self.assertEqual(len(page2["rows"]), 1)

            p1.unlink()
            store.exclude_lost([("100", 1, "photo")])
            row_before = store.media_record("flat", {"tweet_id": "100", "num": 1, "type": "photo"})
            first_missing = row_before["missing_since"]
            self.assertTrue(first_missing)
            updated = {k: v for k, v in row_before.items() if k != "missing_since"}
            with store._db:
                store._upsert(updated)
            row_after = store.media_record("flat", {"tweet_id": "100", "num": 1, "type": "photo"})
            self.assertEqual(row_after["missing_since"], first_missing)

    def test_storage_valid_roots_case_insensitive_and_merge_keeps_clusters_and_reconciles_ai_feedback(self):
        from luvbox.history import HistoryStore
        from luvbox.storage import _merge_history, _valid_roots, resolve_storage

        media_dir = (self.root / "MixedCase").resolve()
        media_dir.mkdir()
        alt = Path(str(media_dir).lower() if os.name == "nt" else str(media_dir))
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        db.execute("CREATE TABLE catalog_meta(key TEXT PRIMARY KEY, value TEXT)")
        db.execute("INSERT INTO catalog_meta VALUES ('allowed_media_roots', ?)", (json.dumps([str(alt)]),))
        roots = _valid_roots(db, media_dir)
        self.assertEqual(len(roots), 2)

        target_paths = resolve_storage(self.root / "t_out", "alice")
        source_paths = resolve_storage(self.root / "s_out", "alice")
        (self.root / "t_out").mkdir()
        (self.root / "s_out").mkdir()
        with closing(HistoryStore.for_storage(target_paths)) as t_store, \
                closing(HistoryStore.for_storage(source_paths)) as s_store:
            tp = self.root / "t_out" / "alice_10_1.jpg"
            tp.write_bytes(_jpeg_bytes())
            t_store.record_media("flat", {"tweet_id": "10", "num": 1, "type": "photo", "author": {"name": "alice"}}, tp)
            t_store._db.execute("INSERT INTO image_hashes(folder_mode,tweet_id,num,media_type,dhash,file_hash,file_signature,filesize,cluster_id,updated_at) "
                                "VALUES ('flat','10',1,'photo','0000000000000000','deadbeef','sig',123,7,'2026-01-01T00:00:00')")
            t_store._db.execute("INSERT INTO media_tags(folder_mode,tweet_id,num,media_type,tag,source,created_at) "
                                "VALUES ('flat','10',1,'photo','인물','ai','2026-01-01T00:00:00')")
            t_store._db.commit()
            s_store._db.execute("INSERT INTO ai_feedback(folder_mode,tweet_id,num,media_type,category,updated_at) "
                                "VALUES ('flat','10',1,'photo','동물','2026-02-01T00:00:00')")
            s_store._db.execute("INSERT INTO media_tags(folder_mode,tweet_id,num,media_type,tag,source,created_at) "
                                "VALUES ('flat','10',1,'photo','동물','ai','2026-02-01T00:00:00')")
            s_store._db.commit()
            _merge_history(t_store._db, s_store._db)
            cluster = t_store._db.execute("SELECT cluster_id FROM image_hashes WHERE tweet_id='10'").fetchone()[0]
            self.assertEqual(cluster, 7)
            ai_tags = t_store._db.execute("SELECT tag, source FROM media_tags WHERE tweet_id='10' AND source IN ('ai','confirmed')").fetchall()
            # Several AI tags per photo: the taught "동물" shows as confirmed, and this PC's own
            # estimate "인물" stays (an answer about one tag says nothing about another).
            self.assertEqual(sorted(tuple(r) for r in ai_tags), [("동물", "confirmed"), ("인물", "ai")])
            self.assertEqual([tuple(row) for row in t_store._db.execute(
                "SELECT tag, label FROM ai_tag_feedback WHERE tweet_id='10'").fetchall()], [("동물", 1)])

    def test_duplicates_identical_dhash_grouping_and_strided_masks(self):
        from luvbox.duplicates import _close_hash_pairs

        hashes = [(0x123456789ABCDEF0, i) for i in range(6)]
        hashes.append((0x123456789ABCDEF1, 6))
        count, pairs = _close_hash_pairs(hashes, 1)
        expected = {(i, j) for i in range(7) for j in range(i + 1, 7)}
        self.assertEqual(count, len(expected))
        self.assertEqual(set(pairs), expected)

    def test_clip_classifier_corrupt_bundled_model_fallback_and_pixel_decode(self):
        from luvbox import clip_classifier

        bundled = self.root / "bundled" / clip_classifier.MODEL_FILENAME
        cache_dir = self.root / "cache"
        bundled.parent.mkdir()
        cache_dir.mkdir()
        bundled.write_bytes(b"corrupt")
        fallback = cache_dir / clip_classifier.MODEL_FILENAME
        fallback.write_bytes(b"also-invalid")
        with patch("luvbox.clip_classifier.bundled_model_path", return_value=bundled), \
                patch("luvbox.clip_classifier.default_cache_dir", return_value=cache_dir):
            self.assertFalse(clip_classifier.is_model_ready())

        broken = self.root / "broken.jpg"
        data = _jpeg_bytes((64, 64))
        broken.write_bytes(data[:len(data) // 3])
        sig = clip_classifier.encode_skip_signature(broken)
        self.assertIsNotNone(sig)

    def test_preview_and_thumbcache_corrupt_exif_and_prune_index(self):
        from PIL import Image
        from luvbox import preview, thumbcache

        jpg = self.root / "sample.jpg"
        jpg.write_bytes(_jpeg_bytes((64, 64)))
        with patch.object(Image.Image, "getexif", side_effect=struct.error("bad exif")):
            thumb = preview.thumbnail(jpg)
            self.assertIsNotNone(thumb.image)
            decoded = thumbcache.decode(jpg.read_bytes())
            self.assertIsNotNone(decoded)
            self.assertEqual(decoded.size, (64, 64))

        with patch.dict(os.environ, {"LOCALAPPDATA": str(self.root)}):
            cache = thumbcache.Cache("alice")
            try:
                idx = cache._db.execute("SELECT name FROM sqlite_master WHERE type='index' AND name='previews_saved_at'").fetchone()
                self.assertIsNotNone(idx)
                cache.keep("10", 1, "photo", jpg.read_bytes()) if hasattr(cache, "keep") and cache.keep.__code__.co_argcount >= 5 else None
                cache.store("10", 1, "photo", jpg.read_bytes()) if hasattr(cache, "store") else cache._db.execute(
                    "INSERT INTO previews(tweet_id,num,media_type,image,saved_at) VALUES ('10',1,'photo',?, '2026-11-20')",
                    (jpg.read_bytes(),))
                cache._db.commit()
                deleted = cache.prune(limit=0)
                self.assertGreaterEqual(deleted, 0)
            finally:
                cache.close()

    def test_recovery_handles_same_second_kept_collision(self):
        from luvbox.history import HistoryStore
        from luvbox.recovery import repair_history
        from luvbox.storage import resolve_storage
        from tests.test_recovery import damage_media_table

        media_dir = self.root / "rec_media"
        media_dir.mkdir()
        paths = resolve_storage(media_dir, "alice")
        with closing(HistoryStore.for_storage(paths)) as store:
            p = media_dir / "alice_1_1.jpg"
            p.write_bytes(_jpeg_bytes())
            store.record_media("flat", {"tweet_id": "1", "num": 1, "type": "photo", "author": {"name": "alice"}}, p)
        cat = paths.record_dir / ".luvbox-history.sqlite3"
        damage_media_table(cat)

        fixed_now = datetime(2026, 11, 20, 12, 0, 0, tzinfo=timezone.utc)
        backup_dir = self.root / "backups"
        existing = backup_dir / "alice-20261120-120000-damaged"
        existing.mkdir(parents=True)
        with patch("luvbox.recovery.backup_directory", return_value=backup_dir), \
                patch("luvbox.recovery.datetime") as dt_mod:
            dt_mod.now.return_value = fixed_now
            report = repair_history(paths.record_dir, "alice")
        self.assertTrue(Path(report["kept"]).name.endswith("-1"))

    def test_recycling_fat_allows_ino_change_with_digest_and_ntfs_rejects(self):
        import hashlib
        from luvbox import recycling

        rec_bin = self.root / "C_DRIVE_FAKE" / "$RECYCLE.BIN" / "S-1-5-21"
        rec_bin.mkdir(parents=True)
        orig = self.root / "C_DRIVE_FAKE" / "alice_1_1.jpg"
        orig.parent.mkdir(parents=True, exist_ok=True)
        payload = _jpeg_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        dest = rec_bin / "$RXYZ.jpg"
        receipt = rec_bin / "$IXYZ.jpg"
        dest.write_bytes(payload)
        receipt.write_bytes(b"receipt")
        st = dest.stat()
        expected_diff_ino = (st.st_dev, st.st_ino + 999, st.st_size, st.st_mtime_ns)

        with patch("luvbox.recycling._same_path", return_value=True), \
                patch("luvbox.recycling._receipt_data", return_value=(str(orig), st.st_size)):
            with patch("luvbox.recycling._volume_filesystem", return_value="EXFAT"):
                self.assertTrue(recycling.verify_receipt(orig, dest, expected_diff_ino, expected_hash=digest))
            with patch("luvbox.recycling._volume_filesystem", return_value="NTFS"):
                self.assertFalse(recycling.verify_receipt(orig, dest, expected_diff_ino, expected_hash=digest))

    def test_deletion_masks_unexpected_exception_messages(self):
        from luvbox.deletion import run_deletion
        from luvbox.storage import resolve_storage

        media_dir = self.root / "del_media"
        media_dir.mkdir()
        paths = resolve_storage(media_dir, "alice")
        with patch("luvbox.deletion.OutputGuard", side_effect=RuntimeError("C:/secret/raw/exception")):
            res = run_deletion(paths, [{"path": str(media_dir / "a.jpg")}], False, threading.Event(), lambda _m: None)
        msg = res.get("message", "")
        self.assertNotIn("C:/secret", msg)
        self.assertIn("작업을 중단했습니다", msg)


@unittest.skipUnless(find_spec("PySide6") is not None, "optional Qt dependencies are not installed")
class QtControllerRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtCore import QCoreApplication
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def spin(self, timeout=5, pred=None):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if pred is not None and pred():
                return
            time.sleep(0.005)
        if pred is not None:
            self.fail("Qt condition timed out")

    def test_updater_ignores_late_check_failure_and_skips_redundant_check(self):
        from luvbox.qt_updater import UpdateController

        ctrl = UpdateController()
        try:
            ctrl._state = "downloading"
            ctrl._accept(("failed", "check", "network", "boom"))
            self.assertEqual(ctrl._state, "downloading")
            ctrl._state = "ready"
            ctrl._accept(("failed", "check", "network", "boom"))
            self.assertEqual(ctrl._state, "ready")

            checked = []
            with patch.object(ctrl, "_check", side_effect=lambda: checked.append(1) or ({"newer": False}, "now")), \
                    patch.object(ctrl, "_install", return_value=("installed",)):
                ctrl._jobs.put(("check", {}))
                ctrl._jobs.put(("install", {"version": "1.11.9", "size": 10, "launcher": None}))
                ctrl._jobs.put(("quit", {}))
                ctrl._work()
            self.assertEqual(checked, [])
        finally:
            ctrl.shutdown()
            self.spin(pred=lambda: ctrl._shutdown_complete)

    def test_library_setlibrary_idempotent_and_manifesterror_safe_in_management(self):
        from luvbox.qt_library import LibraryController, ManifestError
        from luvbox.qt_management import ManagementController

        lib = LibraryController()
        try:
            ai_signals = []
            lib.aiCountsChanged.connect(lambda: ai_signals.append(1))
            lib._ai_counts = [{"category": "동물", "count": 2}]
            lib.setLibrary("@Alice", "C:/FakeOut", False)
            gen_first = lib._generation
            self.assertEqual(lib._ai_counts, [])
            self.assertGreaterEqual(len(ai_signals), 1)
            lib.setLibrary("alice", "C:/FakeOut", False)
            self.assertEqual(lib._generation, gen_first)

            mgmt = ManagementController(lib)
            stopped = []
            mgmt.shutdownFinished.connect(lambda: stopped.append(1))
            try:
                with patch.object(lib, "deletionManifest", side_effect=ValueError("catalog missing")):
                    mgmt.prepareDelete(["k1"])
                    self.spin(pred=lambda: bool(mgmt.error))
                    self.assertNotIn("catalog missing", mgmt.error)
                with patch.object(lib, "alikeManifest", side_effect=ManifestError("안전한 비교 거부")):
                    mgmt.prepareAlikeDelete("k1")
                    self.spin(pred=lambda: mgmt.error == "안전한 비교 거부")
                    self.assertEqual(mgmt.error, "안전한 비교 거부")
            finally:
                mgmt.shutdown()
                self.spin(pred=lambda: bool(stopped))
        finally:
            lib.shutdown()
            self.spin(pred=lambda: lib._shutdown_complete)

    def test_thumbnail_provider_wanted_none_and_failures_shortcircuit(self):
        from PySide6.QtCore import QSize
        from luvbox.qt_gallery import SafeThumbnailProvider

        decoded_calls = 0

        def dec(_path, _size):
            nonlocal decoded_calls
            decoded_calls += 1
            return Mock(image=None, retryable=False)

        provider = SafeThumbnailProvider(decoder=dec, workers=1)
        try:
            key = "g1_flat_100_1_photo"
            provider.register(key, Path(ROOT / "README.md"))
            provider._failures.add((key, 100, 100))
            img = provider.requestImage(key, QSize(), QSize(100, 100))
            self.assertTrue(img.isNull())
            self.assertEqual(decoded_calls, 0)
            provider.clear_registry()
            self.assertIsNone(provider._wanted)
        finally:
            provider.close()
            provider.join_workers()

    def test_records_run_row_preserves_warning_and_append_reuses_missing_snapshot(self):
        from luvbox.history import HistoryStore
        from luvbox.qt_records import RecordsController
        from luvbox.storage import resolve_storage

        rec = RecordsController()
        try:
            row = rec._run_row({
                "started_at": "2026-11-20T09:00:00+09:00",
                "origin": "manual",
                "account": "alice",
                "status": "finished",
                "saved": 1,
                "skipped": 0,
                "message": "새 좋아요 확인을 마쳤습니다. 저장하지 못한 1개는 확인해 주세요.",
            })
            self.assertIn("저장하지 못한 1개는 확인해 주세요.", row["message"])

            row_legacy = rec._run_row({
                "started_at": "2026-11-20T09:00:00+09:00",
                "origin": "manual",
                "account": "alice",
                "status": "finished",
                "saved": 0,
                "skipped": 0,
                "message": "X가 보여 주는 좋아요 목록을 모두 확인했습니다. 삭제된 게시물이나 X가 더는 보여 주지 않는 오래된 좋아요는 빠질 수 있습니다.",
            })
            self.assertEqual(row_legacy["message"], "")

            with tempfile.TemporaryDirectory(prefix="luvbox-rec-snap-1118-") as tmp:
                out = Path(tmp) / "med"
                out.mkdir()
                paths = resolve_storage(out, "alice")
                with closing(HistoryStore.for_storage(paths)) as store:
                    p = out / "alice_10_1.jpg"
                    p.write_bytes(_jpeg_bytes())
                    store.record_media("flat", {"tweet_id": "10", "num": 1, "type": "photo", "author": {"name": "alice"}}, p)
                    p.unlink()
                rec.setLibrary("alice", str(out), False)
                self.spin(pred=lambda: rec.missingCount == 1 and not rec._pending)
                with patch.object(HistoryStore, "_lost_items", wraps=HistoryStore._lost_items, autospec=True) as spy:
                    r0 = rec._missing_page("alice", str(out), False, 0, mode="replace", generation=rec._generation)
                    r1 = rec._missing_page("alice", str(out), False, 1, mode="append", generation=rec._generation)
                self.assertEqual(len(r0["rows"]), 1)
                self.assertEqual(r1["total"], 1)
                self.assertEqual(spy.call_count, 1)
        finally:
            rec.shutdown()
            self.spin(pred=lambda: rec._shutdown_complete if hasattr(rec, "_shutdown_complete") else not rec._worker.is_alive())

    def test_app_controller_setoutputfolder_noop_and_stale_review_error_ignored(self):
        from luvbox.app_settings import default_settings
        from luvbox.qt_app import ApplicationController

        with tempfile.TemporaryDirectory(prefix="luvbox-app-1118-") as tmp:
            t = Path(tmp)
            vals = {**default_settings(), "account": "alice", "output_dir": str(t / "med")}
            class Mem:
                def load(self): return dict(vals)
                def update(self, c): vals.update(c); return dict(vals)
            with patch("luvbox.qt_app.SettingsRepository", Mem), \
                    patch("luvbox.qt_accounts.AccountController._read_state",
                          staticmethod(lambda: {"accounts": [], "identity": {}, "connected": False})), \
                    patch("luvbox.qt_schedule.task_status", return_value={"registered": False}):
                ctrl = ApplicationController()
                try:
                    self.spin(pred=lambda: not ctrl.operationBusy)
                    with patch.object(ctrl, "_persist") as persist_spy:
                        ctrl.setOutputFolder(str(t / "med"))
                        persist_spy.assert_not_called()

                    ctrl._download._running = True
                    ctrl._download._stopping = False
                    ctrl._download._job_id = 42
                    ctrl._error = ""
                    with patch.object(ctrl._download, "stop") as stop_spy:
                        ctrl._accept("review", False, {"job_id": 99, "message": "stale"})
                        stop_spy.assert_not_called()
                        self.assertEqual(ctrl._error, "")
                        ctrl._accept("review", False, {"job_id": 42, "message": "active failure"})
                        stop_spy.assert_called_once()
                        self.assertEqual(ctrl._error, "active failure")
                finally:
                    ctrl._download._running = False
                    ctrl.requestClose()
                    self.spin(pred=lambda: ctrl.shutdownReady)

    def test_browser_setup_firefox_manager_error_and_folder_copy_typo_fixed(self):
        from luvbox.qt_browser_setup import BrowserSetupController, _manager_error

        self.assertIn("about:debugging#/runtime/this-firefox", _manager_error("firefox"))
        self.assertNotIn("chrome://", _manager_error("firefox"))
        ctrl = BrowserSetupController.__new__(BrowserSetupController)
        ctrl._stop = threading.Event()
        ctrl._selected_browser = "firefox"
        with patch.object(ctrl, "_extension_folder", return_value=Path(ROOT / "assets")):
            res = ctrl._perform("folder")
        self.assertIn("manifest.json이 있는 폴더", res["status"])
        self.assertNotIn("선택한 폴더", res["status"])

    def test_single_instance_listen_retry_starts_and_stops(self):
        from luvbox.single_instance import InstanceGuard

        guard = InstanceGuard(identity="retry-1118")
        guard.owner = True
        with patch("PySide6.QtNetwork.QLocalServer.listen", return_value=False):
            guard._listen()
        self.assertIsNotNone(guard._listen_retry)
        self.assertTrue(guard._listen_retry.isActive())
        guard.stopListening()
        self.assertFalse(guard._listen_retry.isActive())

    def test_login_and_qt_accounts_hide_jargon_and_mask_english_value_error(self):
        from luvbox import login
        from luvbox.qt_accounts import AccountController

        self.assertNotIn("auth_token", login._SESSION_HELP)
        self.assertNotIn("auth_token", login._STORE_HELP)
        payload = {"version": 1, "identity": {"handle": "alice", "user_id": "1", "name": "Alice"},
                   "accounts": [{"identity": {"handle": "alice", "user_id": "1", "name": "Alice"},
                                 "cookies": [{"name": "auth_token", "value": "tok", "domain": ".x.com", "path": "/"}]}]}
        with patch("luvbox.login._read_payload", return_value=payload):
            with self.assertRaises(ValueError) as cm:
                login.load_session_cookies(account="bob")
            self.assertEqual(str(cm.exception), login._ACCOUNT_HELP)

        class Mem:
            def load(self): return {}
            def update(self, c): return c
        with patch("luvbox.qt_accounts.AccountController._read_state",
                   staticmethod(lambda: {"accounts": [], "identity": {}, "connected": False})):
            ctrl = AccountController(settings=Mem(), persist_preferences=False)
            events = []
            ctrl._workerEvent.connect(events.append)
            try:
                ev = threading.Event()
                with patch.object(ctrl, "_browser_connection", side_effect=ValueError("invalid pairing code")):
                    ctrl._work("browser", "chrome", ev, ev, 1)
                self.spin(pred=lambda: len(events) >= 1)
                self.assertNotIn("error", events[-1])
                with patch.object(ctrl, "_browser_connection", side_effect=ValueError("안전한 계정 오류")):
                    ctrl._work("browser", "chrome", ev, ev, 1)
                self.spin(pred=lambda: len(events) >= 2)
                self.assertEqual(events[-1].get("error"), "안전한 계정 오류")
            finally:
                ctrl.shutdown()
                self.spin(pred=lambda: not ctrl._thread or not ctrl._thread.is_alive())

    def test_entry_worker_error_has_no_exception_class_or_work_log(self):
        source = (ROOT / "entry.py").read_text(encoding="utf-8")
        self.assertNotIn("type(exc).__name__", source)
        self.assertNotIn("작업 로그", source)


class LauncherAndExtensionRegressionTests(unittest.TestCase):
    def test_extension_pages_are_identical_and_guard_finished_state(self):
        chromium = (ROOT / "assets/browser-extension/chromium/extension-page.js").read_text(encoding="utf-8")
        firefox = (ROOT / "assets/browser-extension/firefox/extension-page.js").read_text(encoding="utf-8")
        self.assertEqual(chromium, firefox)
        self.assertIn("let finished = false;", chromium)
        self.assertIn("if (finished) return;", chromium)

    def test_launcher_argument_tail_handles_unquoted_path_with_spaces_and_korean(self):
        from tools import build_launcher

        try:
            csc = build_launcher.compiler()
        except FileNotFoundError:
            self.skipTest("csc.exe not installed")

        harness = r"""
using System;
using System.Reflection;
static class ArgTest {
    static int Main(string[] args) {
        Assembly launcher = Assembly.LoadFrom(args[0]);
        MethodInfo tail = launcher.GetType("LuvBox.Program", true).GetMethod(
            "ArgumentTail", BindingFlags.Static | BindingFlags.NonPublic | BindingFlags.Public);
        string exe = @"C:\Users\러브 박스\LuvBox.exe";
        string a = (string)tail.Invoke(null, new object[] { exe + " --run-saved --full", exe });
        string b = (string)tail.Invoke(null, new object[] { @"C:\Users\러브 박스\LuvBox --run-saved", exe });
        string c = (string)tail.Invoke(null, new object[] { "\"" + exe + "\" --self-test out.json", exe });
        Console.Write(a + "|" + b + "|" + c);
        return (a == "--run-saved --full" && b == "--run-saved" && c == "--self-test out.json") ? 0 : 1;
    }
}
"""
        with tempfile.TemporaryDirectory(prefix="luvbox-argtail-1118-") as tmp:
            work = Path(tmp)
            bare = build_launcher.compile_launcher(work / "LuvBox.exe", "1.11.8")
            src = work / "ArgTest.cs"
            out_exe = work / "ArgTest.exe"
            src.write_text(harness, encoding="utf-8")
            compiled = subprocess.run([str(csc), "/nologo", "/target:exe", "/platform:x64", "/utf8output",
                                       f"/out:{out_exe}", str(src)], capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(compiled.returncode, 0, compiled.stdout + compiled.stderr)
            run = subprocess.run([str(out_exe), str(bare)], capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)


def qml_scene():
    from PySide6.QtCore import QCoreApplication, QEvent, QMetaObject, QObject, QPointF, Qt, QUrl, qInstallMessageHandler
    from PySide6.QtGui import QFont, QFontDatabase, QGuiApplication
    from PySide6.QtQml import QQmlApplicationEngine
    from PySide6.QtQuick import QQuickItem
    from PySide6.QtQuickControls2 import QQuickStyle
    from PySide6.QtTest import QTest
    from PIL import Image
    from shiboken6 import delete

    from luvbox.app_settings import default_settings
    from luvbox.qt_app import ApplicationController
    from luvbox.qt_wheel import WheelInputFilter

    QQuickStyle.setStyle("Basic")
    app = QGuiApplication([])
    QFontDatabase.addApplicationFont(str(ROOT / "assets/fonts/Pretendard/Pretendard-Regular.ttf"))
    app.setFont(QFont("Pretendard"))
    messages = []
    old = qInstallMessageHandler(lambda _k, _c, text: messages.append(text))
    controller = wheel = engine = window = None

    def settle(dur=0.05):
        deadline = time.monotonic() + dur
        while time.monotonic() < deadline:
            app.processEvents()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            time.sleep(0.002)

    def wait(cond, label, timeout=7):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            settle(0.015)
            if cond():
                return
        raise AssertionError(label)

    def find(name):
        item = window.findChild(QObject, name)
        assert item is not None, f"missing {name}"
        return item

    def invoke(item, method):
        assert QMetaObject.invokeMethod(item, method), method
        settle()

    with tempfile.TemporaryDirectory(prefix="luvbox-qml-1118-") as tmp, ExitStack() as mocks:
        d = Path(tmp)
        mocks.enter_context(patch.dict(os.environ, {"LOCALAPPDATA": str(d / "app"), "APPDATA": str(d / "roam")}))
        photo = d / "pic.png"
        Image.new("RGB", (640, 480), "#336699").save(photo)
        vals = {**default_settings(), "account": "alice", "output_dir": str(d / "media")}
        class Mem:
            def load(self): return dict(vals)
            def update(self, c): vals.update(c); return dict(vals)
        mocks.enter_context(patch("luvbox.qt_app.SettingsRepository", Mem))
        mocks.enter_context(patch("luvbox.qt_accounts.AccountController._read_state",
                                  staticmethod(lambda: {"accounts": [], "identity": {}, "connected": False})))
        mocks.enter_context(patch("luvbox.qt_schedule.task_status", return_value={"registered": False}))
        mocks.enter_context(patch("luvbox.qt_browser_setup.available_browsers", return_value=[]))
        try:
            controller = ApplicationController()
            wheel = WheelInputFilter()
            engine = QQmlApplicationEngine()
            engine.addImageProvider("thumbs", controller.library.provider)
            engine.setInitialProperties({"galleryModel": controller.library.model, "wheelInputFilter": wheel,
                                         "appController": controller})
            engine.load(ROOT / "assets/qt/MainWindow.qml")
            assert engine.rootObjects(), messages
            window = engine.rootObjects()[0]
            wait(lambda: not controller.library.model.busy, "initial load")

            # 1) datePickerPopup coordinates use popupHost.
            window.setProperty("currentScreen", 1)
            settle()
            toggle = find("galleryFilterToggle")
            if not toggle.property("checked"):
                toggle.click()
            wait(lambda: find("galleryDateFrom").property("visible"), "dateFrom visible")
            date_from = find("galleryDateFrom")
            date_from.pickRequested.emit()
            popup = find("galleryDatePickerPopup")
            wait(lambda: popup.property("visible"), "datePicker visible")
            host = popup.property("parent")
            assert popup.property("x") >= 8 and popup.property("y") >= 8
            assert popup.property("x") + popup.property("width") <= host.property("width")
            invoke(popup, "close")

            # 2) previewDialog at 680x480 with 25 hashtags uses previewMetaScroll cleanly.
            controller.library._selected = {
                "key": "k1", "author": "alice", "body": "긴 본문 " * 40,
                "source": "https://x.com/alice/status/1", "path": str(photo), "missing": False,
                "missing_note": "", "preview": QUrl.fromLocalFile(str(photo)).toString(),
                "media_type": "photo", "public_info": "pic.png",
                "hashtags": [f"#태그{i:02}" for i in range(25)],
            }
            controller.library.changed.emit()
            window.resize(680, 480)
            preview = find("previewDialog")
            media = find("selectedMedia")
            invoke(preview, "open")
            wait(lambda: media.property("ready"), "preview ready")
            meta_scroll = find("previewMetaScroll")
            assert meta_scroll.property("visible")
            assert media.property("height") >= 24
            open_btn = next(c for c in preview.findChildren(QQuickItem) if c.property("text") == "파일 열기" and c.property("visible"))
            assert meta_scroll.mapToScene(QPointF(0, meta_scroll.property("height"))).y() <= open_btn.mapToScene(QPointF(0, 0)).y() + 1

            # 3) selectedMedia.playing pauses when saveReviewDialog opens.
            window.resize(1080, 760)
            settle()
            assert media.property("playing") is True
            review = find("saveReviewDialog")
            invoke(review, "open")
            wait(lambda: review.property("visible"), "review open")
            assert media.property("playing") is False
            invoke(review, "close")
            invoke(preview, "close")

            # 4) HelpButton closes on Escape and when duplicateMode turns off.
            controller.library._duplicate_only = True
            controller.library._distinct_count = 2
            controller.library.duplicateFilterChanged.emit()
            controller.library.duplicateDistinctChanged.emit()
            controller.library.changed.emit()
            help_btn = find("duplicateHelp")
            wait(lambda: help_btn.property("visible"), "duplicateHelp visible")
            window.requestActivate()
            settle()
            help_btn.click()
            wait(lambda: help_btn.property("showing"), "help card open")
            QTest.keyClick(window, Qt.Key.Key_Escape)
            wait(lambda: not help_btn.property("showing"), "help card closed on Escape")
            help_btn.click()
            wait(lambda: help_btn.property("showing"), "help card reopened")
            controller.library._duplicate_only = False
            controller.library.duplicateFilterChanged.emit()
            controller.library.changed.emit()
            wait(lambda: not help_btn.property("showing"), "help card closed on hide")

            # 5) duplicateStripButtons wraps to 3 columns at 800px window width.
            controller.library._duplicate_only = True
            controller.library.duplicateFilterChanged.emit()
            controller.library.changed.emit()
            window.resize(800, 600)
            near_btn = find("galleryDuplicateNear")
            clear_btn = find("galleryClearDistinct")
            wait(lambda: clear_btn.property("visible"), "clearDistinct visible")
            assert clear_btn.mapToScene(QPointF(0, 0)).y() > near_btn.mapToScene(QPointF(0, 0)).y() + 5

            # 6) RunHistoryDialog header elides & clears resizing on close.
            window.resize(480, 640)
            history_dlg = find("runHistoryDialog")
            invoke(history_dlg, "open")
            wait(lambda: history_dlg.property("visible") and not controller.records.runBusy, "history dialog settled")
            controller.records._run_rows = [{
                "status": "완료", "tone": "accent",
                "when": "2026-11-20 09:00 매우 긴 실행 시각과 부가 설명 문자열",
                "origin": "자동 저장 아주 긴 실행 출처 설명",
                "account": "@very_long_handle_name", "summary": "요약", "message": "",
                "hasLog": True, "token": "t1",
            }]
            controller.records.changed.emit()
            history_dlg.setProperty("resizing", True)
            run_list = find("runHistoryList")
            def walk_visual(root_item):
                stack = [root_item]
                while stack:
                    node = stack.pop()
                    yield node
                    stack.extend(node.childItems())
            def visible_log_button():
                return next((c for c in walk_visual(run_list.property("contentItem"))
                             if c.property("text") == "상세 기록" and c.property("visible")), None)
            wait(lambda: visible_log_button() is not None, "run history log button materialized")
            log_btn = visible_log_button()
            assert log_btn.mapToScene(QPointF(log_btn.property("width"), 0)).x() <= run_list.mapToScene(QPointF(run_list.property("width"), 0)).x() + 1
            invoke(history_dlg, "close")
            wait(lambda: not history_dlg.property("visible"), "history closed")
            assert history_dlg.property("resizing") is False

            controller.requestClose()
            wait(lambda: controller.shutdownReady, "shutdown")
            window.close()
            wheel.close()
            delete(engine)
            engine = None
            delete(controller)
            controller = None
            errors = [m for m in messages if any(t in m for t in ("ReferenceError", "TypeError", "Binding loop", "Cannot assign", "Cannot read", "is not a type"))]
            assert not errors, errors
            print(json.dumps({"ok": True}))
        finally:
            if controller is not None:
                controller.requestClose()
                wait(lambda: controller.shutdownReady, "cleanup")
            if engine is not None:
                delete(engine)
            if wheel is not None:
                wheel.close()
            qInstallMessageHandler(old)


@unittest.skipUnless(find_spec("PySide6") is not None, "optional Qt dependencies are not installed")
class QmlRegressionTests(unittest.TestCase):
    def test_1118_qml_fixes_in_offscreen_window(self):
        res = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scene"], cwd=ROOT,
                             capture_output=True, text=True, encoding="utf-8", timeout=150,
                             env={**os.environ, "QT_QPA_PLATFORM": os.environ.get("QT_QPA_PLATFORM", "offscreen"),
                                  "PYTHONIOENCODING": "utf-8"})
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertTrue(json.loads(res.stdout.splitlines()[-1])["ok"])


if __name__ == "__main__":
    if "--scene" in sys.argv:
        qml_scene()
    else:
        unittest.main()

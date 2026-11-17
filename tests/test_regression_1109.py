"""Regression tests covering issues fixed in 1.10.9 across download, catalog, Qt, and launcher layers."""

from __future__ import annotations

from contextlib import closing
import ctypes
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch

try:
    from tests import app_data_isolation  # noqa: F401  (never touch this PC's real app data)
except ModuleNotFoundError:  # invoked as subprocess script directly
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from tests import app_data_isolation  # noqa: F401

from gallery_dl.extractor.common import Extractor
from gallery_dl.extractor.message import Message

from luvbox import launcher, metadata, recovery, settings, storage, unsaved, updater
from luvbox.core import QUICK_STOP_AFTER, JobOptions, run_worker
from luvbox.history import CatalogDamaged, HistoryError, HistoryStore
from luvbox.qt_app import _start_controller
from luvbox.recycling import file_signature
from luvbox.storage import MigrationError, resolve_storage


ROOT = Path(__file__).resolve().parents[1]
_QT = importlib.util.find_spec("PySide6") is not None
if _QT:
    from PySide6.QtCore import QCoreApplication
    from luvbox.qt_library import LibraryController, LibraryRepository, _Request
    from luvbox.qt_records import RecordsController
    from luvbox.qt_schedule import ScheduleController


class _FakeLikesExtractor(Extractor):
    category = "twitter"
    subcategory = "likes"
    cookies_domain = ".x.com"
    filename_fmt = "{tweet_id}_{num}.{extension}"
    archive_fmt = "{tweet_id}_{num}"

    def __init__(self, url, rows):
        super().__init__(re.match(r"https://x\.com/([^/]+)/likes", url))
        self.rows = rows

    def items(self):
        for row in self.rows:
            metadata_item = {
                "tweet_id": row[0],
                "num": row[1],
                "type": row[2],
                "extension": "mp4" if row[2] in {"video", "animated_gif"} else "jpg",
                "author": {"name": row[3]},
                "user": {"name": "myaccount", "id": 999},
            }
            if len(row) > 4 and row[4] is not None:
                metadata_item["date"] = row[4]
            yield Message.Directory, "", metadata_item
            yield Message.Url, f"https://pbs.twimg.com/media/{row[0]}_{row[1]}?format=jpg&name=orig", metadata_item


class _FakeTweetExtractor(Extractor):
    category = "twitter"
    subcategory = "tweet"
    cookies_domain = ".x.com"
    filename_fmt = "{tweet_id}_{num}.{extension}"
    archive_fmt = "{tweet_id}_{num}"

    def __init__(self, url, tweets):
        super().__init__(re.match(r"https://x\.com/i/web/status/([0-9]+)", url))
        self.tweet_id = self.groups[0]
        self.tweets = tweets

    def items(self):
        for number, kind, author, posted in self.tweets.get(self.tweet_id, ()):
            meta = {
                "tweet_id": self.tweet_id,
                "num": number,
                "type": kind,
                "extension": "jpg",
                "author": {"name": author, "id": "42"},
                "user": {"name": "myaccount", "id": 999},
                "date": posted,
                "width": 640,
                "height": 480,
            }
            yield Message.Directory, "", meta
            yield Message.Url, f"https://pbs.twimg.com/media/{self.tweet_id}_{number}?format=jpg&name=orig", meta


class _FakeHttpDownloader:
    scheme = "http"
    downloaded = []
    fail = False

    def __init__(self, job):
        self.job = job

    def download(self, url, pathfmt):
        if self.fail:
            return False
        self.downloaded.append(url)
        with pathfmt.open() as fp:
            fp.write(b"\xff\xd8\xffsynthetic")
        return True


def _unused_drive_anchor() -> str:
    for letter in "ZYXWVUTSRQPONMLKJIHGFED":
        anchor = f"{letter}:\\"
        if not Path(anchor).exists():
            return anchor
    raise unittest.SkipTest("No unused Windows drive letter available")


class CoreAndPlatformRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="luvbox-reg-1109-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_spawn_detached_uses_breakaway_and_falls_back_on_permission_error(self):
        base = getattr(subprocess, "DETACHED_PROCESS", 0)
        expected = base | (0x01000000 if sys.platform == "win32" else 0)
        calls = []

        def fake_popen(cmd, **kwargs):
            calls.append(kwargs["creationflags"])
            if len(calls) == 1 and kwargs["creationflags"] != base:
                raise PermissionError("breakaway denied")
            return object()

        with patch("luvbox.launcher.subprocess.Popen", side_effect=fake_popen):
            launcher.spawn_detached(["C:\\fixture\\LuvBox.exe"])
        if sys.platform == "win32":
            self.assertEqual(calls, [expected, base])
        else:
            self.assertEqual(calls, [base])

    def test_updater_install_retries_transient_winerror_on_replace_and_rollback(self):
        downloaded = self.root / "downloaded.exe"
        downloaded.write_bytes(b"new-launcher-payload")
        expected = hashlib.sha256(downloaded.read_bytes()).hexdigest()
        target = self.root / "LuvBox.exe"
        target.write_bytes(b"old-launcher-payload")

        real_replace = os.replace
        attempts = {"swap": 0}

        def flaky_swap(src, dst):
            if Path(dst) == target and Path(src).name.endswith(".new") and attempts["swap"] == 0:
                attempts["swap"] += 1
                err = OSError("sharing violation")
                err.winerror = 32
                raise err
            return real_replace(src, dst)

        with patch("luvbox.updater.os.replace", side_effect=flaky_swap), patch("luvbox.updater.time.sleep"):
            updater.install(downloaded, target, expected)
        self.assertEqual(target.read_bytes(), b"new-launcher-payload")

        downloaded2 = self.root / "downloaded2.exe"
        downloaded2.write_bytes(b"newer-payload")
        expected2 = hashlib.sha256(downloaded2.read_bytes()).hexdigest()
        state = {"rollback": 0}

        def failing_swap_flaky_rollback(src, dst):
            src_name, dst_name = Path(src).name, Path(dst).name
            if dst_name == "LuvBox.exe" and src_name.endswith(".new"):
                raise PermissionError("blocked")
            if dst_name == "LuvBox.exe" and ".old" in src_name and state["rollback"] == 0:
                state["rollback"] += 1
                err = OSError("scanner lock")
                err.winerror = 32
                raise err
            return real_replace(src, dst)

        with patch("luvbox.updater.os.replace", side_effect=failing_swap_flaky_rollback), \
                patch("luvbox.updater.time.sleep"):
            with self.assertRaises(updater.UpdateError):
                updater.install(downloaded2, target, expected2)
        self.assertEqual(target.read_bytes(), b"new-launcher-payload")

    def test_start_controller_handles_app_data_conflict_and_error(self):
        class FakeStandardButton:
            Yes = 1
            No = 2

        class FakeBox:
            StandardButton = FakeStandardButton

            def __init__(self, answer):
                self.answer = answer
                self.questions = []
                self.criticals = []

            def question(self, *args):
                self.questions.append(args)
                return self.answer

            def critical(self, *args):
                self.criticals.append(args)

        sentinel = object()
        box_no = FakeBox(FakeStandardButton.No)
        with patch("luvbox.qt_app.ApplicationController", side_effect=settings.AppDataConflict(settings.CONFLICT_HELP)), \
                patch("luvbox.qt_app.settings.set_aside_previous") as set_aside, \
                patch("luvbox.launcher.signal_ready") as ready:
            self.assertIsNone(_start_controller(box_no))
            set_aside.assert_not_called()
            ready.assert_called_once()
        self.assertEqual(len(box_no.questions), 1)

        box_yes = FakeBox(FakeStandardButton.Yes)
        with patch("luvbox.qt_app.ApplicationController",
                   side_effect=[settings.AppDataConflict(settings.CONFLICT_HELP), sentinel]), \
                patch("luvbox.qt_app.settings.set_aside_previous") as set_aside, \
                patch("luvbox.launcher.signal_ready"):
            self.assertIs(_start_controller(box_yes), sentinel)
            set_aside.assert_called_once()

        box_err = FakeBox(FakeStandardButton.No)
        with patch("luvbox.qt_app.ApplicationController", side_effect=settings.AppDataError("이동 실패 상세")), \
                patch("luvbox.launcher.signal_ready"):
            self.assertIsNone(_start_controller(box_err))
        self.assertEqual(box_err.criticals[0][2], "이동 실패 상세")

    def test_metadata_preserve_permissions_handles_non_acl_volumes_and_sets_permission_reason(self):
        if os.name != "nt":
            self.skipTest("Windows DACL preservation check")
        src = self.root / "src.jpg"
        dst = self.root / "dst.jpg"
        src.write_bytes(b"a")
        dst.write_bytes(b"b")

        def make_dlls(code):
            def get_info(*_args):
                return code

            def get_ctrl(*_args):
                return 1

            def set_info(*_args):
                return code

            def local_free(*_args):
                return None

            sec = types.SimpleNamespace(
                GetNamedSecurityInfoW=get_info,
                GetSecurityDescriptorControl=get_ctrl,
                SetNamedSecurityInfoW=set_info,
            )
            kern = types.SimpleNamespace(LocalFree=local_free)
            return lambda name, use_last_error=False: sec if name == "advapi32" else kern

        with patch.object(ctypes, "WinDLL", side_effect=make_dlls(50)):
            metadata._preserve_permissions(src, dst)  # ERROR_NOT_SUPPORTED (exFAT/FAT32) ignored
        with patch.object(ctypes, "WinDLL", side_effect=make_dlls(5)):
            with self.assertRaises(PermissionError):
                metadata._preserve_permissions(src, dst)

        # embed_source attributes _preserve_permissions PermissionError to "permission", not "verification".
        with patch("luvbox.metadata._run", return_value=subprocess.CompletedProcess(["exiftool"], 0, b"", b"")), \
                patch("luvbox.metadata._read_tags", return_value={
                    "XMP-dc:Source": "https://x.com/i/web/status/10",
                    "XMP-dc:Description": "X post: https://x.com/i/web/status/10",
                }), \
                patch("luvbox.metadata._preserve_creation_time"), \
                patch("luvbox.metadata._preserve_permissions", side_effect=PermissionError("denied")):
            outcome = metadata.embed_source(src, "10")
        self.assertEqual(outcome["status"], "failed")
        self.assertEqual(outcome["reason"], "permission")

    def test_core_catalog_damaged_propagates_and_not_clobbered_in_finally(self):
        output = self.root / "media"
        options = JobOptions("@myaccount", str(output), folder_mode="author", separate_accounts=True)
        cookies = [{"name": "auth_token", "value": "tok", "domain": ".x.com", "path": "/"}]
        events = []
        _FakeHttpDownloader.downloaded = []
        _FakeHttpDownloader.fail = False
        with patch("luvbox.protection.state_directory", return_value=self.root / "prot"), \
                patch("luvbox.login.load_session_cookies", return_value=cookies), \
                patch("luvbox.metadata.available", return_value=False), \
                patch("gallery_dl.extractor.find", side_effect=lambda url: _FakeLikesExtractor(url, [("901", 1, "photo", "alice")])), \
                patch("gallery_dl.downloader.find", return_value=_FakeHttpDownloader), \
                patch.object(HistoryStore, "record_media", side_effect=CatalogDamaged("broken catalog")), \
                patch.object(HistoryStore, "finish_run", side_effect=HistoryError("flush error")):
            code = run_worker(options, events.append)
        finished = events[-1]
        self.assertEqual(code, 1)
        self.assertEqual(finished["status"], "error")
        self.assertTrue(finished["catalog_damaged"])
        self.assertEqual(finished["message"], "broken catalog")

    def test_core_quick_scan_stops_ignoring_disabled_review_pending_types(self):
        output = self.root / "media"
        paths = resolve_storage(output, "myaccount", True)
        full_opts = JobOptions("@myaccount", str(output), folder_mode="author",
                               include_videos=False, include_gifs=False, scan_mode="full", separate_accounts=True)
        quick_opts = replace(full_opts, scan_mode="quick")
        now = datetime(2026, 10, 1, 12, tzinfo=timezone(timedelta(hours=9)))
        rows = [(str(1000 + idx), 1, "photo", "alice", now - timedelta(minutes=idx))
                for idx in range(QUICK_STOP_AFTER + 2)]
        cookies = [{"name": "auth_token", "value": "tok", "domain": ".x.com", "path": "/"}]
        _FakeHttpDownloader.downloaded = []
        _FakeHttpDownloader.fail = False
        with patch("luvbox.protection.state_directory", return_value=self.root / "prot"), \
                patch("luvbox.login.load_session_cookies", return_value=cookies), \
                patch("luvbox.metadata.available", return_value=False), \
                patch("gallery_dl.extractor.find", side_effect=lambda url: _FakeLikesExtractor(url, rows)), \
                patch("gallery_dl.downloader.find", return_value=_FakeHttpDownloader):
            self.assertEqual(run_worker(full_opts, lambda _e: None), 0)
            with closing(HistoryStore.for_storage(paths)) as store:
                store.set_review_pending({("9999", 1, "video")})
            events = []
            code = run_worker(quick_opts, events.append)
        self.assertEqual(code, 0)
        self.assertIn("이미 저장한 파일이", events[-1]["message"])
        with closing(HistoryStore.for_storage(paths)) as store:
            self.assertEqual(store.review_pending(), {("9999", 1, "video")})

    def test_core_retry_preserves_extracted_metadata_after_post_tweet_fallback_note(self):
        output = self.root / "media"
        options = JobOptions("@myaccount", str(output), folder_mode="flat",
                             separate_accounts=False, targets=("501_1",))
        posted = datetime(2026, 10, 5, 8, tzinfo=timezone(timedelta(hours=9)))
        tweets = {"501": [(1, "photo", "artist_alpha", posted)]}
        cookies = [{"name": "auth_token", "value": "tok", "domain": ".x.com", "path": "/"}]
        events = []
        _FakeHttpDownloader.fail = True
        try:
            with patch("luvbox.protection.state_directory", return_value=self.root / "prot"), \
                    patch("luvbox.login.load_session_cookies", return_value=cookies), \
                    patch("luvbox.metadata.available", return_value=False), \
                    patch("gallery_dl.extractor.find", side_effect=lambda url: _FakeTweetExtractor(url, tweets)), \
                    patch("gallery_dl.downloader.find", return_value=_FakeHttpDownloader):
                run_worker(options, events.append)
        finally:
            _FakeHttpDownloader.fail = False
        page = unsaved.page("myaccount", str(output), (unsaved.FAILED,), 0, 20)
        self.assertEqual(len(page["rows"]), 1)
        row = page["rows"][0]
        self.assertEqual((row["author"], row["width"], row["height"]), ("artist_alpha", 640, 480))
        self.assertTrue(row["posted_at"].startswith("2026-10-05"))

    def test_history_adopt_moved_root_handles_turkish_i_and_rebases_deletion_items(self):
        old_root = self.root / "old_\u0130_media"
        new_root = self.root / "new_media"
        old_root.mkdir()
        new_root.mkdir()
        old_paths = resolve_storage(old_root, "alice")
        keep_old = old_root / "sub" / "alice_101_1.jpg"
        recycled_old = old_root / "sub" / "alice_102_1.jpg"
        keep_old.parent.mkdir(parents=True)
        keep_old.write_bytes(b"keep")
        recycled_old.write_bytes(b"gone")
        with closing(HistoryStore.for_storage(old_paths)) as store:
            store.record_media("flat", {"tweet_id": "101", "num": 1, "type": "photo", "author": {"name": "alice"}}, keep_old)
            store.record_media("flat", {"tweet_id": "102", "num": 1, "type": "photo", "author": {"name": "alice"}}, recycled_old)
            rec = next(r for r in store.records() if r["tweet_id"] == "102")
            ident = store.begin_deletion("batch1", rec, False, file_signature(recycled_old))
            recycled_old.unlink()
            store.finish_deletion(ident, "recycled")

        keep_new = new_root / "sub" / "alice_101_1.jpg"
        keep_new.parent.mkdir(parents=True)
        keep_new.write_bytes(b"keep")
        new_paths = resolve_storage(new_root, "alice")
        storage.migrate_records(old_paths, new_paths)
        with closing(HistoryStore.for_storage(new_paths)) as store:
            adopted = store.adopt_moved_root(old_root)
            self.assertEqual(adopted, 1)
            listed = store.records(include_deleted=False)
            self.assertEqual([r["tweet_id"] for r in listed], ["101"])
            self.assertEqual(listed[0]["path"], str(keep_new.resolve()))
            row_deleted = store._db.execute("SELECT path FROM deletion_items WHERE tweet_id='102'").fetchone()
            self.assertEqual(row_deleted["path"], str((new_root / "sub" / "alice_102_1.jpg").resolve()))

    def test_history_restored_then_deleted_later_is_not_hidden_by_old_receipt(self):
        paths = resolve_storage(self.root / "media", "alice")
        paths.media_dir.mkdir(parents=True)
        f = paths.media_dir / "alice_201_1.jpg"
        with closing(HistoryStore.for_storage(paths)) as store:
            f.write_bytes(b"v1")
            store.record_media("flat", {"tweet_id": "201", "num": 1, "type": "photo", "author": {"name": "alice"}}, f)
            rec = store.records()[0]
            ident = store.begin_deletion("batch1", rec, False, file_signature(f))
            f.unlink()
            store.finish_deletion(ident, "recycled")
            self.assertEqual([r["tweet_id"] for r in store.records(include_deleted=False)], [])
            store._db.execute("UPDATE deletion_items SET finished_at='2026-01-01T00:00:00+00:00'")
            store._db.execute("UPDATE media SET missing_since='2026-01-01T00:00:00+00:00' WHERE tweet_id='201'")
            store._db.commit()
            f.write_bytes(b"v2")
            store.record_media("flat", {"tweet_id": "201", "num": 1, "type": "photo", "author": {"name": "alice"}}, f)
            f.unlink()
            store.import_legacy(refresh=True)
            self.assertEqual([r["tweet_id"] for r in store.records(include_deleted=False)], ["201"])
            self.assertEqual(store.lost_count(), 1)

    def test_history_disconnected_former_root_keeps_filesize_nocase_and_not_lost(self):
        if os.name != "nt":
            self.skipTest("Windows drive letter disconnection check")
        drive = _unused_drive_anchor()
        old_root = Path(drive) / "OldLuvBox"
        new_root = self.root / "NewLuvBox"
        new_root.mkdir()
        new_paths = resolve_storage(new_root, "alice")
        former_file = old_root / "alice_301_1.jpg"
        with closing(HistoryStore.for_storage(new_paths)) as store:
            store._db.execute(
                "INSERT INTO catalog_meta(key,value) VALUES ('allowed_media_roots',?)",
                (json.dumps([str(new_root), str(old_root)]),),
            )
            store._db.execute(
                "INSERT INTO media(folder_mode,tweet_id,num,media_type,author,filename,path,filesize,status,record_source,updated_at)"
                " VALUES ('flat','301',1,'photo','alice','alice_301_1.jpg',?,4321,'saved','saved','2026-01-01T00:00:00+00:00')",
                (str(former_file),),
            )
            store._db.commit()
            store._media_roots = store._read_media_roots()
            self.assertIsNotNone(store._media_path(str(old_root / "ALICE_301_1.JPG")))
            store.import_legacy(refresh=True)
            row = store.records()[0]
            self.assertEqual((row["path"], row["filesize"]), (str(former_file), 4321))
            store.mark_metadata(row, "embedded")
            self.assertEqual(store.records()[0]["filesize"], 4321)
            self.assertEqual(store.lost_count(), 0)

    def test_storage_migrate_records_initializes_full_schema_when_missing_history(self):
        src = resolve_storage(self.root / "src_media", "alice")
        dst = resolve_storage(self.root / "dst_media", "alice")
        src.media_dir.mkdir(parents=True)
        res = storage.migrate_records(src, dst)
        self.assertTrue(res.changed)
        names = {p.name for p in dst.record_dir.iterdir()}
        self.assertEqual(names, {".luvbox-history.sqlite3", "snapshot.json"})
        with closing(sqlite3.connect(dst.record_dir / ".luvbox-history.sqlite3")) as db:
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"media", "runs", "catalog_meta", "deletion_items"}.issubset(tables))

    def test_backup_stall_deadline_allows_slow_progress_and_aborts_on_stall(self):
        src = self.root / "src.sqlite3"
        with closing(sqlite3.connect(src)) as db:
            db.execute("CREATE TABLE t(x TEXT)")
            for _ in range(400):
                db.execute("INSERT INTO t VALUES (?)", ("v" * 200,))
            db.commit()

        now = {"t": 100.0}
        real_connect = sqlite3.connect

        class _ProxyConn:
            def __init__(self, inner, mode):
                self._inner = inner
                self._mode = mode

            def backup(self, target, *, pages=256, progress=None, name="main", sleep=0.25):
                real_target = target._inner if isinstance(target, _ProxyConn) else target
                if self._mode == "advance":
                    for rem in (40, 20, 0):
                        now["t"] += 2.1
                        progress(0, rem, 60)
                    return self._inner.backup(real_target, pages=-1, name=name, sleep=sleep)
                progress(0, 40, 60)
                now["t"] += 6.0
                progress(0, 40, 60)
                return None

            def __getattr__(self, item):
                return getattr(self._inner, item)

        def connect_factory(mode):
            def connect(*args, **kwargs):
                return _ProxyConn(real_connect(*args, **kwargs), mode)
            return connect

        with patch("luvbox.storage.time.monotonic", side_effect=lambda: now["t"]), \
                patch("luvbox.storage.sqlite3.connect", side_effect=connect_factory("advance")):
            storage._backup_database(src, self.root / "out_ok.sqlite3")
        self.assertTrue((self.root / "out_ok.sqlite3").is_file())

        now["t"] = 200.0
        with patch("luvbox.storage.time.monotonic", side_effect=lambda: now["t"]), \
                patch("luvbox.storage.sqlite3.connect", side_effect=connect_factory("stall")):
            with self.assertRaises(MigrationError):
                storage._backup_database(src, self.root / "out_stall.sqlite3")

        acct = self.root / "acct"
        acct.mkdir()
        (acct / recovery.HISTORY_NAME).write_bytes(src.read_bytes())
        now["t"] = 300.0
        with patch("luvbox.recovery.time.monotonic", side_effect=lambda: now["t"]), \
                patch("luvbox.recovery._catalog_backups", return_value=self.root / "backups"), \
                patch("luvbox.recovery.sqlite3.connect", side_effect=connect_factory("advance")):
            out = recovery.backup_catalog(acct, "alice")
        self.assertIsNotNone(out)


@unittest.skipUnless(_QT, "Qt dependencies are not installed")
class QtControllerRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="luvbox-qt-reg-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def wait(self, predicate, seconds=5):
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            self.app.processEvents()
            if predicate():
                return
            time.sleep(0.005)
        self.fail("Qt regression fixture timed out")

    def test_schedule_shutdown_cancels_slow_powershell(self):
        killed = threading.Event()
        entered = threading.Event()

        class FakeProc:
            def __init__(self, *args, **kwargs):
                self.returncode = 0
                entered.set()

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def communicate(self, timeout=None):
                if not killed.is_set():
                    raise subprocess.TimeoutExpired("pwsh", timeout or 0)
                return b"", b""

            def kill(self):
                killed.set()

        with patch("luvbox.scheduled.subprocess.Popen", FakeProc):
            ctrl = ScheduleController()
            self.wait(entered.is_set, seconds=3)
            t0 = time.monotonic()
            ctrl.shutdown()
            self.wait(lambda: not ctrl._worker.is_alive(), seconds=3)
            self.assertLess(time.monotonic() - t0, 3.0)
            self.assertTrue(killed.is_set())

    def test_library_partial_reload_and_teach_shutdown_and_duplicate_paging_filters(self):
        paths = resolve_storage(self.root / "media", "alice")
        paths.media_dir.mkdir(parents=True)
        with closing(HistoryStore.for_storage(paths)) as store:
            imgs = {}
            for idx, (author, content) in enumerate((("alice", "dup1"), ("alice", "dup1"),
                                                     ("alice", "dup2"), ("alice", "dup2"),
                                                     ("bob", "dup3"), ("bob", "dup3")), 1):
                p = paths.media_dir / f"{author}_{100 + idx}_1.jpg"
                payload = content.encode()
                p.write_bytes(payload)
                tid = str(100 + idx)
                store.record_media("flat", {"tweet_id": tid, "num": 1, "type": "photo",
                                            "author": {"name": author}}, p)
                imgs[tid] = hashlib.sha256(payload).hexdigest()
            store.save_image_hashes([
                ("flat", str(100 + i), 1, "photo", 4, imgs[str(100 + i)], f"{i:016x}")
                for i in range(1, 7)
            ])
            store.save_clusters([
                [("flat", "101", 1, "photo"), ("flat", "102", 1, "photo")],
                [("flat", "103", 1, "photo"), ("flat", "104", 1, "photo")],
                [("flat", "105", 1, "photo"), ("flat", "106", 1, "photo")],
            ])

        repo = LibraryRepository()
        repo.provider = types.SimpleNamespace(register=lambda *_a: None)
        req = _Request(1, "alice", str(paths.output_dir), False, "", "all", "saved",
                       "", "posted", "", "", "", False, "", True, "", "")
        repo.configure(req)
        total, p0 = repo.read("1", 0, 2)
        self.assertEqual((total, len(p0)), (6, 2))
        self.assertEqual([r["key"].split("_")[2] for r in p0], ["102", "101"])
        total1, p1 = repo.read("1", 2, 2)
        self.assertEqual((total1, len(p1)), (6, 2))
        self.assertEqual([r["key"].split("_")[2] for r in p1], ["104", "103"])
        total2, p2 = repo.read("1", 4, 2)
        self.assertEqual((total2, len(p2)), (6, 2))
        self.assertEqual([r["key"].split("_")[2] for r in p2], ["106", "105"])
        _, p_end = repo.read("1", 6, 2)
        self.assertEqual(p_end, [])

        req_bob = _Request(2, "alice", str(paths.output_dir), False, "", "all", "saved",
                           "bob", "posted", "", "", "", False, "", True, "", "")
        repo.configure(req_bob)
        self.assertEqual([k.split("_")[2] for k in repo.duplicate_copy_keys(2)], ["105"])

        repo.configure(req)

        def fake_ensure_model(*, progress_callback=None):
            repo.configure(None)
            progress_callback(1, 2)

        from luvbox import clip_classifier
        from luvbox.duplicates import ScanCancelled
        with patch.object(clip_classifier, "is_available", return_value=False), \
                patch.object(clip_classifier, "ensure_model", side_effect=fake_ensure_model):
            with self.assertRaises(ScanCancelled):
                repo.teach_labels(1, [("flat", "101", 1, "photo")], "인물")

        ctrl = LibraryController()
        self.addCleanup(lambda: (ctrl.shutdown(), self.wait(lambda: ctrl.shutdownComplete)))
        ctrl._account = "alice"
        ctrl._output_dir = str(paths.output_dir)
        ctrl._separate_accounts = False
        ctrl._authors_loaded = ctrl._hashtags_loaded = ctrl._user_tags_loaded = True
        ctrl._duplicate_counts_loaded = True
        ctrl._distinct_counts_loaded = True
        ctrl._ai_counts_loaded = False
        self.assertFalse(ctrl._counts_loaded)
        asked = []
        with patch.object(ctrl._repository, "authors", side_effect=lambda *_a: asked.append("authors") or []), \
                patch.object(ctrl._repository, "hashtags", side_effect=lambda *_a: asked.append("hashtags") or []), \
                patch.object(ctrl._repository, "user_tags", side_effect=lambda *_a: asked.append("user_tags") or []), \
                patch.object(ctrl._repository, "duplicate_counts", side_effect=lambda *_a: asked.append("duplicate_counts") or 0), \
                patch.object(ctrl._repository, "distinct_counts", side_effect=lambda *_a: asked.append("distinct_counts") or 0), \
                patch.object(ctrl._repository, "ai_counts", side_effect=lambda *_a: asked.append("ai_counts") or []):
            ctrl._refresh(reload_authors=False)
            self.wait(lambda: ctrl._ai_counts_loaded)
        self.assertEqual(asked, ["ai_counts"])
        self.assertTrue(ctrl._counts_loaded)

    def test_records_priority_queue_retry_dedupe_and_missing_cache(self):
        paths = resolve_storage(self.root / "media", "alice")
        paths.media_dir.mkdir(parents=True)
        with closing(HistoryStore.for_storage(paths)) as store:
            for i in range(120):
                p = paths.media_dir / f"alice_{700 + i}_1.jpg"
                p.write_bytes(b"x")
                store.record_media("flat", {"tweet_id": str(700 + i), "num": 1, "type": "photo",
                                            "author": {"name": "alice"}}, p)
                p.unlink()
        rec = RecordsController()
        self.addCleanup(lambda: (rec.shutdown(), self.wait(lambda: not rec._worker.is_alive())))
        with rec._condition:
            rec._pending.clear()
        rec._enqueue("counts", None, replace=True)
        rec._enqueue("runs", (0, 1), replace=True)
        with rec._condition:
            self.assertEqual([item[0] for item in rec._pending], ["runs", "counts"])

        rec._retry_busy = False
        with rec._condition:
            rec._pending.clear()
        rec.retryAll()
        rec.retryAll()
        with rec._condition:
            self.assertEqual([item[0] for item in rec._pending], ["retry_keys"])
        rec._accept(("retry_keys", rec._generation, {"keys": ()}, ""))
        self.assertFalse(rec._retry_busy)

        with patch.object(HistoryStore, "_lost_items", wraps=HistoryStore._lost_items, autospec=True) as lost_spy:
            rec.setLibrary("alice", str(paths.output_dir), False)
            rec.setActive("unsaved", True)
            rec.setUnsavedView("missing")
            self.wait(lambda: len(rec.unsavedRows) == 100 and not rec.unsavedBusy)
            calls_before_append = lost_spy.call_count
            rec.loadMoreUnsaved()
            self.wait(lambda: len(rec.unsavedRows) == 120 and not rec.unsavedBusy)
            self.assertEqual(lost_spy.call_count, calls_before_append)


def _run_qml_scene():
    from PIL import Image
    from PySide6.QtCore import QEvent, QMetaObject, QObject, QTimer, Qt, qInstallMessageHandler
    from PySide6.QtGui import QFont, QFontDatabase, QKeyEvent
    from PySide6.QtQml import QQmlApplicationEngine, QQmlEngine, QQmlExpression
    from PySide6.QtQuickControls2 import QQuickStyle
    from PySide6.QtWidgets import QApplication
    from shiboken6 import delete

    from luvbox.qt_app import ApplicationController
    from luvbox.qt_wheel import WheelInputFilter

    QQuickStyle.setStyle("Basic")
    app = QApplication([])
    for filename in ("Pretendard-Regular.ttf", "Pretendard-Bold.ttf"):
        QFontDatabase.addApplicationFont(str(ROOT / "assets/fonts/Pretendard" / filename))
    font = QFont("Pretendard")
    font.setPixelSize(14)
    app.setFont(font)

    errors = []
    qInstallMessageHandler(
        lambda _level, _ctx, msg: errors.append(msg)
        if not msg.startswith(("QFontDatabase:", "Using Qt multimedia")) else None)
    base = Path(os.environ["LOCALAPPDATA"])
    paths = resolve_storage(base / "photos", "offline")
    paths.media_dir.mkdir(parents=True)
    same_png = paths.media_dir / "same.png"
    other_png = paths.media_dir / "other.png"
    Image.new("RGB", (32, 32), (200, 80, 80)).save(same_png)
    Image.new("RGB", (64, 64), (40, 120, 200)).save(other_png)
    same_bytes = same_png.read_bytes()
    other_bytes = other_png.read_bytes()
    same_png.unlink()
    other_png.unlink()
    with closing(HistoryStore.for_storage(paths)) as store:
        for tid, payload in (("101", same_bytes), ("102", same_bytes), ("103", other_bytes)):
            p = paths.media_dir / f"alice_{tid}_1.png"
            p.write_bytes(payload)
            store.record_media("flat", {"tweet_id": tid, "num": 1, "type": "photo", "author": {"name": "alice"}}, p)
        store.save_image_hashes([
            ("flat", "101", 1, "photo", len(same_bytes), hashlib.sha256(same_bytes).hexdigest(), "0000000000000001"),
            ("flat", "102", 1, "photo", len(same_bytes), hashlib.sha256(same_bytes).hexdigest(), "0000000000000001"),
            ("flat", "103", 1, "photo", len(other_bytes), hashlib.sha256(other_bytes).hexdigest(), "0000000000000002"),
        ])
        store.save_clusters([[("flat", "103", 1, "photo"), ("flat", "101", 1, "photo"), ("flat", "102", 1, "photo")]])
        store.set_duplicate_rule("near")
    unsaved.record([
        {"reason": unsaved.FAILED, "tweet_id": "201", "num": 1, "media_type": "photo",
         "author": "alice", "posted_at": "2026-10-01T00:00:00+00:00",
         "width": 100, "height": 100, "duration": 0, "size_bytes": 0, "limit_mb": 0, "detail": "failed"}
    ], account="offline", output_dir=str(paths.output_dir))
    (base / "LuvBox").mkdir(exist_ok=True)
    (base / "LuvBox" / "settings.json").write_text(
        json.dumps({"account": "offline", "output_dir": str(paths.output_dir)}), encoding="utf-8")

    with patch("luvbox.qt_schedule.task_status", return_value={"registered": False}):
        controller = ApplicationController()
        wheel = WheelInputFilter()
        engine = QQmlApplicationEngine()
        engine.warnings.connect(lambda warnings: errors.extend(str(w) for w in warnings))
        engine.addImageProvider("thumbs", controller._library.provider)
        engine.setInitialProperties({"galleryModel": controller._library.model,
                                     "wheelInputFilter": wheel, "appController": controller,
                                     "uiFontFamily": "Pretendard"})
        engine.load(ROOT / "assets/qt/MainWindow.qml")
        assert engine.rootObjects(), errors
        win = engine.rootObjects()[0]
        win.setWidth(680)
        win.setHeight(480)
        win.show()

        def settle(ms=20):
            until = time.monotonic() + ms / 1000
            while True:
                app.processEvents()
                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                if time.monotonic() >= until:
                    return
                time.sleep(0.005)

        def wait(pred, msg):
            until = time.monotonic() + 8
            while time.monotonic() < until:
                settle()
                if pred():
                    return
            raise AssertionError(msg)

        def qml(item, expr):
            ev = QQmlExpression(QQmlEngine.contextForObject(item), item, expr)
            val = ev.evaluate()
            assert not ev.hasError(), ev.error().toString()
            return val[0] if isinstance(val, tuple) else val

        def every(name):
            out, stack = [], [win.contentItem()]
            while stack:
                item = stack.pop()
                if item.objectName() == name and item.isVisible():
                    out.append(item)
                stack.extend(item.childItems())
            return out

        wait(lambda: not controller.library.model.busy and controller.library.model.count == 3, "library ready")
        win.setProperty("currentScreen", 1)
        settle(40)

        controller.library.search("very_long_search_query_for_narrow_window_layout")
        controller.library._status = "AI 분류 작업을 진행하는 중입니다. 상태 메시지가 길어져도 진행 표시가 화면 밖으로 밀리지 않습니다."
        controller.library._ai_scanning = True
        controller.library._ai_total = 10
        controller.library._exclusive = "ai"
        controller.library.changed.emit()
        controller.library.aiScanningChanged.emit()
        controller.library.aiScanProgress.emit(3, 10)
        settle(60)
        status_line = win.findChild(QObject, "galleryStatusLine")
        work_progress = win.findChild(QObject, "galleryWorkProgress")
        cancel_ai = win.findChild(QObject, "cancelAiScanButton")
        assert cancel_ai.isVisible() and work_progress.isVisible()
        assert cancel_ai.x() + cancel_ai.width() <= status_line.width() + 1, (
            cancel_ai.x(), cancel_ai.width(), status_line.width())
        controller.library._ai_scanning = False
        controller.library._ai_total = 0
        controller.library._exclusive = ""
        controller.library._status = ""
        controller.library.search("")
        controller.library.aiScanningChanged.emit()
        controller.library.aiScanProgress.emit(0, 0)

        schedule_dialog = win.findChild(QObject, "scheduleDialog")
        assert schedule_dialog is not None
        assert QMetaObject.invokeMethod(schedule_dialog, "open")
        wait(lambda: schedule_dialog.property("visible") and controller.schedule.known, "schedule dialog open")
        panel = schedule_dialog.findChild(QObject, "dailySchedulePanel")
        time_field = schedule_dialog.findChild(QObject, "scheduleTimeField")
        time_help = schedule_dialog.findChild(QObject, "scheduleTimeHelp")
        time_field.setProperty("text", "99:99")
        assert QMetaObject.invokeMethod(time_field, "textEdited")
        settle(40)
        assert time_help.property("visible")
        assert QMetaObject.invokeMethod(schedule_dialog, "close")
        wait(lambda: not schedule_dialog.property("visible"), "schedule dialog close")
        assert QMetaObject.invokeMethod(schedule_dialog, "open")
        wait(lambda: schedule_dialog.property("visible"), "schedule dialog reopen")
        assert not panel.property("timeEdited")
        assert time_field.property("text") == "09:00"
        assert not time_help.property("visible")
        assert QMetaObject.invokeMethod(schedule_dialog, "close")
        wait(lambda: not schedule_dialog.property("visible"), "schedule dialog finish")

        conn_dialog = win.findChild(QObject, "connectionDialog")
        assert QMetaObject.invokeMethod(conn_dialog, "open")
        wait(lambda: conn_dialog.property("visible"), "connection dialog open")
        conn_scroll = conn_dialog.findChild(QObject, "connectionScroll")
        assert abs(conn_scroll.property("contentWidth") - conn_scroll.property("availableWidth")) <= 1
        assert QMetaObject.invokeMethod(conn_dialog, "close")
        wait(lambda: not conn_dialog.property("visible"), "connection dialog close")

        controller.library.setDuplicateOnly(True)
        wait(lambda: len(every("duplicateTile")) == 3, "duplicate tiles shown")
        dup_tiles = every("duplicateTile")
        assert all(t.property("activeFocusOnTab") for t in dup_tiles)
        dup_tiles[0].forceActiveFocus()
        settle(20)
        QCoreApplication.postEvent(win, QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Space, Qt.KeyboardModifier.NoModifier))
        QCoreApplication.postEvent(win, QKeyEvent(QEvent.Type.KeyRelease, Qt.Key.Key_Space, Qt.KeyboardModifier.NoModifier))
        settle(40)
        assert qml(dup_tiles[0], "chosen") is True
        compares = every("duplicateCompare")
        assert compares and all(c.property("activeFocusOnTab") for c in compares)
        controller.library.setDuplicateOnly(False)

        win.setProperty("currentScreen", 2)
        wait(lambda: len(every("unsavedTile")) == 1, "unsaved tile shown")
        unsaved_tile = every("unsavedTile")[0]
        assert unsaved_tile.property("activeFocusOnTab")
        unsaved_tile.forceActiveFocus()
        settle(20)
        QCoreApplication.postEvent(win, QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Space, Qt.KeyboardModifier.NoModifier))
        QCoreApplication.postEvent(win, QKeyEvent(QEvent.Type.KeyRelease, Qt.Key.Key_Space, Qt.KeyboardModifier.NoModifier))
        settle(40)
        assert qml(unsaved_tile, "picked") is True

        controller.shutdownFinished.connect(app.quit)
        QTimer.singleShot(0, controller.requestClose)
        app.exec()
        wheel.close()
        controller._library.provider = None
        delete(engine)
        assert not errors, errors


@unittest.skipUnless(_QT, "Qt dependencies are not installed")
class QmlUiRegressionTests(unittest.TestCase):
    def test_qml_ui_fixes_in_real_main_window(self):
        with tempfile.TemporaryDirectory(prefix="luvbox-qml-1109-") as folder:
            env = {
                **os.environ,
                "LOCALAPPDATA": folder,
                "QML_DISK_CACHE_PATH": str(Path(folder) / "qmlcache"),
                "QT_QPA_PLATFORM": "offscreen",
            }
            result = subprocess.run(
                [sys.executable, str(Path(__file__).resolve()), "--qml-scene"],
                cwd=ROOT, env=env, capture_output=True, text=True, timeout=45,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)


if __name__ == "__main__":
    if "--qml-scene" in sys.argv:
        _run_qml_scene()
    else:
        unittest.main()

"""Regression coverage for the 1.13.0 stabilization audit."""

from contextlib import closing
from hashlib import sha256
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests import app_data_isolation  # noqa: F401
from luvbox import ai_profile, deletion, file_safety, local_deletion, local_store, recovery, run_history, storage
from luvbox import browser_bridge, download_service, login, metadata, thumbcache, unsaved, updater
from luvbox.clip_model import ACTIVE_MODEL_ID, embedding_bytes
from luvbox.history import HistoryStore, _normalize_tag
from luvbox.local_store import LocalFolderError, LocalFolderStore
from luvbox.qt_local_folder import LocalFolderRepository
from luvbox.recycling import file_signature


class LocalStoreAuditTests(unittest.TestCase):
    def setUp(self):
        workspace = tempfile.TemporaryDirectory(prefix="luvbox-1130-")
        self.addCleanup(workspace.cleanup)
        self.root = Path(workspace.name).resolve()
        self.folder = self.root / "사진 보관"
        self.folder.mkdir()
        self.catalog = self.root / "folder.sqlite3"

    @staticmethod
    def add_rows(store, rows):
        with store._write() as db:
            db.executemany(
                "INSERT INTO local_files(rel_path, filename, media_type, filesize, mtime_ns, "
                "cluster_id, updated_at) VALUES (?, ?, 'photo', ?, ?, ?, 'now')", rows)

    def test_paged_reading_matches_one_read_for_every_order(self):
        rows = [(f"photo-{i:03d}.jpg", f"photo-{i:03d}.jpg", 100, 1, 1) for i in range(250)]
        rows[99] = ("A/photo-099X.jpg", "photo-099X.jpg", 100, 1, 1)
        rows[100] = ("B/photo-099x.jpg", "photo-099x.jpg", 100, 1, 1)
        rows[247] = ("사진-한글.jpg", "사진-한글.jpg", 100, 1, 1)
        rows[248] = ("사진-🐈.jpg", "사진-🐈.jpg", 100, 1, 1)

        for order in ("name", "size", "modified", "duplicates"):
            for change in (False, True):
                with self.subTest(order=order, change_between_pages=change):
                    catalog = self.root / f"{order}-{change}.sqlite3"
                    with LocalFolderStore(self.folder, path=catalog) as store:
                        self.add_rows(store, rows)
                        with store._write() as db:
                            db.execute("UPDATE local_files SET favorite=1 WHERE rel_path='photo-120.jpg'")
                            db.execute("UPDATE local_files SET filesize=110, mtime_ns=2 WHERE rel_path='photo-121.jpg'")
                        grouped = order == "duplicates"
                        read = store.duplicate_cursor if grouped else lambda **kw: store.record_cursor(order=order, **kw)
                        page = read(limit=100)
                        self.assertEqual(len(page["rows"]), 100)
                        self.assertTrue(page["has_more"])
                        seen = [row["rel_path"] for row in page["rows"]]
                        if change:
                            removed = seen.pop(0)
                            store.mark_gone([removed])
                            self.add_rows(store, [("최신-🦊.jpg", "최신-🦊.jpg", 90, 0, 2),
                                                  ("최신-🐈.jpg", "최신-🐈.jpg", 90, 0, 2)])
                        while page["has_more"]:
                            after = LocalFolderRepository._page_cursor(page["rows"][-1], order, grouped)
                            page = read(limit=100, after=after, count_total=False)
                            seen.extend(row["rel_path"] for row in page["rows"])
                            self.assertLessEqual(len(seen), len(rows) + 2)
                        expected = (store.duplicate_page(0, 1000)[1] if grouped
                                    else store.records(0, 1000, order=order)[1])
                        self.assertEqual(seen, [row["rel_path"] for row in expected])

    def test_a_store_without_the_cluster_index_still_lists_duplicates(self):
        with LocalFolderStore(self.folder, path=self.catalog) as store:
            self.add_rows(store, [("a.jpg", "a.jpg", 100, 1, 1), ("b.jpg", "b.jpg", 100, 1, 1)])

        for old_schema in (True, False):
            with self.subTest(old_schema=old_schema):
                with closing(sqlite3.connect(self.catalog)) as db, db:
                    db.execute("DROP INDEX IF EXISTS local_files_cluster")
                    if old_schema:
                        db.execute("PRAGMA user_version=1")
                with LocalFolderStore(self.folder, path=self.catalog) as store:
                    try:
                        total, rows = store.duplicate_page()
                    except LocalFolderError as error:
                        self.fail(f"Duplicates could not be read after reopening the catalog: {error}")
                    self.assertEqual((total, [row["rel_path"] for row in rows]), (2, ["a.jpg", "b.jpg"]))


class HistoryStorageAuditTests(unittest.TestCase):
    def setUp(self):
        workspace = tempfile.TemporaryDirectory(prefix="luvbox-1130-history-")
        self.addCleanup(workspace.cleanup)
        self.root = Path(workspace.name).resolve()
        environment = patch.dict(os.environ, {"LOCALAPPDATA": str(self.root / "appdata")})
        environment.start()
        self.addCleanup(environment.stop)
        self.paths = storage.resolve_storage(self.root / "사진 보관", "offline")

    def record_photo(self, store, tweet, layouts=("flat",)):
        path = self.paths.media_dir / f"offline_{tweet}_1.jpg"
        path.write_bytes(b"photo content")
        media = {"tweet_id": tweet, "num": 1, "type": "photo", "author": {"name": "offline"}}
        for layout in layouts:
            store.record_media(layout, media, path)
        return path

    def test_lost_count_counts_files_not_rows(self):
        with closing(HistoryStore.for_storage(self.paths)) as store:
            path = self.record_photo(store, "101", ("author", "flat"))
            self.record_photo(store, "102", ("author", "flat"))
            self.assertEqual(store._db.execute("SELECT COUNT(*) FROM media").fetchone()[0], 4)
            self.assertEqual(store.lost_count(), 0)
            self.assertFalse(store.has_embeddings())
            self.assertFalse(store.ai_cleared())
            vector = bytes(embedding_bytes(ACTIVE_MODEL_ID))
            store.save_embeddings([(layout, "102", 1, "photo", vector, 0.9)
                                   for layout in ("author", "flat")])
            self.assertTrue(store.has_embeddings())
            self.assertEqual(store.count_embeddings(), 1)
            store.clear_ai_tags()
            self.assertTrue(store.ai_cleared())
            path.unlink()
            self.assertEqual(store.lost_count(), 1)
            self.assertEqual(store.lost_media()["total"], 1)

    def test_legacy_run_at_the_same_folder_and_instant_belongs_to_its_own_account(self):
        with closing(HistoryStore.for_storage(self.paths)) as store:
            run_id = store.start_run("flat")
            started = store.runs(limit=1)[0]["started_at"]
        ledger = [{"run_id": "other", "history_run_id": run_id, "account": "another",
                   "output_dir": str(self.paths.output_dir), "started_at": started}]
        with patch.object(run_history, "outcome_page", return_value={"rows": ledger}):
            result = run_history.run_page(self.paths)
        self.assertEqual({row["account"] for row in result["rows"]}, {"another", "offline"})
        self.assertEqual(result["total"], 2)

    def test_a_size_limited_item_with_unknown_size_remains_retryable(self):
        unsaved.record([{"tweet_id": "101", "num": 1, "media_type": "video",
                         "reason": unsaved.SIZE, "limit_mb": 10}],
                       account=self.paths.account, output_dir=self.paths.output_dir)
        self.assertEqual(unsaved.retryable_count(self.paths.account, self.paths.output_dir, 10), 0)
        self.assertEqual(unsaved.retryable_count(self.paths.account, self.paths.output_dir, 20), 1)

    def test_the_last_pending_thumbnail_is_written_at_close(self):
        from PIL import Image

        key = ("101", 1, "photo")
        recorder = thumbcache.Recorder(self.paths.account)
        self.addCleanup(recorder.close)
        with patch.object(thumbcache.Cache, "put_many", side_effect=sqlite3.OperationalError("cache busy")):
            recorder._enqueue(key, Image.new("RGB", (20, 20)))
        self.assertEqual(recorder.failed, 1)
        recorder.close()
        self.assertEqual(recorder.failed, 0)
        with closing(thumbcache.Cache(self.paths.account, create=False)) as cache:
            self.assertIsNotNone(cache.get(key))

    def test_a_snapshot_replace_failure_preserves_the_previous_file(self):
        with closing(HistoryStore.for_storage(self.paths)) as store:
            self.record_photo(store, "101")
            self.assertEqual(storage.publish_snapshot(self.paths), 1)
            snapshot = self.paths.snapshot_dir / store.path.name
            before = snapshot.read_bytes()
            self.record_photo(store, "102")
        with patch.object(storage.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                storage.publish_snapshot(self.paths)
        self.assertEqual(snapshot.read_bytes(), before)
        self.assertEqual(list(snapshot.parent.glob(".snapshot-*.sqlite3")), [])

    def copy_catalog_with_pending_checkpoint(self):
        record_dir = self.root / "records-to-repair"
        record_dir.mkdir()
        with closing(HistoryStore.for_storage(self.paths)) as store:
            self.record_photo(store, "101")
            store._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self.record_photo(store, "102")
            catalog_parts = [store.path.with_name(store.path.name + suffix) for suffix in ("", "-wal", "-shm")]
            for part in catalog_parts:
                shutil.copy2(part, record_dir / part.name)
        return record_dir, [record_dir / part.name for part in catalog_parts]

    def test_failed_catalog_replacement_preserves_committed_uncheckpointed_rows(self):
        record_dir, catalog_parts = self.copy_catalog_with_pending_checkpoint()
        original = {part: part.read_bytes() for part in catalog_parts}
        replace = recovery.replace_file

        def fail_catalog_swap(source, target):
            if target == record_dir / recovery.HISTORY_NAME:
                raise OSError("disk full")
            replace(source, target)

        with patch.object(recovery, "replace_file", side_effect=fail_catalog_swap):
            with self.assertRaisesRegex(OSError, "disk full"):
                recovery.repair_history(record_dir, self.paths.account)
        for part, content in original.items():
            with self.subTest(name=part.name):
                self.assertTrue(part.exists(), f"Original catalog part was lost: {part.name}")
                self.assertEqual(part.read_bytes(), content)

    def test_failed_checkpoint_during_repair_does_not_drop_committed_rows(self):
        record_dir, _ = self.copy_catalog_with_pending_checkpoint()
        connect = sqlite3.connect

        def fail_writable_open(path, *args, **kwargs):
            if isinstance(path, Path) and path.parent.name == "work" and path.name == recovery.HISTORY_NAME:
                raise sqlite3.OperationalError("database or disk is full")
            return connect(path, *args, **kwargs)

        with patch.object(recovery.sqlite3, "connect", side_effect=fail_writable_open):
            repair = recovery.repair_history(record_dir, self.paths.account)
        self.assertEqual(repair["counts"]["media"], 2)


class FileSafetyAuditTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="luvbox-1130-file-safety-")
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()

    def test_file_safety_matches_the_old_checks_at_every_call_site(self):
        photo = self.root / "사진.jpg"
        photo.write_bytes(b"media fixture")
        cases = [(photo, False, True), (self.root / "missing.jpg", False, False)]
        if os.name == "nt":
            import _winapi

            destination = self.root / "ordinary"
            destination.mkdir()
            junction = self.root / "junction"
            _winapi.CreateJunction(str(destination), str(junction))
            self.addCleanup(junction.rmdir)
            cases.append((junction, True, False))
        checks = (HistoryStore._linked, storage._linked, recovery._linked, ai_profile._linked, local_store._linked)
        for path, linked, regular in cases:
            with self.subTest(path=path.name):
                self.assertEqual([check(path) for check in checks], [linked] * len(checks))
                self.assertEqual(file_safety.regular_file_stat(path) is not None, regular)

        cloud_file = SimpleNamespace(st_mode=stat.S_IFREG, st_reparse_tag=0x9000001A)
        with patch.object(Path, "lstat", return_value=cloud_file):
            self.assertEqual([check(photo) for check in checks], [False] * len(checks))
            self.assertIs(file_safety.regular_file_stat(photo), cloud_file)
        link_file = SimpleNamespace(st_mode=stat.S_IFREG, st_reparse_tag=file_safety.NAME_SURROGATE)
        with patch.object(Path, "lstat", return_value=link_file):
            self.assertEqual([check(photo) for check in checks], [True] * len(checks))
            self.assertIsNone(file_safety.regular_file_stat(photo))

    def test_a_symbolic_link_never_counts_as_a_regular_file(self):
        photo = self.root / "a.jpg"
        photo.touch()
        link = self.root / "alias.jpg"
        try:
            link.symlink_to(photo)
        except OSError as error:
            if os.name == "nt" and getattr(error, "winerror", None) == 1314:
                self.skipTest("creating symbolic links needs Windows permission")
            raise
        self.assertTrue(file_safety.is_link_stat(link.lstat()))
        self.assertTrue(file_safety.is_reparse_or_link(link))
        self.assertIsNone(file_safety.regular_file_stat(link))

    def test_tags_stored_by_1_12_13_keep_their_identity(self):
        examples = {"  ## Ａbc  ": "Ａbc", " #AbC ": "AbC", "あ#猫🐈 ": "あ#猫🐈",
                    "ｶﾀｶﾅ": "ｶﾀｶﾅ", "#Ａ\x1fｂ\x7f": "Ａｂ"}
        normalizers = (_normalize_tag, ai_profile.normalize_tag, local_store.normalize_tag)
        path = self.root / "tags.sqlite3"
        with LocalFolderStore(self.root, path=path) as store:
            LocalStoreAuditTests.add_rows(store, [("a.jpg", "a.jpg", 100, 1, 0)])
            with store._write() as db:
                db.executemany("INSERT INTO local_tags VALUES ('a.jpg', ?, 'user', 'now')",
                               ((stored,) for stored in examples.values()))
            for entered, stored in examples.items():
                with self.subTest(tag=stored):
                    self.assertEqual([normalizer(entered) for normalizer in normalizers], [stored] * 3)
                    self.assertEqual(store.records(user_tag=entered)[0], 1)

    def test_a_gallery_file_offloaded_after_selection_is_not_recycled(self):
        with patch.dict(os.environ, {"LOCALAPPDATA": str(self.root / "appdata")}):
            paths = storage.resolve_storage(self.root / "photo-library", "offline")
            with closing(HistoryStore.for_storage(paths)) as store:
                photo = paths.media_dir / "offline_101_1.jpg"
                photo.write_bytes(b"media fixture")
                store.record_media("flat", {"tweet_id": "101", "num": 1, "type": "photo"}, photo)
                row = store.records()[0]
            row["_delete_signature"] = file_signature(photo)
            with (patch.object(deletion, "cloud_only", side_effect=[False, True]),
                  patch.object(deletion, "recycle_file", return_value="mock recycle") as recycler):
                result = deletion.run_deletion(paths, [row], False, threading.Event(), lambda _event: None)
            recycler.assert_not_called()
            self.assertEqual(result["recycled"], 0)
            self.assertEqual(result["failed"], 1)
            self.assertTrue(photo.is_file())

    def test_a_local_file_offloaded_before_recycling_is_left_in_place(self):
        with patch.dict(os.environ, {"LOCALAPPDATA": str(self.root / "appdata")}):
            photo = self.root / "a.jpg"
            photo.write_bytes(b"media fixture")
            row = {"path": str(photo), "rel_path": photo.name, "_delete_signature": file_signature(photo)}
            with (patch.object(local_deletion, "cloud_only", side_effect=[False, True]),
                  patch.object(local_deletion, "recycle_file", return_value="mock recycle") as recycler):
                result = local_deletion.run_local_deletion(self.root, [row], threading.Event(), lambda _event: None)
            recycler.assert_not_called()
            self.assertEqual(result["recycled"], 0)
            self.assertEqual(result["failed"], 1)
            self.assertTrue(photo.is_file())

    def test_a_keeper_that_disappears_during_comparison_is_no_proof(self):
        candidate = self.root / "copy.jpg"
        keeper = self.root / "keeper.jpg"
        for path in (candidate, keeper):
            path.write_bytes(b"same media")
        with LocalFolderStore(self.root, path=self.root / "folder.sqlite3") as store:
            compare = deletion._same_content

            def compare_then_remove(first, second):
                matched = compare(first, second)
                second.unlink()
                return matched

            with patch.object(deletion, "_same_content", side_effect=compare_then_remove):
                self.assertEqual(deletion.kept_copy(store, str(candidate), file_signature(candidate), [str(keeper)]), "")
        self.assertTrue(candidate.is_file())

    def test_a_file_with_the_same_size_and_different_bytes_is_no_copy(self):
        candidate = self.root / "one.jpg"
        candidate.write_bytes(b"picture A")
        keeper = self.root / "two.jpg"
        keeper.write_bytes(b"picture B")
        with LocalFolderStore(self.root, path=self.root / "folder.sqlite3") as store:
            self.assertEqual(deletion.kept_copy(store, str(candidate), file_signature(candidate), [str(keeper)]), "")


class UpdateAuditTests(unittest.TestCase):
    def test_a_locked_program_survives_cancel_quit_and_giving_up(self):
        from tests.release_server import make_program

        old = make_program("1.12.13", b"a" * (1024 * 1024))
        new = make_program("1.13.0", b"b" * (1024 * 1024))
        replacement = os.replace
        for action in ("cancel", "quit", "give up"):
            with self.subTest(action=action), tempfile.TemporaryDirectory(prefix="luvbox-1130-update-") as folder:
                launcher = Path(folder) / "LuvBox.exe"
                downloaded = Path(folder) / "downloaded.exe"
                launcher.write_bytes(old)
                downloaded.write_bytes(new)
                cancelled = threading.Event()
                shutdown = threading.Event()
                launch_attempts = []

                def replace_locked_launcher(source, target):
                    if source == launcher:
                        launch_attempts.append(target)
                        if len(launch_attempts) == 1 or action == "give up":
                            error = PermissionError(32, "sharing violation")
                            error.winerror = 32
                            raise error
                    replacement(source, target)

                def on_retry(_delay):
                    if action == "cancel":
                        cancelled.set()
                    elif action == "quit":
                        shutdown.set()

                with (patch.object(updater.os, "replace", side_effect=replace_locked_launcher),
                      patch.object(updater.time, "sleep", side_effect=on_retry),
                      self.assertRaises(updater.UpdateError) as captured):
                    updater.install(downloaded, launcher, sha256(new).hexdigest(),
                                    cancelled=lambda: cancelled.is_set() or shutdown.is_set())
                self.assertEqual(captured.exception.kind, "replace" if action == "give up" else "cancelled")
                self.assertEqual(sha256(launcher.read_bytes()).digest(), sha256(old).digest())
                self.assertEqual(set(launcher.parent.iterdir()), {launcher, downloaded})


class SaveAndConnectionAuditTests(unittest.TestCase):
    def test_each_retry_job_uses_only_its_own_accounts_session(self):
        from luvbox.core import JobOptions, run_worker
        from tests.test_core import FakeHttpDownloader
        from tests.test_missing_files import FakeTweetExtractor

        accounts = ("first_user", "second_user")
        sessions = []

        class OfflineExtractor(FakeTweetExtractor):
            def items(self):
                sessions.append((self.session, self.cookies.get("auth_token", domain=".x.com")))
                return super().items()

        class OfflineDownloader(FakeHttpDownloader):
            downloaded = []
            failure = False

        def cookies(account):
            return [{"name": "auth_token", "value": f"synthetic-{account}", "domain": ".x.com", "path": "/",
                     "expires": -1, "secure": True, "httpOnly": True}]

        tweets = {"101": [(1, "photo")], "102": [(1, "photo")]}
        with tempfile.TemporaryDirectory(prefix="luvbox-1130-accounts-") as folder:
            with (patch("gallery_dl.extractor.find", side_effect=lambda url: OfflineExtractor(url, tweets)),
                  patch("gallery_dl.downloader.find", return_value=OfflineDownloader),
                  patch.object(FakeTweetExtractor, "requested", []),
                  patch.object(login, "load_session_cookies", side_effect=cookies) as load,
                  patch.object(metadata, "available", return_value=False)):
                for account in accounts:
                    events = []
                    options = JobOptions(account, str(Path(folder) / account), targets=("101_1", "102_1"))
                    self.assertEqual(run_worker(options, events.append), 0)
                    self.assertEqual(events[-1]["saved"], 2)
        self.assertEqual([entry.kwargs["account"] for entry in load.call_args_list], list(accounts))
        self.assertEqual([cookie for _, cookie in sessions], [f"synthetic-{account}" for account in accounts for _ in range(2)])
        self.assertIs(sessions[0][0], sessions[1][0])
        self.assertIs(sessions[2][0], sessions[3][0])
        self.assertIsNot(sessions[0][0], sessions[2][0])
        self.assertFalse(sessions[0][0].cookies)
        self.assertFalse(sessions[2][0].cookies)

    def test_metadata_deadline_preserves_the_small_file_budget_and_caps_the_extension(self):
        with tempfile.TemporaryDirectory(prefix="luvbox-1130-metadata-") as folder:
            path = Path(folder) / "clip.mp4"
            path.write_bytes(b"fixture")
            original = path.stat()
            for size_mb, extra in ((1, 0), (25, 1), (250, 10), (2000, 60)):
                with self.subTest(size_mb=size_mb):
                    status = SimpleNamespace(st_mode=original.st_mode, st_size=size_mb * 1024 * 1024,
                                             st_mtime_ns=original.st_mtime_ns, st_file_attributes=0)
                    with (patch.object(metadata, "available", return_value=True),
                          patch.object(Path, "stat", return_value=status),
                          patch.object(metadata.time, "monotonic", return_value=100),
                          patch.object(metadata, "_retry_original_io", side_effect=metadata._MediaChanged("stop")) as retry):
                        metadata.embed_source(path, "101")
                    self.assertEqual(retry.call_args.args[3], 100 + metadata.TIMEOUT_SECONDS + extra)

    def test_duplicate_and_late_save_responses_are_ignored(self):
        service = download_service.DownloadService()
        service._running = True
        service._emit(service.job_id, {"type": "review-request", "id": "first"})
        self.assertTrue(service.resolve_review("first", "save", enabled=True))
        self.assertFalse(service.resolve_review("first", "never"))
        service._emit(service.job_id, {"type": "finished"})
        self.assertFalse(service.resolve_review("first", "skip"))
        with patch.object(service._control, "respond") as respond:
            service._update_control()
            service._update_control()
        respond.assert_called_once_with("first", "save", enabled=True)

    def test_profile_normalization_accepts_korean_and_emoji_within_two_hundred_codepoints(self):
        name = "한" * 199 + "🐈"
        payload = {"version": 1, "auth_token": "synthetic-bridge-token", "username": "offline",
                   "user_id": "123456789", "display_name": name}
        identity = {"user_id": "123456789", "handle": "offline", "display_name": name}
        self.assertEqual(browser_bridge._session(payload).identity, identity)
        self.assertEqual(login.normalize_identity(identity), identity)
        with self.assertRaises(ValueError):
            browser_bridge._session(dict(payload, display_name="\ud800"))
        with self.assertRaises(ValueError):
            login.normalize_identity(dict(identity, display_name="\ud800"))


class ControllerAuditTests(unittest.TestCase):
    def test_thumbnail_failure_is_reset_and_late_sizes_are_ignored(self):
        from PySide6.QtCore import QObject, Signal
        from luvbox.qt_gallery import GalleryModel

        class ThumbnailSignals(QObject):
            readyForSize = Signal(str, int, int)
            failedForSize = Signal(str, int, int)

        provider = ThumbnailSignals()
        model = GalleryModel(lambda *_: (0, []), provider=provider, lazy=True)
        self.addCleanup(model.shutdown)
        model._accept((0, 0, 2, [{"key": "present", "missing": False}, {"key": "absent", "missing": True}], ""))
        item = model.index(0)
        missing, ready = model.ThumbMissingRole, model.ThumbReadyRole
        model.focusViewport(0, 1, 0, 20, 16)
        provider.failedForSize.emit("present", 20, 16)
        self.assertTrue(model.data(item, missing))

        model.focusViewport(0, 1, 0, 40, 32)
        self.assertFalse(model.data(item, missing))
        self.assertTrue(model.data(model.index(1), missing))
        model.focusViewport(0, 1, 0, 60, 48)
        provider.failedForSize.emit("present", 40, 32)
        provider.readyForSize.emit("present", 20, 16)
        self.assertFalse(model.data(item, missing))
        self.assertFalse(model.data(item, ready))
        provider.readyForSize.emit("present", 60, 48)
        self.assertTrue(model.data(item, ready))

    def test_removal_during_page_loading_keeps_the_list_and_retry_count_in_sync(self):
        from PySide6.QtWidgets import QApplication
        from luvbox.qt_records import RecordsController

        app = QApplication.instance() or QApplication([])

        def wait_until(predicate):
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                app.processEvents()
                if predicate():
                    return
                time.sleep(0.005)
            self.fail("The records operation did not finish")

        with tempfile.TemporaryDirectory(prefix="luvbox-1130-records-") as folder:
            output = Path(folder)
            unsaved.record([{"tweet_id": tweet, "num": 1, "media_type": "photo", "reason": unsaved.FAILED}
                            for tweet in ("101", "102")], account="offline", output_dir=output)
            records = RecordsController()
            retries = []
            records.retryRequested.connect(retries.append)
            try:
                records.setLibrary("offline", str(output), False)
                records.setActive("unsaved", True)
                wait_until(lambda: not records.unsavedBusy and records.unsavedTotal == 2)
                token = next(token for token, row in records._unsaved_tokens.items() if row["tweet_id"] == "101")
                entered, resume = threading.Event(), threading.Event()
                page = unsaved.page

                def pause_first_read(*args, **kwargs):
                    result = page(*args, **kwargs)
                    if not entered.is_set():
                        entered.set()
                        if not resume.wait(5):
                            raise RuntimeError("Held read did not resume")
                    return result

                with patch.object(unsaved, "page", side_effect=pause_first_read):
                    records.loadUnsaved(0)
                    try:
                        self.assertTrue(entered.wait(3))
                        serial = records._unsaved_serial
                        records.setActive("unsaved", True)
                        self.assertEqual(records._unsaved_serial, serial)
                        records.dismiss(token)
                        self.assertTrue(records.unsavedMutating)
                    finally:
                        resume.set()
                    wait_until(lambda: not records.unsavedBusy and not records.unsavedMutating
                               and records.unsavedTotal == 1)
                self.assertEqual(len(records.unsavedRows), 1)
                self.assertEqual(records.retryableCount, 1)
                records.retryAll()
                wait_until(lambda: bool(retries))
                self.assertEqual([tuple(result) for result in retries], [("102_1",)])
            finally:
                records.shutdown()
                wait_until(lambda: records._shutdown_complete)


def _qml_contract_scene(case):
    from PySide6.QtCore import QCoreApplication, QMetaObject, QObject, Qt, QUrl
    from PySide6.QtGui import QInputMethodEvent
    from PySide6.QtQml import QQmlApplicationEngine
    from PySide6.QtQuickControls2 import QQuickStyle
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    from shiboken6 import delete

    QQuickStyle.setStyle("Basic")
    app = QApplication([])
    engine = QQmlApplicationEngine()
    qml = Path(__file__).resolve().parents[1] / "assets" / "qt" / "StabilizationFixture.qml"
    fixtures = {
        "menu": '''
            FlatMenu {
                objectName: "menu"
                height: 180
                MENU_ROWS
            }
        '''.replace("MENU_ROWS", "\n".join(f'FlatMenuItem {{ text: "Action {i}" }}' for i in range(20))),
        "ime": 'FlatField { objectName: "field"; width: 240; focus: true }',
        "details": '''
            property string filePath: "first.jpg"
            property bool pending: true
            SavedDetails {
                objectName: "details"
                width: 450
                app: ({download: {currentSavedItem: {
                    filename: window.filePath, path: window.filePath,
                    body: "A long saved post. ".repeat(100), preview_loading: window.pending
                }}})
            }
        ''',
        "schedule": '''
            property bool busy: true
            property bool connected: true
            property string folder: "photos"
            SchedulePanel {
                app: ({canStart: !window.busy && window.connected && !!window.folder,
                    busy: window.busy, account: "offline", outputFolder: window.folder,
                    accounts: {connected: window.connected, identity: {handle: "offline"}}})
            }
        ''',
    }
    source = 'import QtQuick\nimport QtQuick.Controls\nApplicationWindow { id: window; width: 680; height: 480; visible: true\n'
    engine.loadData((source + fixtures[case] + "\n}").encode("utf-8"), QUrl.fromLocalFile(str(qml)))
    assert engine.rootObjects(), "fixture window failed to load"
    window = engine.rootObjects()[0]

    def wait_for(predicate):
        for _ in range(200):
            app.processEvents()
            if predicate():
                return
            QTest.qWait(5)
        raise AssertionError(f"{case} did not settle")

    try:
        window.requestActivate()
        wait_for(window.isActive)
        if case == "menu":
            menu = window.findChild(QObject, "menu")
            assert QMetaObject.invokeMethod(menu, "open")
            wait_for(lambda: menu.property("opened"))
            for _ in range(12):
                QTest.keyClick(window, Qt.Key.Key_Down)
            content = menu.property("contentItem")
            assert menu.property("currentIndex") >= 10
            assert content.property("currentIndex") == menu.property("currentIndex"), "menu list lost keyboard position"
            wait_for(lambda: content.property("contentY") > 0)
        elif case == "ime":
            field = window.findChild(QObject, "field")
            field.forceActiveFocus()
            QCoreApplication.sendEvent(field, QInputMethodEvent("한", []))
            assert field.property("preeditText") == "한"
            assert field.property("text") == ""
            assert QMetaObject.invokeMethod(field, "clearText")
            assert field.property("preeditText") == "", "clear left an uncommitted Korean composition"
        elif case == "details":
            details = window.findChild(QObject, "details")
            flickable = next(child for child in details.findChildren(QObject)
                             if child.metaObject().indexOfProperty("contentY") >= 0)
            wait_for(lambda: flickable.property("contentHeight") > flickable.property("height") + 100)
            flickable.setProperty("contentY", 100)
            window.setProperty("pending", False)
            app.processEvents()
            assert flickable.property("contentY") == 100, "thumbnail refresh moved the post to the top"
            window.setProperty("filePath", "second.jpg")
            wait_for(lambda: flickable.property("contentY") == 0)
        elif case == "schedule":
            hint = window.findChild(QObject, "schedulePrerequisiteHint")
            assert not hint.property("visible"), "connected account was told to connect during a save"
            window.setProperty("busy", False)
            window.setProperty("folder", "")
            wait_for(lambda: hint.property("visible"))
            assert "저장할 폴더" in hint.property("text")
            window.setProperty("connected", False)
            wait_for(lambda: "X 연결" in hint.property("text"))
    finally:
        window.close()
        delete(engine)
        app.processEvents()


class QmlContractAuditTests(unittest.TestCase):
    def check_scene(self, case):
        env = dict(os.environ, QT_QPA_PLATFORM="offscreen", QT_QUICK_BACKEND="software", PYTHONIOENCODING="utf-8")
        result = subprocess.run([sys.executable, "-X", "faulthandler", str(Path(__file__).resolve()), "--qml-contract", case],
                                env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_tall_menu_follows_the_keyboard_selection(self):
        self.check_scene("menu")

    def test_clearing_a_field_discards_uncommitted_korean_text(self):
        self.check_scene("ime")

    def test_saved_post_scroll_survives_thumbnail_updates(self):
        self.check_scene("details")

    def test_schedule_distinguishes_busy_from_missing_connection_or_folder(self):
        self.check_scene("schedule")


if __name__ == "__main__":
    if "--qml-contract" in sys.argv:
        _qml_contract_scene(sys.argv[-1])
    else:
        unittest.main()

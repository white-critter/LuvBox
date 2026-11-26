"""Regression tests for LuvBox 1.12.11 improvements and fixes."""

from contextlib import ExitStack, closing
from datetime import datetime, timedelta, timezone
from importlib.util import find_spec
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
try:
    from tests import app_data_isolation  # noqa: F401  (never this PC's real app data)
except ModuleNotFoundError:
    import app_data_isolation  # noqa: F401


def _qml_scene():
    from PySide6.QtCore import (
        QCoreApplication,
        QEvent,
        QMetaObject,
        QObject,
        QPointF,
        Qt,
        QtMsgType,
        QUrl,
        qInstallMessageHandler,
    )
    from PySide6.QtGui import QFont, QFontDatabase, QGuiApplication
    from PySide6.QtQml import QQmlApplicationEngine
    from PySide6.QtQuick import QQuickItem
    from PySide6.QtQuickControls2 import QQuickStyle
    from PySide6.QtTest import QTest
    from PIL import Image
    from shiboken6 import delete

    from luvbox import thumbcache
    from luvbox.history import HistoryStore
    from luvbox.qt_app import ApplicationController
    from luvbox.qt_wheel import WheelInputFilter
    from luvbox.storage import resolve_storage

    QQuickStyle.setStyle("Basic")
    app = QGuiApplication([])
    QFontDatabase.addApplicationFont(str(ROOT / "assets/fonts/Pretendard/Pretendard-Regular.ttf"))
    app.setFont(QFont("Pretendard"))
    messages = []
    previous = qInstallMessageHandler(
        lambda kind, _ctx, text: messages.append(text)
        if kind in (QtMsgType.QtWarningMsg, QtMsgType.QtCriticalMsg, QtMsgType.QtFatalMsg)
        else None
    )
    controller = wheel = engine = window = None
    stack = ExitStack()

    def settle(duration=0.05):
        deadline = __import__("time").monotonic() + duration
        while __import__("time").monotonic() < deadline:
            app.processEvents()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            __import__("time").sleep(0.002)

    def wait(predicate, label, timeout=8):
        deadline = __import__("time").monotonic() + timeout
        while __import__("time").monotonic() < deadline:
            settle(0.01)
            if predicate():
                return
        raise AssertionError(label)

    def find(name):
        item = window.findChild(QObject, name)
        if item is not None:
            return item
        stack_nodes = [window.property("contentItem")]
        for root_name in ("galleryDatePickerPopup", "localFolderPreview", "previewDialog"):
            dlg = window.findChild(QObject, root_name)
            if dlg is not None:
                stack_nodes.append(dlg.property("contentItem"))
        while stack_nodes:
            node = stack_nodes.pop()
            if node is None:
                continue
            if node.objectName() == name:
                return node
            if hasattr(node, "childItems"):
                stack_nodes.extend(node.childItems())
        raise AssertionError(name)

    try:
        with stack:
            folder = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="luvbox-11211-qml-")))
            stack.enter_context(patch.dict(os.environ, {
                "LOCALAPPDATA": str(folder),
                "QML_DISK_CACHE_PATH": str(folder / "qml-cache"),
            }))
            stack.enter_context(patch("luvbox.qt_schedule.task_status", return_value={"registered": False}))
            paths = resolve_storage(folder / "photos", "tester")
            paths.media_dir.mkdir(parents=True, exist_ok=True)
            p1 = paths.media_dir / "tester_101_1.jpg"
            p2 = paths.media_dir / "tester_102_1.jpg"
            Image.new("RGB", (320, 240), (180, 60, 80)).save(p1, "JPEG")
            Image.new("RGB", (320, 240), (60, 120, 180)).save(p2, "JPEG")
            stamp = datetime(2026, 11, 20, 12, tzinfo=timezone(timedelta(hours=9)))
            with closing(HistoryStore.for_storage(paths)) as store:
                store.record_media("flat", {"tweet_id": "101", "num": 1, "type": "photo",
                                            "author": {"name": "alpha"}, "date": stamp}, p1)
                store.record_media("flat", {"tweet_id": "102", "num": 1, "type": "photo",
                                            "author": {"name": "beta"}, "date": stamp}, p2)
                store.record_declined("flat", {"tweet_id": "901", "num": 1, "type": "photo",
                                               "author": {"name": "beta"}},
                                      paths.media_dir / "beta_901_1.jpg")
                store.add_tag("flat", "101", 1, "photo", "테스트")
            cache = thumbcache.Cache("tester")
            try:
                cache.put(("901", 1, "photo"), Image.new("RGB", (160, 120), (40, 80, 120)))
            finally:
                cache.close()

            local_folder = folder / "local_photos"
            local_folder.mkdir()
            l1 = local_folder / "sample_local.jpg"
            Image.new("RGB", (320, 240), (90, 140, 200)).save(l1, "JPEG")

            (folder / "LuvBox").mkdir(exist_ok=True)
            (folder / "LuvBox/settings.json").write_text(
                json.dumps({"account": "tester", "output_dir": str(paths.output_dir),
                            "gallery_density": 1, "last_local_folder": str(local_folder)}),
                encoding="utf-8",
            )

            controller = ApplicationController()
            wheel = WheelInputFilter()
            engine = QQmlApplicationEngine()
            engine.addImageProvider("thumbs", controller.library.provider)
            engine.addImageProvider("unsavedpreview", controller.records.preview_provider())
            engine.setInitialProperties({
                "appController": controller,
                "galleryModel": controller.library.model,
                "wheelInputFilter": wheel,
            })
            engine.load(ROOT / "assets/qt/MainWindow.qml")
            assert engine.rootObjects(), messages
            window = engine.rootObjects()[0]
            window.setWidth(1080)
            window.setHeight(760)
            window.requestActivate()
            wait(lambda: not controller.busy, "app ready")

            # 1) datePickerPopup roving Tab: exactly one valid day cell accepts TabFocus.
            window.setProperty("currentScreen", 1)
            wait(lambda: not controller.library.model.busy and controller.library.model.count == 2, "gallery ready")
            QMetaObject.invokeMethod(find("galleryFilterToggle"), "click")
            wait(lambda: find("galleryDateFrom").property("visible"), "date filter open")
            QMetaObject.invokeMethod(find("galleryDateFrom"), "pickRequested")
            picker = find("galleryDatePickerPopup")
            wait(lambda: picker.property("visible"), "datePicker open")
            day_cells = []
            stack_items = [picker.property("contentItem")]
            while stack_items:
                cur = stack_items.pop()
                if cur is not None:
                    if cur.objectName().startswith("datePickerDay_"):
                        day_cells.append(cur)
                    stack_items.extend(cur.childItems())
            tab_days = [c.objectName() for c in day_cells if c.property("activeFocusOnTab")]
            assert len(tab_days) == 1, (tab_days, len(day_cells))
            QTest.keyClick(window, Qt.Key.Key_Escape)
            wait(lambda: not picker.property("visible"), "datePicker closed")

            # 2) tagBatchPopup is a FlatDialog.
            tag_popup = find("tagBatchPopup")
            meta = tag_popup.metaObject()
            chain = []
            while meta is not None:
                chain.append(meta.className())
                meta = meta.superClass()
            assert any(name.startswith("FlatDialog") for name in chain), chain

            # 3) DuplicateReview active-filter empty state when duplicates exist elsewhere.
            controller.library._duplicate_only = True
            controller.library._duplicate_count = 4
            controller.library._distinct_count = 1
            controller.library.duplicateFilterChanged.emit()
            controller.library.duplicateCountChanged.emit()
            controller.library.duplicateDistinctChanged.emit()
            controller.library.changed.emit()
            empty = find("duplicateEmptyState")
            wait(lambda: empty.property("visible"), "duplicate empty state visible")
            assert "지금 선택한 조건에 맞는 중복 사진이 없습니다." in empty.property("text"), empty.property("text")
            controller.library._duplicate_only = False
            controller.library._duplicate_count = 0
            controller.library._distinct_count = 0
            controller.library.duplicateFilterChanged.emit()
            controller.library.duplicateCountChanged.emit()
            controller.library.duplicateDistinctChanged.emit()
            controller.library.changed.emit()
            settle(0.05)

            # 4) UnsavedReview: confirmAll resets on Esc before leaving screen, unsavedMutating locks buttons.
            QMetaObject.invokeMethod(find("galleryUnsavedButton"), "click")
            review = find("unsavedReview")
            wait(lambda: window.property("currentScreen") == 2 and not controller.records.unsavedBusy, "unsaved ready")
            QMetaObject.invokeMethod(find("unsavedSettle"), "click")
            wait(lambda: review.property("confirmAll"), "confirmAll active")
            QTest.keyClick(window, Qt.Key.Key_Escape)
            wait(lambda: not review.property("confirmAll"), "Esc cleared confirmAll")
            assert window.property("currentScreen") == 2
            controller.records._unsaved_mutating = True
            controller.records.changed.emit()
            settle(0.05)
            assert not find("unsavedSettle").property("enabled")
            assert not find("unsavedRetry").property("enabled")
            controller.records._unsaved_mutating = False
            controller.records.changed.emit()
            QTest.keyClick(window, Qt.Key.Key_Escape)
            wait(lambda: window.property("currentScreen") == 1, "Esc returned to gallery")

            # 5) LocalFolderView: PreviewTagChip keyboard button & 'm' mute shortcut in localFolderPreview.
            window.setProperty("currentScreen", 3)
            controller.localFolder.openFolder(str(local_folder))
            wait(lambda: controller.localFolder.hasFolder and not controller.localFolder.folderModel.busy
                 and controller.localFolder.folderModel.count >= 1, "local folder loaded")
            local_key = controller.localFolder.folderModel._rows[0]["key"]
            controller.localFolder.selectItem(local_key)
            wait(lambda: controller.localFolder.selectedKey == local_key, "local item selected")
            controller.localFolder.addSelectedTag("로컬태그")
            wait(lambda: any(row.get("tag") == "로컬태그" for row in controller.localFolder.selectedTags), "local tag added")
            local_preview = find("localFolderPreview")
            QMetaObject.invokeMethod(local_preview, "open")
            wait(lambda: local_preview.property("visible"), "local preview open")
            settle(0.08)
            tag_btn = find("localFolderTagName")
            assert tag_btn.property("focusPolicy") == Qt.FocusPolicy.TabFocus
            shortcuts = [
                seq.toString() if hasattr(seq, "toString") else str(seq)
                for child in local_preview.findChildren(QObject)
                if (seq := child.property("sequence")) is not None
            ]
            assert "m" in shortcuts and "Shift+M" in shortcuts, shortcuts
            QTest.keyClick(window, Qt.Key.Key_Escape)
            wait(lambda: not local_preview.property("visible"), "local preview closed")

            # 6) ScheduleDialog initial focus & ConnectionDialog Escape cancellation during connecting.
            schedule_dlg = find("scheduleDialog")
            QMetaObject.invokeMethod(schedule_dlg, "open")
            wait(lambda: schedule_dlg.property("visible"), "scheduleDialog open")
            settle(0.05)
            focused = window.activeFocusItem()
            assert focused is not None and focused.property("text") == "닫기", focused
            QTest.keyClick(window, Qt.Key.Key_Escape)
            wait(lambda: not schedule_dlg.property("visible"), "scheduleDialog closed")

            conn_dlg = find("connectionDialog")
            QMetaObject.invokeMethod(conn_dlg, "open")
            wait(lambda: conn_dlg.property("visible"), "connectionDialog open")
            controller.accounts._operation = "browser"
            controller.accounts._busy = True
            controller.accounts._cancel.clear()
            controller.accounts.changed.emit()
            wait(lambda: conn_dlg.property("connectingActive"), "connectionDialog connectingActive")
            QTest.keyClick(window, Qt.Key.Key_Escape)
            wait(lambda: controller.accounts.cancelling, "Escape triggered cancelBrowser")
            assert conn_dlg.property("visible"), "Escape while connecting must keep ConnectionDialog open"
            controller.accounts._busy = False
            controller.accounts._operation = ""
            controller.accounts._cancel.clear()
            controller.accounts.changed.emit()
            wait(lambda: not conn_dlg.property("connectingActive"), "connectionDialog idle")
            QTest.keyClick(window, Qt.Key.Key_Escape)
            wait(lambda: not conn_dlg.property("visible"), "connectionDialog closed when idle")

            # Shut down cleanly.
            controller.requestClose()
            wait(lambda: controller.shutdownReady, "shutdown")
            window.close()
            wheel.close()
            delete(engine)
            engine = None
            delete(controller)
            controller = None
            qml_errors = [
                m for m in messages
                if any(term in m for term in (
                    "ReferenceError", "TypeError", "Binding loop", "Cannot assign",
                    "Cannot read", "is not a type", "Unable to assign", "does only support types deriving from Item",
                ))
            ]
            assert not qml_errors, qml_errors
    finally:
        if engine is not None:
            delete(engine)
        qInstallMessageHandler(previous)


class Regression11211BackendTests(unittest.TestCase):
    def test_settings_load_propagates_path_conflict_and_reports_damaged_json(self):
        from luvbox import settings
        from luvbox.settings import AppDataConflict

        with patch("luvbox.settings.settings_path", side_effect=AppDataConflict("conflict")):
            with self.assertRaises(AppDataConflict):
                settings.load_settings_with_status()

        with tempfile.TemporaryDirectory(prefix="luvbox-11211-set-") as folder:
            with patch.dict(os.environ, {"LOCALAPPDATA": folder}):
                target = settings.settings_path()
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("{damaged json", encoding="utf-8")
                loaded, damaged = settings.load_settings_with_status()
                self.assertEqual(loaded, {})
                self.assertTrue(damaged)

    def test_storage_trusted_schema_prepare_table_allowlist_and_partial_publish_state(self):
        from luvbox import storage

        with tempfile.TemporaryDirectory(prefix="luvbox-11211-storage-") as folder:
            root = Path(folder)
            paths = storage.resolve_storage(root / "photos", "acct")
            paths.record_dir.mkdir(parents=True, exist_ok=True)
            paths.media_dir.mkdir(parents=True, exist_ok=True)

            # _prepare_table rejects unallowlisted tables and untrusted DDL defaults.
            with closing(sqlite3.connect(":memory:")) as target, closing(sqlite3.connect(":memory:")) as src:
                src.execute("CREATE TABLE evil_table (id INTEGER)")
                with self.assertRaises(storage.MigrationError):
                    storage._prepare_table(target, src, "evil_table")
                src.execute("CREATE TABLE media (id INTEGER DEFAULT (abs(-1)))")
                with self.assertRaises(storage.MigrationError):
                    storage._prepare_table(target, src, "media")

            # publish_snapshot persists snapshot.json for DBs written before a later DB failure.
            db1 = paths.record_dir / storage._DATABASES[0]
            db2 = paths.record_dir / storage._DATABASES[1]
            for db_path in (db1, db2):
                with closing(sqlite3.connect(db_path)) as db:
                    db.execute("CREATE TABLE t (x INT)")
                    db.commit()
            calls = {"n": 0}
            orig_replace = storage._replace_snapshot

            def fail_second(temporary, checked, name, expected_state):
                calls["n"] += 1
                if calls["n"] == 2:
                    raise storage.MigrationError("simulated failure on second db")
                return orig_replace(temporary, checked, name, expected_state)

            with patch("luvbox.storage._replace_snapshot", side_effect=fail_second):
                with self.assertRaises(storage.MigrationError):
                    storage.publish_snapshot(paths)
            state_file = paths.record_dir / storage._SNAPSHOT_STATE
            self.assertTrue(state_file.is_file())
            saved_state = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertIn(storage._DATABASES[0], saved_state["files"])

    def test_local_store_query_plans_have_no_temp_btree(self):
        from luvbox import ai_profile
        from luvbox.local_store import LocalFolderStore

        with tempfile.TemporaryDirectory(prefix="luvbox-11211-local-") as folder:
            root = Path(folder)
            photos = root / "pics"
            photos.mkdir()
            with closing(LocalFolderStore(photos)) as store:
                blob = b"\x01" * ai_profile.embedding_bytes(ai_profile.ACTIVE_MODEL_ID)
                with store._write() as db:
                    for i in range(10):
                        rel = f"p{i}.jpg"
                        db.execute(
                            "INSERT INTO local_files(rel_path,filename,media_type,filesize,mtime_ns,present,cloud,updated_at) "
                            "VALUES (?,?,'photo',100,1,1,0,'now')",
                            (rel, rel),
                        )
                        db.execute(
                            "INSERT INTO local_ai_embeddings(rel_path,model_id,embedding,confidence,updated_at) "
                            "VALUES (?,?,?,?, 'now')",
                            (rel, ai_profile.ACTIVE_MODEL_ID, blob, 0.9),
                        )
                        db.execute(
                            "INSERT INTO local_tags(rel_path,tag,source,created_at) VALUES (?,?, 'ai', 'now')",
                            (rel, "인물"),
                        )
                sql_counts = (
                    "SELECT t.tag AS tag, COUNT(*) AS n FROM local_tags t CROSS JOIN local_files f "
                    "ON f.rel_path=t.rel_path WHERE t.source IN ('ai','confirmed','user') AND t.tag IN (?) "
                    "AND f.present=1 GROUP BY t.tag"
                )
                plan_counts = " ".join(str(r[-1]) for r in store._db.execute(f"EXPLAIN QUERY PLAN {sql_counts}", ("인물",)))
                self.assertNotIn("TEMP B-TREE", plan_counts.upper())

                sql_emb = (
                    "SELECT f.rowid AS rowid, e.rel_path AS rel_path, e.embedding AS embedding "
                    + store._embedding_sql(False, rowid_order=True)
                    + " AND f.rowid>? ORDER BY f.rowid LIMIT ?"
                )
                plan_emb = " ".join(str(r[-1]) for r in store._db.execute(
                    f"EXPLAIN QUERY PLAN {sql_emb}",
                    (ai_profile.ACTIVE_MODEL_ID, ai_profile.embedding_bytes(ai_profile.ACTIVE_MODEL_ID), 0, 100),
                ))
                self.assertNotIn("TEMP B-TREE", plan_emb.upper())

    def test_history_favorite_identities_index_and_finish_deletion_drop_skip(self):
        from PIL import Image
        from luvbox.history import HistoryStore
        from luvbox.storage import resolve_storage

        with tempfile.TemporaryDirectory(prefix="luvbox-11211-hist-") as folder:
            paths = resolve_storage(Path(folder) / "photos", "acct")
            f1 = paths.media_dir / "a_101_1.jpg"
            f2 = paths.media_dir / "a_102_1.jpg"
            f1.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (20, 20), (10, 20, 30)).save(f1, "JPEG")
            Image.new("RGB", (20, 20), (40, 50, 60)).save(f2, "JPEG")
            with closing(HistoryStore.for_storage(paths)) as store:
                indexes = {row[1] for row in store._db.execute("PRAGMA index_list('media')")}
                self.assertIn("media_path_nocase", indexes)
                store.record_media("flat", {"tweet_id": "101", "num": 1, "type": "photo", "author": {"name": "a"}}, f1)
                store.record_media("author", {"tweet_id": "101", "num": 1, "type": "photo", "author": {"name": "a"}}, f1)
                store.toggle_favorite("flat", "101", 1, "photo")
                favs = store.favorite_identities()
                self.assertIn(("flat", "101", 1, "photo"), favs)
                self.assertIn(("author", "101", 1, "photo"), favs)

                store.record_media("flat", {"tweet_id": "102", "num": 1, "type": "photo", "author": {"name": "a"}}, f2)
                row = {"folder_mode": "flat", "tweet_id": "102", "num": 1, "media_type": "photo", "path": str(f2)}
                ident = store.begin_deletion("batch1", row, False, (1, 1, f2.stat().st_size, 1))
                item = store.pending_deletions()[0]["id"]
                f2.unlink()
                with patch.object(store, "_drop_archive_skips") as drop_mock:
                    store.finish_deletion(item, "recycled", recycle_path="")
                drop_mock.assert_called_once()
                self.assertTrue(ident)

    def test_deletion_size_prefilter_recycling_recovery_media_core_and_records(self):
        from PIL import Image
        from luvbox import core, deletion, media, recovery, recycling
        from luvbox.history import HistoryStore
        from luvbox.qt_records import RecordsController
        from luvbox.storage import resolve_storage

        # core._safe_diagnostic masks raw secrets/paths.
        masked = core._safe_diagnostic(r"RuntimeError at C:\Users\홍길동\secret\file.db token=xyz")
        self.assertNotIn("홍길동", masked)
        self.assertNotIn("RuntimeError", masked)

        # media.convert_mp4_to_gif rejects truncated <11 byte GIF output.
        with tempfile.TemporaryDirectory(prefix="luvbox-11211-media-") as mdir:
            src = Path(mdir) / "clip.mp4"
            src.write_bytes(b"\x00" * 32)
            def fake_run(*_a, output=None, **_k):
                if output is not None:
                    output.write_bytes(b"GIF89a\x01\x00\x01\x00")  # 10 bytes < 11
                return b""
            with patch("luvbox.media._run", side_effect=fake_run), patch("luvbox.media.ffmpeg_path", return_value=Path("ffmpeg.exe")):
                with self.assertRaises(media.ConversionError):
                    media.convert_mp4_to_gif(src)

        with tempfile.TemporaryDirectory(prefix="luvbox-11211-misc-") as folder:
            paths = resolve_storage(Path(folder) / "photos", "acct")
            f_main = paths.media_dir / "a_1_1.jpg"
            f_diff_size = paths.media_dir / "a_2_1.jpg"
            f_same = paths.media_dir / "a_3_1.jpg"
            f_main.parent.mkdir(parents=True, exist_ok=True)
            f_main.write_bytes(b"A" * 100)
            f_diff_size.write_bytes(b"B" * 200)
            f_same.write_bytes(b"A" * 100)

            with closing(HistoryStore.for_storage(paths)) as store:
                sig = deletion.file_signature(f_main)
                calls = []
                with patch("luvbox.deletion._same_content", side_effect=lambda a, b: calls.append((a, b)) or a.read_bytes() == b.read_bytes()):
                    res = deletion.kept_copy(store, str(f_main), sig, [str(f_diff_size), str(f_same)])
                self.assertEqual(res, str(f_same))
                self.assertEqual(len(calls), 1)

            # recycling.verify_receipt returns False cleanly for non-$RECYCLE.BIN path.
            self.assertFalse(recycling.verify_receipt(f_main, Path(folder) / "not_recycle.jpg", deletion.file_signature(f_main)))

            # Recovery stops if Windows will not move a -wal sidecar for replacement.
            with patch.dict(os.environ, {"LOCALAPPDATA": folder}):
                rec_paths = resolve_storage(Path(folder) / "photos2", "acct")
                rec_paths.record_dir.mkdir(parents=True, exist_ok=True)
                hist_file = rec_paths.record_dir / recovery.HISTORY_NAME
                wal_file = rec_paths.record_dir / (recovery.HISTORY_NAME + "-wal")
                with closing(sqlite3.connect(hist_file)) as db:
                    db.execute("CREATE TABLE IF NOT EXISTS catalog_meta (key TEXT PRIMARY KEY, value TEXT)")
                    db.commit()
                wal_file.write_bytes(b"wal")
                original_rename = Path.rename
                def deny_wal(self_path, target):
                    if self_path.name.endswith("-wal"):
                        raise PermissionError(32, "sharing violation")
                    return original_rename(self_path, target)
                with patch.object(Path, "rename", deny_wal):
                    with self.assertRaises(recovery.RecoveryError):
                        recovery.repair_history(rec_paths.record_dir, "acct")
                self.assertEqual(wal_file.read_bytes(), b"wal")

            # qt_records.shutdown is idempotent.
            ctrl = RecordsController()
            ctrl.shutdown()
            ctrl.shutdown()
            deadline = __import__("time").monotonic() + 3
            while not ctrl._shutdown_complete and __import__("time").monotonic() < deadline:
                ctrl._poll_shutdown()
                __import__("time").sleep(0.01)
            self.assertTrue(ctrl._shutdown_complete)


@unittest.skipUnless(find_spec("PySide6") is not None, "Qt dependencies are not installed")
class Regression11211QmlTests(unittest.TestCase):
    def test_11211_qml_fixes_in_offscreen_window(self):
        env = dict(os.environ, QT_QPA_PLATFORM="offscreen", QT_QUICK_BACKEND="software", PYTHONIOENCODING="utf-8")
        res = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scene"],
                             cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
                             timeout=120)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)


if __name__ == "__main__":
    if "--scene" in sys.argv:
        _qml_scene()
    else:
        unittest.main()

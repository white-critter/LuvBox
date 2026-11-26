"""Regression tests for LuvBox 1.12.14 stability, performance, and UI/UX fixes."""

from contextlib import ExitStack, closing
from datetime import datetime, timedelta, timezone
from importlib.util import find_spec
import json
import os
from pathlib import Path
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
        QPoint,
        QPointF,
        Qt,
        QtMsgType,
        qInstallMessageHandler,
    )
    from PySide6.QtGui import QFont, QFontDatabase
    from PySide6.QtQml import QQmlApplicationEngine
    from PySide6.QtQuickControls2 import QQuickStyle
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    from PIL import Image
    from shiboken6 import delete

    from luvbox.history import HistoryStore
    from luvbox.qt_app import ApplicationController
    from luvbox.qt_updater import UpdateController
    from luvbox.qt_wheel import WheelInputFilter
    from luvbox.storage import resolve_storage
    from tests.release_server import ReleaseServer, make_program

    QQuickStyle.setStyle("Basic")
    app = QApplication([])
    for filename in ("Pretendard-Regular.ttf", "Pretendard-Bold.ttf"):
        QFontDatabase.addApplicationFont(str(ROOT / "assets/fonts/Pretendard" / filename))
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
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            app.processEvents()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            time.sleep(0.002)

    def wait(predicate, label, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            settle(0.01)
            if predicate():
                return
        raise AssertionError(label)

    def find(name):
        item = window.findChild(QObject, name)
        if item is not None:
            return item
        stack_nodes = [window.property("contentItem")]
        for root_name in ("galleryDatePickerPopup", "localFolderPreview", "previewDialog", "scheduleDialog", "tagBatchPopup"):
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
        raise AssertionError(f"Missing control: {name}")

    def every(name):
        found = []
        pending = [window.contentItem()]
        while pending:
            item = pending.pop()
            if item.objectName() == name and item.isVisible():
                found.append(item)
            pending.extend(item.childItems())
        return sorted(found, key=lambda it: (it.mapToScene(QPointF(0, 0)).y(), it.mapToScene(QPointF(0, 0)).x()))

    try:
        with stack:
            server = ReleaseServer()
            stack.callback(server.close)
            folder = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="luvbox-11214-qml-")))
            launcher = folder / "place" / "LuvBox.exe"
            launcher.parent.mkdir(parents=True, exist_ok=True)
            launcher.write_bytes(make_program("1.8.0"))
            stack.enter_context(patch.dict(os.environ, {
                "LOCALAPPDATA": str(folder),
                "QML_DISK_CACHE_PATH": str(folder / "qml-cache"),
            }))
            stack.enter_context(patch("luvbox.qt_schedule.task_status", return_value={"registered": False}))
            stack.enter_context(patch(
                "luvbox.qt_app.UpdateController",
                side_effect=lambda parent, **opts: UpdateController(
                    parent, current="1.8.0", source=server.source, launcher=lambda: launcher,
                    folder=folder / "updates", **opts,
                ),
            ))
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

            (folder / "LuvBox").mkdir(exist_ok=True)
            (folder / "LuvBox/settings.json").write_text(
                json.dumps({"account": "tester", "output_dir": str(paths.output_dir), "gallery_density": 1}),
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

            # 1) ScheduleDialog / SchedulePanel prerequisite hint & popupParent binding.
            schedule_dlg = find("scheduleDialog")
            QMetaObject.invokeMethod(schedule_dlg, "open")
            wait(lambda: schedule_dlg.property("visible"), "scheduleDialog open")
            hint = find("schedulePrerequisiteHint")
            time_field = find("scheduleTimeField")
            assert hint.property("visible") is True, "hint should show when account not connected"
            assert time_field.property("enabled") is False, "timeField must be disabled when !canStart"
            confirmation = find("scheduleConfirmation")
            assert confirmation.property("parent") == schedule_dlg.property("parent")
            QTest.keyClick(window, Qt.Key.Key_Escape)
            wait(lambda: not schedule_dlg.property("visible"), "scheduleDialog closed")

            # 2) UpdateNotice keeps horizontal RowLayout buttons aligned in available & failed states.
            from luvbox import updater
            server.publish("1.9.0", make_program("1.9.0", b"P" * 4096), notes="### 주요 변경 사항\n\n- 개선\n")
            controller.updates.checkNow()
            wait(lambda: controller.updates.state == "available", "update available")
            settle(0.06)
            install_btn = find("updateInstallButton")
            later_btn = find("updateLaterButton")
            assert abs(install_btn.mapToScene(QPointF(0, 0)).y() - later_btn.mapToScene(QPointF(0, 0)).y()) <= 2
            del server.files["/download/v1.9.0/SHA256-1.9.0.txt"]
            with patch.object(updater, "check", side_effect=updater.UpdateError("limited")):
                pt = install_btn.mapToScene(QPointF(install_btn.width() / 2, install_btn.height() / 2)).toPoint()
                QTest.mouseClick(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, pt)
                wait(lambda: controller.updates.state == "failed", "update failed")
            settle(0.06)
            release_btn = find("updateReleasePageButton")
            assert abs(install_btn.mapToScene(QPointF(0, 0)).y() - release_btn.mapToScene(QPointF(0, 0)).y()) <= 2
            controller.updates.dismiss()
            wait(lambda: not find("updateNotice").property("visible"), "notice dismissed")

            # 3) Gallery click + GridBandSelection drag + FlatField.clearText + BatchTagDialog.
            window.setProperty("currentScreen", 1)
            wait(lambda: not controller.library.model.busy and controller.library.model.count == 2, "gallery loaded")
            tiles = every("galleryTile")
            assert len(tiles) == 2
            t0_mid = tiles[0].mapToScene(QPointF(tiles[0].width() / 2, tiles[0].height() / 2)).toPoint()
            QTest.mouseClick(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ControlModifier, t0_mid)
            wait(lambda: controller.library.selectedCount == 1, "ctrl click picked tile")
            controller.library.clearSelection()
            wait(lambda: controller.library.selectedCount == 0, "selection cleared")

            t1_mid = tiles[1].mapToScene(QPointF(tiles[1].width() / 2, tiles[1].height() / 2)).toPoint()
            QTest.mousePress(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, t0_mid)
            for step in range(1, 6):
                QTest.mouseMove(window, QPoint(
                    t0_mid.x() + (t1_mid.x() - t0_mid.x()) * step // 5,
                    t0_mid.y() + (t1_mid.y() - t0_mid.y()) * step // 5,
                ))
                settle(0.015)
            wait(lambda: find("galleryBand").property("visible"), "gallery band active")
            wait(lambda: controller.library.selectedCount == 2, "band selected 2 tiles")
            QTest.mouseRelease(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, t1_mid)
            settle(0.05)

            tag_popup = find("tagBatchPopup")
            meta = tag_popup.metaObject()
            chain = []
            while meta is not None:
                chain.append(meta.className())
                meta = meta.superClass()
            assert any(name.startswith("FlatDialog") for name in chain), chain
            QMetaObject.invokeMethod(tag_popup, "open")
            wait(lambda: tag_popup.property("visible"), "tagBatchPopup open")
            tag_input = find("batchTagInput")
            tag_input.setProperty("text", "#샘플태그")
            assert tag_input.property("cleanTag") == "샘플태그"
            QMetaObject.invokeMethod(tag_input, "clearText")
            assert tag_input.property("text") == ""
            tag_input.setProperty("text", "#샘플태그")
            QMetaObject.invokeMethod(tag_popup, "submitTag")
            wait(lambda: not tag_popup.property("visible"), "tag submitted")
            assert "추가했습니다" in controller.library.status
            QMetaObject.invokeMethod(tag_popup, "open")
            wait(lambda: tag_popup.property("visible"), "tag dialog opens for the same selection")
            tag_input.setProperty("text", "#다른태그")
            with patch.object(HistoryStore, "add_tags_batch", side_effect=OSError("fixture write failed")):
                QMetaObject.invokeMethod(tag_popup, "submitTag")
            assert tag_popup.property("visible") and tag_popup.property("notice")
            assert "추가했습니다" not in tag_popup.property("notice"), "a failed write showed the prior success notice"
            QTest.keyClick(window, Qt.Key.Key_Escape)
            wait(lambda: not tag_popup.property("visible"), "tagBatchPopup closed")

            search_box = find("searchBox")
            search_box.setProperty("text", "alpha")
            QMetaObject.invokeMethod(search_box, "clearText")
            assert search_box.property("text") == ""

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
                    "Cannot read", "is not a type", "Unable to assign",
                ))
            ]
            assert not qml_errors, qml_errors
            print(json.dumps({"ok": True, "qml_errors": qml_errors}))
    finally:
        if engine is not None:
            delete(engine)
        qInstallMessageHandler(previous)


class Regression11214BackendTests(unittest.TestCase):
    def test_file_safety_helpers(self):
        from luvbox import file_safety

        self.assertEqual(file_safety.normalize_tag(" ##Cat\x00\x1f "), "Cat")
        self.assertEqual(file_safety.normalize_tag(None), "")
        self.assertFalse(file_safety.is_link_stat(None))
        self.assertTrue(file_safety.is_link_stat(SimpleNamespace(st_mode=stat.S_IFREG | 0o644,
                                                                 st_reparse_tag=file_safety.NAME_SURROGATE)))
        self.assertFalse(file_safety.is_link_stat(SimpleNamespace(st_mode=stat.S_IFREG | 0o644, st_reparse_tag=0)))
        with tempfile.TemporaryDirectory(prefix="luvbox-11214-fs-") as folder:
            root = Path(folder)
            f = root / "sample.jpg"
            f.write_bytes(b"abc")
            self.assertIsNotNone(file_safety.regular_file_stat(f))
            self.assertIsNone(file_safety.regular_file_stat(root))
            self.assertIsNone(file_safety.regular_file_stat(root / "missing.jpg"))

    def test_history_and_local_store_fixes(self):
        from luvbox.clip_model import ACTIVE_MODEL_ID, embedding_bytes
        from luvbox.history import HistoryStore
        from luvbox.local_store import LocalFolderStore
        from luvbox.storage import resolve_storage

        with tempfile.TemporaryDirectory(prefix="luvbox-11214-stores-") as folder:
            with patch.dict(os.environ, {"LOCALAPPDATA": folder}):
                root = Path(folder)
                paths = resolve_storage(root / "photos", "acct")
                paths.media_dir.mkdir(parents=True, exist_ok=True)
                img = paths.media_dir / "alice_201_1.png"
                img.write_bytes(b"img-bytes")
                with closing(HistoryStore.for_storage(paths)) as store:
                    indexes = {r[0] for r in store._db.execute(
                        "SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
                    self.assertIn("ai_embeddings_model_len", indexes)
                    self.assertIn("tweets_hashtags_nonempty", indexes)
                    self.assertFalse(store.has_embeddings())
                    store.record_media("flat", {"tweet_id": "201", "num": 1, "type": "photo",
                                                "author": {"name": "alice"}}, img)
                    vec = b"\x00" * embedding_bytes(ACTIVE_MODEL_ID)
                    self.assertEqual(store.save_embeddings([("flat", "201", 1, "photo", vec, 0.9)]), 1)
                    self.assertTrue(store.has_embeddings())
                    self.assertEqual(store.lost_count(), 0)
                    img.unlink()
                    self.assertEqual(store.lost_count(), 1)
                    self.assertEqual(store.authors(), [{"author": "alice", "count": 1}])
                    self.assertEqual(store.ranked_duplicate_groups([]), ({}, set()))
                    self.assertFalse(store.ai_cleared())
                    store.clear_ai_tags()
                    self.assertTrue(store.ai_cleared())

                local_dir = root / "local"
                local_dir.mkdir()
                lf1 = local_dir / "a.jpg"
                lf2 = local_dir / "b.jpg"
                lf1.write_bytes(b"same-bytes")
                lf2.write_bytes(b"same-bytes")
                with closing(LocalFolderStore(local_dir)) as lstore:
                    self.assertFalse(lstore.has_embeddings())
                    v_before = lstore._scan_version()
                    lstore.sync_folder(include_subfolders=False)
                    self.assertNotEqual(lstore._scan_version(), v_before)
                    lstore.save_clusters([["a.jpg", "b.jpg"]], "exact")
                    # Simulate cloud offload of a.jpg (cloud 0 -> 1): clears cluster_id and prunes singleton.
                    with lstore._write() as db:
                        old_meta = lstore._meta_with(db, "folder_scan_seq", "0")
                        lstore._apply_folder_scan(
                            db,
                            {
                                "a.jpg": (10, lf1.stat().st_mtime_ns, True, "photo", "a.jpg"),
                                "b.jpg": (10, lf2.stat().st_mtime_ns, False, "photo", "b.jpg"),
                            },
                            False, None,
                        )
                        new_meta = lstore._meta_with(db, "folder_scan_seq", "0")
                        self.assertNotEqual(old_meta, new_meta)
                    row_a = lstore._read("SELECT cloud, cluster_id FROM local_files WHERE rel_path='a.jpg'")[0]
                    self.assertEqual((row_a["cloud"], row_a["cluster_id"]), (1, 0))
                    self.assertEqual(lstore.record_cursor(limit=10)["total"], 2)
                    self.assertEqual(lstore.duplicate_cursor(limit=10)["total"], 0)

    def test_metadata_deadline_unsaved_run_history_download_service_and_updater(self):
        from luvbox import download_service, metadata, run_history, unsaved, updater
        from luvbox.history import HistoryStore
        from luvbox.storage import resolve_storage
        from luvbox.user_errors import UserValueError

        # 1) metadata large-file deadline scaling
        with tempfile.TemporaryDirectory(prefix="luvbox-11214-backend-") as folder:
            with patch.dict(os.environ, {"LOCALAPPDATA": folder}):
                root = Path(folder)
                media_file = root / "clip.mp4"
                media_file.write_bytes(b"0" * 64)
                real_stat = media_file.stat()
                huge_stat = SimpleNamespace(
                    st_mode=real_stat.st_mode,
                    st_size=250 * 1024 * 1024,  # +10s over TIMEOUT_SECONDS
                    st_mtime_ns=real_stat.st_mtime_ns,
                    st_file_attributes=0,
                )
                seen_deadlines = []

                def fake_retry(_target, _orig, _fn, deadline, _cancelled):
                    seen_deadlines.append(deadline)
                    raise metadata._MediaChanged("stop")

                with (patch("luvbox.metadata.available", return_value=True),
                      patch("luvbox.metadata.Path.stat", return_value=huge_stat),
                      patch("luvbox.metadata._retry_original_io", side_effect=fake_retry)):
                    start = time.monotonic()
                    metadata.embed_source(media_file, "1234567890")
                self.assertTrue(seen_deadlines)
                self.assertGreaterEqual(seen_deadlines[0] - start, metadata.TIMEOUT_SECONDS + 9.0)

                # 2) unsaved remove(kinds=[]) == 0, counted_attempt=False, size_bytes bound
                out_dir = root / "photos"
                out_dir.mkdir(parents=True, exist_ok=True)
                unsaved_items = [
                    {"tweet_id": "10", "num": 1, "media_type": "photo", "reason": unsaved.SIZE,
                     "size_bytes": 50 * 1024 * 1024, "limit_mb": 10},
                    {"tweet_id": "11", "num": 1, "media_type": "photo", "reason": unsaved.SIZE,
                     "size_bytes": 12 * 1024 * 1024, "limit_mb": 10},
                ]
                unsaved.record(unsaved_items, account="acct", output_dir=out_dir)
                self.assertEqual(unsaved.remove("acct", out_dir, kinds=[]), 0)
                self.assertEqual(unsaved.retryable_count("acct", out_dir, 20), 1)
                unsaved.record(unsaved_items, account="acct", output_dir=out_dir, counted_attempt=False)
                rows = unsaved.page("acct", out_dir, kinds=(unsaved.SIZE,))["rows"]
                self.assertTrue(all(r["attempts"] == 1 for r in rows))

                # 3) run_history deduplicates across media_roots and started_at
                paths = resolve_storage(out_dir, "acct")
                with closing(HistoryStore.for_storage(paths)) as store:
                    run_id = store.start_run("flat")
                    store.finish_run(run_id, "finished", saved=1)
                    store_rows = store.runs(limit=10)
                    started_at = store_rows[0]["started_at"]
                ledger = [{"run_id": "99", "account": "ACCT", "history_run_id": run_id,
                           "output_dir": str(out_dir), "started_at": started_at}]
                legacy, unreadable = run_history._legacy_runs(paths, ledger)
                self.assertEqual((legacy, unreadable), ([], False))

                # 4) download_service _ProtocolError & atomic _update_control
                self.assertTrue(issubclass(download_service._ProtocolError, UserValueError))
                svc = download_service.DownloadService()
                calls = []
                svc._control = SimpleNamespace(
                    respond=lambda ident, action, enabled=None: calls.append(("respond", ident, action, enabled)),
                    set_enabled=lambda enabled: calls.append(("set_enabled", enabled)),
                )
                svc._review_response = ("r1", "save", True)
                svc._review_enabled = False
                svc._review_dirty = True
                svc._update_control()
                self.assertEqual(calls, [("respond", "r1", "save", False)])

                # 5) updater _replace_loop retry/cancel + 2-arg _replace_with_retry
                src, dst = root / "new.exe", root / "old.exe"
                src.write_bytes(b"new")
                attempts = []

                def flaky_replace(a, b):
                    attempts.append((a, b))
                    if len(attempts) == 1:
                        err = PermissionError(32, "busy")
                        err.winerror = 32
                        raise err

                with patch("luvbox.updater.os.replace", side_effect=flaky_replace), patch("luvbox.updater.time.sleep"):
                    updater._replace_with_retry(src, dst)
                self.assertEqual(len(attempts), 2)
                err32 = PermissionError(32, "busy")
                err32.winerror = 32
                with (patch("luvbox.updater.os.replace", side_effect=err32),
                      patch("luvbox.updater.time.sleep"),
                      self.assertRaises(updater.UpdateError)):
                    updater._replace_loop(src, dst, cancelled=lambda: True)

    def test_extension_parity_and_qt_controller_contracts(self):
        from PySide6.QtCore import QMetaObject
        from luvbox.qt_gallery import SafeThumbnailProvider
        from luvbox.qt_records import RecordsController

        shared = ("background.js", "profile.js", "connect.js", "connect.html", "connect.css", "extension-page.js",
                  "icons/icon-16.png", "icons/icon-32.png", "icons/icon-48.png", "icons/icon-128.png")
        chromium = ROOT / "assets/browser-extension/chromium"
        firefox = ROOT / "assets/browser-extension/firefox"
        for rel in shared:
            self.assertEqual((chromium / rel).read_bytes(), (firefox / rel).read_bytes(), f"parity mismatch: {rel}")
        self.assertIn('Array.from((lines.find(x => x !== "@" + handle', (chromium / "profile.js").read_text("utf-8"))

        provider = SafeThumbnailProvider()
        self.assertGreaterEqual(provider.metaObject().indexOfSignal("readyForSize(QString,int,int)"), 0)
        self.assertGreaterEqual(provider.metaObject().indexOfSignal("failedForSize(QString,int,int)"), 0)
        provider.close()

        ctrl = RecordsController()
        try:
            self.assertFalse(ctrl.unsavedMutating)
            ctrl._retry_busy = True
            self.assertTrue(ctrl.unsavedMutating)
            self.assertGreaterEqual(ctrl.metaObject().indexOfMethod("refreshRuns()"), 0)
        finally:
            ctrl._retry_busy = False
            ctrl.shutdown()
            deadline = time.monotonic() + 3
            while not ctrl._shutdown_complete and time.monotonic() < deadline:
                ctrl._poll_shutdown()
                time.sleep(0.01)


@unittest.skipUnless(find_spec("PySide6") is not None, "Qt dependencies are not installed")
class Regression11214QmlTests(unittest.TestCase):
    def test_11214_qml_fixes_in_offscreen_window(self):
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

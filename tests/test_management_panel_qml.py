"""Exercise the real management tabs with isolated generated catalog data."""

from importlib.util import find_spec
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]


def scene():
    from contextlib import ExitStack, closing
    from datetime import datetime, timedelta, timezone
    import sqlite3
    import tempfile
    import time
    from unittest.mock import patch

    from PIL import Image
    from PySide6.QtCore import QCoreApplication, QEvent, QObject, QPointF, Qt, qInstallMessageHandler
    from PySide6.QtQml import QQmlApplicationEngine
    from PySide6.QtQuick import QQuickItem
    from PySide6.QtQuickControls2 import QQuickStyle
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    from shiboken6 import delete

    sys.path.insert(0, str(ROOT))
    from luvbox.activity import record_outcome
    from luvbox.history import HistoryStore, archive_key
    from luvbox.qt_app import ApplicationController
    from luvbox.qt_wheel import WheelInputFilter
    from luvbox.recycling import file_signature
    from luvbox.storage import resolve_storage

    QQuickStyle.setStyle("Basic")
    app = QApplication([])
    messages = []
    previous = qInstallMessageHandler(lambda _level, _context, message: messages.append(message))
    controller = engine = wheel = window = None
    resources = ExitStack()

    def settle():
        app.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        time.sleep(.003)

    def wait(predicate, label):
        until = time.monotonic() + 12
        while time.monotonic() < until:
            settle()
            if predicate():
                return
        raise AssertionError(label + ": timeout")

    def find(name):
        result = window.findChild(QObject, name)
        if result is None:
            raise AssertionError("Missing product control: " + name)
        return result

    def button(text):
        def visual_items(parent):
            yield parent
            for child in parent.childItems():
                yield from visual_items(child)

        items = list(visual_items(window.contentItem()))
        matching = []
        for item in items:
            if (item.property("text") == text and item.property("visible")
                    and item.metaObject().indexOfSignal("clicked()") >= 0):
                matching.append(item)
        if len(matching) != 1:
            shown = [(str(item.property("text")).encode("unicode_escape").decode(), item.metaObject().className(),
                      item.metaObject().indexOfSignal("clicked()")) for item in items
                     if item.property("visible") and item.property("text")]
            raise AssertionError(f"Expected one visible button {text.encode('unicode_escape')!r}, got {len(matching)}; {shown}")
        return matching[0]

    def visible_texts(parent):
        result = []
        for item in parent.childItems():
            text = item.property("text")
            if item.property("visible") and isinstance(text, str):
                result.append(text)
            result.extend(visible_texts(item))
        return result

    def ignored_identities():
        database = paths.record_dir / ".luvbox-history.sqlite3"
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
            return {row[0] for row in db.execute("SELECT tweet_id FROM missing_choices WHERE ignored=1")}

    def click(item):
        QTest.qWait(40)  # Wait for Qt Quick layout polish before mapping coordinates.
        assert item.property("visible") and item.property("enabled"), item.property("text")
        point = item.mapToScene(QPointF(item.width() / 2, item.height() / 2)).toPoint()
        assert 0 <= point.x() < window.width() and 0 <= point.y() < window.height(), (
            item.property("text"), point.x(), point.y(), window.width(), window.height())
        QTest.mouseClick(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, point)
        settle()

    def click_tab(text, section):
        click(button(text))
        wait(lambda: controller.management.section == section and not controller.management.busy, text)

    def scroll_to_bottom(item):
        ancestor = item.parentItem()
        while ancestor is not None:
            content_height = ancestor.property("contentHeight")
            content_y = ancestor.property("contentY")
            if content_height is not None and content_y is not None:
                ancestor.setProperty("contentY", max(0, content_height - ancestor.height()))
                QTest.qWait(80)
                return
            ancestor = ancestor.parentItem()
        raise AssertionError("Schedule panel needs a scrolling viewport")

    try:
        folder = resources.enter_context(tempfile.TemporaryDirectory(prefix="luvbox-management-qml-"))
        resources.enter_context(patch.dict(os.environ, {"LOCALAPPDATA": folder}))
        resources.enter_context(patch("luvbox.qt_schedule.task_status", return_value={"registered": False}))
        resources.enter_context(patch("luvbox.qt_schedule.register_task", side_effect=AssertionError("real schedule mutation")))
        resources.enter_context(patch("luvbox.download_service.DownloadService.start", side_effect=AssertionError("network must remain unused")))
        base = Path(folder)
        paths = resolve_storage(base / "사진 폴더", "offline")
        files = []
        with closing(HistoryStore.for_storage(paths)) as store:
            for ident, author, day in ((100, "alice", 1), (101, "alice", 2),
                                       (102, "bob", 2), (103, "bob", 3)):
                path = paths.media_dir / f"{author}_{ident}_1.png"
                with Image.new("RGB", (24, 16), (200, 30, 50)) as image:
                    image.save(path)
                store.record_media("flat", {"tweet_id": str(ident), "num": 1, "type": "photo",
                                            "author": {"name": author},
                                            "date": datetime(2026, 10, day, 12, tzinfo=timezone(timedelta(hours=9)))}, path)
                files.append(path)
            row = store.media_record("flat", {"tweet_id": "100", "num": 1, "type": "photo"})
            deletion = store.begin_deletion("qml-fixture", row, False, file_signature(files[0]))
            store.finish_deletion(deletion, "failed", "합성 이력: 파일을 이동하지 않았습니다.")
            store.set_missing_ignored([("102", 1, "photo")], True)
            run_id = store.start_run("flat", scan_mode="full")
            store.finish_run(run_id, "finished", saved=2, skipped=0, message="합성 저장 완료")
        existing_bytes = {path: path.read_bytes() for path in files}
        files[1].unlink()
        files[2].unlink()
        record_outcome({"run_id": "qml-scheduled-fixture", "status": "finished", "saved": 2,
                        "message": "합성 자동 저장 완료", "history_run_id": run_id},
                       origin="scheduled", account="offline", output_dir=str(paths.output_dir),
                       started_at="2026-10-28T09:00:00+09:00")
        preferences = base / "LuvBox/settings.json"
        preferences.write_text(json.dumps({"account": "offline", "output_dir": str(paths.output_dir)}), encoding="utf-8")
        controller = ApplicationController()
        # Synthetic in-memory state makes the create dialog reachable; no
        # real session, Task Scheduler write, or network request is made.
        controller._accounts._connected = True
        controller._accounts._authentication_state = "saved"
        controller._accounts.changed.emit()
        retry_calls = []
        controller.management.retryRequested.disconnect(controller.retryMissing)
        controller.management.retryRequested.connect(lambda keys: retry_calls.append(tuple(keys)))
        wheel = WheelInputFilter()
        engine = QQmlApplicationEngine()
        engine.addImageProvider("thumbs", controller.library.provider)
        engine.setInitialProperties({"appController": controller, "galleryModel": controller.library.model,
                                     "wheelInputFilter": wheel})
        engine.load(ROOT / "assets/qt/MainWindow.qml")
        assert engine.rootObjects(), messages
        window = engine.rootObjects()[0]
        window.setProperty("currentScreen", 2)
        wait(lambda: controller.schedule.known and not controller.schedule.busy, "schedule lookup")

        for width, height in ((680, 480), (1080, 760)):
            window.setWidth(width)
            window.setHeight(height)
            QTest.qWait(100)
            click_tab("자동 저장", "schedule")
            wait(lambda: controller.schedule.known and not controller.schedule.busy, "empty schedule")
            assert "예약이 없습니다" in find("scheduleSummary").property("text")
            schedule_save = find("scheduleSaveButton")
            scroll_to_bottom(schedule_save)
            click(schedule_save)
            confirmation = find("scheduleConfirmation")
            wait(lambda: confirmation.property("visible"), "schedule confirmation")
            QTest.keyClick(window, Qt.Key.Key_Escape)
            wait(lambda: not confirmation.property("visible"), "cancel schedule")

            click_tab("사라진 파일", "missing")
            wait(lambda: controller.management.total == 2 and len(controller.management.rows) == 2, "missing rows")
            assert find("missingModeFilter").width() >= 152
            assert all(row["selectable"] for row in controller.management.rows)
            click(button("현재 목록 선택"))
            assert controller.management.selectedCount == 2
            controller._accounts._connected = False
            controller._accounts.changed.emit()
            wait(lambda: not button("선택 다시 받기").property("enabled"), "retry requires account")
            assert find("retryConnectionHelp").property("visible")
            controller._accounts._connected = True
            controller._accounts.changed.emit()
            wait(lambda: button("선택 다시 받기").property("enabled"), "connected retry")
            click(button("다시 받지 않기"))
            wait(lambda: not controller.management.mutating and not controller.management.busy, "exclude missing")
            assert all("다시 받지 않기" in row["detail"] for row in controller.management.rows)
            assert ignored_identities() == {"101", "102"}
            click(button("현재 목록 선택"))
            click(button("제외 해제"))
            wait(lambda: not controller.management.mutating and not controller.management.busy, "allow missing")
            assert all("파일 없음" in row["detail"] for row in controller.management.rows)
            assert ignored_identities() == set()
            click(button("현재 목록 선택"))
            click(button("선택 다시 받기"))
            wait(lambda: bool(retry_calls), "selected retry")
            assert set(retry_calls.pop()) == {archive_key("101", 1, "photo"), archive_key("102", 1, "photo")}

            click_tab("삭제 이력", "deletions")
            assert controller.management.total == 1
            assert "실패" in controller.management.rows[0]["detail"]
            click_tab("통계", "statistics")
            summary = controller.management.statistics["summary"]
            assert (summary["files"], summary["posts"]) == (2, 2), summary
            wait(lambda: any("현재 파일 2개" in text for text in visible_texts(window.contentItem())), "statistics rendered")
            click_tab("실행 기록", "runs")
            assert controller.management.total == 1  # linked catalog/ledger event appears once
            assert "자동" in controller.management.rows[0]["detail"]
            assert files[0].read_bytes() == existing_bytes[files[0]]
            assert files[3].read_bytes() == existing_bytes[files[3]]

        controller.requestClose()
        wait(lambda: controller.shutdownReady, "shutdown")
        window.close()
        wheel.close()
        delete(engine)
        engine = None
        delete(controller)
        controller = None
        errors = [message for message in messages if any(term in message for term in
                  ("ReferenceError", "TypeError", "Binding loop", "Cannot assign", "Cannot read", "is not a type"))]
        assert not errors, errors
        print(json.dumps({"ok": True, "sizes": ["680x480", "1080x760"], "qml_errors": errors}))
    finally:
        if controller is not None:
            controller.requestClose()
            wait(lambda: controller.shutdownReady, "cleanup")
        if engine is not None:
            delete(engine)
        if wheel is not None:
            wheel.close()
        qInstallMessageHandler(previous)
        resources.close()


@unittest.skipUnless(find_spec("PySide6") is not None, "Qt dependencies are not installed")
class ManagementPanelSceneTests(unittest.TestCase):
    def test_real_management_controls_and_small_window_accessibility(self):
        result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scene"], cwd=ROOT,
                                capture_output=True, text=True, encoding="utf-8", timeout=100,
                                env={**os.environ, "QT_QPA_PLATFORM": "offscreen", "PYTHONIOENCODING": "utf-8"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(json.loads(result.stdout.splitlines()[-1])["ok"])


if __name__ == "__main__":
    if "--scene" in sys.argv:
        scene()
    else:
        unittest.main()

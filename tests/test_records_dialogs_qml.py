"""Exercise settings, automatic save, run history and unsaved media in the real window.

Set LUVBOX_SCENE_SHOTS to a folder to also keep a screenshot of each state.
"""

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
    import tempfile
    import time
    from unittest.mock import patch

    from PIL import Image
    from PySide6.QtCore import QCoreApplication, QEvent, QObject, QPointF, Qt, qInstallMessageHandler
    from PySide6.QtQml import QQmlApplicationEngine
    from PySide6.QtQuickControls2 import QQuickStyle
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    from shiboken6 import delete

    sys.path.insert(0, str(ROOT))
    from luvbox import unsaved
    from luvbox.activity import record_outcome
    from luvbox.history import HistoryStore
    from luvbox.qt_app import ApplicationController
    from luvbox.qt_wheel import WheelInputFilter
    from luvbox.storage import resolve_storage

    QQuickStyle.setStyle("Basic")
    app = QApplication([])
    messages = []
    previous = qInstallMessageHandler(lambda _level, _context, message: messages.append(message))
    controller = engine = wheel = window = None
    resources = ExitStack()
    shots = os.environ.get("LUVBOX_SCENE_SHOTS")

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
        state = (f" [busy={controller.busy} canStart={controller.canStart} error={controller.error!r} "
                 f"status={controller.status!r}]" if controller is not None else "")
        raise AssertionError(label + ": timeout" + state)

    def find(name):
        result = window.findChild(QObject, name)
        if result is None:
            raise AssertionError("Missing product control: " + name)
        return result

    def items(parent):
        yield parent
        for child in parent.childItems():
            yield from items(child)

    def inside(popup):
        return items(popup.property("contentItem"))

    def visible_texts():
        return [str(item.property("text")) for item in items(window.contentItem())
                if item.property("visible") and isinstance(item.property("text"), str)]

    def shot(name):
        if shots:
            QTest.qWait(120)
            Path(shots).mkdir(parents=True, exist_ok=True)
            window.grabWindow().save(str(Path(shots) / f"{name}.png"))

    def click(target):
        QTest.qWait(40)  # Wait for Qt Quick layout polish before mapping coordinates.
        item = find(target) if isinstance(target, str) else target
        assert item.property("visible") and item.property("enabled"), target
        point = item.mapToScene(QPointF(item.width() / 2, item.height() / 2)).toPoint()
        assert 0 <= point.x() < window.width() and 0 <= point.y() < window.height(), (
            target, point.x(), point.y(), window.width(), window.height())
        QTest.mouseClick(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, point)
        settle()

    def click_text(text):
        matching = [item for item in items(window.contentItem())
                    if item.property("text") == text and item.property("visible") and item.property("enabled")
                    and item.metaObject().indexOfSignal("clicked()") >= 0]
        assert len(matching) == 1, (text, len(matching))
        click(matching[0])

    def reveal(item):
        # Scroll the nearest scrolling ancestor until the control sits inside its viewport.
        view = item.parentItem()
        while view is not None and (view.property("contentY") is None or view.property("contentHeight") is None):
            view = view.parentItem()
        if view is None:
            return
        for _ in range(8):
            top = view.mapToScene(QPointF(0, 0)).y()
            middle = item.mapToScene(QPointF(0, item.height() / 2)).y()
            if top + 8 <= middle <= top + view.height() - 8:
                return
            limit = max(0, view.property("contentHeight") - view.height())
            target = view.property("contentY") + (middle - top) - view.height() / 2
            view.setProperty("contentY", max(0, min(limit, target)))
            QTest.qWait(80)
        raise AssertionError("could not scroll the control into view: " + str(item.objectName()))

    try:
        folder = resources.enter_context(tempfile.TemporaryDirectory(prefix="luvbox-records-qml-"))
        resources.enter_context(patch.dict(os.environ, {"LOCALAPPDATA": folder}))
        resources.enter_context(patch("luvbox.qt_schedule.task_status", return_value={"registered": False}))
        resources.enter_context(patch("luvbox.qt_schedule.register_task", side_effect=AssertionError("real schedule mutation")))
        resources.enter_context(patch("luvbox.download_service.DownloadService.start",
                                      side_effect=AssertionError("network must remain unused")))
        base = Path(folder)
        paths = resolve_storage(base / "사진 폴더", "offline")
        files = []
        with closing(HistoryStore.for_storage(paths)) as store:
            for ident, author, day in ((100, "alice", 1), (101, "alice", 2), (102, "bob", 2)):
                path = paths.media_dir / f"{author}_{ident}_1.png"
                Image.new("RGB", (48, 32), (200, 30, 50)).save(path)
                store.record_media("flat", {"tweet_id": str(ident), "num": 1, "type": "photo", "author": {"name": author},
                                            "date": datetime(2026, 10, day, 12, tzinfo=timezone(timedelta(hours=9)))}, path)
                files.append(path)
            run_id = store.start_run("flat", scan_mode="full")
            store.finish_run(run_id, "finished", saved=2, skipped=0, message="이전 저장 완료")
            # A run only the catalog remembers (saved before the ledger existed).
            old_run = store.start_run("flat", scan_mode="full")
            store.finish_run(old_run, "finished", saved=7, skipped=3, message="이전 버전에서 저장")
            with store._db:
                store._db.execute("UPDATE runs SET started_at='2026-10-01T09:00:00+09:00' WHERE id=?", (old_run,))
        original = {path: path.read_bytes() for path in files}
        record_outcome({"run_id": "scheduled-fixture", "status": "error", "message": "X 연결이 만료됐습니다.",
                        "saved": 0, "skipped": 4},
                       origin="scheduled", account="offline", output_dir=str(paths.output_dir),
                       started_at="2026-10-30T09:00:00+09:00")
        record_outcome({"run_id": "manual-fixture", "status": "finished", "saved": 12, "skipped": 340,
                        "size_skipped": 1, "failed_media": 1, "history_run_id": run_id,
                        "message": "X가 보여 주는 좋아요 목록을 모두 확인했습니다. 삭제된 게시물은 빠질 수 있습니다."},
                       origin="manual", account="offline", output_dir=str(paths.output_dir),
                       started_at="2026-10-31T21:05:00+09:00")
        record_outcome({"run_id": "other-account", "status": "finished", "saved": 1}, origin="manual",
                       account="someone_else", output_dir=str(base / "다른 폴더"),
                       started_at="2026-10-29T08:00:00+09:00")
        later = {"posted_at": "2026-10-02T09:30:00+09:00", "author": "carol", "detail": "파일을 받지 못했습니다."}
        fixture_rows = [
            {"tweet_id": "300", "num": 1, "media_type": "photo", "reason": "failed", **later},
            {"tweet_id": "301", "num": 2, "media_type": "video", "reason": "size", "width": 1280, "height": 720,
             "duration": 133, "size_bytes": 340 * 1048576, "limit_mb": 200,
             "detail": "크기 제한(200MB)을 넘어 저장하지 않았습니다.", **{k: v for k, v in later.items() if k != "detail"}},
            {"tweet_id": "302", "num": 1, "media_type": "animated_gif", "reason": "excluded", "author": "dave",
             "detail": "저장 확인에서 ‘저장 안 함’으로 골랐습니다."},
        ]
        unsaved.record(fixture_rows, account="offline", output_dir=str(paths.output_dir))
        (base / "LuvBox/settings.json").write_text(json.dumps({"account": "offline", "output_dir": str(paths.output_dir)}),
                                                    encoding="utf-8")
        controller = ApplicationController()
        controller._accounts._connected = True
        controller._accounts._authentication_state = "saved"
        controller._accounts.changed.emit()
        wheel = WheelInputFilter()
        engine = QQmlApplicationEngine()
        engine.addImageProvider("thumbs", controller.library.provider)
        engine.setInitialProperties({"appController": controller, "galleryModel": controller.library.model,
                                     "wheelInputFilter": wheel})
        engine.load(ROOT / "assets/qt/MainWindow.qml")
        assert engine.rootObjects(), messages
        window = engine.rootObjects()[0]
        wait(lambda: controller.schedule.known and not controller.schedule.busy and controller.records.unsavedCount == 2,
             "schedule lookup and unsaved count")

        started = []
        retry_patch = patch("luvbox.qt_download.DownloadController.start",
                            side_effect=lambda options: started.append(options) or True)
        resources.enter_context(retry_patch)

        for width, height in ((680, 480), (1080, 760)):
            tag = f"{width}x{height}"
            window.setWidth(width)
            window.setHeight(height)
            window.setProperty("currentScreen", 0)
            QTest.qWait(120)

            # There is no management screen or menu any more.
            assert window.findChild(QObject, "managementNavigation") is None
            assert not any(text in ("관리", "삭제 이력", "통계") for text in visible_texts()), visible_texts()
            # The save screen points at media that were not saved, and says what its counters count.
            summary = find("saveUnsavedSummary")
            assert summary.property("visible"), "unsaved summary"
            assert any("저장하지 못한 미디어 2개" in text for text in visible_texts())
            assert "이미 저장됨" in find("saveCounters").property("text")
            shot(f"{tag}-save")

            # Settings → advanced reaches automatic save, run history and unsaved media.
            click_text("설정")
            preferences = find("preferencesDialog")
            wait(lambda: preferences.property("visible"), "settings open")
            toggle = find("advancedSettingsToggle")
            if not toggle.property("checked"):
                reveal(toggle)
                click(toggle)
            for name in ("preferencesScheduleButton", "preferencesRunHistoryButton", "preferencesUnsavedButton"):
                reveal(find(name))
                assert find(name).property("visible"), name
            assert "(2)" in find("preferencesUnsavedButton").property("text")
            assert "예약이 없습니다" in find("preferencesScheduleSummary").property("text")
            shot(f"{tag}-settings-advanced")
            # While a save runs the records stay readable; only the changeable options are locked.
            controller._download._running = True
            controller._download.changed.emit()
            wait(lambda: controller.busy, "a save is running")
            for name in ("preferencesScheduleButton", "preferencesRunHistoryButton", "preferencesUnsavedButton"):
                assert find(name).property("enabled"), name
            assert find("videoLimitField").property("visible") and not find("videoLimitField").property("enabled")
            assert find("advancedSettingsToggle").property("enabled")
            assert find("preferencesBusyNote").property("visible")
            controller._download._running = False
            controller._download.changed.emit()
            wait(lambda: not controller.busy, "the save ended")

            # Run history: every account at once, short rows, no scope switch.
            reveal(find("preferencesRunHistoryButton"))
            click("preferencesRunHistoryButton")
            history = find("runHistoryDialog")
            wait(lambda: history.property("visible") and controller.records.runTotal == 4, "run history")
            rows = controller.records.runRows
            assert [row["account"] for row in rows] == ["@offline", "@offline", "@someone_else", "@offline"], rows
            assert (rows[0]["status"], rows[0]["summary"], rows[0]["message"]) == (
                "완료", "저장 12개 · 이미 저장됨 340개 · 저장 못 함 2개", ""), rows[0]
            assert rows[1]["message"] == "X 연결이 만료됐습니다."
            assert rows[3]["origin"] == "이전 기록"
            assert not any("모든 계정" in text for text in visible_texts())
            shot(f"{tag}-run-history")
            QTest.keyClick(window, Qt.Key.Key_Escape)
            wait(lambda: not history.property("visible"), "history closed")
            assert preferences.property("visible")

            # Unsaved media: reasons, details, and the user's own exclusions kept apart.
            reveal(find("preferencesUnsavedButton"))
            click("preferencesUnsavedButton")
            dialog = find("unsavedDialog")
            wait(lambda: dialog.property("visible") and controller.records.unsavedTotal == 2
                 and not controller.records.unsavedBusy, "unsaved list")
            reasons = {row["reasonLabel"] for row in controller.records.unsavedRows}
            assert reasons == {"저장 실패", "용량 제한"}, reasons
            texts = visible_texts()
            assert any("영상 · 1280×720 · 2분 13초 · 약 340MB" in text for text in texts), texts
            assert any("설정 › 고급에서 크기 제한을 올린 뒤" in text for text in texts), texts
            assert any("미리볼 수 없습니다" in text for text in texts)
            shot(f"{tag}-unsaved")
            click("unsavedViewExcluded")
            wait(lambda: controller.records.unsavedView == "excluded" and controller.records.unsavedTotal == 1
                 and not controller.records.unsavedBusy, "excluded view")
            assert controller.records.unsavedRows[0]["reasonLabel"] == "직접 제외"
            shot(f"{tag}-unsaved-excluded")
            click("unsavedViewFailed")
            wait(lambda: controller.records.unsavedView == "unsaved" and controller.records.unsavedTotal == 2
                 and not controller.records.unsavedBusy, "back to failed view")

            # One retry reaches the downloader as exactly that media, and the save screen takes over.
            started.clear()
            retry_buttons = [item for item in inside(dialog) if item.objectName() == "unsavedRetry"
                             and item.property("visible") and item.property("enabled")]
            assert len(retry_buttons) == 1, len(retry_buttons)  # the size-limited one waits for a higher limit
            reveal(retry_buttons[0])
            click(retry_buttons[0])
            wait(lambda: bool(started), "retry started")
            assert started[0].targets == ("300_1",), started[0].targets
            assert not dialog.property("visible") and not preferences.property("visible")
            assert window.property("currentScreen") == 0
            wait(lambda: not controller.busy, "retry prepared")

            # Raising the limit makes the size-limited media retryable; "all" sends both.
            controller.setPreference("max_video_mb", 500)
            wait(lambda: not controller.busy, "limit saved")
            click_text("설정")
            wait(lambda: preferences.property("visible"), "settings reopened")
            click("preferencesUnsavedButton")
            wait(lambda: dialog.property("visible") and controller.records.retryableCount == 2, "both retryable")
            started.clear()
            click("unsavedRetryAll")
            wait(lambda: bool(started), "retry all started")
            assert set(started[0].targets) == {"300_1", "301_video_2"}, started[0].targets
            controller.setPreference("max_video_mb", 200)
            wait(lambda: not controller.busy, "settled")

            # Forgetting a row changes only the list.
            click_text("설정")
            wait(lambda: preferences.property("visible"), "settings for dismiss")
            click("preferencesUnsavedButton")
            wait(lambda: dialog.property("visible") and controller.records.unsavedTotal == 2
                 and not controller.records.unsavedBusy, "list before dismiss")
            dismiss = next(item for item in inside(dialog) if item.objectName() == "unsavedDismiss" and item.property("visible"))
            reveal(dismiss)
            click(dismiss)
            wait(lambda: controller.records.unsavedTotal == 1 and not controller.records.unsavedBusy, "row forgotten")
            assert {path: path.read_bytes() for path in files} == original
            unsaved.record(fixture_rows[:2], account="offline", output_dir=str(paths.output_dir))
            controller.records.refresh()
            wait(lambda: controller.records.unsavedTotal == 2 and not controller.records.unsavedBusy, "restored fixture")
            QTest.keyClick(window, Qt.Key.Key_Escape)
            wait(lambda: not dialog.property("visible"), "unsaved closed")

            # Automatic save is a dialog of the settings now; a confirmation still guards a change.
            reveal(find("preferencesScheduleButton"))
            click("preferencesScheduleButton")
            schedule = find("scheduleDialog")
            wait(lambda: schedule.property("visible"), "schedule dialog")
            assert "예약이 없습니다" in find("scheduleSummary").property("text")
            save = find("scheduleSaveButton")
            reveal(save)
            shot(f"{tag}-schedule")
            click(save)
            confirmation = find("scheduleConfirmation")
            wait(lambda: confirmation.property("visible"), "schedule confirmation")
            QTest.keyClick(window, Qt.Key.Key_Escape)
            wait(lambda: not confirmation.property("visible"), "cancel schedule")
            QTest.keyClick(window, Qt.Key.Key_Escape)
            wait(lambda: not schedule.property("visible"), "schedule closed")
            QTest.keyClick(window, Qt.Key.Key_Escape)
            wait(lambda: not preferences.property("visible"), "settings closed")

            # The save screen's own shortcut opens the same schedule dialog.
            reveal(find("saveScheduleButton"))
            click("saveScheduleButton")
            wait(lambda: schedule.property("visible"), "schedule from save screen")
            QTest.keyClick(window, Qt.Key.Key_Escape)
            wait(lambda: not schedule.property("visible"), "schedule closed again")
            # And the unsaved summary opens its list directly.
            reveal(find("saveUnsavedButton"))
            click("saveUnsavedButton")
            wait(lambda: dialog.property("visible"), "unsaved from save screen")
            QTest.keyClick(window, Qt.Key.Key_Escape)
            wait(lambda: not dialog.property("visible"), "unsaved closed again")

        assert {path: path.read_bytes() for path in files} == original
        controller.requestClose()
        wait(lambda: controller.shutdownReady, "shutdown")
        window.close()
        wheel.close()
        delete(engine)
        engine = None
        delete(controller)
        controller = None
        errors = [message for message in messages if any(term in message for term in (
            "ReferenceError", "TypeError", "Binding loop", "Cannot assign", "Cannot read", "is not a type"))]
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
class RecordsDialogsSceneTests(unittest.TestCase):
    def test_settings_schedule_history_and_unsaved_media_in_the_real_window(self):
        result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scene"], cwd=ROOT,
                                capture_output=True, text=True, encoding="utf-8", timeout=150,
                                env={**os.environ, "QT_QPA_PLATFORM": os.environ.get("QT_QPA_PLATFORM", "offscreen"),
                                     "PYTHONIOENCODING": "utf-8"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(json.loads(result.stdout.splitlines()[-1])["ok"])


if __name__ == "__main__":
    if "--scene" in sys.argv:
        scene()
    else:
        unittest.main()

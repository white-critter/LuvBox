"""Keys and double clicks in the dialogs and on the unsaved-media page (1.10.8 inspection).

dialogs  Enter presses the button a dialog put the keys on (the save review's 저장); Esc while
         typing the size limit in the settings takes the typing back instead of saving it; the
         connection dialog with its help open fits a window of the default size without scrolling
unsaved  a double click or a held Enter key on a "모두 …" button only asks for confirmation;
         a double click on a tile opens the post and leaves the tile as it was
"""

from importlib.util import find_spec
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
try:
    from tests import app_data_isolation  # noqa: F401  (never this PC's real app data)
except ModuleNotFoundError:      # run as a script: the tests folder is the path
    import app_data_isolation  # noqa: F401


ROOT = Path(__file__).resolve().parents[1]
TOTAL = 40


def scene(part):
    import faulthandler

    faulthandler.dump_traceback_later(100, exit=True)
    from contextlib import ExitStack, closing
    from datetime import datetime, timedelta, timezone
    from unittest.mock import patch

    from PIL import Image
    from PySide6.QtCore import QCoreApplication, QEvent, QMetaObject, QObject, QPointF, Qt, QUrl, qInstallMessageHandler
    from PySide6.QtGui import QKeyEvent, QMouseEvent
    from PySide6.QtQml import QQmlApplicationEngine
    from PySide6.QtQuickControls2 import QQuickStyle
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    from shiboken6 import delete

    sys.path.insert(0, str(ROOT))
    from luvbox import thumbcache
    from luvbox.history import HistoryStore
    from luvbox.qt_app import ApplicationController
    from luvbox.qt_wheel import WheelInputFilter
    from luvbox.storage import resolve_storage

    QQuickStyle.setStyle("Basic")
    application = QApplication([])
    messages = []
    previous = qInstallMessageHandler(lambda _level, _context, message: messages.append(message))
    controller = engine = window = folder = None
    resources = ExitStack()
    opened = []

    def settle(milliseconds=0):
        until = time.monotonic() + milliseconds / 1000
        while True:
            application.processEvents()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            if time.monotonic() >= until:
                return
            time.sleep(.003)

    def wait(predicate, label, limit=15):
        until = time.monotonic() + limit
        while time.monotonic() < until:
            settle()
            if predicate():
                return
            time.sleep(.003)
        raise AssertionError(label + ": timeout")

    def find(name):
        result = window.findChild(QObject, name)
        if result is None:
            raise AssertionError("Missing product control: " + name)
        return result

    def presses(point, steps):
        """Posted mouse events with their times: a tile and a button tell a double click by them."""
        position = QPointF(window.mapToGlobal(point.toPoint()))
        start = int(time.monotonic() * 1000)
        for kind, after in steps:
            held = Qt.MouseButton.NoButton if kind == QEvent.Type.MouseButtonRelease else Qt.MouseButton.LeftButton
            event = QMouseEvent(kind, point, position, Qt.MouseButton.LeftButton, held, Qt.KeyboardModifier.NoModifier)
            event.setTimestamp(start + after)
            QCoreApplication.postEvent(window, event)
        settle(80)

    def place(target):
        """Where to press, read after things have settled: a dialog that just opened is placed a moment later."""
        settle(450)
        return target() if callable(target) else target.mapToScene(QPointF(target.width() / 2, target.height() / 2))

    def click_at(target):
        presses(place(target), ((QEvent.Type.MouseButtonPress, 0), (QEvent.Type.MouseButtonRelease, 5)))

    def double_at(target):
        presses(place(target), ((QEvent.Type.MouseButtonPress, 0), (QEvent.Type.MouseButtonRelease, 5),
                                (QEvent.Type.MouseButtonPress, 80), (QEvent.Type.MouseButtonDblClick, 80),
                                (QEvent.Type.MouseButtonRelease, 85)))

    try:
        folder = resources.enter_context(tempfile.TemporaryDirectory(prefix="luvbox-dialog-keys-"))
        resources.enter_context(patch.dict(os.environ, {"LOCALAPPDATA": folder}))
        resources.enter_context(patch("luvbox.qt_schedule.task_status", return_value={"registered": False}))
        resources.enter_context(patch("luvbox.qt_records._UNSAVED_PAGE", 20))
        # Never a real browser.
        resources.enter_context(patch("luvbox.qt_records.QDesktopServices.openUrl",
                                      side_effect=lambda url: opened.append(url.toString()) or True))
        base = Path(folder)
        paths = resolve_storage(base / "photos", "offline")
        source = paths.media_dir / "user_1_1.jpg"
        with closing(HistoryStore.for_storage(paths)) as store:
            Image.new("RGB", (600, 450), (90, 60, 160)).save(source, "JPEG")
            store.record_media("flat", {"tweet_id": "1", "num": 1, "type": "photo", "author": {"name": "user"},
                                        "date": datetime(2026, 10, 1, 12, tzinfo=timezone(timedelta(hours=9)))}, source)
            if part == "unsaved":
                for index in range(TOTAL):
                    tweet = str(5000 + index)
                    store.record_declined("flat", {"tweet_id": tweet, "num": 1, "type": "photo",
                                                   "author": {"name": f"writer{index:02d}"}},
                                          paths.media_dir / f"writer_{tweet}_1.jpg")
        if part == "unsaved":
            cache = thumbcache.Cache("offline")
            try:
                for index in range(TOTAL):
                    cache.put((str(5000 + index), 1, "photo"),
                              Image.new("RGB", (320, 240), (40 + index * 5, 90, 200 - index * 4)))
            finally:
                cache.close()
        (base / "LuvBox").mkdir(exist_ok=True)
        (base / "LuvBox/settings.json").write_text(
            json.dumps({"account": "offline", "output_dir": str(paths.output_dir), "gallery_density": 0}),
            encoding="utf-8")

        controller = ApplicationController()
        wheel_filter = WheelInputFilter()
        engine = QQmlApplicationEngine()
        engine.addImageProvider("thumbs", controller.library.provider)
        engine.addImageProvider("unsavedpreview", controller.records.preview_provider())
        engine.setInitialProperties({"appController": controller, "galleryModel": controller.library.model,
                                     "wheelInputFilter": wheel_filter})
        engine.load(ROOT / "assets/qt/MainWindow.qml")
        assert engine.rootObjects(), messages
        window = engine.rootObjects()[0]
        window.setWidth(1280)
        window.setHeight(860)
        report = {}

        if part == "dialogs":
            window.requestActivate()
            wait(lambda: not controller.busy, "idle")
            answers = []
            controller._download.resolveReview = lambda ident, action: answers.append((ident, action))
            review = find("saveReviewDialog")

            def offer(ident):
                controller._review = {"id": ident, "url": QUrl.fromLocalFile(str(source)).toString(),
                                      "media_type": "photo", "filename": source.name, "author": "user",
                                      "pixel_width": 600, "pixel_height": 450, "source_width": 600, "source_height": 450}
                controller.changed.emit()
                controller.reviewReady.emit()
                wait(lambda: review.property("visible") and review.property("opened"), "review shown")
                settle(200)

            # The dialog opens with the keys on its red 저장 button: Enter saves, as Space does.
            offer("r1")
            QTest.keyClick(window, Qt.Key.Key_Return)
            wait(lambda: answers == [("r1", "save")], "Enter saved")
            # Esc still stops the whole save.
            del answers[:]
            offer("r2")
            QTest.keyClick(window, Qt.Key.Key_Escape)
            wait(lambda: answers == [("r2", "stop")], "Esc stopped")
            controller._review = {}
            controller.changed.emit()
            controller.reviewCleared.emit()
            settle(100)

            # Settings: a size limit half typed, then Esc. The typing is taken back, not saved.
            preferences = find("preferencesDialog")
            QMetaObject.invokeMethod(preferences, "open")
            wait(lambda: preferences.property("visible"), "settings shown")
            click_at(find("advancedSettingsToggle"))
            field = find("videoLimitField")
            wait(lambda: field.property("visible"), "advanced open")
            assert controller.preferences["max_video_mb"] == 200
            click_at(field)
            QTest.keyClick(window, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
            QTest.keyClick(window, Qt.Key.Key_5)
            settle(80)
            assert field.property("text") == "5", field.property("text")
            QTest.keyClick(window, Qt.Key.Key_Escape)
            wait(lambda: not preferences.property("visible"), "settings closed")
            settle(300)
            wait(lambda: not controller.busy, "settings settled")
            report["limit"] = [controller.preferences["max_video_mb"], field.property("text")]
            assert report["limit"] == [200, "200"], report

            # The connection dialog with its help open: beside the connection, nothing to scroll.
            connection = find("connectionDialog")
            QMetaObject.invokeMethod(connection, "open")
            wait(lambda: connection.property("visible") and connection.property("opened"), "connection shown")
            toggle, guide, scroll = find("connectionHelpToggle"), find("connectionHelp"), find("connectionScroll")
            if not toggle.property("checked"):
                click_at(toggle)
            wait(lambda: bool(guide.property("visible")), "help open")
            settle(400)
            report["connection"] = [round(scroll.property("contentHeight")), round(scroll.height()),
                                    guide.mapToScene(QPointF(0, 0)).x() > toggle.mapToScene(QPointF(0, 0)).x() + 100]
            assert report["connection"][0] <= report["connection"][1] + 2 and report["connection"][2], report
            # In a narrow window the help goes back under the connection.
            window.setWidth(900)
            settle(400)
            assert guide.mapToScene(QPointF(0, 0)).y() > toggle.mapToScene(QPointF(0, 0)).y(), "help under the toggle"
            QMetaObject.invokeMethod(connection, "close")
            wait(lambda: not connection.property("visible"), "connection closed")

        else:
            window.setProperty("currentScreen", 1)
            records = controller.records
            wait(lambda: not controller.busy and records.excludedCount == TOTAL, "excluded media counted")
            click_at(find("galleryUnsavedButton"))
            grid = find("unsavedGrid")
            wait(lambda: window.property("currentScreen") == 2 and records.unsavedView == "excluded"
                 and not records.unsavedBusy and grid.property("count") >= 20, "page opened")
            settle(400)
            # A double click on the whole-list button with nothing picked: it asks, nothing is released.
            review = find("unsavedReview")
            settle_button = find("unsavedSettle")
            assert records.unsavedSelectedCount == 0
            double_at(settle_button)
            wait(lambda: not records.unsavedBusy, "settled")
            settle(600)
            report["after_double_click"] = [records.excludedCount,
                                            bool(review.property("confirmAll"))]
            assert report["after_double_click"] == [TOTAL, True], report

            for key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                QTest.keyClick(window, Qt.Key.Key_Escape)
                wait(lambda: not review.property("confirmAll"), "confirmation cleared")
                settle_button.forceActiveFocus(Qt.FocusReason.TabFocusReason)
                QTest.keyPress(window, key)
                wait(lambda: review.property("confirmAll"), "Enter asked for confirmation")
                QCoreApplication.sendEvent(window, QKeyEvent(
                    QEvent.Type.KeyPress, key, Qt.KeyboardModifier.NoModifier, "", True))
                QTest.keyRelease(window, key)
                wait(lambda: not records.unsavedBusy, "held key settled")
                assert review.property("confirmAll"), "holding Enter confirmed the whole-list action"
                assert records.excludedCount == TOTAL

            # A double click on a tile opens its post and leaves the tile unpicked.
            def point():
                return grid.mapToScene(QPointF(grid.property("cellWidth") * 1.5, grid.property("cellHeight") * 0.5))

            double_at(point)
            wait(lambda: len(opened) == 1, "the post was opened once")
            settle(300)
            report["after_tile_double_click"] = [records.unsavedSelectedCount, len(opened)]
            assert report["after_tile_double_click"] == [0, 1], report
            # A single click still picks it.
            click_at(point)
            wait(lambda: records.unsavedSelectedCount == 1, "a click picks the tile")

            # A separate second press still confirms the action.
            records.clearUnsavedSelection()
            settle_button.forceActiveFocus(Qt.FocusReason.TabFocusReason)
            QTest.keyClick(window, Qt.Key.Key_Return)
            wait(lambda: review.property("confirmAll"), "first Enter asked for confirmation")
            QTest.keyClick(window, Qt.Key.Key_Return)
            wait(lambda: records.excludedCount == 0, "second Enter confirmed")

        controller.requestClose()
        wait(lambda: controller.shutdownReady, "shutdown")
        window.close()
        wheel_filter.close()
        delete(engine)
        engine = None
        delete(controller)
        controller = None
        errors = [message for message in messages if any(term in message for term in (
            "ReferenceError", "TypeError", "Binding loop", "Cannot assign", "Cannot read", "is not a type",
            "Unable to assign"))]
        assert not errors, errors
        print(json.dumps({"ok": True, "report": report}, ensure_ascii=True))
        sys.stdout.flush()
    except BaseException:
        import shutil
        import traceback

        traceback.print_exc()
        print("qt messages:", [message.encode("unicode_escape").decode("ascii") for message in messages[-10:]],
              file=sys.stderr)
        sys.stdout.flush()
        sys.stderr.flush()
        if folder is not None:
            shutil.rmtree(folder, ignore_errors=True)
        os._exit(1)
    finally:
        if controller is not None:
            controller.requestClose()
            wait(lambda: controller.shutdownReady, "cleanup")
        if engine is not None:
            delete(engine)
        qInstallMessageHandler(previous)
        resources.close()


@unittest.skipUnless(find_spec("PySide6") is not None, "Qt dependencies are not installed")
class DialogKeysSceneTests(unittest.TestCase):
    def run_scene(self, part):
        try:
            result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scene", part],
                                    cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                    timeout=120, env={**os.environ, "QT_QPA_PLATFORM": "offscreen"})
        except subprocess.TimeoutExpired as error:
            self.fail("scene timed out: " + str(error.stdout or "") + str(error.stderr or ""))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(json.loads(result.stdout.strip().splitlines()[-1])["ok"])

    def test_enter_presses_the_focused_button_and_escape_takes_back_a_half_typed_limit(self):
        self.run_scene("dialogs")

    def test_a_double_click_or_held_enter_cannot_confirm_a_whole_list_action(self):
        self.run_scene("unsaved")


if __name__ == "__main__":
    if "--scene" in sys.argv:
        scene(sys.argv[sys.argv.index("--scene") + 1])
    else:
        unittest.main()

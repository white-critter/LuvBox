"""The 1.9.0 look, checked in the real product window.

One emphasised button a screen, the statistics and the key list one click away,
one way to end choosing files, a tip that follows the state, and settings that
fit without scrolling. Pass --shots DIR to keep a picture of each step; without
QT_QPA_PLATFORM the scene opens a real Windows window.
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


def scene(shots=None):
    import faulthandler

    faulthandler.dump_traceback_later(50, exit=True)
    from contextlib import ExitStack, closing
    from datetime import datetime, timezone, timedelta
    from unittest.mock import patch

    from PIL import Image
    from PySide6.QtCore import QCoreApplication, QEvent, QMetaObject, QObject, QPoint, QPointF, Qt, qInstallMessageHandler
    from PySide6.QtGui import QFont, QFontDatabase
    from PySide6.QtQuick import QQuickItem
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    from PySide6.QtQml import QQmlApplicationEngine
    from PySide6.QtQuickControls2 import QQuickStyle
    from shiboken6 import delete

    sys.path.insert(0, str(ROOT))
    from luvbox.history import HistoryStore
    from luvbox.storage import resolve_storage
    from luvbox.qt_app import ApplicationController
    from luvbox.qt_wheel import WheelInputFilter

    QQuickStyle.setStyle("Basic")
    application = QApplication([])
    for filename in ("Pretendard-Regular.ttf", "Pretendard-Bold.ttf"):
        QFontDatabase.addApplicationFont(str(ROOT / "assets/fonts/Pretendard" / filename))
    font = QFont("Pretendard")
    font.setPixelSize(14)
    application.setFont(font)
    messages = []
    previous = qInstallMessageHandler(lambda _level, _context, message: messages.append(message))
    controller = engine = window = None
    resources = ExitStack()

    def settle(milliseconds=0):
        until = time.monotonic() + milliseconds / 1000
        while True:
            application.processEvents()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            if time.monotonic() >= until:
                return
            time.sleep(.003)

    def wait(predicate, label):
        until = time.monotonic() + 8
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

    def shot(name):
        if shots is not None:
            settle(150)
            Path(shots).mkdir(parents=True, exist_ok=True)
            window.grabWindow().save(str(Path(shots) / (name + ".png")))

    def center(item):
        return item.mapToScene(QPointF(item.width() / 2, item.height() / 2)).toPoint()

    def click(name):
        settle(40)
        item = find(name)
        assert item.property("visible") and item.property("enabled"), name
        QTest.mouseClick(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, center(item))
        settle()

    def emphasised():
        """Names (or texts) of the red main-action buttons showing right now."""
        return sorted(item.objectName() or str(item.property("text"))
                      for item in window.findChildren(QQuickItem)
                      if item.property("tone") == "primary" and item.property("visible"))

    def opened(name, at_most, method="open"):
        dialog = find(name)
        assert QMetaObject.invokeMethod(dialog, method), name
        wait(lambda: dialog.property("visible"), name + " opened")
        settle(60)
        showing = emphasised()
        assert len(showing) <= at_most, (name, showing)
        shot("dialog-" + name)
        return dialog, showing

    def closed(dialog, label):
        assert QMetaObject.invokeMethod(dialog, "close"), label
        wait(lambda: not dialog.property("visible"), label + " closed")
        settle(40)

    try:
        folder = resources.enter_context(tempfile.TemporaryDirectory(prefix="luvbox-look-"))
        resources.enter_context(patch.dict(os.environ, {"LOCALAPPDATA": folder}))
        resources.enter_context(patch("luvbox.qt_schedule.task_status", return_value={"registered": False}))
        base = Path(folder)
        paths = resolve_storage(base / "사진 폴더", "offline")
        with closing(HistoryStore.for_storage(paths)) as store:
            for index in range(24):
                author = ("alice", "bob", "carol")[index % 3]
                path = paths.media_dir / f"{author}_{3000 + index}_1.png"
                Image.new("RGB", (48, 32), (40 + index * 6, 30, 50)).save(path)
                store.record_media("flat", {"tweet_id": str(3000 + index), "num": 1, "type": "photo",
                                            "author": {"name": author},
                                            "date": datetime(2026, 9, 1 + index, 12,
                                                             tzinfo=timezone(timedelta(hours=9)))}, path)
        (base / "LuvBox").mkdir(exist_ok=True)
        (base / "LuvBox/settings.json").write_text(
            json.dumps({"account": "offline", "output_dir": str(paths.output_dir)}), encoding="utf-8")

        controller = ApplicationController()
        wheel_filter = WheelInputFilter()
        engine = QQmlApplicationEngine()
        engine.addImageProvider("thumbs", controller.library.provider)
        engine.setInitialProperties({"appController": controller, "galleryModel": controller.library.model,
                                     "wheelInputFilter": wheel_filter})
        engine.load(ROOT / "assets/qt/MainWindow.qml")
        assert engine.rootObjects(), messages
        window = engine.rootObjects()[0]
        assert (window.width(), window.height()) == (1280, 860)
        library, model = controller.library, controller.library.model
        wait(lambda: not controller.busy, "idle application")
        report = {"platform": application.platformName()}

        # --- The window carries the startup screen's light, and it rests while nothing is being saved.
        light = find("windowLight")
        assert light.property("strength") > 0 and light.width() > 600 and light.height() > 200
        assert not window.property("lightBreathing") and light.property("pulse") == 1

        # --- Save screen: one emphasised button, and it is the one that starts the save.
        assert emphasised() == ["saveStartButton"], emphasised()
        assert not find("galleryStatisticsButton").property("visible")
        shot("1-save")

        # --- Gallery: nothing is emphasised; statistics and the key list are in the title bar.
        window.setProperty("currentScreen", 1)
        wait(lambda: not model.busy and model.count == 24, "gallery loaded")
        settle(120)
        assert emphasised() == [], emphasised()
        for name in ("galleryStatisticsButton", "shortcutHelpButton", "galleryRefresh", "gallerySelectionToggle"):
            assert find(name).property("visible") and find(name).property("enabled"), name
        assert window.findChild(QObject, "galleryToolsButton") is None, "no menu hides the statistics"
        shot("2-gallery")
        click("galleryStatisticsButton")
        statistics = find("statisticsDialog")
        wait(lambda: statistics.property("visible") and statistics.property("loaded"), "statistics by one click")
        assert emphasised() == []
        shot("3-statistics")
        closed(statistics, "statistics")
        click("shortcutHelpButton")
        help_dialog = find("shortcutHelp")
        wait(lambda: help_dialog.property("visible"), "key list by one click")
        # Both lists show side by side and nothing needs scrolling at this size.
        assert help_dialog.property("wide")
        settle(120)
        assert help_dialog.property("height") < window.height() - 32, (
            help_dialog.property("height"), window.height(), help_dialog.property("chromeHeight"))
        shot("4-shortcuts")
        closed(help_dialog, "key list")

        # --- A new-photos notice lights the refresh button instead of adding a second one.
        refresh = find("galleryRefresh")
        assert refresh.property("text") == "새로고침" and not refresh.property("lit")
        controller._gallery_dirty = True
        controller.changed.emit()
        wait(lambda: refresh.property("text") == "새 사진 보기" and refresh.property("lit"), "refresh lit")
        controller._gallery_dirty = False
        controller.changed.emit()
        wait(lambda: refresh.property("text") == "새로고침", "refresh quiet again")

        # --- Choosing files: the bar is at the bottom, and only one control ends it.
        grid = find("galleryGrid")
        toggle = find("gallerySelectionToggle")
        assert toggle.property("text") == "여러 개 선택" and not find("gallerySelectionBar").property("visible")
        grid_top = grid.mapToScene(QPointF(0, 0)).y()
        click("gallerySelectionToggle")
        bar = find("gallerySelectionBar")
        wait(lambda: library.selectionMode and bar.property("visible"), "selection bar shown")
        settle(80)
        assert toggle.property("text") == "선택 끝내기" and toggle.property("lit")
        assert window.findChild(QObject, "gallerySelectionDone") is None, "a second button to end selecting"
        ending = [item for item in window.findChildren(QQuickItem)
                  if item.property("visible") and item.property("tone") is not None
                  and str(item.property("text") or "") in ("끝내기", "선택 끝내기")]
        assert [item.objectName() for item in ending] == ["gallerySelectionToggle"], [i.objectName() for i in ending]
        # The bar sits under the pictures and does not push them down.
        assert abs(grid.mapToScene(QPointF(0, 0)).y() - grid_top) < 1
        assert bar.mapToScene(QPointF(0, 0)).y() >= grid.mapToScene(QPointF(0, grid.height())).y() - 1
        assert emphasised() == [], emphasised()
        click("gallerySelectLoaded")
        assert library.selectedCount == 24
        assert find("galleryRecycleSelected").property("tone") == "danger"
        shot("5-selection")
        click("gallerySelectionToggle")
        wait(lambda: not library.selectionMode and not bar.property("visible"), "selection ended")

        # --- The date chip's tip says what a click will do now, never what it did.
        chip = find("galleryFilterToggle")
        assert not chip.property("checked") and chip.property("tip") == "게시일이나 저장일로 좁히기"
        QTest.mouseMove(window, center(chip))
        settle(60)
        assert not chip.property("tipSpent")
        click("galleryFilterToggle")
        wait(lambda: chip.property("checked") and find("galleryDateFrom").property("visible"), "date range open")
        assert chip.property("tip") == "날짜 범위 접기"
        assert chip.property("tipSpent"), "the tip leaves on a click until the pointer comes back"
        assert not chip.property("lit"), "open is not the same as a date being applied"
        click("galleryFilterToggle")
        wait(lambda: not chip.property("checked"), "date range closed")
        assert chip.property("tip") == "게시일이나 저장일로 좁히기" and chip.property("tipSpent")
        QTest.mouseMove(window, QPoint(window.width() // 2, 30))
        wait(lambda: not chip.property("tipSpent"), "tip ready again after the pointer left")

        # --- Settings: two columns in a wide window, nothing to scroll with the advanced part open.
        preferences, _showing = opened("preferencesDialog", 0)
        scroll = find("preferencesScroll")
        assert preferences.property("wide")
        click("advancedSettingsToggle")
        wait(lambda: find("videoLimitField").property("visible"), "advanced part open")
        settle(120)
        flick = scroll.property("contentItem")
        assert flick.property("contentHeight") <= flick.height() + 2, (
            flick.property("contentHeight"), flick.height())
        assert preferences.property("height") < window.height() - 32
        for name in ("preferencesScheduleButton", "preferencesRunHistoryButton", "preferencesUnsavedButton"):
            item = find(name)
            bottom = item.mapToScene(QPointF(0, item.height())).y()
            assert item.property("visible") and bottom <= window.height(), (name, bottom)
        report["settings"] = [preferences.property("width"), preferences.property("height")]
        shot("6-settings-advanced")
        # A narrow window stacks the same settings in one column (and scrolls).
        window.setWidth(680)
        window.setHeight(480)
        settle(150)
        assert not preferences.property("wide")
        assert preferences.property("width") <= 680 - 32 and preferences.property("height") <= 480 - 32
        window.setWidth(1280)
        window.setHeight(860)
        settle(150)
        assert preferences.property("wide")
        closed(preferences, "settings")

        # --- Every other window: at most one emphasised button, and the confirmations have exactly one.
        for name in ("runHistoryDialog", "shortcutHelp", "statisticsDialog"):
            dialog, showing = opened(name, 0)
            closed(dialog, name)
        for name in ("aiClearConfirm", "repairDialog", "storageDialog"):
            dialog, showing = opened(name, 1)
            assert len(showing) == 1, (name, showing)
            closed(dialog, name)
        for name in ("connectionDialog", "scheduleDialog"):
            dialog, showing = opened(name, 1)
            closed(dialog, name)
        # The unsaved media have a page of their own, laid out like the gallery: one main button at most.
        screen = window.property("currentScreen")
        from PySide6.QtCore import Q_ARG
        assert QMetaObject.invokeMethod(window, "openUnsaved", Q_ARG("QVariant", ""))
        wait(lambda: window.property("currentScreen") == 2 and find("unsavedReview").property("visible"),
             "unsaved page opened")
        settle(60)
        assert set(emphasised()) <= {"unsavedRetry"}, emphasised()
        shot("page-unsaved")
        window.setProperty("currentScreen", screen)
        settle(60)
        key = model._rows[0]["key"]
        controller.management.prepareDelete([key])
        confirmation = find("deleteConfirmation")
        wait(lambda: confirmation.property("visible"), "delete confirmation")
        settle(60)
        assert emphasised() == ["confirmRecycle"], emphasised()
        # The confirmation is only as tall as what it says.
        assert confirmation.property("height") < 300, confirmation.property("height")
        shot("7-delete")
        assert QMetaObject.invokeMethod(confirmation, "reject")
        wait(lambda: not controller.management.deleteConfirmationPending, "delete cancelled")
        window.setProperty("currentScreen", 0)
        settle(80)
        assert emphasised() == ["saveStartButton"], emphasised()

        controller.requestClose()
        wait(lambda: controller.shutdownReady, "shutdown")
        window.close()
        wheel_filter.close()
        delete(engine)
        engine = None
        delete(controller)
        controller = None
        errors = [m for m in messages if any(term in m for term in (
            "ReferenceError", "TypeError", "Binding loop", "Cannot assign", "Cannot read", "is not a type",
            "Unable to assign", "is not defined"))]
        assert not errors, errors
        report.update(ok=True, qml_errors=errors)
        print(json.dumps(report))
    finally:
        if controller is not None:
            controller.requestClose()
            wait(lambda: controller.shutdownReady, "cleanup")
        if engine is not None:
            delete(engine)
        qInstallMessageHandler(previous)
        resources.close()


@unittest.skipUnless(find_spec("PySide6") is not None, "Qt dependencies are not installed")
class LookSceneTests(unittest.TestCase):
    def test_one_main_button_per_screen_and_everything_in_reach(self):
        result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scene"],
                                cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=60,
                                env={**os.environ, "QT_QPA_PLATFORM": "offscreen"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(json.loads(result.stdout.splitlines()[-1])["ok"])


if __name__ == "__main__":
    if "--scene" in sys.argv:
        scene(sys.argv[sys.argv.index("--shots") + 1] if "--shots" in sys.argv else None)
    else:
        unittest.main()

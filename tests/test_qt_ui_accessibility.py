"""Keyboard focus and error layouts remain usable in the product QML."""

from importlib.util import find_spec
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]


def scene(*, measure=False, qml_root=None, screenshot_dir=None):
    from contextlib import ExitStack
    import tempfile
    import time
    from unittest.mock import patch

    from PySide6.QtCore import QObject, QPointF, Qt, qInstallMessageHandler
    from PySide6.QtGui import QFont, QFontDatabase
    from PySide6.QtQml import QQmlApplicationEngine, QQmlProperty
    from PySide6.QtQuickControls2 import QQuickStyle
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    from shiboken6 import delete

    sys.path.insert(0, str(ROOT))
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
    previous = qInstallMessageHandler(lambda _kind, _context, message: messages.append(message))
    controller = engine = window = wheel = None
    resources = ExitStack()

    def wait(predicate):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            application.processEvents()
            if predicate():
                return
            time.sleep(.003)
        raise AssertionError("Product scene did not settle")

    def find(name):
        value = window.findChild(QObject, name)
        assert value is not None, name
        return value

    def visual_items(parent):
        yield parent
        for child in parent.childItems():
            yield from visual_items(child)

    def bounds(item):
        origin = item.mapToScene(QPointF(0, 0))
        return [round(origin.x(), 1), round(origin.y(), 1),
                round(item.width(), 1), round(item.height(), 1)]

    def screenshot(name):
        if screenshot_dir:
            destination = Path(screenshot_dir)
            destination.mkdir(parents=True, exist_ok=True)
            assert window.grabWindow().save(str(destination / (name + ".png")))

    try:
        folder = resources.enter_context(tempfile.TemporaryDirectory(prefix="luvbox-keyboard-ui-"))
        resources.enter_context(patch.dict(os.environ, {"LOCALAPPDATA": folder}))
        resources.enter_context(patch("luvbox.qt_schedule.task_status", return_value={"registered": False}))
        controller = ApplicationController()
        wheel = WheelInputFilter()
        engine = QQmlApplicationEngine()
        engine.addImageProvider("thumbs", controller.library.provider)
        engine.setInitialProperties({"appController": controller, "galleryModel": controller.library.model,
                                     "wheelInputFilter": wheel})
        engine.load((Path(qml_root) if qml_root else ROOT / "assets/qt") / "MainWindow.qml")
        assert engine.rootObjects(), messages
        window = engine.rootObjects()[0]
        window.requestActivate()
        wait(lambda: not controller.busy)
        window.setProperty("currentScreen", 1)
        wait(lambda: not controller.library.model.busy)
        report = {"geometry": [], "keyboard": {}, "platform": application.platformName(),
                  "device_pixel_ratio": window.devicePixelRatio()}
        error_text = "사진 목록을 불러오지 못했습니다. 저장 폴더와 접근 권한을 확인해 주세요."
        controller.library.model._error = error_text
        controller.library.model.changed.emit()
        for width, height in ((680, 480), (1080, 760)):
            window.setWidth(width)
            window.setHeight(height)
            QTest.qWait(100)
            label = next(item for item in visual_items(window.contentItem())
                         if item.property("visible") and item.property("text") == error_text)
            refresh = next(item for item in visual_items(window.contentItem())
                           if item.property("visible") and item.property("text") == "새로고침")
            label_bounds, refresh_bounds = bounds(label), bounds(refresh)
            report["geometry"].append({"window": [width, height], "error": label_bounds,
                                       "refresh": refresh_bounds, "grid": bounds(find("galleryGrid"))})
            screenshot(f"error-{width}x{height}")
            if not measure:
                assert not find("galleryEmptyState").property("visible")
                assert label_bounds[0] >= 24 and label_bounds[0] + label_bounds[2] <= width - 23
                assert label_bounds[1] >= refresh_bounds[1] + refresh_bounds[3]
                assert refresh_bounds[0] + refresh_bounds[2] <= width - 23
                assert label.height() >= label.property("implicitHeight") - 1
                assert find("galleryGrid").height() >= 40
        # A long filename/path must wrap instead of widening the whole gallery.
        controller.library.model._error = error_text + " C:/" + ("long_folder_name_" * 18) + "long-filename.png"
        controller.library.model.changed.emit()
        window.setWidth(680)
        window.setHeight(480)
        QTest.qWait(100)
        screenshot("long-path-680x480")
        if not measure:
            error = find("galleryLoadError")
            assert error.height() > 14
            assert error.width() <= 632 and find("galleryGrid").height() >= 40
            report["long_error"] = bounds(error)
        controller.library.model._error = ""
        controller.library.model.changed.emit()
        QTest.qWait(40)
        if not measure:
            assert find("galleryEmptyState").property("visible")
        # The reset button is disabled while nothing is filtered, so check a button that is always on.
        reset = find("galleryFilterToggle")
        reset.forceActiveFocus(Qt.FocusReason.TabFocusReason)
        QTest.qWait(80)
        assert reset.property("visualFocus")
        button_border = QQmlProperty(reset.property("background"), "border.width").read()
        report["keyboard"]["button_border"] = button_border
        screenshot("keyboard-focus-680x480")
        if not measure:
            assert button_border == 2
        search = find("searchBox")
        search.setProperty("text", "existing search")
        QTest.keyClick(window, Qt.Key.Key_F, Qt.KeyboardModifier.ControlModifier)
        QTest.qWait(80)
        assert search.property("activeFocus")
        report["keyboard"]["search_selected"] = search.property("selectedText") == "existing search"
        if not measure:
            assert search.property("selectedText") == "existing search"
        prefs = find("preferencesDialog")
        prefs.open()
        QTest.qWait(80)
        toggle = find("advancedSettingsToggle")
        for _ in range(20):
            if toggle.property("activeFocus"):
                break
            QTest.keyClick(window, Qt.Key.Key_Tab)
            QTest.qWait(15)
        QTest.qWait(80)
        assert toggle.property("visualFocus")
        report["keyboard"]["accordion_bounds"] = bounds(toggle)
        if not measure:
            assert 0 <= bounds(toggle)[1] < window.height() - toggle.height()
        accordion_border = QQmlProperty(toggle.property("background"), "border.width").read()
        report["keyboard"]["accordion_border"] = accordion_border
        QTest.keyClick(window, Qt.Key.Key_F, Qt.KeyboardModifier.ControlModifier)
        QTest.qWait(80)
        report["keyboard"]["modal_keeps_focus"] = bool(toggle.property("activeFocus"))
        screenshot("preferences-keyboard-680x480")
        if not measure:
            assert accordion_border == 2
            assert toggle.property("activeFocus") and not search.property("activeFocus")
            QTest.keyClick(window, Qt.Key.Key_Space)
            QTest.qWait(60)
            assert toggle.property("checked")
            limit = find("videoLimitField")
            for _ in range(12):
                if limit.property("activeFocus"):
                    break
                QTest.keyClick(window, Qt.Key.Key_Tab)
                QTest.qWait(15)
            assert limit.property("activeFocus")
            assert 0 <= bounds(limit)[1] < window.height() - limit.height()
            QTest.keyClick(window, Qt.Key.Key_Tab, Qt.KeyboardModifier.ShiftModifier)
            QTest.qWait(60)
            focused = window.activeFocusItem()
            assert focused is not None and 0 <= bounds(focused)[1] < window.height() - focused.height()
            report["keyboard"]["advanced_fields_revealed"] = True
            screenshot("preferences-advanced-keyboard-680x480")
        prefs.close()
        window.setProperty("currentScreen", 0)
        QTest.qWait(80)
        switch = find("savedDetailsToggle")
        switch.forceActiveFocus(Qt.FocusReason.TabFocusReason)
        QTest.qWait(80)
        assert switch.property("visualFocus")
        switch_border = QQmlProperty(switch.property("indicator"), "border.width").read()
        report["keyboard"]["switch_border"] = switch_border
        if not measure:
            assert switch_border == 2
            previous_checked = switch.property("checked")
            QTest.keyClick(window, Qt.Key.Key_Space)
            QTest.qWait(60)
            assert switch.property("checked") != previous_checked
        controller.requestClose()
        wait(lambda: controller.shutdownReady)
        window.close()
        wheel.close()
        delete(engine)
        engine = None
        delete(controller)
        controller = None
        errors = [value for value in messages if any(term in value for term in (
            "ReferenceError", "TypeError", "Binding loop", "Cannot assign", "Cannot read", "is not a type"))]
        assert not errors, errors
        return report
    finally:
        if controller is not None:
            controller.requestClose()
            wait(lambda: controller.shutdownReady)
        if engine is not None:
            delete(engine)
        if wheel is not None:
            wheel.close()
        resources.close()
        qInstallMessageHandler(previous)


@unittest.skipUnless(find_spec("PySide6") is not None, "Qt dependencies are not installed")
class UiAccessibilityTests(unittest.TestCase):
    def test_error_wrapping_keyboard_focus_and_modal_shortcuts(self):
        result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scene"], cwd=ROOT,
                                env={**os.environ, "QT_QPA_PLATFORM": "offscreen", "QT_QUICK_BACKEND": "software"},
                                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=25)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(json.loads(result.stdout)["keyboard"]["modal_keeps_focus"])


if __name__ == "__main__":
    if "--scene" in sys.argv:
        options = {key: sys.argv[sys.argv.index(flag) + 1]
                   for key, flag in (("qml_root", "--qml-root"), ("screenshot_dir", "--screenshots"))
                   if flag in sys.argv}
        print(json.dumps(scene(measure="--measure" in sys.argv, **options)))
    else:
        unittest.main()

"""Real QML caption buttons and tray lifecycle with an isolated synthetic library."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tests import app_data_isolation  # noqa: E402,F401


def scene(destination):
    import time
    from unittest.mock import patch
    from PySide6.QtCore import QObject, QPointF, QRectF, QMetaObject, Qt, qInstallMessageHandler
    from PySide6.QtGui import QIcon, QWindow
    from PySide6.QtQml import QQmlApplicationEngine
    from PySide6.QtQuickControls2 import QQuickStyle
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QSystemTrayIcon
    from shiboken6 import delete
    from luvbox.qt_app import ApplicationController
    from luvbox.qt_smoke import _fixture
    from luvbox.qt_tray import TrayController
    from luvbox.qt_wheel import WheelInputFilter

    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    QQuickStyle.setStyle("Basic")
    application = QApplication([])
    application.setWindowIcon(QIcon(str(ROOT / "assets/icons/luvbox.ico")))
    messages = []
    previous = qInstallMessageHandler(lambda _kind, _context, message: messages.append(message))

    def wait(predicate):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            application.processEvents()
            if predicate():
                return
            time.sleep(.005)
        raise AssertionError("Window did not settle: " + repr(messages))

    with tempfile.TemporaryDirectory(prefix="luvbox-tray-") as folder, \
            patch.dict(os.environ, {"LOCALAPPDATA": folder}), \
            patch("luvbox.qt_schedule.task_status", return_value={"registered": False}):
        _fixture(Path(folder))
        controller = ApplicationController()
        wheel = WheelInputFilter()
        engine = QQmlApplicationEngine()
        engine.addImageProvider("thumbs", controller.library.provider)
        engine.setInitialProperties({"appController": controller, "galleryModel": controller.library.model,
                                     "wheelInputFilter": wheel})
        engine.load(ROOT / "assets/qt/MainWindow.qml")
        assert engine.rootObjects(), messages
        window = engine.rootObjects()[0]
        controller.shutdownFinished.connect(window.close)
        tray = TrayController(application, window, controller)

        def item(name):
            found = window.findChild(QObject, name)
            assert found is not None, name
            return found

        def invoke(obj, method):
            assert QMetaObject.invokeMethod(obj, method, Qt.ConnectionType.DirectConnection), method

        def click(name):
            # On Windows the state signal arrives before the resized frame is laid out.
            QTest.qWait(150)
            button = item(name)
            point = button.mapToScene(QPointF(button.width() / 2, button.height() / 2)).toPoint()
            QTest.mouseClick(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, point)
            application.processEvents()

        try:
            wait(lambda: not controller.busy and controller.library.model.count == 1)
            window.setProperty("currentScreen", 1)
            window.setWidth(680)
            window.setHeight(480)
            wait(lambda: window.isVisible() and not item("normalPlaceTimer").property("running"))
            assert window.flags() & Qt.WindowType.FramelessWindowHint
            assert not application.quitOnLastWindowClosed()
            assert tray.icon.isVisible()
            assert [a.text() for a in tray._menu.actions() if not a.isSeparator()] == ["LuvBox 열기", "설정", "종료"]
            # Minimum width: all window controls and Settings fit without overlapping.
            settings = item("settingsButton")
            minimize = item("windowMinimizeButton")
            settings_right = settings.mapToScene(QPointF(settings.width(), 0)).x()
            assert settings_right <= minimize.mapToScene(QPointF()).x(), (settings_right, minimize.mapToScene(QPointF()).x())
            window.grabWindow().save(str(destination.with_suffix(".png")))
            click("windowMinimizeButton")
            wait(lambda: window.visibility() == QWindow.Visibility.Minimized)
            tray._activated(QSystemTrayIcon.ActivationReason.Trigger)
            wait(lambda: window.visibility() == QWindow.Visibility.Windowed)
            click("windowMaximizeButton")
            wait(lambda: window.visibility() == QWindow.Visibility.Maximized)
            # The maximized window may have moved away from its old normal screen.
            window.setProperty("normalPlace", QRectF(-5000, 20, 680, 480))
            # A background save survives the caption close unchanged.
            controller.download._running = True
            click("windowCloseButton")
            wait(lambda: not window.isVisible())
            assert window.screen().geometry().contains(window.property("normalPlace").center().toPoint())
            assert not controller.closing and controller.download.running
            tray._open.trigger()
            wait(lambda: window.visibility() == QWindow.Visibility.Maximized)
            controller.download._running = False
            controller.download.changed.emit()
            wait(lambda: not controller.busy)
            click("windowMaximizeButton")
            wait(lambda: window.visibility() == QWindow.Visibility.Windowed)
            wait(lambda: (window.width(), window.height()) == (680, 480))
            # A modal dialog must neither cover nor swallow the caption controls.
            tray._settings.trigger()
            wait(lambda: item("preferencesDialog").property("visible"))
            window.grabWindow().save(str(destination.with_name(destination.stem + "-settings.png")))
            click("windowCloseButton")
            wait(lambda: not window.isVisible())
            assert not controller.closing
            tray.show()
            wait(lambda: window.isVisible())
            assert item("preferencesDialog").property("visible")
            invoke(item("preferencesDialog"), "close")
            # A preview keeps its selected photo, but releases hidden video/GIF work.
            controller.library.selectItem(controller.library.model._rows[0]["key"])
            wait(lambda: bool(controller.library.selectedKey) and not controller.library.selectionBusy)
            invoke(item("previewDialog"), "open")
            wait(lambda: item("selectedMedia").property("playing"))
            window.grabWindow().save(str(destination.with_name(destination.stem + "-preview.png")))
            window.close()  # Same QWindow closing event as Alt+F4 / taskbar Close.
            wait(lambda: not window.isVisible())
            assert not item("selectedMedia").property("playing")
            assert not controller.closing
            tray.show()
            wait(lambda: item("selectedMedia").property("playing"))
            invoke(item("previewDialog"), "close")
            # New interactive questions bring a hidden save back where it can be answered.
            window.close()
            wait(lambda: not window.isVisible())
            controller.storageConfirmationRequested.emit()
            wait(lambda: window.isVisible())
            invoke(item("storageDialog"), "close")
            window.close()
            wait(lambda: not window.isVisible())
            tray._exit.trigger()
            wait(lambda: controller.shutdownReady)
            assert not tray.icon.isVisible()
            assert not window.isVisible()
            assert not tray._exit.isEnabled()
            failures = [m for m in messages if any(s in m for s in (
                "ReferenceError", "TypeError", "Cannot assign", "Binding loop", "failed to load component"))]
            assert not failures, failures
            report = {"ok": True, "platform": application.platformName(), "qml_errors": failures,
                      "close_keeps_save": True, "modal_caption": True, "restore_maximized": True,
                      "hidden_preview_released": True, "tray_exit": True}
            destination.write_text(json.dumps(report, indent=2), encoding="utf-8")
            return report
        finally:
            controller.download._running = False
            controller.requestClose()
            wait(lambda: controller.shutdownReady)
            tray.close()
            wheel.close()
            controller.library.provider = None
            delete(engine)
            qInstallMessageHandler(previous)


class TraySceneTests(unittest.TestCase):
    def test_caption_and_tray_lifecycle(self):
        with tempfile.TemporaryDirectory(prefix="luvbox-tray-report-") as folder:
            run = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scene", str(Path(folder) / "tray.json")],
                                 cwd=ROOT, env={**os.environ, "QT_QPA_PLATFORM": "offscreen",
                                               "QT_QUICK_BACKEND": "software"}, capture_output=True,
                                 text=True, encoding="utf-8", errors="replace", timeout=90)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--scene":
        print(json.dumps(scene(sys.argv[2])))
    else:
        unittest.main()

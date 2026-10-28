"""The real schedule panel stays reachable for empty, loading and error states."""

from importlib.util import find_spec
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(find_spec("PySide6") is not None, "Qt dependencies are not installed")
class SchedulePanelTests(unittest.TestCase):
    def test_empty_loading_error_and_create_are_reachable_in_real_qml(self):
        script = r'''
from copy import deepcopy
from pathlib import Path
import threading
import time
from unittest.mock import patch
from PySide6.QtCore import QObject, Property, QMetaObject, QUrl
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuickControls2 import QQuickStyle
from shiboken6 import delete
from luvbox.qt_schedule import ScheduleController

QQuickStyle.setStyle("Basic")
gui = QGuiApplication([])
native = {"registered": False}
blocking = [False]
failed = [False]
entered, resume = threading.Event(), threading.Event()
registrations = []

def read():
    if blocking[0]:
        entered.set()
        assert resume.wait(3)
    if failed[0]:
        raise OSError("fixture only")
    return deepcopy(native)

def register(value):
    registrations.append(value)
    native.update(registered=True, enabled=True, current=True, running=False, time=value,
                  program="fixture.exe", next_run="2026-10-28T09:15:00")

def wait(condition):
    end = time.monotonic() + 5
    while time.monotonic() < end:
        gui.processEvents()
        if condition():
            return
        time.sleep(0.005)
    raise AssertionError("QML schedule fixture did not finish")

class App(QObject):
    @Property(QObject, constant=True)
    def schedule(self): return controller
    @Property(str, constant=True)
    def account(self): return "fixture"
    @Property(str, constant=True)
    def outputFolder(self): return str(Path.cwd() / "synthetic-media")
    @Property(bool, constant=True)
    def canStart(self): return True

with patch("luvbox.qt_schedule.task_status", side_effect=read), patch("luvbox.qt_schedule.register_task", side_effect=register):
    controller = ScheduleController()
    app = App()
    engine = QQmlApplicationEngine()
    errors = []
    engine.warnings.connect(lambda warnings: errors.extend(str(warning) for warning in warnings))
    engine.setInitialProperties({"testApp": app})
    engine.loadData(b''' + 'r"""' + r'''
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
ApplicationWindow {
    required property var testApp
    width: 680; height: 760; visible: true
    ScrollView {
        anchors.fill: parent
        ColumnLayout {
            width: parent.availableWidth
            SchedulePanel { app: testApp; Layout.fillWidth: true }
        }
    }
}
''' + '"""' + r''', QUrl.fromLocalFile(str(Path.cwd() / "assets/qt/schedule-fixture.qml")))
    assert engine.rootObjects(), errors
    root = engine.rootObjects()[0]
    wait(lambda: controller.known and not controller.busy)
    panel = root.findChild(QObject, "dailySchedulePanel")
    summary = root.findChild(QObject, "scheduleSummary")
    save = root.findChild(QObject, "scheduleSaveButton")
    refresh = root.findChild(QObject, "scheduleRefreshButton")
    assert panel.property("visible") and save.property("visible") and save.property("enabled")
    assert "예약이 없습니다" in summary.property("text")
    blocking[0] = True
    assert controller.refresh()
    assert entered.wait(2)
    gui.processEvents()
    assert panel.property("visible") and save.property("visible") and not save.property("enabled")
    blocking[0] = False
    resume.set()
    wait(lambda: not controller.busy)
    assert panel.property("visible") and save.property("visible")
    failed[0] = True
    controller.refresh()
    wait(lambda: not controller.busy)
    assert panel.property("visible") and refresh.property("enabled") and not save.property("enabled")
    failed[0] = False
    controller.refresh()
    wait(lambda: controller.known and not controller.busy)
    root.findChild(QObject, "scheduleTimeField").setProperty("text", "09:15")
    assert QMetaObject.invokeMethod(save, "clicked")
    confirmation = root.findChild(QObject, "scheduleConfirmation")
    assert confirmation.property("visible")
    assert QMetaObject.invokeMethod(confirmation, "accept")
    wait(lambda: not controller.busy)
    assert registrations == ["09:15"]
    assert controller.registered and "09:15" in summary.property("text")
    assert not errors, errors
    controller.shutdown()
    wait(lambda: not controller._worker.is_alive())
    root.close()
    delete(engine)
'''
        with tempfile.TemporaryDirectory(prefix="luvbox-schedule-qml-") as folder:
            result = subprocess.run([sys.executable, "-c", script], cwd=ROOT,
                                    env={**os.environ, "LOCALAPPDATA": folder, "QT_QPA_PLATFORM": "offscreen"},
                                    capture_output=True, timeout=20,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))


if __name__ == "__main__":
    unittest.main()

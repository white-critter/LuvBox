"""Exercise the real Qt window with isolated records and no account or scheduler access."""

import ctypes
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import traceback

from . import __version__


class StartupProbe:
    def __init__(self, report, destination):
        self.report = report
        self.destination = destination
        self.completed = False
        self.attempts = 0

    def attach(self, application, controller, window):
        from PySide6.QtCore import QTimer

        self.application = application
        self.controller = controller
        self.window = window
        self.report["platform"] = application.platformName()
        self.report["stage"] = "window"
        controller.shutdownFinished.connect(self._closed)
        self.watchdog = QTimer(application)
        self.watchdog.setSingleShot(True)
        self.watchdog.timeout.connect(lambda: self.fail("Qt startup check timed out."))
        self.watchdog.start(25000)
        self.later(600, self.check_window)

    def later(self, delay, action):
        from PySide6.QtCore import QTimer

        def guarded():
            try:
                action()
            except Exception:
                self.fail(traceback.format_exc())

        QTimer.singleShot(delay, guarded)

    def fail(self, message):
        from PySide6.QtCore import QTimer

        if "error" not in self.report:
            self.report["error"] = message
        self.watchdog.stop()
        self.controller.requestClose()
        QTimer.singleShot(8000, lambda: self.application.exit(1))

    def _closed(self):
        self.report["shutdown_ready"] = self.controller.shutdownReady

    def find(self, name):
        from PySide6.QtCore import QObject

        item = self.window.findChild(QObject, name)
        if item is None:
            raise RuntimeError("Qt object not found: " + name)
        return item

    @staticmethod
    def invoke(item, action):
        from PySide6.QtCore import QMetaObject, Qt

        if not QMetaObject.invokeMethod(item, action, Qt.ConnectionType.DirectConnection):
            raise RuntimeError("Qt action failed: " + action)

    def check_window(self):
        if not self.window.isVisible() or not self.window.isExposed():
            self.attempts += 1
            if self.attempts > 15:
                raise RuntimeError("Qt window was not exposed.")
            self.later(150, self.check_window)
            return
        if self.window.property("currentScreen") != 0:
            raise RuntimeError("The initial screen is not Save.")
        self.report.update(window_visible=True, window_exposed=True,
                           title=self.window.title(), save_screen=True,
                           renderer=str(self.window.rendererInterface().graphicsApi()))
        self.window.setProperty("currentScreen", 1)
        self.report["stage"] = "gallery"
        self.attempts = 0
        self.later(150, self.check_gallery)

    def check_gallery(self):
        model = self.controller.library.model
        if model.error:
            raise RuntimeError(model.error)
        if model.count != 1 or not model._ready:
            self.attempts += 1
            if self.attempts > 45:
                raise RuntimeError("The real gallery did not load its fixture thumbnail.")
            self.later(100, self.check_gallery)
            return
        grid = self.find("galleryGrid")
        if grid.property("width") <= 0 or grid.property("height") <= 0:
            raise RuntimeError("The gallery has no visible viewport.")
        self.report.update(gallery_items=model.count, gallery_thumbnail_ready=True)
        screenshot = self.window.grabWindow()
        if screenshot.isNull() or not screenshot.save(str(self.destination.with_suffix(".png"))):
            raise RuntimeError("The native Qt window did not produce a frame.")
        self.report["screenshot"] = str(self.destination.with_suffix(".png"))
        self.controller.library.selectItem(model._rows[0]["key"])
        self.attempts = 0
        self.later(100, self.open_preview)

    def open_preview(self):
        library = self.controller.library
        if library.busy or not library.selectedKey:
            self.attempts += 1
            if self.attempts > 30:
                raise RuntimeError("The gallery fixture could not be selected.")
            self.later(100, self.open_preview)
            return
        if library.selectedMissing:
            raise RuntimeError("The gallery fixture is unexpectedly missing.")
        self.invoke(self.find("previewDialog"), "open")
        self.report["stage"] = "photo-preview"
        self.attempts = 0
        self.later(150, self.check_preview)

    def check_preview(self):
        media = self.find("selectedMedia")
        if media.property("failed"):
            raise RuntimeError("The photo preview failed.")
        if not media.property("ready"):
            self.attempts += 1
            if self.attempts > 30:
                raise RuntimeError("The photo preview did not become ready.")
            self.later(100, self.check_preview)
            return
        self.report["photo_preview_ready"] = True
        self.invoke(self.find("previewDialog"), "close")
        self.invoke(self.find("preferencesDialog"), "open")
        self.later(200, self.check_preferences)

    def check_preferences(self):
        preferences = self.find("preferencesDialog")
        if not preferences.property("visible"):
            raise RuntimeError("Settings did not open.")
        self.invoke(self.find("scheduleConfirmation"), "open")
        self.later(150, self.check_confirmation)

    def check_confirmation(self):
        confirmation = self.find("scheduleConfirmation")
        if not confirmation.property("visible"):
            raise RuntimeError("The schedule confirmation did not open.")
        self.invoke(confirmation, "reject")
        self.invoke(self.find("preferencesDialog"), "close")
        self.report["settings_and_confirmation"] = True
        self.invoke(self.find("connectionDialog"), "open")
        self.later(200, self.check_connection)

    def check_connection(self):
        connection = self.find("connectionDialog")
        if not connection.property("visible"):
            raise RuntimeError("The account connection dialog did not open.")
        self.invoke(connection, "close")
        self.report["connection_dialog"] = True
        self.report["stage"] = "shutdown"
        self.completed = True
        self.watchdog.stop()
        self.controller.requestClose()
        self.later(8000, lambda: self.application.exit(1))


def _fixture(directory):
    from .history import HistoryStore
    from .storage import resolve_storage

    output = directory / "media"
    output.mkdir()
    image = output / "smoketest_100_1.png"
    shutil.copyfile(Path(__file__).resolve().parents[1] / "assets/icons/luvbox.png", image)
    preferences = directory / "LuvBox/settings.json"
    preferences.parent.mkdir()
    preferences.write_text(json.dumps({"account": "smoketest", "output_dir": str(output)}), encoding="utf-8")
    paths = resolve_storage(output, "smoketest", False)
    store = HistoryStore(paths.record_dir, media_dir=paths.media_dir,
                         output_dir=paths.output_dir, legacy_dir=paths.legacy_dir)
    try:
        store.record_media("flat", {"tweet_id": "100", "num": 1, "type": "photo",
                                    "author": {"name": "smoketest", "id": "101"}}, image)
    finally:
        store.close()


def run(destination):
    destination = Path(destination).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    report = {"ok": False, "version": __version__, "frozen": bool(getattr(sys, "frozen", False)),
              "stage": "qt-import", "isolated": True, "native_scheduler_access": False,
              "authentication_or_network": False}
    previous_environment = {name: os.environ.get(name) for name in ("LOCALAPPDATA", "APPDATA", "QT_QPA_PLATFORM")}
    messages = []
    previous_handler = None
    handler_installed = False
    original_task_status = None
    temporary = tempfile.TemporaryDirectory(prefix="luvbox-qt-startup-")
    try:
        if os.name == "nt":
            ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x0002 | 0x8000)
        os.environ["LOCALAPPDATA"] = temporary.name
        os.environ["APPDATA"] = str(Path(temporary.name) / "roaming")
        os.environ["QT_QPA_PLATFORM"] = "windows" if os.name == "nt" else "offscreen"
        from PySide6.QtCore import qInstallMessageHandler

        previous_handler = qInstallMessageHandler(lambda severity, context, message: messages.append(message))
        handler_installed = True
        from . import qt_schedule

        original_task_status = qt_schedule.task_status
        qt_schedule.task_status = lambda: {"registered": False}
        report["stage"] = "fixture"
        _fixture(Path(temporary.name))
        from .qt_app import launch

        probe = StartupProbe(report, destination)
        report["stage"] = "qml-load"
        exit_code = launch(startup_probe=probe)
        report["exit_code"] = exit_code
        report["qml_errors"] = [message for message in messages if re.search(
            r"ReferenceError|TypeError|failed to load component|is not a type|Cannot assign|Cannot read|Binding loop", message)]
        report["ok"] = (exit_code == 0 and probe.completed and report.get("shutdown_ready") is True
                        and not report.get("error") and not report["qml_errors"])
    except Exception:
        report["error"] = traceback.format_exc()
    finally:
        if original_task_status is not None:
            qt_schedule.task_status = original_task_status
        if handler_installed:
            qInstallMessageHandler(previous_handler)
        report["qt_messages"] = messages
        try:
            temporary.cleanup()
            report["temporary_data_removed"] = True
        except OSError:
            report["temporary_data_removed"] = False
            report["ok"] = False
        for name, value in previous_environment.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if report["ok"] else 1

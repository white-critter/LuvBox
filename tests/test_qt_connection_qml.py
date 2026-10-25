"""Exercise the product connection dialog with real Qt controllers, offline."""

from contextlib import ExitStack
from copy import deepcopy
from importlib.util import find_spec
import json
import os
from pathlib import Path
from queue import Empty, Queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def check_scene(screenshot_directory=None):
    sys.path.insert(0, str(ROOT))
    from PySide6.QtCore import (QCoreApplication, QEvent, QMetaObject, QObject,
                                Property, QUrl, QtMsgType, qInstallMessageHandler)
    from PySide6.QtGui import QColor, QFont, QFontDatabase, QGuiApplication, QPalette
    from PySide6.QtQml import QQmlApplicationEngine
    from PySide6.QtQuick import QQuickItem
    from PySide6.QtQuickControls2 import QQuickStyle
    from shiboken6 import delete
    from luvbox.browser_bridge import BrowserBridgeError, BrowserSession
    from luvbox.qt_accounts import AccountController
    from luvbox.qt_browser_setup import BrowserSetupController

    QQuickStyle.setStyle("Basic")
    application = QGuiApplication([])
    palette = QPalette()
    for role, color in ((QPalette.ColorRole.Window, "#101010"),
                        (QPalette.ColorRole.WindowText, "#f4f1f1"),
                        (QPalette.ColorRole.Text, "#f4f1f1"),
                        (QPalette.ColorRole.ButtonText, "#f4f1f1"),
                        (QPalette.ColorRole.PlaceholderText, "#b9afb1"),
                        (QPalette.ColorRole.Highlight, "#d93645")):
        palette.setColor(role, QColor(color))
    application.setPalette(palette)
    QFontDatabase.addApplicationFont(str(ROOT / "assets/fonts/Pretendard/Pretendard-Regular.ttf"))
    font = QFont("Pretendard")
    font.setPixelSize(14)
    application.setFont(font)
    messages = []
    def record_message(kind, _context, message):
        if kind in (QtMsgType.QtWarningMsg, QtMsgType.QtCriticalMsg, QtMsgType.QtFatalMsg):
            messages.append(message)
    previous_handler = qInstallMessageHandler(record_message)
    engine = window = controller = setup = None
    commands = Queue()
    settings_lock = threading.Lock()
    state = {"accounts": [], "identity": {}, "connected": False}
    opened_browsers = []
    checks = []
    screenshots = []

    class MemorySettings:
        def __init__(self):
            self.values = {"connection_browser": "chrome"}
        def load(self):
            with settings_lock:
                return deepcopy(self.values)
        def update(self, changes):
            with settings_lock:
                self.values.update(changes)
                return deepcopy(self.values)
    settings = MemorySettings()

    class SyntheticBridge:
        """A controlled source of extension replies; no socket or browser."""
        pairing_code = "TEST118X"
        port = 12345
        def __init__(self, **_kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *_args): pass
        def open_browser(self, *, opener):
            if not opener("http://127.0.0.1:12345/connect#synthetic-test-only"):
                raise AssertionError("synthetic opener failed")
        def wait(self, *, cancel, on_progress):
            while not cancel.is_set():
                try:
                    command = commands.get(timeout=.025)
                except Empty:
                    continue
                if command == "session":
                    return BrowserSession("synthetic-only-token", "offline_user", "123", "오프라인 테스트")
                on_progress(command)
            raise BrowserBridgeError("로그인 연결을 취소했습니다.")

    def read_state():
        with settings_lock:
            return deepcopy(state)

    def save_browser(_token, identity, **_kwargs):
        with settings_lock:
            state.update(accounts=[dict(identity)], identity=dict(identity), connected=True)

    def save_token(_token):
        with settings_lock:
            state.update(accounts=[], identity={}, connected=True)

    def open_browser(identifier, url=""):
        opened_browsers.append((identifier, url))
        return True

    class AppFixture(QObject):
        @Property(QObject, constant=True)
        def browserSetup(self): return setup
        @Property(bool, constant=True)
        def closing(self): return False

    def settle(duration=.08):
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            application.processEvents()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            time.sleep(.002)

    def wait_for(condition, label, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            application.processEvents()
            if condition():
                settle()
                return
            time.sleep(.002)
        raise AssertionError(label + " did not settle")

    def invoke(item, method):
        if not QMetaObject.invokeMethod(item, method):
            raise AssertionError("QML method missing: " + method)

    def click(item):
        if not item.property("visible") or not item.property("enabled"):
            raise AssertionError("QML button is unavailable: " + str(item.property("text")))
        invoke(item, "clicked")

    def capture(label):
        if screenshot_directory is None:
            return
        directory = Path(screenshot_directory).resolve()
        directory.mkdir(parents=True, exist_ok=True)
        settle(.15)
        target = directory / ("connection-" + label + ".png")
        image = window.grabWindow()
        if image.isNull() or not image.save(str(target)):
            raise AssertionError("connection screenshot could not be saved")
        screenshots.append(str(target))

    def shutdown_controllers():
        if controller is not None:
            controller.shutdown()
        if setup is not None:
            setup.shutdown()
        wait_for(lambda: (controller is None or controller._shutdown_emitted)
                 and (setup is None or setup._shutdown_emitted), "controller shutdown")

    try:
        with tempfile.TemporaryDirectory(prefix="luvbox-connection-qml-") as temporary, ExitStack() as stack:
            # Every storage/session/browser boundary is isolated. Real Qt
            # controllers, worker signals, timers and product QML remain active.
            stack.enter_context(patch("luvbox.qt_browser_setup.available_browsers", return_value=[
                {"id": "chrome", "label": "Google Chrome"}, {"id": "edge", "label": "Microsoft Edge"},
                {"id": "firefox", "label": "Mozilla Firefox"}]))
            stack.enter_context(patch("luvbox.qt_browser_setup.app_directory", return_value=Path(temporary)))
            stack.enter_context(patch("luvbox.qt_accounts.BrowserBridge", SyntheticBridge))
            stack.enter_context(patch("luvbox.qt_accounts.open_browser_url", side_effect=open_browser))
            stack.enter_context(patch("luvbox.qt_browser_setup.open_browser_url", side_effect=open_browser))
            stack.enter_context(patch.object(AccountController, "_read_state", new=staticmethod(read_state)))
            store = stack.enter_context(patch("luvbox.qt_accounts.login.save_authenticated_session", side_effect=save_browser))
            token_store = stack.enter_context(patch("luvbox.qt_accounts.login.save_auth_token", side_effect=save_token))
            setup = BrowserSetupController(settings=settings)
            # This runs before the mocks are restored, including when a scene
            # assertion fails while a worker is still waiting for consent.
            stack.callback(shutdown_controllers)
            controller = AccountController(settings=settings, browser_setup=setup)
            fixture = AppFixture()
            engine = QQmlApplicationEngine()
            engine.rootContext().setContextProperty("fixtureApp", fixture)
            engine.rootContext().setContextProperty("fixtureAccounts", controller)
            engine.loadData(b'''import QtQuick
import QtQuick.Controls
import "."
ApplicationWindow {
    id: window
    width: 720; height: 760; visible: true
    title: "LuvBox connection check - synthetic account"
    color: "#101010"
    property bool scrollBarVisible: connection.contentItem.ScrollBar.vertical.visible
    property int scrollBarPolicy: connection.contentItem.ScrollBar.vertical.policy
    ConnectionDialog {
        id: connection; objectName: "fixtureConnection"
        parent: Overlay.overlay
        accounts: fixtureAccounts; app: fixtureApp
    }
    function openConnection() { connection.open() }
}
''', QUrl.fromLocalFile(str(ROOT / "assets/qt/connection-test.qml")))
            if not engine.rootObjects():
                raise AssertionError("product connection dialog failed to load: " + str(messages))
            window = engine.rootObjects()[0]
            wait_for(lambda: not setup.busy and setup.selectedBrowser == "chrome", "browser discovery")
            invoke(window, "openConnection")
            dialog = window.findChild(QObject, "fixtureConnection")
            combo = dialog.findChild(QQuickItem, "connectionBrowser")
            connect = dialog.findChild(QQuickItem, "connectBrowserButton")
            approve = dialog.findChild(QQuickItem, "approveConnectionButton")
            if any(item is None for item in (dialog, combo, connect, approve)):
                raise AssertionError("product connection controls are missing")
            wait_for(lambda: not setup.busy and connect.property("enabled"), "extension preparation")
            if combo.property("currentValue") != "chrome" or combo.property("currentText") != "Google Chrome":
                raise AssertionError("browser combo did not bind its initial model/selection")
            capture("initial")
            # Change the backing selection, then invoke the product's activated
            # handler in QML to change it back, covering both binding directions.
            if not setup.setBrowser("edge"):
                raise AssertionError("browser selection was rejected")
            wait_for(lambda: not setup.busy and combo.property("currentValue") == "edge", "browser model update")
            from PySide6.QtCore import Q_ARG
            combo.setProperty("currentIndex", 2)
            if not QMetaObject.invokeMethod(combo, "activated", Q_ARG(int, 2)):
                raise AssertionError("browser activation signal is missing")
            wait_for(lambda: not setup.busy and setup.selectedBrowser == "firefox", "QML browser selection")
            checks.append("browser-selection-both-directions")
            click(connect)
            wait_for(lambda: controller.connectionStage == "extension_wait", "browser launch")
            if opened_browsers != [("firefox", "http://127.0.0.1:12345/connect#synthetic-test-only")]:
                raise AssertionError("QML connection did not use the selected browser")
            # Advance the silence threshold without spending eight seconds;
            # the actual production QTimer opens the help section.
            controller._extension_wait_started = time.monotonic() - 9
            wait_for(lambda: controller.setupNeeded, "extension silence")
            help_toggle = next((item for item in dialog.findChildren(QObject)
                                if item.property("checkable") is True
                                and "확장 설치·연결 도움말" in str(item.property("text"))), None)
            if help_toggle is None or not help_toggle.property("checked"):
                raise AssertionError("extension silence did not expand installation help")
            checks.append("extension-silence-opens-help")
            help_toggle.setProperty("checked", False)
            remaining = controller.secondsRemaining
            controller._deadline -= 1.1
            wait_for(lambda: controller.secondsRemaining < remaining, "silence countdown tick")
            if help_toggle.property("checked"):
                raise AssertionError("countdown reopened help after the user collapsed it")
            checks.append("countdown-preserves-collapsed-help")
            help_toggle.setProperty("checked", True)
            window.setWidth(400)
            window.setHeight(390)
            settle(.15)
            scroll = dialog.property("contentItem")
            if not (scroll.property("contentHeight") > scroll.property("height") + 2
                    and window.property("scrollBarVisible") and window.property("scrollBarPolicy") == 2):
                raise AssertionError("small-window connection content did not retain an always-visible scrollbar")
            checks.append("small-window-scrollbar")
            capture("small-help")
            window.setWidth(720)
            window.setHeight(760)
            commands.put("extension_ready")
            commands.put("login_required")
            wait_for(lambda: controller.connectionStage == "login_required", "login-required progress")
            if help_toggle.property("checked") or controller.setupNeeded:
                raise AssertionError("responding extension did not collapse installation help")
            help_toggle.setProperty("checked", True)
            remaining = controller.secondsRemaining
            controller._deadline -= 1.1
            wait_for(lambda: controller.secondsRemaining < remaining, "ready countdown tick")
            if not help_toggle.property("checked"):
                raise AssertionError("countdown collapsed help after the user expanded it")
            checks.append("countdown-preserves-expanded-help")
            help_toggle.setProperty("checked", False)
            login_button = next((item for item in dialog.findChildren(QObject)
                                 if item.property("text") == "선택한 브라우저에서 X 열기"), None)
            if login_button is None:
                raise AssertionError("login-required recovery button is missing")
            click(login_button)
            wait_for(lambda: not setup.busy, "selected-browser X recovery")
            if opened_browsers[-1] != ("firefox", "https://x.com/home"):
                raise AssertionError("login-required recovery opened the wrong browser")
            checks.append("typed-login-required-recovery")
            status_labels = [item for item in dialog.findChildren(QQuickItem)
                             if item.property("visible") and item.property("text") == controller.status]
            if not status_labels or any(item.height() + 1 < item.property("implicitHeight") for item in status_labels):
                raise AssertionError("connection recovery status text was clipped")
            capture("login-required")
            commands.put("checking_account")
            commands.put("awaiting_approval")
            commands.put("session")
            wait_for(lambda: controller.approvalPending and approve.property("visible"), "app approval")
            if store.called or not dialog.property("visible") or not controller.busy:
                raise AssertionError("session was saved before the app's explicit final approval")
            capture("approval")
            click(approve)
            wait_for(lambda: not controller.busy and not dialog.property("visible"), "approved connection")
            if store.call_count != 1 or controller.authenticationState != "browser_confirmed":
                raise AssertionError("explicit approval did not save the browser-confirmed connection")
            checks.append("explicit-final-consent-closes-dialog")
            invoke(window, "openConnection")
            wait_for(lambda: dialog.property("visible") and not setup.busy, "token dialog reopening")
            dialog.setProperty("methodIndex", 1)
            settle()
            token = next((item for item in dialog.findChildren(QObject)
                          if item.property("maximumLength") == 4096
                          and item.property("placeholderText") == "auth_token 값"), None)
            if token is None:
                raise AssertionError("advanced token field is missing")
            token.setProperty("text", "synthetic-only-token")
            invoke(dialog, "submitToken")
            wait_for(lambda: not controller.busy, "token storage")
            if token_store.call_count != 1 or token.property("text") or not dialog.property("visible"):
                raise AssertionError("token was not cleared/stored or incorrectly closed the dialog")
            if controller.authenticationState != "saved" or "실제 X 인증" not in controller.status:
                raise AssertionError("local token storage incorrectly claimed authentication success")
            checks.append("token-storage-remains-open-and-unverified")
            capture("token-saved")
            qml_errors = [message for message in messages if any(
                marker in message for marker in (".qml", "QQml", "ReferenceError", "TypeError", "Binding loop"))]
            if qml_errors:
                raise AssertionError("QML warnings/errors: " + str(qml_errors))
            return {"checks": checks, "qml_errors": qml_errors, "screenshots": screenshots,
                    "browser": setup.selectedBrowser, "token_state": controller.authenticationState}
    finally:
        shutdown_controllers()
        if window is not None:
            window.close()
        if engine is not None:
            delete(engine)
        settle()
        qInstallMessageHandler(previous_handler)


@unittest.skipUnless(find_spec("PySide6") is not None, "Qt dependencies are not installed")
class ConnectionQmlTests(unittest.TestCase):
    def test_product_connection_flow_and_small_window_recovery(self):
        environment = dict(os.environ, QT_QPA_PLATFORM="offscreen", QT_QUICK_BACKEND="software",
                           PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
        result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scene"],
                                cwd=ROOT, env=environment, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=35)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(len(report["checks"]), 8)
        self.assertEqual(report["qml_errors"], [])
        self.assertEqual(report["browser"], "firefox")
        self.assertEqual(report["token_state"], "saved")


if __name__ == "__main__":
    if "--scene" in sys.argv:
        screenshots = sys.argv[sys.argv.index("--screenshots") + 1] if "--screenshots" in sys.argv else None
        print(json.dumps(check_scene(screenshots), ensure_ascii=False))
    else:
        unittest.main()

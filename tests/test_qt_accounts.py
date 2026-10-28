"""Connection stages, consent, cancellation and saved-vs-verified login health."""

import importlib.util
import threading
import time
import unittest
from unittest.mock import Mock, patch

_QT = importlib.util.find_spec("PySide6") is not None
if _QT:
    from PySide6.QtCore import QCoreApplication
    from luvbox.qt_accounts import AccountController
    from luvbox.browser_bridge import BrowserSession


@unittest.skipUnless(_QT, "Qt dependencies are not installed")
class AccountConnectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def setUp(self):
        self.state = {"accounts": [], "identity": {}, "connected": False}
        self.reader = patch.object(AccountController, "_read_state", new=staticmethod(lambda: self.state.copy()))
        self.reader.start()
        self.addCleanup(self.reader.stop)
        self.settings = Mock()
        self.controller = AccountController(settings=self.settings)
        self.addCleanup(self.close)

    def wait(self, condition, seconds=4):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.app.processEvents()
            if condition():
                return
            time.sleep(.002)
        self.fail("connection operation did not settle")

    def close(self):
        self.controller.shutdown()
        self.wait(lambda: self.controller._shutdown_emitted)

    def test_account_refresh_cannot_mix_snapshots_from_different_app_windows(self):
        self.reader.stop()
        identity = {"user_id": "123", "handle": "first_user", "display_name": "First"}
        cookies = [{"name": "auth_token", "value": "synthetic-first-token", "domain": ".x.com",
                    "path": "/", "expires": -1, "secure": True, "httpOnly": True}]
        # An independent app window disconnects immediately after the first
        # read. Three separate reads used to show its empty active identity
        # alongside the previous snapshot's still-active account list.
        first = {"version": 1, "identity": identity, "cookies": cookies,
                 "accounts": [{"identity": identity, "cookies": cookies}]}
        later = {"version": 1, "cookies": [], "accounts": []}
        with patch("luvbox.login._read_payload", side_effect=[first, later, later]) as read:
            state = AccountController._read_state()
        self.assertEqual(state, {"accounts": [{**identity, "active": True}],
                                 "identity": identity, "connected": True})
        read.assert_called_once_with()

    def test_saved_token_does_not_claim_verified_login_or_close_the_dialog(self):
        completed = Mock()
        self.controller.connectionCompleted.connect(completed)
        def save(_token):
            self.state["connected"] = True
        with patch("luvbox.qt_accounts.login.save_auth_token", side_effect=save):
            self.assertTrue(self.controller.connectToken("synthetic-token"))
            self.wait(lambda: not self.controller.busy)
        self.assertTrue(self.controller.connected)
        self.assertEqual(self.controller.authenticationState, "saved")
        self.assertIn("아직", self.controller.authenticationLabel)
        completed.assert_not_called()
        self.controller.updateAuthentication("network_error")
        self.assertEqual(self.controller.authenticationState, "saved")
        self.controller.updateAuthentication("verified")
        self.assertEqual(self.controller.authenticationState, "verified")
        self.controller.updateAuthentication("reconnect_required")
        self.assertTrue(self.controller.connected)  # Preserve the encrypted session.
        self.assertEqual(self.controller.authenticationState, "reconnect_required")

    def test_connection_keeps_the_selected_browser_and_requires_final_consent(self):
        ready = threading.Event()
        opened = Mock(return_value=True)
        class Bridge:
            pairing_code = "ABCD1234"
            port = 12345
            def __init__(self, **_kwargs): pass
            def __enter__(self): return self
            def __exit__(self, *_args): pass
            def open_browser(self, *, opener): opener("http://127.0.0.1:12345/connect#synthetic")
            def wait(self, *, cancel, on_progress):
                on_progress("extension_ready")
                on_progress("checking_account")
                ready.set()
                return BrowserSession("synthetic-token", "example", "123", "Example")
        def save(_token, identity, **_kwargs):
            self.state.update(connected=True, identity=identity, accounts=[identity])
        with patch("luvbox.qt_accounts.BrowserBridge", Bridge), \
                patch("luvbox.qt_accounts.open_browser_url", opened), \
                patch("luvbox.qt_accounts.login.save_authenticated_session", side_effect=save) as store:
            self.assertTrue(self.controller.connectBrowser("firefox"))
            self.wait(lambda: self.controller.approvalPending)
            self.assertTrue(ready.is_set())
            self.assertEqual(self.controller.connectionStage, "app_approval")
            self.assertTrue(self.controller.extensionReady)
            store.assert_not_called()
            opened.assert_called_once_with("firefox", "http://127.0.0.1:12345/connect#synthetic")
            self.controller.approveBrowser()
            self.wait(lambda: not self.controller.busy)
            store.assert_called_once()
        self.assertEqual(self.controller.authenticationState, "browser_confirmed")
        self.assertEqual(self.controller.identity["handle"], "example")
        self.settings.update.assert_called_once_with({"account": "example",
                                                     "connection_browser": "firefox"})

    def test_cancel_before_approval_preserves_old_session(self):
        self.state.update(connected=True, identity={"user_id": "5", "handle": "old", "display_name": "Old"})
        self.controller._refresh()
        self.controller.updateAuthentication("reconnect_required")
        class Bridge:
            pairing_code = "ABCD1234"
            port = 12345
            def __init__(self, **_kwargs): pass
            def __enter__(self): return self
            def __exit__(self, *_args): pass
            def open_browser(self, **_kwargs): pass
            def wait(self, **_kwargs): return BrowserSession("synthetic", "new", "6", "New")
        with patch("luvbox.qt_accounts.BrowserBridge", Bridge), \
                patch("luvbox.qt_accounts.login.save_authenticated_session") as store:
            self.assertTrue(self.controller.connectBrowser("edge"))
            self.wait(lambda: self.controller.approvalPending)
            self.controller.cancelBrowser()
            self.controller.approveBrowser()
            self.wait(lambda: not self.controller.busy)
            store.assert_not_called()
        self.assertEqual(self.controller.identity["handle"], "old")
        self.assertTrue(self.controller.connected)
        self.assertEqual(self.controller.authenticationState, "reconnect_required")
        self.assertEqual(self.controller.secondsRemaining, 0)

    def test_extension_silence_offers_setup_without_claiming_it_is_uninstalled(self):
        self.controller._busy = True
        self.controller._operation = "browser"
        self.controller._receive({"kind": "pairing", "attempt": 0, "code": "ABCD1234", "url": ""})
        self.controller._extension_wait_started -= 9
        self.controller._retire_worker()
        self.assertTrue(self.controller.setupNeeded)
        self.assertIn("응답이 없습니다", self.controller.status)
        self.assertGreater(self.controller.secondsRemaining, 0)
        self.controller._receive({"kind": "progress", "attempt": 0, "stage": "login_required"})
        self.assertFalse(self.controller.setupNeeded)
        self.assertTrue(self.controller.extensionReady)
        self.assertEqual(self.controller.connectionStage, "login_required")
        self.controller._receive({"kind": "progress", "attempt": -1, "stage": "account_changed"})
        self.assertEqual(self.controller.connectionStage, "login_required")
        self.controller._receive({"kind": "progress", "attempt": 0, "stage": "arbitrary private message"})
        self.assertNotIn("private", self.controller.status)
        self.controller._busy = False
        self.controller._operation = ""

    def test_invalid_browser_does_not_start_a_connection(self):
        with patch("luvbox.qt_accounts.BrowserBridge") as bridge:
            self.assertFalse(self.controller.connectBrowser("C:/unknown.exe"))
            bridge.assert_not_called()
        self.assertFalse(self.controller.busy)
        self.assertIn("선택", self.controller.error)

    def test_shell_can_own_preferences_without_duplicate_account_writes(self):
        controller = AccountController(settings=self.settings, persist_preferences=False)
        try:
            def save(_token):
                self.state["connected"] = True
            self.settings.update.side_effect = OSError("settings must be written by the shell")
            with patch("luvbox.qt_accounts.login.save_auth_token", side_effect=save):
                self.assertTrue(controller.connectToken("synthetic-token"))
                self.wait(lambda: not controller.busy)
            self.assertTrue(controller.connected)
            self.assertEqual(controller.error, "")
            self.settings.update.assert_not_called()
        finally:
            controller.shutdown()
            self.wait(lambda: controller._shutdown_emitted)

    def test_selecting_same_account_cannot_clear_known_expiry(self):
        self.state.update(connected=True, identity={"user_id": "5", "handle": "old", "display_name": "Old"})
        self.controller._refresh()
        self.controller.updateAuthentication("reconnect_required")
        self.controller._complete_operation({"state": self.state.copy(), "success": True,
                                             "cancelled": False, "operation": "select"})
        self.assertEqual(self.controller.authenticationState, "reconnect_required")

    def test_closing_ignores_late_health_events(self):
        self.state["connected"] = True
        self.controller._refresh()
        self.controller.shutdown()
        self.controller.updateAuthentication("verified")
        self.assertEqual(self.controller.authenticationState, "saved")

    def test_failed_post_save_read_does_not_claim_browser_confirmed(self):
        self.controller._complete_operation({"state": self.state.copy(), "success": True,
            "cancelled": False, "operation": "browser", "warning": "저장된 연결을 읽지 못했습니다."})
        self.assertFalse(self.controller.connected)
        self.assertEqual(self.controller.authenticationState, "disconnected")
        self.assertIn("읽지 못했습니다", self.controller.error)


if __name__ == "__main__":
    unittest.main()

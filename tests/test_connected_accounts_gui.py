"""Connected identity selection and browser handoff with synthetic sessions."""

import threading
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tests import test_gui as gui_fixtures
from tests.legacy_ui import previous_window
from luvbox.gui import App, AUTH_MODES


ALICE = {"user_id": "101", "handle": "alice", "display_name": "앨리스"}
BOB = {"user_id": "202", "handle": "bob", "display_name": "밥"}


@previous_window
class ConnectedAccountsGuiTests(unittest.TestCase):
    def setUp(self):
        self.rows = []
        self.accounts = patch("luvbox.gui.list_accounts", side_effect=lambda: list(self.rows))
        self.accounts.start()
        self.addCleanup(self.accounts.stop)
        self.fixture = gui_fixtures.GuiTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.app, self.root = self.fixture.app, self.fixture.root
        self.addCleanup(self.__dict__.pop, "app", None)
        self.addCleanup(self.__dict__.pop, "root", None)

    def connected(self):
        self.rows[:] = [dict(ALICE, active=True), dict(BOB, active=False)]
        self.fixture.session_mock.return_value = True
        self.app._refresh_accounts()
        self.app._auth_changed()

    def test_login_result_automatically_selects_authenticated_account(self):
        self.connected()
        self.app.account.set("old_manual_value")
        self.app.record_locations["alice"] = self.fixture.temp.name
        self.app.show_connect()
        self.app._browser_result({"message": "", "identity": ALICE})
        self.assertEqual(self.app.account.get(), "alice")
        self.assertEqual(self.app.folder.get(), self.fixture.temp.name)
        self.assertEqual(str(self.app.account_entry.cget("state")), "readonly")
        self.assertEqual(tuple(self.app.account_entry.cget("values")), ("alice", "bob"))
        self.assertIsNone(self.app.connect_window)

    def test_dropdown_switches_session_and_restores_accounts_folder(self):
        self.connected()
        folder = str(Path(self.fixture.temp.name) / "밥의 사진")
        self.app.record_locations["bob"] = folder

        def select(user_id):
            self.rows[:] = [dict(ALICE, active=False), dict(BOB, active=True)]
            return BOB

        self.app.account.set("bob")
        with patch("luvbox.gui.select_account", side_effect=select) as selected:
            self.app._choose_account()
        selected.assert_called_once_with("202")
        self.assertEqual(self.app.account.get(), "bob")
        self.assertEqual(self.app.folder.get(), folder)
        self.assertEqual(self.app._active_identity["user_id"], "202")

    def test_running_job_cannot_switch_credentials(self):
        self.connected()
        self.app.running = True
        self.app.account.set("bob")
        with patch("luvbox.gui.select_account") as selected:
            self.app._choose_account()
        selected.assert_not_called()
        self.assertEqual(self.app.account.get(), "alice")
        self.app.running = False

    def test_known_identity_must_match_download_account(self):
        self.connected()
        self.app.account.set("bob")
        with self.assertRaisesRegex(ValueError, "계정"):
            self.app._validated_options()

    def test_disconnect_removes_only_selected_identity(self):
        self.connected()

        def remove(user_id):
            self.rows[:] = [dict(BOB, active=True)]

        with patch("luvbox.gui.remove_account", side_effect=remove) as removed, \
                patch("luvbox.gui.clear_session") as cleared:
            self.app.disconnect()
        removed.assert_called_once_with("101")
        cleared.assert_not_called()
        self.assertEqual(self.app.account.get(), "bob")

    def test_disconnect_write_failure_preserves_active_identity_and_reports_error(self):
        self.connected()
        with patch("luvbox.gui.remove_account", side_effect=ValueError("synthetic-secret")), \
                patch("luvbox.gui.dialogs.showerror") as error:
            self.app.disconnect()
        error.assert_called_once()
        self.assertNotIn("synthetic-secret", str(error.call_args))
        self.assertEqual(self.app._active_identity["user_id"], "101")

    def test_manual_disconnect_preserves_other_connected_accounts(self):
        self.rows[:] = [dict(ALICE, active=False), dict(BOB, active=False)]
        self.app._refresh_accounts()
        with patch("luvbox.gui.clear_active_session") as clear_active, \
                patch("luvbox.gui.clear_session") as clear_all:
            self.app.disconnect()
        clear_active.assert_called_once_with()
        clear_all.assert_not_called()

    def test_browser_worker_returns_identity_without_accessing_widgets(self):
        cancel = threading.Event()
        with patch("luvbox.browser_login.sign_in", return_value=ALICE):
            App._browser_worker(self.app.events, cancel)
        self.assertEqual(self.app.events.get_nowait()["identity"], ALICE)

    def test_extension_free_default_login_runs_off_ui_thread_and_prevents_other_attempts(self):
        self.app.show_connect()
        self.assertEqual(self.app.fresh_browser_button.cget("text"), "기본 브라우저에서 새 로그인")
        self.assertIn("확장 필요", self.app.default_browser_button.cget("text"))
        with patch("luvbox.gui.threading.Thread") as thread:
            self.app.connect_fresh_browser()
            self.app.connect_fresh_browser()
            self.app.connect_browser()
            self.app.connect_default_browser()
        thread.assert_called_once()
        self.assertTrue(self.app.login_busy)
        for button in (self.app.fresh_browser_button, self.app.browser_button, self.app.default_browser_button):
            self.assertEqual(str(button.cget("state")), "disabled")
        self.assertIs(thread.call_args.kwargs["target"], App._fresh_browser_worker)
        with patch("luvbox.browser_login.sign_in_default", return_value=ALICE) as sign_in:
            thread.call_args.kwargs["target"](*thread.call_args.kwargs["args"])
        sign_in.assert_called_once_with(self.app.login_cancel)
        self.assertEqual(self.app.events.get_nowait()["identity"], ALICE)
        self.app.login_busy = False

    def test_extension_free_login_failure_has_a_fixed_secret_free_message(self):
        cancel = threading.Event()
        with patch("luvbox.browser_login.sign_in_default", side_effect=RuntimeError("synthetic-secret")):
            App._fresh_browser_worker(self.app.events, cancel)
        event = self.app.events.get_nowait()
        self.assertIsNone(event["identity"])
        self.assertIn("다시 시도", event["message"])
        self.assertNotIn("synthetic-secret", event["message"])

    def test_default_browser_worker_saves_verified_session(self):
        cancel = threading.Event()
        session = Mock(auth_token="synthetic-token", identity=ALICE)
        with patch("luvbox.browser_bridge.BrowserBridge") as bridge_class, \
                patch("luvbox.gui.save_authenticated_session", return_value=ALICE) as save:
            bridge = bridge_class.return_value.__enter__.return_value
            bridge.wait.return_value = session
            App._default_browser_worker(self.app.events, cancel)
        bridge.open_browser.assert_called_once()
        save.assert_called_once_with("synthetic-token", ALICE, cancel=cancel)
        self.assertEqual(self.app.events.get_nowait()["type"], "browser-pairing")
        self.assertEqual(self.app.events.get_nowait()["identity"], ALICE)

    def test_cancelled_handoff_does_not_replace_previous_session(self):
        cancel = threading.Event()
        cancel.set()
        with patch("luvbox.browser_bridge.BrowserBridge"), \
                patch("luvbox.gui.save_authenticated_session") as save:
            App._default_browser_worker(self.app.events, cancel)
        save.assert_not_called()

    def test_pairing_code_displays_all_urlsafe_characters(self):
        self.app.show_connect()
        self.app.login_busy = True
        self.app._browser_pairing({"code": "AB_C-D12"})
        self.assertIn("AB_C-D12", self.app.browser_status.get())
        before = self.app.browser_status.get()
        self.app._browser_pairing({"code": "<script>"})
        self.assertEqual(self.app.browser_status.get(), before)
        self.app.login_busy = False

    def test_file_mode_restores_its_own_account_and_folder(self):
        self.rows[:] = [dict(BOB, active=True)]
        window = tk.Toplevel(self.root)
        window.withdraw()
        try:
            with patch("luvbox.gui.load_settings", return_value={
                    "auth_mode": "file", "account": "alice", "output_dir": self.fixture.temp.name,
                    "record_locations": {"bob": str(Path(self.fixture.temp.name) / "bob")}}):
                app = App(window)
            self.assertEqual(app.account.get(), "alice")
            self.assertEqual(app.folder.get(), self.fixture.temp.name)
        finally:
            window.destroy()

    def test_file_mode_refresh_and_dropdown_do_not_switch_browser_credentials(self):
        self.connected()
        self.app.auth.set(next(label for label, mode in AUTH_MODES.items() if mode == "file"))
        self.app.account.set("file_account")
        self.app._refresh_accounts()
        self.assertEqual(self.app.account.get(), "file_account")
        with patch("luvbox.gui.select_account") as select:
            self.app._choose_account()
        select.assert_not_called()
        self.app._browser_result({"message": "", "identity": BOB})
        self.assertEqual(self.app.account.get(), "file_account")

    def test_primary_buttons_keep_position_when_job_starts(self):
        self.root.geometry("1020x880")
        self.root.deiconify()
        self.root.update()
        before = [(button.winfo_rootx(), button.winfo_rooty(), button.winfo_width())
                  for button in (self.app.start_button, self.app.stop_button)]
        self.app.running = True
        self.app._set_running(True)
        self.root.update()
        after = [(button.winfo_rootx(), button.winfo_rooty(), button.winfo_width())
                 for button in (self.app.start_button, self.app.stop_button)]
        self.assertEqual(before, after)
        self.app.running = False

    def test_resize_by_one_pixel_keeps_navigation_and_content_origin(self):
        self.root.geometry("1020x880")
        self.root.deiconify()
        self.root.update()
        before = (self.app.content_view.winfo_rootx(), self.app.content_view.winfo_rooty())
        self.root.geometry("1019x880")
        self.root.update()
        self.assertEqual(before, (self.app.content_view.winfo_rootx(), self.app.content_view.winfo_rooty()))


if __name__ == "__main__":
    unittest.main()

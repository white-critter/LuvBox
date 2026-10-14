"""Dedicated login lifecycle and WebSocket tests, using only fake credentials."""

import base64
import hashlib
import json
from pathlib import Path
import struct
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from luvbox import browser_login as login


TOKEN = "offline-browser-token"


def cookie(**changes):
    return dict(name="auth_token", value=TOKEN, domain=".x.com", path="/",
                expires=-1, secure=True, httpOnly=True, **changes)


class BrowserLoginTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.addCleanup(patch.stopall)
        patch("luvbox.browser_login.app_directory", return_value=self.directory).start()
        patch("luvbox.browser_login._unused_port", return_value=12345).start()
        patch("luvbox.browser_login._browser_endpoint", return_value=(12345, "/devtools/browser/offline")).start()
        self.save = patch("luvbox.browser_login.save_auth_token").start()
        self.cancel = threading.Event()
        self.process = Mock()
        self.process.poll.return_value = None
        self.process.wait.return_value = 0

    def launch(self, args, **kwargs):
        self.args = args
        self.profile = Path(next(arg.split("=", 1)[1] for arg in args if arg.startswith("--user-data-dir=")))
        (self.profile / "DevToolsActivePort").write_text("12345\n/devtools/browser/offline\n")
        return self.process

    def call(self, port, path, method, **kwargs):
        self.assertEqual((port, path), (12345, "/devtools/browser/offline"))
        if method == "Storage.getCookies":
            return {"cookies": [cookie()]}
        if method == "Target.getTargets":
            return {"targetInfos": [{"type": "page"}]}
        return {}

    def sign_in(self, **kwargs):
        return login.sign_in(self.cancel, edge=Path("offline-edge.exe"), launch=self.launch, **kwargs)

    def test_success_encrypts_only_x_token_and_cleans_only_own_profile(self):
        other = self.directory / "login-profile-other-attempt"
        other.mkdir()
        (other / "marker").write_text("another running app")
        with patch("luvbox.browser_login.devtools_call", side_effect=self.call) as wire:
            self.sign_in()
        self.save.assert_called_once_with(TOKEN)
        self.assertFalse(self.profile.exists())
        self.assertTrue((other / "marker").exists())
        self.assertIn("--remote-debugging-address=127.0.0.1", self.args)
        self.assertIn("--remote-debugging-port=12345", self.args)
        self.assertNotIn("--remote-debugging-port=0", self.args)
        self.assertEqual(self.args[-1], login.LOGIN_URL)
        self.assertEqual(wire.call_args.args[-1], "Browser.close")

    def test_malformed_expired_other_domain_and_other_path_cookies_are_ignored(self):
        bad = [dict(cookie(), domain="x.com.evil"), dict(cookie(), path="/unrelated"),
               dict(cookie(), expires=time.time() - 30), dict(cookie(), expires="bad"),
               dict(cookie(), expires=float("nan")), dict(cookie(), domain="..x.com"), None]
        self.assertEqual(login._x_token(bad), "")
        self.assertEqual(login._x_token(bad + [cookie()]), TOKEN)

    def test_exited_edge_launcher_does_not_cancel_browser_and_browser_still_closes(self):
        self.process.poll.return_value = 0
        with patch("luvbox.browser_login.devtools_call", side_effect=self.call) as wire:
            self.sign_in()
        self.save.assert_called_once_with(TOKEN)
        self.assertEqual(wire.call_args.args[-1], "Browser.close")
        self.assertFalse(self.profile.exists())

    def test_cancel_after_cookie_read_never_replaces_existing_login(self):
        def call(*args, **kwargs):
            result = self.call(*args, **kwargs)
            if args[-1] == "Storage.getCookies":
                self.cancel.set()
            return result
        with patch("luvbox.browser_login.devtools_call", side_effect=call), \
                self.assertRaisesRegex(login.SignInError, login.CANCELLED):
            self.sign_in()
        self.save.assert_not_called()
        self.assertFalse(self.profile.exists())

    def test_cancel_before_start_does_not_launch_or_create_profile(self):
        self.cancel.set()
        with self.assertRaisesRegex(login.SignInError, login.CANCELLED):
            self.sign_in()
        self.assertFalse(hasattr(self, "profile"))

    def test_timeout_closes_browser_and_preserves_previous_session(self):
        with patch("luvbox.browser_login.devtools_call", side_effect=self.call), \
                self.assertRaisesRegex(login.SignInError, login.TIMED_OUT):
            self.sign_in(timeout=-1)
        self.save.assert_not_called()
        self.assertFalse(self.profile.exists())

    def test_cancel_during_startup_closes_handed_off_browser(self):
        def launch(args, **kwargs):
            result = self.launch(args, **kwargs)
            self.cancel.set()
            self.process.poll.return_value = 0
            return result
        with patch("luvbox.browser_login.devtools_call", side_effect=self.call) as wire, \
                self.assertRaisesRegex(login.SignInError, login.CANCELLED):
            login.sign_in(self.cancel, edge=Path("offline.exe"), launch=launch)
        self.assertEqual(wire.call_args.args[-1], "Browser.close")
        self.save.assert_not_called()
        self.assertFalse(self.profile.exists())

    def test_failed_close_stops_only_attempts_profile_processes(self):
        def call(*args, **kwargs):
            if args[-1] == "Browser.close":
                raise ConnectionError("offline")
            return self.call(*args, **kwargs)
        with patch("luvbox.browser_login.devtools_call", side_effect=call), \
                patch("luvbox.browser_login._stop_profile_processes") as stop:
            self.sign_in()
        stop.assert_called_once_with(self.profile)

    def test_blank_login_reloads_only_empty_x_login_page(self):
        pages = [{"url": "https://x.com/login", "targetId": "offline"},
                 {"url": "https://example.org/login", "targetId": "other"},
                 {"url": "https://x.com/home", "targetId": "home"}]
        for blank in (True, False, "true"):
            with self.subTest(blank=blank), \
                    patch("luvbox.browser_login.devtools_call", return_value={"result":{"value":{"ready":True,"blank":blank}}}) as wire:
                self.assertTrue(login._recover_blank_login(12345, pages))
                self.assertEqual(wire.call_count, 2 if blank is True else 1)
                self.assertEqual(wire.call_args.args[1], "/devtools/page/offline")
                if blank is True:
                    self.assertEqual(wire.call_args.args[-1], "Page.reload")
                    self.assertEqual(wire.call_args.kwargs["params"], {"ignoreCache":True})

    def test_login_recovery_waits_for_slow_document_without_spending_retry(self):
        pages = [{"url":login.LOGIN_URL,"targetId":"offline"}]
        with patch("luvbox.browser_login.devtools_call", return_value={"result":{"value":{"ready":False,"blank":True}}}) as wire:
            self.assertFalse(login._recover_blank_login(12345,pages))
        wire.assert_called_once()

    def test_partial_socket_receive_obeys_total_deadline(self):
        wire = Mock()
        wire.recv.return_value = b"a"
        with patch("luvbox.browser_login.time.monotonic", side_effect=[1,2,4]), \
                self.assertRaises(TimeoutError):
            login._receive_exact(wire, 3, 3)
        self.assertEqual(wire.recv.call_count, 2)
        self.assertEqual([call.args[0] for call in wire.settimeout.call_args_list], [2,1])

    def test_user_closes_all_pages_without_connecting(self):
        with patch("luvbox.browser_login.devtools_call", return_value={}), \
                self.assertRaisesRegex(login.SignInError, login.CLOSED):
            self.sign_in()
        self.save.assert_not_called()
        self.assertFalse(self.profile.exists())

    def test_no_edge_has_a_fixed_error_and_does_not_launch(self):
        with patch("luvbox.browser_login.find_edge", return_value=None), \
                self.assertRaisesRegex(login.SignInError, login.NO_EDGE):
            login.sign_in(self.cancel, launch=self.launch)
        self.save.assert_not_called()
        self.assertFalse(hasattr(self, "profile"))

    def test_policy_cannot_redirect_login_to_a_personal_profile(self):
        with patch("luvbox.browser_login._check_profile_policy", side_effect=login.SignInError(login.NO_DEBUGGING)), \
                self.assertRaises(login.SignInError):
            self.sign_in()
        self.assertFalse(hasattr(self, "profile"))
        self.save.assert_not_called()

    def test_launch_failure_removes_profile(self):
        def launch(args, **kwargs):
            self.launch(args, **kwargs)
            raise OSError("offline failure")
        with self.assertRaises(OSError):
            login.sign_in(self.cancel, edge=Path("offline.exe"), launch=launch)
        self.assertFalse(self.profile.exists())

    def test_storage_failure_still_closes_and_cleans_browser(self):
        self.save.side_effect = ValueError("암호化 저장 실패")
        with patch("luvbox.browser_login.devtools_call", side_effect=self.call), \
                self.assertRaises(login.SignInError):
            self.sign_in()
        self.assertFalse(self.profile.exists())
        self.process.wait.assert_called_once()

    def test_websocket_handles_partial_frames_ping_and_events(self):
        class Wire:
            def __init__(self):
                self.buffer = b""
                self.sent = []

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def settimeout(self, timeout):
                pass

            def sendall(self, data):
                self.sent.append(data)
                if data.startswith(b"GET "):
                    key = data.split(b"Sec-WebSocket-Key: ")[1].split(b"\r\n")[0]
                    accept = base64.b64encode(hashlib.sha1(key + login._WEBSOCKET_GUID.encode()).digest())
                    self.buffer = b"HTTP/1.1 101 Switching Protocols\r\nSec-WebSocket-Accept: " + accept + b"\r\n\r\n"
                elif len(self.sent) == 2:
                    result = json.dumps({"id": 1, "result": {"cookies": [cookie()]}}).encode()
                    middle = len(result) // 2
                    self.buffer += b'\x89\x02hi' + b'\x81\x02{}'
                    self.buffer += bytes([0x01, middle]) + result[:middle]
                    self.buffer += bytes([0x80, len(result) - middle]) + result[middle:]

            def recv(self, size):
                result, self.buffer = self.buffer[:min(size, 7)], self.buffer[min(size, 7):]
                return result

        wire = Wire()
        with patch("luvbox.browser_login.socket.create_connection", return_value=wire) as connect:
            result = login.devtools_call(12345, "/devtools/browser/offline", "Storage.getCookies")
        connect.assert_called_once_with(("127.0.0.1", 12345), timeout=5.0)
        self.assertEqual(result["cookies"][0]["value"], TOKEN)
        frame = wire.sent[1]
        self.assertTrue(frame[1] & 0x80)
        mask, payload = frame[2:6], frame[6:]
        command = json.loads(bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload)))
        self.assertEqual(command, {"id": 1, "method": "Storage.getCookies"})
        self.assertEqual(wire.sent[-1][0], 0x8A)


if __name__ == "__main__":
    unittest.main()

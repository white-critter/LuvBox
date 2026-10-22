"""Dedicated login lifecycle and inherited-pipe tests; only synthetic credentials."""

import json
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from luvbox import browser_login as login


TOKEN = "offline-browser-token"
IDENTITY = {"user_id": "123456789", "handle": "offline_user", "display_name": "Offline User"}


def signed_in_cookies():
    return [cookie(), dict(cookie(), name="twid", value="u%3D123456789")]


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
        self.connection = Mock(spec=login._PipeDevTools)
        self.connection.launch.side_effect = self.pipe_launch
        patch("luvbox.browser_login._PipeDevTools", return_value=self.connection).start()
        self.save = patch("luvbox.browser_login.save_authenticated_session", return_value=dict(IDENTITY)).start()
        self.cancel = threading.Event()
        self.process = Mock()
        self.process.poll.return_value = None
        self.process.wait.return_value = 0

    def pipe_launch(self, launch, args):
        return launch([args[0], "--remote-debugging-pipe", "--remote-debugging-io-pipes=101,102", *args[1:]])

    def launch(self, args, **kwargs):
        self.args = args
        self.profile = Path(next(arg.split("=", 1)[1] for arg in args if arg.startswith("--user-data-dir=")))
        return self.process

    def call(self, connection, method, **kwargs):
        self.assertIs(connection, self.connection)
        if method == "Storage.getCookies":
            return {"cookies": signed_in_cookies()}
        if method == "Target.getTargets":
            return {"targetInfos": [{"type": "page", "url": "https://x.com/home", "targetId": "offline-page"}]}
        if method == "Target.attachToTarget":
            return {"sessionId": "synthetic-session"}
        if method == "Runtime.evaluate":
            return {"result": {"value": {"url": "https://x.com/home", "profile": "https://x.com/Offline_User",
                                           "accountText": "Offline User\n@Offline_User"}}}
        return {}

    def sign_in(self, **kwargs):
        return login.sign_in(self.cancel, edge=Path("offline-edge.exe"), launch=self.launch, **kwargs)

    def test_success_encrypts_only_x_token_and_cleans_only_own_profile(self):
        other = self.directory / "login-profile-other-attempt"
        other.mkdir()
        (other / "marker").write_text("another running app")
        with patch("luvbox.browser_login.devtools_call", side_effect=self.call) as wire:
            identity = self.sign_in()
        self.assertEqual(identity, IDENTITY)
        self.save.assert_called_once_with(TOKEN, IDENTITY, cancel=self.cancel)
        self.assertFalse(self.profile.exists())
        self.assertTrue((other / "marker").exists())
        self.assertIn("--remote-debugging-pipe", self.args)
        self.assertFalse(any(arg.startswith("--remote-debugging-port") for arg in self.args))
        self.assertFalse(any(arg.startswith("--remote-debugging-address") for arg in self.args))
        self.connection.close.assert_called_once_with()
        self.assertEqual(self.args[-1], login.LOGIN_URL)
        self.assertEqual(wire.call_args.args[-1], "Browser.close")

    def test_malformed_expired_other_domain_and_other_path_cookies_are_ignored(self):
        bad = [dict(cookie(), domain="x.com.evil"), dict(cookie(), path="/unrelated"),
               dict(cookie(), expires=time.time() - 30), dict(cookie(), expires="bad"),
               dict(cookie(), expires=float("nan")), dict(cookie(), domain="..x.com"), None]
        self.assertEqual(login._x_token(bad), "")
        self.assertEqual(login._x_token(bad + [cookie()]), TOKEN)

    def test_login_never_uses_socket_or_http_discovery(self):
        with patch("socket.socket", side_effect=AssertionError("login opened TCP")), \
                patch("urllib.request.build_opener", side_effect=AssertionError("login used HTTP discovery")), \
                patch("luvbox.browser_login.devtools_call", side_effect=self.call):
            self.sign_in()
        self.save.assert_called_once_with(TOKEN, IDENTITY, cancel=self.cancel)

    def test_unavailable_pipe_fails_closed_without_tcp_fallback_or_session_write(self):
        def call(connection, method, **kwargs):
            if method == "Browser.getVersion":
                raise TimeoutError("synthetic unsupported pipe")
            return {}
        with patch("socket.socket", side_effect=AssertionError("login fell back to TCP")), \
                patch("luvbox.browser_login.devtools_call", side_effect=call), \
                self.assertRaises(login.SignInError) as error:
            self.sign_in()
        self.assertEqual(str(error.exception), login.NO_DEBUGGING)
        self.save.assert_not_called()
        self.connection.close.assert_called_once_with()
        self.assertFalse(self.profile.exists())

    def test_exited_edge_launcher_does_not_cancel_browser_and_browser_still_closes(self):
        self.process.poll.return_value = 0
        with patch("luvbox.browser_login.devtools_call", side_effect=self.call) as wire:
            self.sign_in()
        self.save.assert_called_once_with(TOKEN, IDENTITY, cancel=self.cancel)
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
            def call(connection, method, **kwargs):
                if method == "Target.attachToTarget":
                    return {"sessionId": "synthetic-session"}
                if method == "Runtime.evaluate":
                    self.assertEqual(kwargs["session_id"], "synthetic-session")
                    return {"result": {"value": {"ready": True, "blank": blank}}}
                return {}
            with self.subTest(blank=blank), patch("luvbox.browser_login.devtools_call", side_effect=call) as wire:
                self.assertTrue(login._recover_blank_login(self.connection, pages))
                self.assertEqual(wire.call_count, 4 if blank is True else 3)
                self.assertEqual(wire.call_args.args[-1], "Target.detachFromTarget")
                self.assertEqual(wire.call_args.kwargs["params"], {"sessionId": "synthetic-session"})
                if blank is True:
                    reload = wire.call_args_list[-2]
                    self.assertEqual(reload.args[-1], "Page.reload")
                    self.assertEqual(reload.kwargs["params"], {"ignoreCache": True})
                    self.assertEqual(reload.kwargs["session_id"], "synthetic-session")

    def test_login_recovery_waits_for_slow_document_without_spending_retry(self):
        pages = [{"url": login.LOGIN_URL, "targetId": "offline"}]
        def call(connection, method, **kwargs):
            if method == "Target.attachToTarget":
                return {"sessionId": "synthetic-session"}
            return {"result": {"value": {"ready": False, "blank": True}}}
        with patch("luvbox.browser_login.devtools_call", side_effect=call) as wire:
            self.assertFalse(login._recover_blank_login(self.connection, pages))
        self.assertEqual(wire.call_count, 3)
        self.assertEqual(wire.call_args.args[-1], "Target.detachFromTarget")

    def test_recovery_rejects_other_origins_before_attaching(self):
        for url in ("https://x.com.evil/login", "https://x.com@evil.example/login", "http://x.com/login",
                    "https://x.com:444/login", "https://other@x.com/login"):
            with self.subTest(url=url), patch("luvbox.browser_login.devtools_call") as wire:
                self.assertFalse(login._recover_blank_login(self.connection, [{"url": url, "targetId": "offline"}]))
                wire.assert_not_called()

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

    def test_identity_is_not_inferred_from_token_or_viewed_profile(self):
        pages = [{"url": "https://x.com/somebody_else", "targetId": "offline-page"}]
        with patch("luvbox.browser_login.devtools_call", side_effect=self.call):
            self.assertIsNone(login._authenticated_identity(self.connection, pages, [cookie()]))
            self.assertEqual(login._authenticated_identity(self.connection, pages, signed_in_cookies()), IDENTITY)

    def test_identity_rejects_untrusted_page_before_reading_its_dom(self):
        for url in ("http://x.com/home", "https://x.com.evil/home", "https://other@x.com/home",
                    "https://x.com:444/home", "https://x.com:invalid/home"):
            with self.subTest(url=url), patch("luvbox.browser_login.devtools_call") as wire:
                self.assertIsNone(login._authenticated_identity(
                    self.connection, [{"url": url, "targetId": "offline-page"}], signed_in_cookies()))
                wire.assert_not_called()

    def test_identity_rejects_wrong_profile_and_account_switch_during_read(self):
        pages = [{"url": "https://x.com/home", "targetId": "offline-page"}]
        for profile, text in (("https://x.com.evil/Offline_User", "Offline User\n@Offline_User"),
                              ("https://x.com/Offline_User?redirect=1", "Offline User\n@Offline_User"),
                              ("https://x.com/Offline_User", "Other\n@other_user")):
            def call(connection, method, **kwargs):
                if method == "Runtime.evaluate":
                    return {"result": {"value": {"url": "https://x.com/home", "profile": profile,
                                                   "accountText": text}}}
                return self.call(connection, method, **kwargs)
            with self.subTest(profile=profile, text=text), patch("luvbox.browser_login.devtools_call", side_effect=call):
                self.assertIsNone(login._authenticated_identity(self.connection, pages, signed_in_cookies()))
        def switched(connection, method, **kwargs):
            if method == "Storage.getCookies":
                return {"cookies": [dict(cookie(), value="another-session"),
                                     dict(cookie(), name="twid", value="u%3D777")]}
            return self.call(connection, method, **kwargs)
        with patch("luvbox.browser_login.devtools_call", side_effect=switched):
            self.assertIsNone(login._authenticated_identity(self.connection, pages, signed_in_cookies()))
        self.save.assert_not_called()

    def test_user_id_requires_unexpired_unambiguous_x_cookie(self):
        self.assertEqual(login._x_user_id(signed_in_cookies()), "123456789")
        for invalid in (dict(cookie(), name="twid", value="u%3D123", domain="x.com.evil"),
                        dict(cookie(), name="twid", value="u%3D123", path="/other"),
                        dict(cookie(), name="twid", value="u%3D0"),
                        dict(cookie(), name="twid", value="u%3D123\n"),
                        dict(cookie(), name="twid", value="u%3D123", expires=time.time() - 5)):
            with self.subTest(value=invalid["value"]):
                self.assertEqual(login._x_user_id([invalid]), "")
        self.assertEqual(login._x_user_id(signed_in_cookies() + [dict(cookie(), name="twid", value="u%3D777")]), "")



class PipeDevToolsTests(unittest.TestCase):
    def setUp(self):
        self.connection = login._PipeDevTools()
        self.addCleanup(self.connection.close)
        self.commands = []
        self.peer_errors = []

    def peer(self, replies, *, count=1):
        child_read, child_write = self.connection._child
        def run():
            try:
                buffered = b""
                for _ in range(count):
                    while b"\0" not in buffered:
                        chunk = os.read(child_read, 65536)
                        if not chunk:
                            return
                        buffered += chunk
                    raw, buffered = buffered.split(b"\0", 1)
                    command = json.loads(raw)
                    self.commands.append(command)
                    for chunk in replies(command):
                        os.write(child_write, chunk)
            except OSError as exc:
                self.peer_errors.append(type(exc).__name__)
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        return thread

    def test_null_framing_fragmented_replies_events_and_session_routing(self):
        def replies(command):
            event = {"method": "Target.targetCreated", "params": {}}
            wrong = {"id": command["id"], "sessionId": "other-session", "result": {"ignored": True}}
            result = {"id": command["id"], "sessionId": command["sessionId"], "result": {"safe": True}}
            content = b"\0".join(json.dumps(value).encode() for value in (event, wrong, result)) + b"\0"
            return [content[:7], content[7:29], content[29:]]
        thread = self.peer(replies)
        self.assertEqual(self.connection.call("Runtime.evaluate", session_id="synthetic-session"), {"safe": True})
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.commands, [{"id": 1, "method": "Runtime.evaluate", "sessionId": "synthetic-session"}])
        self.assertEqual(self.peer_errors, [])

    def test_reads_timeout_and_cancellation_are_bounded(self):
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            self.connection.call("Browser.getVersion", timeout=.03)
        self.assertLess(time.monotonic() - started, .5)
        cancel = threading.Event()
        cancel.set()
        with self.assertRaisesRegex(login.SignInError, login.CANCELLED):
            self.connection.call("Browser.getVersion", cancel=cancel)

    def test_cancellation_interrupts_an_inflight_read(self):
        cancel = threading.Event()
        timer = threading.Timer(.03, cancel.set)
        self.addCleanup(timer.cancel)
        timer.start()
        started = time.monotonic()
        with self.assertRaisesRegex(login.SignInError, login.CANCELLED):
            self.connection.call("Browser.getVersion", timeout=3, cancel=cancel)
        self.assertLess(time.monotonic() - started, .5)

    def test_full_output_pipe_cannot_block_writes_past_deadline(self):
        while True:
            try:
                os.write(self.connection._writer, b"x" * 1024)
            except BlockingIOError:
                break
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            self.connection.call("Browser.getVersion", timeout=.03)
        self.assertLess(time.monotonic() - started, .5)

    def test_oversized_reply_is_rejected(self):
        self.connection._MAX_MESSAGE = 1024
        thread = self.peer(lambda _: [b"x" * 2048])
        with self.assertRaisesRegex(ConnectionError, "too large"):
            self.connection.call("Browser.getVersion")
        thread.join(2)
        self.assertFalse(thread.is_alive())

    def test_command_size_is_bounded_before_writing(self):
        with self.assertRaisesRegex(ValueError, "too large"):
            self.connection.call("Runtime.evaluate", params={"expression": "x" * 65536})

    def test_close_releases_all_four_descriptors_and_is_repeatable(self):
        descriptors = [self.connection._reader, self.connection._writer, *self.connection._child]
        self.connection.close()
        self.connection.close()
        for fd in descriptors:
            with self.assertRaises(OSError):
                os.fstat(fd)

    def test_malformed_json_and_non_dictionary_result_are_rejected(self):
        for content in (b"{invalid}\0", b'{"id":1,"result":[]}\0'):
            with self.subTest(content=content):
                connection = login._PipeDevTools()
                try:
                    os.write(connection._child[1], content)
                    with self.assertRaises(ValueError):
                        connection.call("Browser.getVersion")
                finally:
                    connection.close()

    def test_protocol_errors_do_not_forward_peer_contents(self):
        thread = self.peer(lambda command: [json.dumps({"id": command["id"], "error": {"message": TOKEN}}).encode() + b"\0"])
        with self.assertRaises(ConnectionError) as error:
            self.connection.call("Storage.getCookies")
        self.assertNotIn(TOKEN, str(error.exception))
        thread.join(2)

    @unittest.skipUnless(os.name == "nt", "Windows inherited handle allowlist")
    def test_launch_inherits_only_child_pipe_handles_and_closes_parent_copies(self):
        import msvcrt
        child = list(self.connection._child)
        handles = [msvcrt.get_osfhandle(fd) for fd in child]
        parent = [self.connection._reader, self.connection._writer]
        def launch(arguments, **kwargs):
            self.assertTrue(all(os.get_inheritable(fd) for fd in child))
            self.assertTrue(all(not os.get_inheritable(fd) for fd in parent))
            self.assertEqual(kwargs["startupinfo"].lpAttributeList, {"handle_list": handles})
            self.assertTrue(kwargs["close_fds"])
            self.assertIn("--remote-debugging-pipe", arguments)
            self.assertIn("--remote-debugging-io-pipes=" + ",".join(str(value & 0xFFFFFFFF) for value in handles), arguments)
            self.assertFalse(any(value.startswith("--remote-debugging-port") for value in arguments))
            return "synthetic process"
        self.assertEqual(self.connection.launch(launch, ["synthetic-edge.exe", "about:blank"]), "synthetic process")
        self.assertEqual(self.connection._child, [])
        for fd in child:
            with self.assertRaises(OSError):
                os.fstat(fd)

    @unittest.skipUnless(os.name == "nt", "Windows inherited handle allowlist")
    def test_failed_launch_closes_inheritable_child_handles(self):
        child = list(self.connection._child)
        with self.assertRaises(OSError):
            self.connection.launch(Mock(side_effect=OSError("synthetic launch failure")), ["synthetic-edge.exe"])
        self.assertEqual(self.connection._child, [])
        for fd in child:
            with self.assertRaises(OSError):
                os.fstat(fd)

    @unittest.skipUnless(os.name == "nt", "Windows real inherited pipe child")
    def test_real_child_can_read_only_the_inherited_pipe_protocol(self):
        program = r'''
import json, msvcrt, os, sys
value = next(arg.split('=', 1)[1] for arg in sys.argv if arg.startswith('--remote-debugging-io-pipes='))
incoming, outgoing = (msvcrt.open_osfhandle(int(handle), os.O_BINARY) for handle in value.split(','))
data = b''
while b'\0' not in data:
    data += os.read(incoming, 65536)
command = json.loads(data.split(b'\0', 1)[0])
os.write(outgoing, json.dumps({'id': command['id'], 'result': {'synthetic': True}}).encode() + b'\0')
os.close(incoming)
os.close(outgoing)
'''
        def launch(arguments, **kwargs):
            return subprocess.Popen([sys.executable, "-c", program, *arguments[1:]], **kwargs)
        process = self.connection.launch(launch, ["synthetic-child.exe"])
        try:
            self.assertEqual(self.connection.call("Browser.getVersion"), {"synthetic": True})
            self.assertEqual(process.wait(timeout=3), 0)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=3)


if __name__ == "__main__":
    unittest.main()

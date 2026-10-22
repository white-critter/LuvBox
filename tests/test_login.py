"""Offline manual-token/session tests; no browser or X connection."""

import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from luvbox import login


SECRET = "offline-login-secret-never-print-or-save-as-plaintext"


def cookie(name="auth_token", *, domain=".x.com", expires=-1, value=SECRET):
    return {"name": name, "value": value, "domain": domain, "path": "/",
            "expires": expires, "secure": True, "httpOnly": True,
            "sameSite": "None"}


class LoginTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.session = self.base / "app" / "session.bin"
        self.path_patch = patch("luvbox.login.session_path", return_value=self.session)
        self.path_patch.start()

    def tearDown(self):
        self.path_patch.stop()
        self.temporary.cleanup()

    def test_session_path_is_per_user_app_file(self):
        self.path_patch.stop()
        try:
            with patch.dict(os.environ, {"LOCALAPPDATA": str(self.base)}):
                self.assertEqual(login.session_path(), self.base / "LuvBox" / "session.bin")
        finally:
            self.path_patch.start()

    def test_filters_unrelated_malformed_and_expired_cookies_and_extra_fields(self):
        cookies = [cookie(), cookie("ct0", domain="x.com"),
                   cookie(domain="example.com"), cookie(domain="x.com.evil.example"),
                   cookie(domain="subdomain.x.com"), cookie(expires=time.time() - 30),
                   cookie(expires=float("nan")), cookie(expires=True),
                   cookie(expires=10**1000), cookie(expires=-(10**1000)),
                   cookie(value=""), cookie(value="invalid\r\nvalue"), None,
                   dict(cookie(), name="bad;name"), dict(cookie(), path="bad"),
                   dict(cookie(), secure="TRUE"),
                   {key: value for key, value in cookie().items() if key != "expires"}]
        clean = login._x_cookies(cookies)
        self.assertEqual([row["name"] for row in clean], ["auth_token", "ct0"])
        self.assertEqual(set(clean[0]), {"name", "value", "domain", "path", "expires", "secure", "httpOnly"})
        self.assertEqual(login._x_cookies({"cookies": cookies}), [])

    def test_missing_and_bad_session_is_not_connected(self):
        self.assertFalse(login.has_session())
        self.session.parent.mkdir()
        self.session.write_bytes(SECRET.encode())
        self.assertFalse(login.has_session())
        with self.assertRaises(ValueError) as error:
            login.load_session_cookies()
        self.assertNotIn(SECRET, str(error.exception))

    def test_store_rejects_missing_auth_without_writing(self):
        for cookies in ([], [cookie("ct0")], [cookie(domain="evil.example")],
                        [cookie(expires=time.time() - 30)]):
            with self.subTest(cookies=len(cookies)), self.assertRaises(ValueError):
                login._store_session_cookies(cookies)
        self.assertFalse(self.session.exists())

    def test_clear_removes_only_session_file(self):
        self.session.parent.mkdir()
        self.session.write_bytes(b"encrypted-example")
        settings = self.session.with_name("settings.json")
        settings.write_text("{}")
        login.clear_session()
        login.clear_session()
        self.assertFalse(self.session.exists())
        self.assertTrue(settings.is_file())

    @unittest.skipUnless(os.name == "nt", "Windows current-user DPAPI")
    def test_real_dpapi_roundtrip_never_writes_plaintext(self):
        login._store_session_cookies([cookie(), cookie("ct0"), cookie(domain="other.example")])
        binary = self.session.read_bytes()
        self.assertTrue(binary.startswith(login._HEADER))
        self.assertNotIn(SECRET.encode(), binary)
        self.assertTrue(login.has_session())
        self.assertEqual([row["name"] for row in login.load_session_cookies()], ["auth_token", "ct0"])
        self.assertEqual(list(self.session.parent.iterdir()), [self.session])

    @unittest.skipUnless(os.name == "nt", "Windows current-user DPAPI")
    def test_corrupt_and_expired_encrypted_payloads_are_rejected(self):
        self.session.parent.mkdir()
        for payload in ({"version": 1, "cookies": [cookie(expires=time.time() - 30)]},
                        {"version": 1, "cookies": [cookie("ct0")]},
                        {"version": 2, "cookies": [cookie()]},
                        {"version": True, "cookies": [cookie()]}, []):
            encoded = json.dumps(payload).encode()
            self.session.write_bytes(login._HEADER + login._dpapi(encoded, protect=True))
            with self.subTest(payload_type=type(payload).__name__):
                self.assertFalse(login.has_session())
        self.session.write_bytes(login._HEADER + b"damaged ciphertext")
        self.assertFalse(login.has_session())

    def test_encryption_failure_does_not_write_or_replace_session(self):
        self.session.parent.mkdir()
        self.session.write_bytes(b"previous-encrypted-session")
        with patch("luvbox.login._dpapi", side_effect=ValueError("cannot encrypt")), self.assertRaises(ValueError):
            login._store_session_cookies([cookie()])
        self.assertEqual(self.session.read_bytes(), b"previous-encrypted-session")
        self.assertEqual(list(self.session.parent.iterdir()), [self.session])

    def test_atomic_replace_failure_keeps_previous_file_and_removes_temp(self):
        self.session.parent.mkdir()
        self.session.write_bytes(b"previous-encrypted-session")
        with patch("luvbox.login._dpapi", return_value=b"ciphertext"), \
                patch("luvbox.login.os.replace", side_effect=PermissionError), self.assertRaises(PermissionError):
            login._store_session_cookies([cookie()])
        self.assertEqual(self.session.read_bytes(), b"previous-encrypted-session")
        self.assertEqual(list(self.session.parent.iterdir()), [self.session])

    def test_save_auth_token_accepts_variable_safe_values_without_fixed_hex_assumption(self):
        for value in ("a", "token-with-different-length_123", "base64+/value=", "x" * 4096):
            with self.subTest(length=len(value)), patch("luvbox.login._store_session_cookies") as store:
                self.assertIsNone(login.save_auth_token(value))
            store.assert_called_once_with([{"name": "auth_token", "value": value,
                                           "domain": ".x.com", "path": "/", "expires": -1,
                                           "secure": True, "httpOnly": True}])

    def test_save_auth_token_trims_outer_copy_whitespace(self):
        with patch("luvbox.login._store_session_cookies") as store:
            login.save_auth_token(" \t\n" + SECRET + "\r\n ")
        self.assertEqual(store.call_args.args[0][0]["value"], SECRET)

    def test_bad_token_preserves_previous_session_without_touching_encryption(self):
        self.session.parent.mkdir()
        self.session.write_bytes(b"previous-encrypted-session")
        invalid = (None, True, {}, "", " \t\r\n ", "space inside", "new\nline", "new\tline",
                   'quoted"value', "comma,value", "semi;colon", "back\\slash", "del\x7fvalue",
                   "한글", "x" * 4097, SECRET + ";injected=value")
        with patch("luvbox.login._store_session_cookies") as store:
            for value in invalid:
                with self.subTest(kind=type(value).__name__), self.assertRaisesRegex(ValueError, "auth_token") as error:
                    login.save_auth_token(value)
                self.assertNotIn(SECRET, str(error.exception))
                self.assertEqual(self.session.read_bytes(), b"previous-encrypted-session")
        store.assert_not_called()

    def test_manual_token_storage_is_encrypted_and_never_opens_browser_or_network(self):
        with patch("luvbox.login._dpapi", return_value=b"encrypted-fixture") as dpapi, \
                patch("requests.Session.request") as request, patch("subprocess.Popen") as process, \
                patch("webbrowser.open") as browser:
            login.save_auth_token(SECRET)
        request.assert_not_called()
        process.assert_not_called()
        browser.assert_not_called()
        self.assertTrue(dpapi.call_args.kwargs["protect"])
        payload = json.loads(dpapi.call_args.args[0])
        self.assertEqual(payload["version"], 1)
        self.assertEqual(payload["cookies"][0]["value"], SECRET)
        self.assertEqual(self.session.read_bytes(), login._HEADER + b"encrypted-fixture")
        self.assertNotIn(SECRET.encode(), self.session.read_bytes())
        self.assertEqual(list(self.session.parent.iterdir()), [self.session])
        self.assertFalse(hasattr(login, "run_login"))
        self.assertFalse(hasattr(login, "_playwright"))

    def test_public_save_normalizes_encryption_failure_and_preserves_previous_session(self):
        self.session.parent.mkdir()
        self.session.write_bytes(b"previous-encrypted-session")
        with patch("luvbox.login._dpapi", side_effect=ValueError(SECRET)), \
                self.assertRaisesRegex(ValueError, "암호화해 저장") as error:
            login.save_auth_token(SECRET)
        self.assertNotIn(SECRET, str(error.exception))
        self.assertEqual(self.session.read_bytes(), b"previous-encrypted-session")
        self.assertEqual(list(self.session.parent.iterdir()), [self.session])

    def test_public_save_normalizes_atomic_replace_failure_and_cleans_ciphertext_temp(self):
        self.session.parent.mkdir()
        self.session.write_bytes(b"previous-encrypted-session")
        with patch("luvbox.login._dpapi", return_value=b"encrypted-fixture"), \
                patch("luvbox.login.os.replace", side_effect=PermissionError(SECRET)), \
                self.assertRaisesRegex(ValueError, "암호화해 저장") as error:
            login.save_auth_token(SECRET)
        self.assertNotIn(SECRET, str(error.exception))
        self.assertEqual(self.session.read_bytes(), b"previous-encrypted-session")
        self.assertEqual(list(self.session.parent.iterdir()), [self.session])

    def test_existing_v1_session_with_multiple_cookies_remains_readable(self):
        original = {"version": 1, "cookies": [cookie(), cookie("ct0")]}
        self.session.parent.mkdir()
        self.session.write_bytes(login._HEADER + b"legacy-encrypted-fixture")
        with patch("luvbox.login._dpapi", return_value=json.dumps(original).encode()) as dpapi:
            self.assertEqual([item["name"] for item in login.load_session_cookies()], ["auth_token", "ct0"])
            self.assertTrue(login.has_session())
        for call in dpapi.call_args_list:
            self.assertFalse(call.kwargs["protect"])

    def test_new_manual_token_payload_is_readable_in_original_session_format(self):
        encoded = None

        def fake_dpapi(data, *, protect):
            nonlocal encoded
            if protect:
                encoded = data
                return b"manual-encrypted-fixture"
            return encoded

        with patch("luvbox.login._dpapi", side_effect=fake_dpapi):
            login.save_auth_token(SECRET)
            self.assertTrue(login.has_session())
            clean = login.load_session_cookies()
        self.assertEqual([item["name"] for item in clean], ["auth_token"])
        self.assertEqual(clean[0]["value"], SECRET)



class AccountSessionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.session = Path(temporary.name) / "session.bin"
        self.addCleanup(patch.stopall)
        patch("luvbox.login.session_path", return_value=self.session).start()
        # Reversible fixture ciphertext permits all store/load/selection paths
        # on Linux while ensuring that no plaintext fixture is written to disk.
        patch("luvbox.login._dpapi", side_effect=lambda data, **_: bytes(value ^ 0xA5 for value in data)).start()
        self.first = {"user_id": "123", "handle": "First_User", "display_name": "첫 번째 계정"}
        self.second = {"user_id": "456", "handle": "second_user", "display_name": "두 번째 계정"}

    def test_connected_accounts_switch_cookies_and_keep_identity_inside_ciphertext(self):
        self.assertEqual(login.save_authenticated_session(SECRET, self.first)["handle"], "first_user")
        login.save_authenticated_session("second-token", self.second)
        self.assertEqual(login.active_account()["user_id"], "456")
        rows = login.list_accounts()
        self.assertEqual([(row["user_id"], row["active"]) for row in rows], [("123", False), ("456", True)])
        self.assertEqual(set(rows[0]), {"user_id", "handle", "display_name", "active"})
        login.select_account("123")
        self.assertEqual(login.active_account()["handle"], "first_user")
        self.assertEqual(login.load_session_cookies()[0]["value"], SECRET)
        binary = self.session.read_bytes()
        for value in (SECRET, "first_user", "second_user", "첫 번째 계정"):
            self.assertNotIn(value.encode(), binary)
        self.assertEqual(list(self.session.parent.iterdir()), [self.session])

    def test_reconnecting_same_stable_user_id_updates_handle_without_duplicate_account(self):
        login.save_authenticated_session(SECRET, self.first)
        login.save_authenticated_session("updated-token", dict(self.first, handle="renamed_user"))
        self.assertEqual(len(login.list_accounts()), 1)
        self.assertEqual(login.active_account()["handle"], "renamed_user")
        self.assertEqual(login.load_session_cookies()[0]["value"], "updated-token")

    def test_removing_one_account_preserves_other_and_last_removal_disconnects(self):
        login.save_authenticated_session(SECRET, self.first)
        login.save_authenticated_session("second-token", self.second)
        login.remove_account("123")
        self.assertEqual(login.active_account()["user_id"], "456")
        self.assertEqual([row["user_id"] for row in login.list_accounts()], ["456"])
        login.remove_account("456")
        self.assertFalse(login.has_session())
        self.assertFalse(self.session.exists())

    def test_removing_active_account_selects_remaining_session_atomically(self):
        login.save_authenticated_session(SECRET, self.first)
        login.save_authenticated_session("second-token", self.second)
        login.remove_account("456")
        self.assertEqual(login.active_account()["user_id"], "123")
        self.assertEqual(login.load_session_cookies()[0]["value"], SECRET)

    def test_legacy_cookie_only_session_stays_usable_without_identity(self):
        login._store_session_cookies([cookie(), cookie("ct0")])
        self.assertTrue(login.has_session())
        self.assertIsNone(login.active_account())
        self.assertEqual(login.list_accounts(), [])
        self.assertEqual([item["name"] for item in login.load_session_cookies()], ["auth_token", "ct0"])
        self.assertEqual(login.load_session_cookies(account="previous_user")[0]["value"], SECRET)

    def test_named_account_reads_its_saved_cookies_without_changing_active_selection(self):
        login.save_authenticated_session(SECRET, self.first)
        login.save_authenticated_session("second-token", self.second)
        previous = self.session.read_bytes()
        self.assertEqual(login.load_session_cookies(account="FIRST_USER")[0]["value"], SECRET)
        self.assertEqual(login.active_account()["user_id"], "456")
        self.assertEqual(self.session.read_bytes(), previous)
        with self.assertRaises(ValueError) as error:
            login.load_session_cookies(account="unknown_user")
        self.assertNotIn(SECRET, str(error.exception))
        self.assertEqual(self.session.read_bytes(), previous)

    def test_named_account_rejects_ambiguous_reused_handle(self):
        login.save_authenticated_session(SECRET, self.first)
        login.save_authenticated_session("second-token", dict(self.second, handle=self.first["handle"]))
        with self.assertRaises(ValueError):
            login.load_session_cookies(account="first_user")

    def test_manual_token_does_not_claim_previous_identity_and_keeps_known_accounts(self):
        login.save_authenticated_session(SECRET, self.first)
        login.save_auth_token("manual-token")
        self.assertIsNone(login.active_account())
        self.assertEqual(login.load_session_cookies()[0]["value"], "manual-token")
        self.assertEqual(login.load_session_cookies(account="first_user")[0]["value"], "manual-token")
        self.assertEqual(login.list_accounts()[0]["user_id"], "123")
        self.assertFalse(login.list_accounts()[0]["active"])
        login.select_account("123")
        self.assertEqual(login.load_session_cookies()[0]["value"], SECRET)

    def test_clearing_manual_session_retains_accounts_until_explicitly_selected(self):
        login.save_authenticated_session(SECRET, self.first)
        login.save_authenticated_session("second-token", self.second)
        login.save_auth_token("manual-token")
        login.clear_active_session()
        self.assertFalse(login.has_session())
        self.assertIsNone(login.active_account())
        self.assertEqual([(row["user_id"], row["active"]) for row in login.list_accounts()],
                         [("123", False), ("456", False)])
        with self.assertRaises(ValueError):
            login.load_session_cookies()
        login.select_account("123")
        self.assertEqual(login.load_session_cookies()[0]["value"], SECRET)
        login.clear_active_session()
        self.assertEqual(login.active_account()["user_id"], "456")
        login.clear_active_session()
        self.assertFalse(self.session.exists())
        login.clear_active_session()

    def test_failed_active_disconnect_preserves_ciphertext_and_hides_upstream_error(self):
        login.save_authenticated_session(SECRET, self.first)
        login.save_auth_token("manual-token")
        previous = self.session.read_bytes()
        with patch("luvbox.login.os.replace", side_effect=PermissionError(SECRET)):
            with self.assertRaises(ValueError) as error:
                login.clear_active_session()
        self.assertNotIn(SECRET, str(error.exception))
        self.assertEqual(self.session.read_bytes(), previous)
        self.assertEqual(login.load_session_cookies()[0]["value"], "manual-token")
        self.assertEqual(login.list_accounts()[0]["user_id"], "123")
        self.assertEqual(list(self.session.parent.iterdir()), [self.session])

    def test_selection_and_removal_failure_preserve_previous_active_account(self):
        login.save_authenticated_session(SECRET, self.first)
        login.save_authenticated_session("second-token", self.second)
        previous = self.session.read_bytes()
        for operation in (lambda: login.select_account("123"), lambda: login.remove_account("456"),
                          lambda: login.save_authenticated_session("new-token", self.first)):
            with self.subTest(operation=operation), patch("luvbox.login.os.replace", side_effect=PermissionError(SECRET)):
                with self.assertRaises(ValueError) as error:
                    operation()
                self.assertNotIn(SECRET, str(error.exception))
            self.assertEqual(self.session.read_bytes(), previous)
            self.assertEqual(list(self.session.parent.iterdir()), [self.session])

    def test_cancellation_during_encryption_never_replaces_previous_session(self):
        login.save_authenticated_session(SECRET, self.first)
        previous = self.session.read_bytes()
        cancel = threading.Event()

        def encrypt_and_cancel(data, *, protect, **_):
            if protect:
                cancel.set()
            return bytes(value ^ 0xA5 for value in data)

        with patch("luvbox.login._dpapi", side_effect=encrypt_and_cancel):
            with self.assertRaises(login.SessionCancelled):
                login.save_authenticated_session("second-token", self.second, cancel=cancel)
        self.assertEqual(self.session.read_bytes(), previous)
        self.assertEqual(list(self.session.parent.iterdir()), [self.session])

    def test_invalid_identity_and_paths_never_overwrite_or_leak_secrets(self):
        login.save_authenticated_session(SECRET, self.first)
        previous = self.session.read_bytes()
        invalid = (None, {}, dict(self.first, user_id="../session"), dict(self.first, user_id="0"),
                   dict(self.first, user_id=True), dict(self.first, handle="https://x.com/user"),
                   dict(self.first, handle="a" * 16), dict(self.first, display_name=SECRET + "\n"))
        for identity in invalid:
            with self.subTest(identity_type=type(identity).__name__), self.assertRaises(ValueError) as error:
                login.save_authenticated_session(SECRET, identity)
            self.assertNotIn(SECRET, str(error.exception))
            self.assertEqual(self.session.read_bytes(), previous)
        for account_id in ("../session", "unknown", "777", None):
            with self.subTest(account_id=account_id), self.assertRaises(ValueError):
                login.select_account(account_id)
        self.assertEqual(self.session.read_bytes(), previous)

    def test_extra_cookies_are_filtered_and_cannot_replace_the_verified_token(self):
        login.save_authenticated_session(SECRET, self.first, cookies=[cookie(), cookie("ct0"),
                                                                      cookie(domain="example.org")])
        self.assertEqual([item["name"] for item in login.load_session_cookies()], ["auth_token", "ct0"])
        previous = self.session.read_bytes()
        for cookies in ([cookie(value="different-token")], [cookie(domain="x.com.evil")], [cookie("ct0")]):
            with self.subTest(count=len(cookies)), self.assertRaises(ValueError):
                login.save_authenticated_session(SECRET, self.first, cookies=cookies)
            self.assertEqual(self.session.read_bytes(), previous)


if __name__ == "__main__":
    unittest.main()

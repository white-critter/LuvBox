"""Loopback pairing boundaries; every account/cookie here is synthetic."""

import contextlib
import base64
import hmac
import http.client
import io
import json
from pathlib import Path
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from luvbox.browser_bridge import BrowserBridge, BrowserBridgeError, _decrypt, _derive_key, _session, extension_directory


PAYLOAD = {"version": 1, "auth_token": "synthetic-bridge-token", "username": "Offline_User",
           "user_id": "123456789", "display_name": "오프라인 계정"}
ORIGIN = "chrome-extension://abcdefghijklmnopabcdefghijklmnop"


def envelope(payload, secret, port):
    """Test-only GCM encryption using the pinned dependency's AES primitives.

    The Node/WebCrypto cross-check below independently exercises the format.
    """
    from gallery_dl import aes
    nonce = os.urandom(12)
    key = list(_derive_key(secret, port, "encryption"))
    raw = list(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    cipher = aes.aes_ctr_encrypt(raw, key, list(nonce) + [0, 0, 0, 2])
    hash_subkey = aes.aes_encrypt([0] * 16, aes.key_expansion(key))
    tagged = cipher + [0] * (-len(cipher) % 16) + [0] * 8 + list((len(cipher) * 8).to_bytes(8, "big"))
    digest = aes.ghash(hash_subkey, tagged)
    tag = aes.aes_ctr_encrypt(digest, key, list(nonce) + [0, 0, 0, 1])
    encrypted = bytes(cipher + tag)
    proof = hmac.digest(_derive_key(secret, port, "proof"), nonce + encrypted, "sha256")
    encode = lambda value: base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")
    return {"version": 1, "nonce": encode(nonce), "ciphertext": encode(encrypted), "proof": encode(proof)}


class BrowserBridgeTests(unittest.TestCase):
    def setUp(self):
        self.bridge = BrowserBridge().start()
        self.addCleanup(self.bridge.close)
        self.port = self.bridge.port
        self.secret = self.bridge._secret

    def request(self, method="POST", path="/session", *, payload=PAYLOAD, headers=None, secret=None, encrypted=False):
        if payload is not None and not encrypted:
            payload = envelope(payload, self.secret if secret is None else secret, self.port)
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        standard = {"Origin": ORIGIN, "Content-Type": "application/json"}
        if headers:
            standard.update(headers)
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
        try:
            connection.request(method, path, body=body, headers=standard)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_success_returns_account_without_saving_or_remote_requests(self):
        with patch("webbrowser.open", side_effect=AssertionError("unexpected browser open")):
            status, headers, body = self.request()
            session = self.bridge.wait()
        self.assertEqual(status, 200)
        self.assertEqual(body, b'{"ok":true}')
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(session.auth_token, PAYLOAD["auth_token"])
        self.assertEqual(session.identity, {"handle": "offline_user", "user_id": "123456789", "display_name": "오프라인 계정"})
        self.assertEqual(self.bridge._secret, "")

    def test_browser_uses_already_bound_port_and_fragment_only(self):
        def opener(url):
            parsed = urlsplit(url)
            self.assertEqual(parsed.hostname, "127.0.0.1")
            self.assertEqual(parsed.port, self.port)
            self.assertEqual(parsed.path, "/connect")
            self.assertEqual(parsed.query, "")
            self.assertEqual(parsed.fragment, "luvbox=" + self.secret)
            self.assertEqual(len(self.secret), 43)
            self.assertEqual(self.request("GET", "/connect", payload=None)[0], 200)
            with socket.socket() as competitor:
                with self.assertRaises(OSError):
                    competitor.bind(("127.0.0.1", self.port))
            return True
        self.bridge.open_browser(opener=opener)

    def test_bridge_page_never_contains_pairing_secret_or_cookie(self):
        status, headers, body = self.request("GET", "/connect", payload=None)
        self.assertEqual(status, 200)
        self.assertNotIn(self.secret.encode(), body)
        self.assertNotIn(PAYLOAD["auth_token"].encode(), body)
        self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")

    def test_origin_and_capability_are_both_required(self):
        for origin in ("https://x.com", "https://evil.example", "null", "", ORIGIN + "/", "moz-extension://uuid/path"):
            with self.subTest(origin=origin):
                self.assertEqual(self.request(headers={"Origin": origin})[0], 403)
        self.assertEqual(self.request(secret="wrong")[0], 400)
        self.assertIsNone(self.bridge._result)

    def test_firefox_extension_origin_is_accepted(self):
        self.assertEqual(self.request(headers={"Origin": "moz-extension://01234567-89ab-cdef-0123-456789abcdef"})[0], 200)

    def test_dns_rebinding_other_paths_and_query_capabilities_are_rejected(self):
        self.assertEqual(self.request(headers={"Host": f"evil.example:{self.port}"})[0], 400)
        self.assertEqual(self.request(headers={"Host": f"localhost:{self.port}"})[0], 400)
        for path in ("/", "/session?secret=" + self.secret, "http://127.0.0.1/session", "/session/", "/connect#anything"):
            self.assertEqual(self.request(path=path)[0], 400)
        self.assertIsNone(self.bridge._result)

    def test_payload_limit_and_transfer_encoding_are_rejected_before_read(self):
        self.assertEqual(self.request(headers={"Content-Length": "8193"})[0], 400)
        self.assertEqual(self.request(headers={"Content-Length": "-1"})[0], 400)
        self.assertEqual(self.request(headers={"Content-Type": "text/plain"})[0], 400)
        self.assertEqual(self.request(headers={"Transfer-Encoding": "chunked"})[0], 400)

    def test_duplicate_security_headers_are_rejected(self):
        with socket.create_connection(("127.0.0.1", self.port), timeout=2) as connection:
            request = (f"POST /session HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n"
                       f"Host: evil.example\r\nOrigin: {ORIGIN}\r\nAuthorization: Bearer {self.secret}\r\n"
                       "Content-Length: 0\r\n\r\n")
            connection.sendall(request.encode())
            self.assertIn(b" 400 ", connection.recv(1024).split(b"\r\n")[0])

    def test_invalid_sessions_do_not_consume_attempt(self):
        for changes in ({"user_id": "0"}, {"user_id": "abc"}, {"username": "@wrong"},
                        {"username": "a" * 16}, {"display_name": "line\nbreak"},
                        {"auth_token": "bad;cookie"}, {"auth_token": "a" * 4097},
                        {"version": True}, {"extra": "unexpected"}):
            with self.subTest(changes=list(changes)):
                self.assertEqual(self.request(payload=dict(PAYLOAD, **changes))[0], 400)
        self.assertEqual(self.request()[0], 200)

    def test_errors_and_reprs_never_log_secrets(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            self.request(secret="wrong")
            self.request(payload={"auth_token": PAYLOAD["auth_token"]})
        output.write(repr(self.bridge))
        output.write(repr(_session(PAYLOAD)))
        self.assertNotIn(self.secret, output.getvalue())
        self.assertNotIn(PAYLOAD["auth_token"], output.getvalue())

    def test_cancellation_closes_listener_without_returning_captured_session(self):
        cancel = threading.Event()
        self.request()
        cancel.set()
        with self.assertRaisesRegex(BrowserBridgeError, "취소"):
            self.bridge.wait(cancel=cancel)
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", self.port), timeout=0.2)

    def test_expiration_closes_listener_and_clears_capability(self):
        bridge = BrowserBridge(timeout=0.05).start()
        port = bridge.port
        with self.assertRaisesRegex(BrowserBridgeError, "시간"):
            bridge.wait()
        self.assertEqual(bridge._secret, "")
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=0.2)

    def test_missing_default_browser_is_secret_free_and_closes_listener(self):
        with self.assertRaisesRegex(BrowserBridgeError, "기본 브라우저") as caught:
            self.bridge.open_browser(opener=lambda url: (_ for _ in ()).throw(OSError(url)))
        self.assertNotIn(self.secret, str(caught.exception))
        self.assertEqual(self.bridge._secret, "")

    def test_context_close_releases_port_even_without_wait(self):
        self.bridge.close()
        self.bridge.close()
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", self.port), timeout=0.2)

    def test_ciphertext_does_not_disclose_cookie_or_secret_to_reused_port(self):
        encrypted = envelope(PAYLOAD, self.secret, self.port)
        wire = json.dumps(encrypted)
        self.assertNotIn(PAYLOAD["auth_token"], wire)
        self.assertNotIn(self.secret, wire)
        self.assertNotIn(PAYLOAD["username"], wire)
        self.bridge.close()
        # A new listener receives only ciphertext, and has neither the old
        # pairing key nor a proof from which it can recover the credential.
        with socket.socket() as replacement:
            replacement.bind(("127.0.0.1", self.port))
            with self.assertRaises(ValueError):
                _decrypt(encrypted, "attacker-chosen-secret", self.port)
        with self.assertRaises(ValueError):
            _decrypt(encrypted, self.secret, self.port + 1)

    def test_tampering_and_plaintext_payload_are_rejected(self):
        encrypted = envelope(PAYLOAD, self.secret, self.port)
        self.assertEqual(self.request(payload=PAYLOAD, encrypted=True)[0], 400)
        for key in ("nonce", "proof", "ciphertext"):
            changed = dict(encrypted)
            changed[key] = ("A" if changed[key][0] != "A" else "B") + changed[key][1:]
            self.assertEqual(self.request(payload=changed, encrypted=True)[0], 400)
        self.assertEqual(self.request()[0], 200)

    @unittest.skipUnless(shutil.which("node"), "Node is optional for WebCrypto compatibility tests")
    def test_extension_api_boundaries_and_webcrypto_interoperate(self):
        harness = Path(__file__).with_name("browser_bridge_extension_harness.js")
        result = subprocess.run([shutil.which("node"), str(harness), str(extension_directory() / "chromium")],
                                capture_output=True, text=True, timeout=10, check=True)
        encrypted = json.loads(result.stdout)
        session = _decrypt(encrypted, "offline-pairing-secret-for-webcrypto", 49123)
        self.assertEqual(session.auth_token, PAYLOAD["auth_token"])
        self.assertEqual(session.user_id, PAYLOAD["user_id"])

    def test_extension_artifacts_and_permissions_are_bounded(self):
        root = extension_directory()
        for browser in ("chromium", "firefox"):
            manifest = json.loads((root / browser / "manifest.json").read_text())
            self.assertEqual(manifest["permissions"], ["cookies"])
            self.assertEqual(set(manifest["host_permissions"]), {"https://x.com/*", "http://127.0.0.1/*"})
            self.assertNotIn("externally_connectable", manifest)
            for source in ("background.js", "connect.js", "profile.js"):
                self.assertTrue((root / browser / source).is_file())
            self.assertNotIn("<all_urls>", json.dumps(manifest))


try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None


@unittest.skipUnless(sync_playwright and shutil.which("chromium"), "Optional local Chromium/Playwright integration")
class BrowserBridgeExtensionTests(unittest.TestCase):
    def test_real_extension_pairs_with_synthetic_x_session(self):
        # Browser only: intercept every X page locally. No real login/network.
        extension = str(extension_directory() / "chromium")
        profile_html = ('<!doctype html><html><body><a data-testid="AppTabBar_Profile_Link" '
                        'href="https://x.com/Offline_User">Profile</a>'
                        '<button data-testid="SideNav_AccountSwitcher_Button">Offline Name\n@Offline_User</button>'
                        '</body></html>')
        with tempfile.TemporaryDirectory() as profile, sync_playwright() as playwright, BrowserBridge() as bridge:
            context = playwright.chromium.launch_persistent_context(
                profile, executable_path=shutil.which("chromium"), headless=True,
                ignore_default_args=["--disable-extensions"],
                args=[f"--disable-extensions-except={extension}", f"--load-extension={extension}", "--no-sandbox", "--enable-unsafe-extension-debugging"])
            try:
                if not context.service_workers:
                    try:
                        context.browser.new_browser_cdp_session().send("Extensions.loadUnpacked", {"path": extension})
                        context.wait_for_event("serviceworker", timeout=5000)
                    except Exception as error:
                        if "disabled by the administrator" in str(error):
                            self.skipTest("Chromium administrator policy blocks unpacked test extensions")
                        raise
                context.route("https://x.com/**", lambda route: route.fulfill(status=200, content_type="text/html", body=profile_html))
                context.add_cookies([
                    {"name": "auth_token", "value": PAYLOAD["auth_token"], "domain": ".x.com", "path": "/", "secure": True, "httpOnly": True},
                    {"name": "twid", "value": "u%3D123456789", "domain": ".x.com", "path": "/", "secure": True}
                ])
                captured = []
                bridge.open_browser(opener=lambda url: captured.append(url) or True)
                page = context.pages[0]
                page.goto(captured[0])
                page.wait_for_url("chrome-extension://**/connect.html")
                page.get_by_text(bridge.pairing_code, exact=True).wait_for()
                self.assertEqual(page.locator("#pairing-code").inner_text(), bridge.pairing_code)
                page.get_by_role("checkbox").check()
                page.get_by_role("button", name="현재 X 계정 확인", exact=True).click()
                page.get_by_role("button", name="@Offline_User 연결", exact=True).wait_for(timeout=30000)
                self.assertEqual(urlsplit(page.url).fragment, "")
                # Explicit second click is required; viewing an account is not consent.
                self.assertIsNone(bridge._result)
                page.get_by_role("button", name="@Offline_User 연결", exact=True).click()
                page.get_by_text("계정 정보를 전달했습니다.", exact=False).wait_for(timeout=10000)
                result = bridge.wait()
                self.assertEqual(result.username, "offline_user")
                self.assertEqual(result.user_id, PAYLOAD["user_id"])
                self.assertEqual(result.auth_token, PAYLOAD["auth_token"])
                self.assertEqual(len(context.pages), 1)
            finally:
                context.close()


if __name__ == "__main__":
    unittest.main()

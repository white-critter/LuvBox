"""Offline request-protection tests; no X account or network requests."""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests

from luvbox.protection import AccountGuard, ProtectionError, cooldown_until


class ProtectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.guard = AccountGuard("offline", sleep=Mock(), root=self.root)
        self.guard.acquire()

    def tearDown(self):
        self.guard.close()
        self.temp.cleanup()

    def response(self, status=200, payload=None, headers=None):
        response = requests.Response()
        response.status_code = status
        response._content = json.dumps(payload or {}).encode()
        response._content_consumed = True
        response.headers.update(headers or {})
        if payload is not None:
            response.headers["Content-Type"] = "application/json"
        return response

    def test_server_deadlines_use_later_value_and_never_shorten_long_reset(self):
        now = 1_000_000.0
        self.assertEqual(cooldown_until({}, now), now + 900)
        self.assertEqual(cooldown_until({"Retry-After": "60", "x-rate-limit-reset": str(now + 7200)}, now), now + 7205)
        self.assertEqual(cooldown_until({"retry-after": "3600"}, now), now + 3605)
        stamp = format_datetime(datetime.fromtimestamp(now + 1800, timezone.utc), usegmt=True)
        self.assertEqual(cooldown_until({"RETRY-AFTER": stamp}, now), now + 1805)
        for value in ("bad", "nan", "inf", "-10"):
            self.assertEqual(cooldown_until({"Retry-After": value}, now), now + 900)

    def test_local_record_lock_does_not_remove_or_disable_download_cooldown(self):
        self.guard.record_limit({"Retry-After": "1800"}, fail=False)
        original = self.guard.state_path.read_bytes()
        self.guard.close()
        self.guard.acquire(respect_cooldown=False)
        other = AccountGuard("offline", sleep=Mock(), root=self.root)
        with self.assertRaises(ProtectionError):
            other.acquire(respect_cooldown=False)
        self.guard.close()
        with self.assertRaisesRegex(ProtectionError, "한국 시간"):
            other.acquire()
        self.assertEqual(self.guard.state_path.read_bytes(), original)

    def test_pacing_applies_to_media_fallbacks_api_and_session_redirect_send(self):
        session = SimpleNamespace(send=Mock(side_effect=lambda *args, **kwargs: self.response()))
        original = session.send
        self.guard.protect_session(session)
        with patch("luvbox.protection.time.monotonic", side_effect=[10, 10.2, 12, 20, 22, 30]):
            session.send(SimpleNamespace(url="https://pbs.twimg.com/media/photo?name=orig"))
            session.send(SimpleNamespace(url="https://pbs.twimg.com/media/photo?name=large"))
            session.send(SimpleNamespace(url="https://x.com/i/api/graphql/Likes"))
            session.send(SimpleNamespace(url="https://x.com/i/api/graphql/Likes?cursor=2"))
        self.assertEqual(original.call_count, 4)
        calls = self.guard.sleep.call_args_list
        self.assertAlmostEqual(calls[0].args[0], 1.8)
        self.assertAlmostEqual(calls[1].args[0], 8.0)

    def test_429_is_closed_before_retry_and_persists_across_guard_instances(self):
        response = self.response(429, headers={"Retry-After": "1800"})
        session = SimpleNamespace(send=Mock(return_value=response))
        self.guard.protect_session(session)
        with patch.object(response, "close", wraps=response.close) as close, \
                self.assertRaisesRegex(ProtectionError, "요청 한도"):
            session.send(SimpleNamespace(url="https://pbs.twimg.com/media/photo"))
        close.assert_called_once()
        self.assertTrue(self.guard.retry_at)
        self.guard.close()
        other = AccountGuard("OFFLINE", sleep=Mock(), root=self.root)
        with self.assertRaisesRegex(ProtectionError, "한국 시간"):
            other.acquire()
        # Expired state permits a new run and does not keep stale errors.
        self.guard.state_path.write_text(json.dumps({"until": time.time() - 1}), encoding="utf-8")
        other.acquire()
        other.close()

    def test_cooldown_write_failure_stops_all_further_requests(self):
        session = SimpleNamespace(send=Mock(return_value=self.response(429)))
        original = session.send
        self.guard.protect_session(session)
        request = SimpleNamespace(url="https://pbs.twimg.com/media/photo")
        with patch("luvbox.protection.os.fsync", side_effect=PermissionError), \
                self.assertRaisesRegex(ProtectionError, "보호 기록"):
            session.send(request)
        with self.assertRaisesRegex(ProtectionError, "보호 기록"):
            session.send(request)
        self.assertEqual(original.call_count, 1)
        self.assertEqual(list(self.root.glob("cooldown-*.tmp")), [])

    def test_transient_cooldown_replace_still_persists_for_the_next_run(self):
        original = os.replace
        calls = []

        def replace(source, target):
            calls.append(Path(target).name)
            if len(calls) == 1:
                raise PermissionError("scanner")
            return original(source, target)

        with patch("luvbox.file_mutex.os.replace", side_effect=replace), \
                patch("luvbox.file_mutex.time.sleep"):
            self.guard.record_limit({"Retry-After": "1800"}, fail=False)
        self.assertGreaterEqual(len(calls), 2)
        self.guard.close()
        other = AccountGuard("offline", sleep=Mock(), root=self.root)
        with self.assertRaisesRegex(ProtectionError, "한국 시간"):
            other.acquire()
        other.close()

    def test_real_requests_redirect_also_uses_protected_send_without_network(self):
        owner = self

        class OfflineAdapter(requests.adapters.BaseAdapter):
            def __init__(self):
                self.urls = []

            def send(self, request, **kwargs):
                self.urls.append(request.url)
                response = owner.response(302 if len(self.urls) == 1 else 429)
                response.url = request.url
                response.request = request
                if response.status_code == 302:
                    response.headers["Location"] = "/redirected"
                return response

            def close(self):
                pass

        session = requests.Session()
        session.trust_env = False
        adapter = OfflineAdapter()
        session.mount("https://", adapter)
        self.guard.protect_session(session)
        try:
            with patch("luvbox.protection.time.monotonic", side_effect=[1, 1.1, 11]), \
                    self.assertRaisesRegex(ProtectionError, "요청 한도"):
                session.get("https://x.com/original")
            self.assertEqual(adapter.urls, ["https://x.com/original", "https://x.com/redirected"])
            self.assertAlmostEqual(self.guard.sleep.call_args.args[0], 9.9)
        finally:
            session.close()

    def test_api_200_error88_also_stops_and_saves_cooldown(self):
        response = self.response(payload={"errors": [{"code": 88, "message": "private server detail"}]})
        with self.assertRaisesRegex(ProtectionError, "요청 한도") as error:
            self.guard.inspect_response(response, "api")
        self.assertNotIn("private", str(error.exception))
        self.assertTrue(self.guard.state_path.exists())

    def test_preemptive_library_abort_honors_last_positive_low_quota_reset(self):
        reset = time.time() + 7200
        self.guard.inspect_response(self.response(headers={"X-Rate-Limit-Remaining": "2",
                                                            "X-Rate-Limit-Reset": str(reset)}), "api")
        self.assertFalse(self.guard.failure)
        with self.assertRaisesRegex(ProtectionError, "요청 한도"):
            self.guard.record_limit()
        self.assertGreaterEqual(self.guard.until, reset)

    def test_low_positive_quota_stops_before_next_request_with_server_reset(self):
        reset = time.time() + 3600
        response = self.response(headers={"x-rate-limit-remaining": "5", "x-rate-limit-reset": str(reset)})
        session = SimpleNamespace(send=Mock(return_value=response))
        original = session.send
        self.guard.protect_session(session)
        request = SimpleNamespace(url="https://x.com/i/api/graphql/Likes")
        self.assertIs(session.send(request), response)
        self.assertGreaterEqual(self.guard.until, reset)
        with self.assertRaisesRegex(ProtectionError, "요청 한도"):
            session.send(request)
        self.assertEqual(original.call_count, 1)

    def test_more_than_five_remaining_does_not_impose_cooldown(self):
        response = self.response(headers={"x-rate-limit-remaining": "6", "x-rate-limit-reset": str(time.time() + 900)})
        self.guard.inspect_response(response, "api")
        self.guard.ensure_allowed()
        self.assertFalse(self.guard.state_path.exists())

    def test_exhausted_successful_response_stops_before_following_request(self):
        response = self.response(headers={"x-rate-limit-remaining": "0", "x-rate-limit-reset": str(time.time() + 900)})
        session = SimpleNamespace(send=Mock(return_value=response))
        self.guard.protect_session(session)
        request = SimpleNamespace(url="https://x.com/i/api/graphql/Likes")
        self.assertIs(session.send(request), response)
        with self.assertRaisesRegex(ProtectionError, "요청 한도"):
            session.send(request)
        self.assertEqual(session.send.__name__, "protected_send")

    def test_api_authentication_and_account_lock_fail_without_automated_retry(self):
        for status, payload in ((401, None), (403, None), (200, {"errors": [{"code": 89}]}),
                                (200, {"errors": [{"code": 326}]})):
            guard = AccountGuard("other", sleep=Mock(), root=self.root)
            with self.subTest(status=status, payload=payload), self.assertRaises(ProtectionError):
                guard.inspect_response(self.response(status, payload), "api")
            self.assertFalse(guard.state_path.exists())
            self.assertTrue(guard.failure)

    def test_cdn_404_can_fall_back_but_other_errors_stop_without_mislabeling_account(self):
        self.guard.inspect_response(self.response(404), "media")
        self.assertFalse(self.guard.failure)
        for status in (400, 403, 500, 503):
            guard = AccountGuard("media", sleep=Mock(), root=self.root)
            with self.subTest(status=status), self.assertRaisesRegex(ProtectionError, "사진 서버") as error:
                guard.inspect_response(self.response(status), "media")
            self.assertNotIn("계정", str(error.exception))

    def test_media_416_always_stops_and_blocks_later_requests_with_fixed_resume_help(self):
        for content_range in ("bytes */34", "bytes */35", None, "private server detail"):
            headers = {} if content_range is None else {"Content-Range": content_range}
            response = self.response(416, headers=headers)
            response.close = Mock()
            guard = AccountGuard("resume", sleep=Mock(), root=self.root)
            session = SimpleNamespace(send=Mock(return_value=response))
            original = session.send
            guard.protect_session(session)
            request = SimpleNamespace(url="https://video.twimg.com/private.mp4",
                                      headers={"Range": "bytes=34-"})
            for _ in range(2):
                with self.subTest(content_range=content_range), \
                        self.assertRaisesRegex(ProtectionError, "이어받기") as raised:
                    session.send(request)
                self.assertIn(".part 파일은 남겨", str(raised.exception))
                self.assertIn("다시 실행", str(raised.exception))
                self.assertNotIn("private", str(raised.exception))
            response.close.assert_called_once()
            self.assertEqual(original.call_count, 1)
            self.assertFalse(guard.state_path.exists())

    def test_api_json_timeout_and_server_errors_stop_before_repeat(self):
        for error in ({"message": "Timeout: private detail"}, {"code": 131, "message": "private detail"},
                      {"code": 130}, {"source": "Server", "message": "private detail"}):
            guard = AccountGuard("api", sleep=Mock(), root=self.root)
            session = SimpleNamespace(send=Mock(return_value=self.response(payload={"errors": [error]})))
            original = session.send
            guard.protect_session(session)
            request = SimpleNamespace(url="https://x.com/i/api/graphql/Likes")
            for _ in range(2):
                with self.subTest(error=error), self.assertRaisesRegex(ProtectionError, "목록 서버") as raised:
                    session.send(request)
                self.assertNotIn("private", str(raised.exception))
            self.assertEqual(original.call_count, 1)

    def test_transport_errors_stop_before_retry_or_media_size_fallback(self):
        for url, label in (("https://x.com/i/api/graphql/Likes", "목록 서버"),
                           ("https://pbs.twimg.com/media/photo", "사진 서버")):
            guard = AccountGuard("network", sleep=Mock(), root=self.root)
            session = SimpleNamespace(send=Mock(side_effect=requests.exceptions.Timeout("private URL/token")))
            original = session.send
            guard.protect_session(session)
            for _ in range(2):
                with self.subTest(url=url), self.assertRaisesRegex(ProtectionError, label) as raised:
                    session.send(SimpleNamespace(url=url))
                self.assertNotIn("private", str(raised.exception))
            self.assertEqual(original.call_count, 1)

    def test_invalid_persisted_cooldown_fails_closed(self):
        self.guard.close()
        for value in ('{"until":"nan"}', "{}", "invalid"):
            self.guard.state_path.write_text(value, encoding="utf-8")
            guard = AccountGuard("offline", sleep=Mock(), root=self.root)
            with self.subTest(value=value), self.assertRaisesRegex(ProtectionError, "보호 기록"):
                guard.acquire()
            self.assertIsNone(guard._lock_file)

    def test_lock_is_per_account_and_releases_after_worker_process_exit(self):
        other = AccountGuard("separate", sleep=Mock(), root=self.root)
        other.acquire()
        other.close()
        script = ("from pathlib import Path; from luvbox.protection import AccountGuard, ProtectionError; "
                  "import sys; guard=AccountGuard('offline', sleep=lambda *a: None, root=Path(sys.argv[1])); "
                  "guard.acquire()")
        result = subprocess.run([sys.executable, "-X", "utf8", "-c", script, str(self.root)], capture_output=True, text=True, encoding="utf-8")
        self.assertNotEqual(result.returncode, 0)
        self.guard.close()
        result = subprocess.run([sys.executable, "-X", "utf8", "-c", script, str(self.root)], capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)
        # Child held no explicit close(), and the OS still released its lock.
        self.guard.acquire()


if __name__ == "__main__":
    unittest.main()

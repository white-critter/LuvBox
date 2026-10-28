"""Authentication evidence uses private API replies and current worker jobs."""

from dataclasses import replace
from importlib.util import find_spec
import json
import logging
import os
import tempfile
import unittest
from unittest.mock import patch

import requests
from gallery_dl import exception

try:
    from . import test_core as support
except ImportError:
    import test_core as support
from luvbox.core import _GalleryLogHandler, _response_authentication_state


LIKES_URL = "https://x.com/i/api/graphql/query-id/Likes"
TIMELINE = {"data": {"user": {"result": {"timeline": {"timeline": {"instructions": []}}}}}}


def response(status=200, payload=None, *, json_type=True):
    result = requests.Response()
    result.status_code = status
    result._content = json.dumps(payload).encode("utf-8")
    result._content_consumed = True
    if json_type:
        result.headers["Content-Type"] = "application/json"
    return result


class ResponseEvidenceTests(unittest.TestCase):
    def test_private_likes_timeline_including_empty_list_verifies_session(self):
        self.assertEqual(_response_authentication_state(response(payload=TIMELINE), LIKES_URL), "verified")

    def test_public_pages_media_and_other_api_success_never_verify_session(self):
        for url in ("https://x.com/home", "https://pbs.twimg.com/media/photo.jpg",
                    "https://x.com/i/api/graphql/query-id/UserByScreenName",
                    "http://x.com/i/api/graphql/query-id/Likes",
                    "https://x.com.evil.invalid/i/api/graphql/query-id/Likes",
                    "https://user@x.com/i/api/graphql/query-id/Likes"):
            with self.subTest(url=url):
                self.assertEqual(_response_authentication_state(response(payload=TIMELINE), url), "")

    def test_only_explicit_credential_rejection_requires_reconnect(self):
        self.assertEqual(_response_authentication_state(response(401), LIKES_URL), "reconnect_required")
        for status in (200, 403):
            for code in (32, 89, 135, 215):
                with self.subTest(status=status, code=code):
                    result = response(status, {"errors": [{"code": code, "message": support.SECRET}]})
                    self.assertEqual(_response_authentication_state(result, LIKES_URL), "reconnect_required")

    def test_rate_limits_access_denials_and_challenges_do_not_revoke_session(self):
        for status, payload in ((403, None), (403, {"errors": [{"message": "Forbidden"}]}),
                                (429, {"errors": [{"code": 89}]}), (500, TIMELINE),
                                (200, {"errors": [{"code": 326}]}),
                                (200, {"errors": [{"code": 88}, {"code": 89}]}),
                                (200, {"errors": [{"code": 420}, {"code": 89}]})):
            with self.subTest(status=status, payload=payload):
                self.assertEqual(_response_authentication_state(response(status, payload), LIKES_URL), "")

    def test_invalid_unknown_or_partial_json_does_not_verify_session(self):
        for payload in (None, [], {}, {"data": {}},
                        {"data": {"user": {"result": {"timeline": {"timeline": {"instructions": {}}}}}}},
                        {**TIMELINE, "errors": [{"message": "unknown failure"}]}):
            with self.subTest(payload=payload):
                self.assertEqual(_response_authentication_state(response(payload=payload), LIKES_URL), "")
        self.assertEqual(_response_authentication_state(response(payload=TIMELINE, json_type=False), LIKES_URL), "")
        invalid = response()
        invalid._content = b"invalid-json"
        self.assertEqual(_response_authentication_state(invalid, LIKES_URL), "")

    def test_media_auth_denial_does_not_revoke_session(self):
        self.assertEqual(_response_authentication_state(response(401), "https://video.twimg.com/clip.mp4"), "")

    def test_media_log_message_cannot_revoke_the_x_account(self):
        handler = _GalleryLogHandler(lambda event: None)
        handler.emit(logging.LogRecord("download", logging.ERROR, __file__, 1,
                                      "AuthenticationError: media access denied", (), None))
        self.assertEqual(handler.auth_state, "")

    def test_visibility_and_authorization_log_messages_do_not_revoke_session(self):
        for message in ("AuthRequired: Protected Tweet", "AuthRequired: NSFW Tweet",
                        "AuthRequired: Protected Tweet (401)", "AuthRequired: NSFW Tweet: 403",
                        "AuthorizationError: Account temporarily locked", "HttpError: 403 Forbidden",
                        "Rate limit exceeded", "SSL certificate error"):
            with self.subTest(message=message):
                handler = _GalleryLogHandler(lambda event: None)
                handler.emit(logging.LogRecord("twitter", logging.ERROR, __file__, 1, message, (), None))
                self.assertEqual(handler.auth_state, "")


class WorkerEvidenceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="luvbox-auth-health-")
        self.addCleanup(temporary.cleanup)
        environment = patch.dict(os.environ, {"LOCALAPPDATA": temporary.name})
        environment.start()
        self.addCleanup(environment.stop)
        self.fixture = support.CoreTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.addCleanup(self.fixture.tearDown)
        self.options = self.fixture.options
        token = patch("luvbox.login.load_session_cookies", return_value=[{
            "name": "auth_token", "value": support.SECRET, "domain": ".x.com", "path": "/",
            "expires": -1, "secure": True, "httpOnly": True}])
        token.start()
        self.addCleanup(token.stop)

    def worker_with_reply(self, result):
        def request(extractor, row):
            prepared = requests.Request("GET", LIKES_URL).prepare()
            extractor.session.send(prepared)

        with patch("requests.Session.send", return_value=result):
            return self.fixture.worker([("100", 1, "photo", "alice")], options=self.options, before_row=request)

    def test_real_session_observer_emits_verification_and_final_evidence(self):
        code, events = self.worker_with_reply(response(payload=TIMELINE))
        self.assertEqual(code, 0)
        self.assertEqual([event for event in events if event["type"] == "authentication"],
                         [{"type": "authentication", "auth_state": "verified"}])
        self.assertEqual(events[-1]["auth_state"], "verified")
        self.assertNotIn(support.SECRET, json.dumps(events))

    def test_auth_rejection_is_observed_before_protection_aborts_the_request(self):
        code, events = self.worker_with_reply(response(401))
        self.assertNotEqual(code, 0)
        self.assertEqual(events[-1]["auth_state"], "reconnect_required")
        self.assertEqual(events[-1]["error_kind"], "authentication")
        self.assertEqual([event["auth_state"] for event in events if event["type"] == "authentication"],
                         ["reconnect_required"])

    def test_generic_403_is_stopped_without_claiming_an_expired_login(self):
        code, events = self.worker_with_reply(response(403))
        self.assertNotEqual(code, 0)
        self.assertNotIn("auth_state", events[-1])
        self.assertFalse(any(event["type"] == "authentication" for event in events))

    def test_local_save_success_without_private_request_is_not_verification(self):
        code, events = self.fixture.worker([("100", 1, "photo", "alice")], options=self.options)
        self.assertEqual(code, 0)
        self.assertNotIn("auth_state", events[-1])

    def test_caught_typed_credential_failure_requires_reconnect(self):
        for error in (exception.AuthRequired("authenticated cookies"), exception.AuthenticationError()):
            with self.subTest(error=type(error).__name__):
                code, events = self.fixture.worker(options=self.options, error=error)
                self.assertNotEqual(code, 0)
                self.assertEqual(events[-1]["auth_state"], "reconnect_required")
                self.assertEqual(events[-1]["error_kind"], "authentication")

    def test_later_catalog_failure_is_preserved_after_authentication_rejection(self):
        from luvbox.history import HistoryError
        with patch("luvbox.history.HistoryStore.finish_run", side_effect=HistoryError("synthetic catalog failure")):
            code, events = self.worker_with_reply(response(401))
        self.assertNotEqual(code, 0)
        self.assertEqual(events[-1]["auth_state"], "reconnect_required")
        self.assertNotIn("error_kind", events[-1])


@unittest.skipUnless(find_spec("PySide6") is not None, "optional Qt dependencies are not installed")
class QtEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtCore import QCoreApplication
        cls.application = QCoreApplication.instance() or QCoreApplication([])

    def controller(self):
        from luvbox.qt_download import DownloadController
        try:
            from .test_qt_download import _Events
        except ImportError:
            from test_qt_download import _Events
        service = _Events()
        controller = DownloadController(service=service)
        self.addCleanup(controller.shutdown)
        self.assertTrue(controller.start(object()))
        controller._timer.stop()
        states = []
        controller.authenticationChanged.connect(states.append)
        return controller, service, states

    def test_only_current_running_job_with_typed_auth_event_changes_health(self):
        controller, service, states = self.controller()
        service.pending.extend((
            {"type": "authentication", "job_id": 1, "auth_state": "reconnect_required"},
            {"type": "progress", "job_id": 7, "auth_state": "verified"},
            {"type": "authentication", "job_id": 7, "auth_state": {"secret": "no"}},
            {"type": "authentication", "job_id": 7, "auth_state": "verified"},
            {"type": "authentication", "job_id": 7, "auth_state": "verified"},
            {"type": "finished", "job_id": 7, "status": "error", "auth_state": "reconnect_required"},
            {"type": "authentication", "job_id": 7, "auth_state": "verified"},
            {"type": "process_exit", "job_id": 7, "code": 1},
        ))
        controller._pump()
        self.assertEqual(states, ["verified", "reconnect_required"])
        self.assertFalse(controller.running)
        service.pending.append({"type": "authentication", "job_id": 7, "auth_state": "verified"})
        controller._pump()
        self.assertEqual(states, ["verified", "reconnect_required"])

    def test_shutdown_drops_authentication_updates(self):
        controller, service, states = self.controller()
        controller.shutdown()
        service.pending.append({"type": "authentication", "job_id": 7, "auth_state": "verified"})
        controller._pump()
        self.assertEqual(states, [])

    def test_new_job_resets_previous_reconnect_evidence(self):
        controller, service, states = self.controller()
        service.pending.extend((
            {"type": "authentication", "job_id": 7, "auth_state": "reconnect_required"},
            {"type": "process_exit", "job_id": 7, "code": 1},
        ))
        controller._pump()
        service.job_id = 8
        self.assertTrue(controller.start(object()))
        controller._timer.stop()
        service.pending.append({"type": "authentication", "job_id": 8, "auth_state": "verified"})
        controller._pump()
        self.assertEqual(states, ["reconnect_required", "verified"])

    def test_new_connection_clears_only_an_idle_typed_authentication_failure(self):
        controller, service, _states = self.controller()
        service.pending.extend((
            {"type": "finished", "job_id": 7, "status": "error", "message": "연결을 다시 확인해 주세요.",
             "auth_state": "reconnect_required", "error_kind": "authentication"},
            {"type": "process_exit", "job_id": 7, "code": 1},
        ))
        controller._pump()
        self.assertNotEqual(controller.error, "")
        controller.clearAuthenticationError()
        self.assertEqual(controller.error, "")
        self.assertEqual(controller.status, "저장 대기 중")
        self.assertIsNone(controller._final)

    def test_reconnect_does_not_clear_running_or_unrelated_failure(self):
        for final in (
                {"auth_state": "reconnect_required", "error_kind": "authentication"},
                {"auth_state": "reconnect_required"},
                {"auth_state": "reconnect_required", "error_kind": "authentication", "catalog_damaged": True}):
            with self.subTest(final=final):
                controller, service, _states = self.controller()
                service.pending.append({"type": "finished", "job_id": 7, "status": "error",
                                        "message": "기존 실패 안내", **final})
                controller._pump()
                controller.clearAuthenticationError()
                self.assertEqual(controller.error, "기존 실패 안내")
                service.pending.append({"type": "process_exit", "job_id": 7, "code": 1})
                controller._pump()
                if final != {"auth_state": "reconnect_required", "error_kind": "authentication"}:
                    controller.clearAuthenticationError()
                    self.assertEqual(controller.error, "기존 실패 안내")

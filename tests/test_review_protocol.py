"""The download child accepts only a live parent's matching file decision."""

import json
import os
from pathlib import Path
import queue
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from luvbox.review import ReviewChannel, ReviewControl, ReviewUnavailable


class Stopped(Exception):
    pass


class ReviewProtocolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.path = self.base / "review-control.json"
        self.source = self.base / "100_1.jpg.part"
        self.source.write_bytes(b"a newly downloaded temporary fixture")
        self.control = ReviewControl(True)
        self.control.attach(self.path)
        self.addCleanup(self.control.detach)
        self.events = queue.Queue()
        self.channel = ReviewChannel(self.path, self.events.put)
        self.stop = threading.Event()
        self.results = queue.Queue()

    def check_stop(self):
        if self.stop.is_set():
            raise Stopped()

    def begin(self):
        def work():
            try:
                value = self.channel.ask(self.source,
                                         {"tweet_id": "100", "type": "photo", "author": "fixture",
                                          "url": "https://example.invalid/private", "cookies": "not shared"},
                                         self.check_stop)
            except Exception as exc:
                value = exc
            self.results.put(value)

        worker = threading.Thread(target=work, daemon=True)
        worker.start()

        def finish():
            self.stop.set()
            worker.join(2)
            self.assertFalse(worker.is_alive())

        self.addCleanup(finish)
        return self.events.get(timeout=2)

    def result(self):
        return self.results.get(timeout=2)

    def test_current_candidate_save_skip_and_never(self):
        for action in ("save", "skip", "never"):
            event = self.begin()
            self.assertEqual(event["type"], "review-request")
            self.assertEqual(event["media_type"], "photo")
            self.assertEqual(event["path"], str(self.source))
            self.assertNotIn("url", event)
            self.assertNotIn("cookies", event)
            self.control.respond(event["id"], action)
            self.assertEqual(self.result(), action)
            self.assertTrue(self.source.is_file())

    def test_old_decision_does_not_approve_next_file(self):
        self.control.respond("0" * 32, "save")
        event = self.begin()
        with self.assertRaises(queue.Empty):
            self.results.get(timeout=.12)
        self.control.respond(event["id"], "skip")
        self.assertEqual(self.result(), "skip")

    def test_turning_off_releases_confirmation_and_can_be_enabled_again(self):
        self.begin()
        self.control.set_enabled(False)
        self.assertEqual(self.result(), "save")
        self.assertFalse(self.channel.enabled())
        self.control.set_enabled(True)
        event = self.begin()
        self.control.respond(event["id"], "skip")
        self.assertEqual(self.result(), "skip")

    def test_changed_source_is_never_approved(self):
        event = self.begin()
        self.source.write_bytes(b"replaced while being viewed")
        self.control.respond(event["id"], "save")
        self.assertIsInstance(self.result(), ReviewUnavailable)

    def test_stop_interrupts_a_wait_without_approval(self):
        self.begin()
        self.stop.set()
        self.assertIsInstance(self.result(), Stopped)

    def test_stale_parent_heartbeat_never_becomes_approval(self):
        event = self.begin()
        value = json.loads(self.path.read_text(encoding="utf8"))
        value["decision"] = {"id": event["id"], "action": "save"}
        value["heartbeat"] = time.time() - 60
        self.path.write_text(json.dumps(value), encoding="utf8")
        self.assertIsInstance(self.result(), ReviewUnavailable)

    def test_late_heartbeat_after_sleep_is_accepted_but_never_as_approval(self):
        value = json.loads(self.path.read_text(encoding="utf8"))
        value["heartbeat"] = time.time() - 60
        self.path.write_text(json.dumps(value), encoding="utf8")
        beat = threading.Timer(.3, self.control.heartbeat)
        beat.start()
        self.addCleanup(beat.cancel)
        self.assertTrue(self.channel.enabled())

    def test_briefly_unreadable_control_is_read_again(self):
        held = self.path.with_name("held.json")
        self.path.replace(held)
        restore = threading.Timer(.2, lambda: held.replace(self.path))
        restore.start()
        self.addCleanup(restore.cancel)
        self.assertTrue(self.channel.enabled())

    def test_new_parent_session_cannot_adopt_a_waiting_child(self):
        self.begin()
        self.control.attach(self.path)
        self.assertIsInstance(self.result(), ReviewUnavailable)

    def test_missing_oversized_or_invalid_control_fails_closed(self):
        for content in (b"not json", b"{}", b"x" * 4097):
            self.path.write_bytes(content)
            with self.assertRaises(ReviewUnavailable):
                self.channel.enabled()
        self.path.unlink()
        with self.assertRaises(ReviewUnavailable):
            self.channel.enabled()

    def test_transient_windows_sharing_error_retries_atomic_replace(self):
        original = os.replace
        calls = []

        def replace(source, destination):
            calls.append(None)
            if len(calls) == 1:
                raise PermissionError("temporary sharing")
            return original(source, destination)

        with patch("luvbox.review.os.replace", side_effect=replace):
            self.control.set_enabled(False)
        self.assertFalse(self.channel.enabled())
        self.assertEqual(len(calls), 2)
        self.assertFalse(self.path.with_suffix(".tmp").exists())

    def test_concurrent_heartbeat_preserves_the_latest_decision(self):
        ident = "f" * 32
        worker = threading.Thread(target=lambda: [self.control.heartbeat() for _ in range(20)])
        worker.start()
        self.control.respond(ident, "skip")
        worker.join(2)
        self.assertFalse(worker.is_alive())
        state = json.loads(self.path.read_text(encoding="utf8"))
        self.assertEqual(state["decision"], {"id": ident, "action": "skip"})

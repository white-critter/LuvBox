"""Live GUI confirmation settings, decisions and unattended separation."""

import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import test_gui as fixtures
from luvbox.recycling import file_signature
from luvbox.review import ReviewControl
from luvbox.storage import resolve_storage


class AppReviewTests(unittest.TestCase):
    setUp = fixtures.GuiTests.setUp

    def candidate(self):
        source = Path(self.temp.name) / "100_1.jpg.part"
        source.write_bytes(b"new temporary image")
        self.app._review_context = SimpleNamespace(media_dir=source.parent)
        self.app.running = True
        return {"type": "review-request", "id": "a" * 32, "path": str(source),
                "signature": list(file_signature(source)), "media_type": "photo", "filename": "100_1.jpg"}

    def test_live_toggle_defaults_off_and_remains_available_while_saving(self):
        self.assertFalse(self.app.confirm_each.get())
        self.app.running = True
        self.app._set_running(True)
        self.assertEqual(str(self.app.review_toggle.cget("state")), "normal")
        self.app.confirm_each.set(True)
        stored = json.loads((Path(self.temp.name) / "settings.json").read_text(encoding="utf8"))
        self.assertTrue(stored["confirm_each"])
        self.assertIn("하나씩 확인", self.app.option_summary.get())
        self.assertNotIn("하나씩 확인", self.app.scheduled_option_summary.get())

    def test_manual_worker_option_is_on_but_scheduled_option_is_off(self):
        self.app.account.set("fixture")
        self.app.folder.set(self.temp.name)
        self.app.confirm_each.set(True)
        self.assertTrue(self.app._validated_options().confirm_each)
        self.assertFalse(self.app._validated_options(scheduled=True).confirm_each)

    def test_options_cancel_restores_the_confirmation_preference(self):
        self.app.show_options()
        self.app.confirm_each.set(True)
        self.app._close_options(save=False, restore=True)
        self.assertFalse(self.app.confirm_each.get())
        self.assertFalse((Path(self.temp.name) / "settings.json").exists())

    def test_off_request_is_approved_without_opening_a_preview(self):
        event = self.candidate()
        with patch.object(self.app.review_control, "respond") as respond:
            self.app._show_review(event)
        respond.assert_called_once_with(event["id"], "save", enabled=False)
        self.assertIsNone(self.app.review_window)

    def test_changed_candidate_stops_before_opening_a_preview(self):
        event = self.candidate()
        self.app.confirm_each.set(True)
        Path(event["path"]).write_bytes(b"changed")
        with patch.object(self.app, "stop") as stop:
            self.app._show_review(event)
        stop.assert_called_once()
        self.assertIsNone(self.app.review_window)

    def test_off_waits_for_decoder_before_releasing_the_child(self):
        event = self.candidate()
        self.app.confirm_each.set(True)
        window = Mock()
        window.winfo_exists.return_value = True
        self.app.review_window = window
        self.app._review_candidate = event
        with patch.object(self.app.review_control, "set_enabled") as enable, \
                patch.object(self.app.review_control, "respond") as respond:
            self.app.confirm_each.set(False)
            window.choose.assert_called_once_with("auto")
            enable.assert_not_called()
            respond.assert_not_called()
            self.app._review_decision(event["id"], "auto")
            window.destroy.assert_called_once()
            respond.assert_called_once_with(event["id"], "save", enabled=False)
        self.assertIsNone(self.app.review_window)

    def test_last_live_toggle_is_preserved_while_decoder_closes(self):
        event = self.candidate()
        self.app.confirm_each.set(True)
        window = Mock()
        window.winfo_exists.return_value = True
        self.app.review_window = window
        self.app._review_candidate = event
        with patch.object(self.app.review_control, "respond") as respond:
            self.app.confirm_each.set(False)
            self.app.confirm_each.set(True)
            self.app._review_decision(event["id"], "auto")
            self.assertTrue(self.app.confirm_each.get())
            respond.assert_called_once_with(event["id"], "save", enabled=True)

    def test_stale_decision_does_not_answer_the_current_file(self):
        event = self.candidate()
        self.app._review_candidate = event
        with patch.object(self.app.review_control, "respond") as respond:
            self.app._review_decision("b" * 32, "save")
        respond.assert_not_called()
        self.assertEqual(self.app._review_candidate, event)

    def test_choice_skip_does_not_inflate_success_or_existing_counts(self):
        self.app.events.put({"type": "review-skipped", "filename": "100_1.jpg", "tweet_id": "100"})
        self.app._poll()
        self.assertEqual((self.app.saved, self.app.skipped, self.app.size_skipped, self.app.review_skipped), (0, 0, 0, 1))
        self.assertIn("이번엔 건너뜀 1개", self.app.review_summary.get())

    def test_never_choice_is_counted_and_passed_to_the_child(self):
        self.app.events.put({"type": "review-never", "filename": "100_1.jpg", "tweet_id": "100"})
        self.app._poll()
        self.assertEqual((self.app.saved, self.app.review_skipped, self.app.review_never), (0, 0, 1))
        self.assertIn("저장 안 함 1개", self.app.review_summary.get())
        event = self.candidate()
        self.app._review_candidate = event
        self.app.confirm_each.set(True)
        with patch.object(self.app.review_control, "respond") as respond:
            self.app._review_decision(event["id"], "never")
        respond.assert_called_once_with(event["id"], "never", enabled=True)
        self.assertFalse(self.app.stopping)

    def test_stop_marks_the_child_flag_and_destroys_pending_preview(self):
        event = self.candidate()
        flag = self.app.stop_path = Path(self.temp.name) / "stop.flag"
        window = Mock()
        window.winfo_exists.return_value = True
        self.app.review_window = window
        self.app._review_candidate = event
        self.app.stop()
        self.assertTrue(flag.is_file())
        self.assertTrue(self.app.stopping)
        window.destroy.assert_called_once()
        self.assertIsNone(self.app._review_candidate)


class ScheduledReviewTests(unittest.TestCase):
    def test_saved_confirmation_preference_never_blocks_an_unattended_run(self):
        import os
        import tempfile
        from luvbox.scheduled import _options_from_settings
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"LOCALAPPDATA": folder}):
            settings = {"account": "fixture", "output_dir": folder, "confirm_each": True}
            options = _options_from_settings(settings, False)
            self.assertFalse(options.confirm_each)

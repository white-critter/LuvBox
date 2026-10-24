"""GUI event ordering, bounded work, batching, and poll lifecycle."""

import gc
import tkinter as tk
import unittest
from unittest.mock import patch

from tests import test_gui as fixtures
from tests.legacy_ui import previous_window
from luvbox.gui import App


@previous_window
class UIEventBudgetTests(unittest.TestCase):
    setUp = fixtures.GuiTests.setUp

    def poll_timers(self, root=None):
        root = root or self.root
        timers = root.tk.call("after", "info")
        return [timer for timer in timers
                if str(root.tk.call("after", "info", timer)[0]).endswith("_poll")]

    def test_consecutive_progress_is_coalesced_without_crossing_state_boundaries(self):
        observed = []
        events = [
            {"type": "progress", "downloaded": 1},
            {"type": "progress", "downloaded": 2},
            {"type": "log", "message": "boundary"},
            {"type": "progress", "downloaded": 3},
            {"type": "progress", "downloaded": 4},
            {"type": "browser-login"},
            {"type": "progress", "downloaded": 5},
            {"type": "review-request"},
            {"type": "converting"},
            {"type": "finished", "status": "finished", "saved": 7, "message": "done"},
            {"type": "process_exit", "code": 0},
        ]
        for event in events:
            self.app.events.put(event)
        log = self.app._log
        def record_log(message, event=None):
            observed.append(("log", message))
            log(message, event)
        with patch("luvbox.gui.time.perf_counter", return_value=0), \
                patch.object(self.app, "_update_progress", side_effect=lambda e: observed.append(("progress", e["downloaded"]))), \
                patch.object(self.app, "_browser_result", side_effect=lambda e: observed.append(("login",))), \
                patch.object(self.app, "_show_review", side_effect=lambda e: observed.append(("review",))), \
                patch.object(self.app, "_update_conversion", side_effect=lambda e: observed.append(("conversion",))), \
                patch.object(self.app, "_log", side_effect=record_log):
            self.app._poll()
        self.assertEqual(observed, [("progress", 2), ("log", "boundary"), ("progress", 4),
                                    ("login",), ("progress", 5), ("review",),
                                    ("conversion",), ("log", "done")])
        self.assertEqual(self.app.saved, 7)
        self.assertTrue(self.app.received_final)
        self.assertFalse(self.app.running)
        self.assertEqual(self.app.progress["value"], 100)
        self.assertIn("done", self.app.log.get("1.0", "end"))

    def test_slow_callbacks_yield_and_backlog_is_rescheduled_in_eight_ms(self):
        clock = [0.]
        observed = []
        def receive(event):
            observed.append(event["number"])
            clock[0] += .003
        for number in range(10):
            self.app.events.put({"type": "browser-login", "number": number})
        with patch("luvbox.gui.time.perf_counter", side_effect=lambda: clock[0]), \
                patch.object(self.app, "_browser_result", side_effect=receive), \
                patch.object(self.root, "after", wraps=self.root.after) as after:
            self.app._poll()
            self.assertEqual(observed, [0, 1])
            self.assertEqual(after.call_args.args[0], 8)
            self.assertEqual(len(self.poll_timers()), 1)
            while not self.app.events.empty():
                self.app._poll()
            self.assertEqual(after.call_args.args[0], 120)
        self.assertEqual(observed, list(range(10)))
        self.assertEqual(len(self.poll_timers()), 1)

    def test_progress_boundary_is_retained_if_rendering_exhausts_the_budget(self):
        clock = [0.]
        observed = []
        def render(event):
            observed.append(("progress", event["downloaded"]))
            clock[0] += .006
        for event in ({"type": "progress", "downloaded": 1}, {"type": "browser-login"},
                      {"type": "progress", "downloaded": 2}):
            self.app.events.put(event)
        with patch("luvbox.gui.time.perf_counter", side_effect=lambda: clock[0]), \
                patch.object(self.app, "_update_progress", side_effect=render), \
                patch.object(self.app, "_browser_result", side_effect=lambda e: observed.append(("login",))):
            self.app._poll()
            self.assertEqual(observed, [("progress", 1)])
            self.assertEqual(self.app._deferred_event["type"], "browser-login")
            self.app._poll()
        self.assertEqual(observed, [("progress", 1), ("login",), ("progress", 2)])
        self.assertIsNone(self.app._deferred_event)

    def test_saved_counts_and_log_writes_are_batched_without_losing_lines_or_tags(self):
        for number in range(300):
            self.app.events.put({"type": "saved", "filename": f"fixture-{number}.jpg"})
        self.app.events.put({"type": "log", "message": "warning-message", "level": "warning"})
        self.app.events.put({"type": "log", "message": "error-message", "level": "error"})
        with patch("luvbox.gui.time.perf_counter", return_value=0), \
                patch.object(self.app.log, "insert", wraps=self.app.log.insert) as insert, \
                patch.object(self.app, "_update_counts", wraps=self.app._update_counts) as counts:
            self.app._poll()
        lines = self.app.log.get("1.0", "end").strip().splitlines()
        self.assertEqual(len(lines), 302)
        for number, line in enumerate(lines[:300]):
            self.assertIn(f"fixture-{number}.jpg", line)
        self.assertLess(insert.call_count, 10)
        counts.assert_called_once()
        self.assertEqual(self.app.saved, 300)
        self.assertIn("300", self.app.saved_text.get())
        self.assertEqual(len(self.app.log.tag_ranges("warning")), 2)
        self.assertEqual(len(self.app.log.tag_ranges("error")), 2)
        self.assertEqual(str(self.app.log.cget("state")), "disabled")

    def test_manual_polls_replace_the_timer_without_leaking_tcl_commands(self):
        commands = len(self.root._tclCommands)
        previous = self.app._poll_timer
        for _ in range(20):
            self.app._poll()
            self.assertNotIn(previous, self.root.tk.call("after", "info"))
            self.assertEqual(self.poll_timers(), [self.app._poll_timer])
            self.assertEqual(len(self.root._tclCommands), commands)
            previous = self.app._poll_timer

    def test_progress_updates_stop_animation_only_when_switching_modes(self):
        self.app.progress.configure(mode="indeterminate")
        self.app.progress.start(12)
        with patch.object(self.app.progress, "stop", wraps=self.app.progress.stop) as stop:
            self.app._update_progress({"downloaded": 20, "total": 100})
            timer = self.app.progress._timer
            self.app._update_progress({"downloaded": 40, "total": 100})
            self.app._update_progress({"downloaded": 60, "total": 100})
        stop.assert_called_once()
        self.assertEqual(self.app.progress._timer, timer)
        self.assertEqual(self.app.progress["value"], 60)

    def test_destroy_cancels_poll_and_a_late_manual_poll_cannot_restart_it(self):
        root = tk.Tk()
        app = App(root)
        timer = app._poll_timer
        root.destroy()
        self.assertTrue(app._ui_closed)
        self.assertIsNone(app._poll_timer)
        self.assertNotIn(timer, root.tk.call("after", "info"))
        app._poll()
        self.assertIsNone(app._poll_timer)
        app = root = None
        gc.collect()


if __name__ == "__main__":
    unittest.main()

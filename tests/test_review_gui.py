"""The save chooser stays non-modal and waits for decoder handle cleanup."""

from pathlib import Path
import queue
import tempfile
from threading import Event
import time
import tkinter as tk
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PIL import Image

from tests.test_gui import destroy_tk, release_tk
from luvbox import theme
from luvbox.review_gui import ReviewWindow
from luvbox.review_preview import ReviewFrame


class ReviewGuiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = tk.Tk()
        self.root.withdraw()
        theme.apply_style(self.root, "light")
        self.addCleanup(release_tk, self, "window", "root")
        self.addCleanup(destroy_tk, self.root)
        self.addCleanup(theme.apply_style, self.root, "light")
        self.path = Path(self.temporary.name) / "선택 사진.part"
        Image.new("RGB", (120, 80), "red").save(self.path, "JPEG")
        self.decisions = []
        self.finished = Event()
        self.finished.set()
        self.decoder = SimpleNamespace(frames=queue.Queue(maxsize=3), finished=self.finished, close=Mock())
        self.patch_preview = patch("luvbox.review_gui.ReviewPreview", return_value=self.decoder)
        self.factory = self.patch_preview.start()
        self.addCleanup(self.patch_preview.stop)

    def window_for(self, **kwargs):
        candidate = {"id": "test", "path": str(self.path), "media_type": "photo", "author": "test_user",
                     "filename": "선택 사진.jpg", "posted_at": "2026-10-12"}
        self.window = ReviewWindow(self.root, candidate, self.decisions.append,
                                  kwargs.pop("permitted", lambda _path: True))
        return self.window

    def test_window_does_not_grab_main_window_and_skip_is_default_focus(self):
        window = self.window_for()
        window.update_idletasks()
        self.assertIsNone(window.grab_current())
        self.assertEqual(window.buttons["skip"].cget("text"), "이번엔 건너뛰기")
        self.assertIn("@test_user", window.details.cget("text"))
        self.assertIn("선택 사진.jpg", window.details.cget("text"))
        self.assertIn("파일을 먼저 받습니다", window.explanation.cget("text"))
        self.factory.assert_called_once_with(self.path, "photo")

    def test_each_decision_is_delivered_once_after_destroy(self):
        for action in ("save", "skip", "never", "stop", "auto"):
            with self.subTest(action=action):
                window = self.window_for()
                self.decisions.clear()
                window.choose(action)
                window.choose("save")
                self.assertFalse(window.winfo_exists())
                self.assertEqual(self.decisions, [action])
                self.assertIsNone(window._on_decision)

    def test_choice_waits_without_blocking_for_decoder_completion(self):
        self.finished.clear()
        window = self.window_for()
        started = time.monotonic()
        window.choose("save")
        self.assertLess(time.monotonic() - started, .2)
        self.assertTrue(window.winfo_exists())
        self.assertEqual(self.decisions, [])
        self.assertTrue(all(button.instate(["disabled"]) for button in window.buttons.values()))
        self.finished.set()
        window._finish_choice()
        self.assertEqual(self.decisions, ["save"])
        self.assertFalse(window.winfo_exists())

    def test_user_close_stops_but_external_destroy_has_no_decision(self):
        window = self.window_for()
        window.cancel()
        self.assertEqual(self.decisions, ["stop"])
        self.decisions.clear()
        window = self.window_for()
        window.destroy()
        self.assertEqual(self.decisions, [])
        window.destroy()
        self.decoder.close.assert_called()

    def test_unreleased_source_never_sends_save_or_auto(self):
        self.decoder.release_error = True
        for action in ("save", "auto"):
            with self.subTest(action=action):
                self.decisions.clear()
                window = self.window_for()
                window.choose(action)
                self.assertEqual(self.decisions, ["stop"])

    def test_permission_is_checked_on_ui_thread_and_unsafe_path_cannot_save(self):
        import threading
        calls = []
        window = self.window_for(permitted=lambda path: calls.append((path, threading.current_thread())) or False)
        self.assertEqual(calls, [(self.path, threading.current_thread())])
        self.factory.assert_not_called()
        window.choose("save")
        self.assertEqual(self.decisions, [])
        self.assertIn("파일 위치", window.note.cget("text"))
        window.choose("skip")
        self.assertEqual(self.decisions, ["skip"])

    def test_animation_frames_are_rendered_on_main_thread_and_released_on_destroy(self):
        window = self.window_for()
        image = Image.new("RGB", (32, 32), "blue")
        self.decoder.frames.put(ReviewFrame(image, 83, "GIF · 소리 없이 재생"))
        window.update_idletasks()
        window._poll()
        self.assertIsNotNone(window._photo)
        self.assertEqual(window.note.cget("text"), "GIF · 소리 없이 재생")
        window.destroy()
        self.assertIsNone(window._photo)
        self.assertIsNone(window._frame_image)
        self.assertIsNone(window._poll_timer)

    def test_dark_and_short_screen_keep_all_actions_visible(self):
        for mode in ("light", "dark"):
            with self.subTest(mode=mode), patch.object(tk.Toplevel, "winfo_screenheight", return_value=600):
                theme.apply(self.root, mode)
                window = self.window_for()
                window.attributes("-alpha", 0)
                window.update()
                self.assertEqual(window.cget("background"), (theme.DARK if mode == "dark" else theme.LIGHT)["BG"])
                for button in window.buttons.values():
                    top = button.winfo_rooty() - window.winfo_rooty()
                    self.assertGreaterEqual(top, 0)
                    self.assertLessEqual(top + button.winfo_height(), window.winfo_height())
                window.destroy()

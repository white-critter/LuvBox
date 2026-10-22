"""The save chooser stays non-modal and waits for decoder handle cleanup."""

from pathlib import Path
import queue
import tempfile
from threading import Event
import time
import tkinter as tk
from tkinter import ttk
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

    def test_peer_actions_have_equal_widths_and_all_actions_have_equal_heights(self):
        window = self.window_for()
        window.update()
        for mode in ("light", "dark"):
            with self.subTest(mode=mode):
                theme.apply(self.root, mode)
                window.update()
                heights = {button.winfo_height() for button in window.buttons.values()}
                self.assertEqual(len(heights), 1)
                self.assertNotIn(1, heights)
                for actions in (("save", "skip", "never"), ("auto", "stop")):
                    widths = [window.buttons[action].winfo_width() for action in actions]
                    self.assertLessEqual(max(widths) - min(widths), 1)

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

    def test_frame_deadlines_keep_cadence_and_hold_a_long_gif_frame(self):
        window = self.window_for()
        clock = [100.0]
        with patch("luvbox.review_gui.time.monotonic", side_effect=lambda: clock[0]), \
                patch.object(window, "after", return_value="synthetic-timer") as after, \
                patch.object(window, "_render"):
            self.decoder.frames.put(ReviewFrame(Image.new("RGB", (8, 8)), 83))
            window._poll()
            self.assertAlmostEqual(window._next_frame, 100.083)
            self.assertIn(after.call_args.args[0], (83, 84))
            clock[0] = 100.084  # One millisecond of timer lateness must not accumulate.
            self.decoder.frames.put(ReviewFrame(Image.new("RGB", (8, 8)), 83))
            window._poll()
            self.assertAlmostEqual(window._next_frame, 100.166)
            self.decoder.frames.put(ReviewFrame(Image.new("RGB", (8, 8)), 996))
            clock[0] = 100.120
            window._poll()
            self.assertEqual(self.decoder.frames.qsize(), 1)
            clock[0] = 100.167
            window._poll()
            self.assertAlmostEqual(window._next_frame, 101.162)
            self.assertGreater(after.call_args.args[0], 990)

    def test_starvation_and_long_ui_stall_restart_without_bursting_queued_frames(self):
        window = self.window_for()
        self.finished.clear()
        clock = [100.0]
        with patch("luvbox.review_gui.time.monotonic", side_effect=lambda: clock[0]), \
                patch.object(window, "after", return_value="synthetic-timer") as after, \
                patch.object(window, "_render") as render:
            self.decoder.frames.put(ReviewFrame(Image.new("RGB", (8, 8)), 83))
            window._poll()
            clock[0] = 100.500
            window._poll()  # Nothing is ready when the previous frame ends.
            self.assertEqual(window._next_frame, 0)
            self.decoder.frames.put(ReviewFrame(Image.new("RGB", (8, 8)), 83))
            clock[0] = 101.0
            window._poll()
            self.assertAlmostEqual(window._next_frame, 101.083)
            self.assertIn(after.call_args.args[0], (83, 84))
            for _ in range(2):
                self.decoder.frames.put(ReviewFrame(Image.new("RGB", (8, 8)), 83))
            clock[0] = 101.165  # Almost one frame late must not create a 1ms flash.
            window._poll()
            self.assertAlmostEqual(window._next_frame, 101.248)
            self.assertIn(after.call_args.args[0], (83, 84))
            self.decoder.frames.put(ReviewFrame(Image.new("RGB", (8, 8)), 83))
            clock[0] = 102.0  # The UI itself was delayed for several frames.
            window._poll()
            self.assertAlmostEqual(window._next_frame, 102.083)
            self.assertEqual(render.call_count, 4)
            self.assertEqual(self.decoder.frames.qsize(), 1)
            self.decoder.frames.get_nowait().image.close()

    def test_manual_poll_replaces_the_old_timer_and_finished_photo_stops_polling(self):
        window = self.window_for()
        self.finished.clear()
        original = window._poll_timer
        window._poll()
        first = window._poll_timer
        window._poll()
        self.assertNotEqual(first, window._poll_timer)
        pending = window.tk.call("after", "info")
        self.assertNotIn(original, pending)
        self.assertNotIn(first, pending)
        self.assertIn(window._poll_timer, pending)
        self.finished.set()
        self.decoder.frames.put(ReviewFrame(Image.new("RGB", (32, 32)), 0, "사진 미리보기"))
        window.update_idletasks()
        window._poll()
        self.assertIsNotNone(window._photo)
        self.assertIsNone(window._poll_timer)

    def test_photo_buffer_is_reused_and_transparent_source_recomposes_for_theme(self):
        window = self.window_for()
        window.update()
        image = Image.new("RGBA", (640, 400), (200, 50, 80, 0))
        image.paste((20, 80, 200, 128), (200, 120, 440, 280))
        window._frame_image = image
        window._render()
        photo = window._photo

        def pixel():
            return tuple(window.tk.call(str(window._photo), "get", 0, 0))

        def background():
            return tuple(channel // 257 for channel in window.winfo_rgb(window.canvas.cget("background")))

        self.assertEqual(pixel(), background())
        center = tuple(window.tk.call(str(photo), "get", photo.width() // 2, photo.height() // 2))
        self.assertEqual(center, tuple((128 * foreground + 127 * behind + 127) // 255
                                       for foreground, behind in zip((20, 80, 200), background())))
        # A later opaque frame must replace pixels in the same native buffer.
        next_image = Image.new("RGB", image.size, (30, 60, 120))
        window._frame_image = next_image
        window._render()
        self.assertIs(window._photo, photo)
        self.assertEqual(pixel(), (30, 60, 120))
        window._frame_image = image
        next_image.close()
        window._render()
        theme.apply(self.root, "dark")
        self.assertIs(window._photo, photo)
        self.assertEqual(pixel(), background())
        self.assertEqual(image.getpixel((0, 0)), (200, 50, 80, 0))
        self.assertEqual(image.getpixel((320, 200)), (20, 80, 200, 128))
        theme.apply(self.root, "light")
        self.assertEqual(pixel(), background())
        window.destroy()
        self.assertIsNone(window.canvas.theme_redraw)

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

    def test_minimum_window_keeps_choices_readable_and_long_korean_details_scrollable(self):
        window = self.window_for()
        window.details.configure(text="@그림을 그리는 사람\n" + "공백이 있는 아주 긴 사진 파일 이름 " * 12 + ".jpg")
        window.note.configure(text="파일 위치를 확인할 수 없습니다. 건너뛰거나 작업을 중단해 주세요.")
        window.geometry("380x340")
        window.update()
        self.assertEqual((window.winfo_width(), window.winfo_height()), (380, 340))
        for button in window.buttons.values():
            self.assertGreaterEqual(button.winfo_width(), button.winfo_reqwidth())
            top = button.winfo_rooty() - window.winfo_rooty()
            self.assertGreaterEqual(top, 0)
            self.assertLessEqual(top + button.winfo_height(), window.winfo_height())
        for label in (window.details, window.explanation, window.note):
            self.assertLessEqual(int(label.cget("wraplength")), label.winfo_width())
            self.assertEqual(label.winfo_height(), label.winfo_reqheight())
        self.assertTrue(window.info_view.scrollbar.winfo_ismapped())
        self.assertGreater(window.canvas.winfo_height(), 30)
        window.info_view.canvas.yview_moveto(1)
        window.update()
        self.assertLessEqual(window.note.winfo_rooty() + window.note.winfo_height(),
                             window.info_view.canvas.winfo_rooty() + window.info_view.canvas.winfo_height())
        self.assertEqual(int(window.buttons["never"].grid_info()["row"]), 1)
        self.assertEqual(window.buttons["skip"].winfo_rooty(), window.buttons["never"].winfo_rooty())
        self.assertGreater(window.buttons["save"].winfo_width(), window.buttons["skip"].winfo_width())
        window.geometry("720x650")
        window.update()
        self.assertTrue(all(int(window.buttons[action].grid_info()["row"]) == 0
                            for action in ("save", "skip", "never")))

    def test_resize_burst_renders_latest_frame_once_and_reuses_canvas_item(self):
        window = self.window_for()
        self.decoder.frames.put(ReviewFrame(Image.new("RGB", (640, 480), "blue"), 83, "사진 미리보기"))
        window.update()
        window._poll()
        window.update()
        with patch.object(window, "_render", wraps=window._render) as render:
            for _ in range(30):
                window._resized()
            render.assert_not_called()
            deadline = time.monotonic() + 1
            while render.call_count == 0 and time.monotonic() < deadline:
                window.update()
                time.sleep(.002)
            render.assert_called_once()
        self.assertEqual(len(window.canvas.find_all()), 1)
        self.assertIsNone(window._render_timer)

    def test_large_button_font_uses_one_column_without_hiding_actions(self):
        window = self.window_for()
        style = ttk.Style(window)
        for name in ("TButton", "DialogPrimary.TButton"):
            style.configure(name, font=(theme.FONT, 18))
        window.geometry("380x340")
        window._queue_layout()
        window.update()
        self.assertGreater(window.minsize()[1], 340)
        for button in window.buttons.values():
            self.assertGreaterEqual(button.winfo_width(), button.winfo_reqwidth())
            self.assertGreaterEqual(button.winfo_rooty(), window.winfo_rooty())
            self.assertLessEqual(button.winfo_rooty() + button.winfo_height(),
                                 window.winfo_rooty() + window.winfo_height())

    def test_destroy_cancels_pending_resize_and_layout_callbacks(self):
        window = self.window_for()
        window._frame_image = Image.new("RGB", (80, 60), "blue")
        window._resized()
        window._queue_layout()
        timers = (window._render_timer, window._layout_timer)
        self.assertTrue(all(timers))
        window.destroy()
        pending = window.tk.call("after", "info")
        self.assertTrue(all(timer not in pending for timer in timers))
        self.assertIsNone(window._render_timer)
        self.assertIsNone(window._layout_timer)

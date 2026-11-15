"""What the main window remembers when it closes, and what the next start reads back.

When the main window closes, MainWindow.qml hands its place, its size and whether it is
maximized to ``ApplicationController.rememberWindow``. That keeps them with the preferences
and writes them on the storage thread. The next start reads them with ``window_geometry``
and places the window with ``qt_app._place_window`` (tests/test_window_placement.py).

The settings file can hold anything (an older version, a hand edit, a damaged write), so
reading keeps only whole numbers inside wide limits and a true or false for "maximized". The
numbers handed over are always the normal window's: MainWindow.qml keeps them while the
window is maximized or minimized. A normal window outside the limits is not remembered at
all; a maximized one without usable numbers (a zero size: it opened maximized without a usable
remembered place and was never a normal window since) keeps the size and place read at start.
A window that closes maximized on another screen than its normal place is moved onto that
screen (qt_app._onto_screen). The write is deliberately not a pending settings
change: it shows no "saving", locks no control, rolls nothing back and reports no failure.
It still goes through the storage thread's queue, so closing lets it land before that thread
ends, and a failed write does not keep the window from closing.

tests/test_qt_window_memory_qml.py takes the same path through the real window.
"""

from importlib.util import find_spec
import json
import os
from pathlib import Path
import tempfile
from threading import Event
import time
import unittest
from unittest.mock import Mock, call, patch
try:
    from tests import app_data_isolation  # noqa: F401  (never this PC's real app data)
except ModuleNotFoundError:      # run as a script: the tests folder is the path
    import app_data_isolation  # noqa: F401


_QT_AVAILABLE = find_spec("PySide6") is not None
if _QT_AVAILABLE:
    from PySide6.QtCore import QCoreApplication, QRect
    from PySide6.QtGui import QGuiApplication
    from luvbox import qt_app
    from luvbox.app_settings import default_settings
    from luvbox.qt_app import ApplicationController
    from luvbox.settings import settings_path
    try:
        from tests.test_window_placement import Application, Screen, Window, declared_window
    except ModuleNotFoundError:
        from test_window_placement import Application, Screen, Window, declared_window

# A window closed at 140,90 in a 1100x760 frame, not maximized.
NORMAL = {"x": 140, "y": 90, "width": 1100, "height": 760, "maximized": False}


@unittest.skipUnless(_QT_AVAILABLE, "optional Qt dependencies are not installed")
class _ControllerCase(unittest.TestCase):
    """A real controller whose settings file is a stand-in that records every write."""

    @classmethod
    def setUpClass(cls):
        cls.application = QCoreApplication.instance() or QCoreApplication([])

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="luvbox-window-memory-")
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.release = Event()
        self.settings = Mock()
        self.patch("luvbox.qt_app.SettingsRepository", return_value=self.settings)
        self.patch("luvbox.qt_accounts.AccountController._read_state", new=staticmethod(lambda: {
            "accounts": [], "identity": {}, "connected": True}))
        self.patch("luvbox.qt_schedule.task_status", return_value={"registered": False})
        environment = patch.dict(os.environ, {"LOCALAPPDATA": str(self.folder)})
        environment.start()
        self.addCleanup(environment.stop)
        self.controller = None
        self.flushes = 0
        self.addCleanup(self.close_controller)
        self.start({})

    def patch(self, target, **kwargs):
        patcher = patch(target, **kwargs)
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def wait_for(self, condition, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.application.processEvents()
            if condition():
                return
            time.sleep(.002)
        self.fail("the controller did not settle")

    def start(self, window):
        """Start (again) as if the settings file held this for the window."""
        self.close_controller()
        self.release.clear()
        self.settings.reset_mock()
        self.settings.update.side_effect = None
        self.settings.load.return_value = {
            **default_settings(), "account": "example", "output_dir": str(self.folder / "media"), "window": window}
        self.controller = ApplicationController()
        self.wait_for(lambda: not self.controller.busy)

    def close_controller(self):
        self.release.set()
        if self.controller is not None:
            self.controller.requestClose()
            self.wait_for(lambda: self.controller.shutdownReady)
            self.controller = None

    def window_writes(self):
        """Every window write so far, in order.

        A preference changed now is written through the same queue after anything already
        in it, so once that write has landed no window write is still on its way.
        """
        self.flushes += 1
        density = (0, 2)[self.flushes % 2]
        self.controller.setPreference("gallery_density", density)
        self.wait_for(lambda: self.settings.update.call_args_list[-1:] == [call({"gallery_density": density})])
        writes = [entry.args[0] for entry in self.settings.update.call_args_list if "window" in entry.args[0]]
        for written in writes:
            self.assertEqual(set(written), {"window"}, "a window write carries nothing else")
        return [written["window"] for written in writes]


class WindowGeometryTests(_ControllerCase):
    """Reading what the settings file says about the window."""

    def read(self, saved):
        self.controller._preferences["window"] = saved
        return self.controller.window_geometry()

    def test_a_remembered_window_is_read_back_as_it_was_kept(self):
        self.start(dict(NORMAL))
        self.assertEqual(self.controller.window_geometry(), NORMAL)
        self.settings.update.assert_not_called()          # reading writes nothing

    def test_nothing_or_a_damaged_value_reads_as_nothing(self):
        for saved in ({}, None, "1100x760", [140, 90, 1100, 760], 1100, True, {"width": None}):
            with self.subTest(saved=saved):
                self.assertEqual(self.read(saved), {})

    def test_each_number_counts_only_inside_its_limits(self):
        cases = [("width", 199, False), ("width", 200, True), ("width", 20000, True), ("width", 20001, False),
                 ("height", 199, False), ("height", 200, True), ("height", 20000, True), ("height", 20001, False),
                 ("x", -20001, False), ("x", -20000, True), ("x", 0, True), ("x", 20000, True), ("x", 20001, False),
                 ("y", -20001, False), ("y", -20000, True), ("y", 0, True), ("y", 20000, True), ("y", 20001, False)]
        for name, value, kept in cases:
            with self.subTest(name=name, value=value):
                expected = dict(NORMAL, **{name: value})
                if not kept:
                    del expected[name]
                self.assertEqual(self.read(dict(NORMAL, **{name: value})), expected)

    def test_only_whole_numbers_count(self):
        for name in ("x", "y", "width", "height"):
            for value in (True, False, 760.0, 760.5, "760", None, [760], {"value": 760}):
                with self.subTest(name=name, value=value):
                    expected = {key: kept for key, kept in NORMAL.items() if key != name}
                    self.assertEqual(self.read(dict(NORMAL, **{name: value})), expected)

    def test_maximized_counts_only_as_true_or_false(self):
        for value in (True, False):
            with self.subTest(value=value):
                self.assertEqual(self.read(dict(NORMAL, maximized=value)), dict(NORMAL, maximized=value))
        for value in (1, 0, "true", None, [True]):
            with self.subTest(value=value):
                expected = {key: kept for key, kept in NORMAL.items() if key != "maximized"}
                self.assertEqual(self.read(dict(NORMAL, maximized=value)), expected)

    def test_nothing_else_is_passed_on(self):
        self.assertEqual(self.read({**NORMAL, "monitor": "DISPLAY2", "dpi": 144}), NORMAL)

    def test_the_answer_is_a_copy(self):
        saved = dict(NORMAL)
        answer = self.read(saved)
        self.assertIsNot(answer, saved)
        answer["width"] = 300
        answer.clear()
        self.assertEqual(saved, NORMAL)
        self.assertEqual(self.controller.window_geometry(), NORMAL)


class RememberWindowTests(_ControllerCase):
    """What a closing window leaves behind."""

    def test_a_closing_window_is_kept_and_written_once(self):
        self.controller.rememberWindow(140, 90, 1100, 760, False)
        self.assertEqual(self.controller._preferences["window"], NORMAL)
        self.assertEqual(self.controller.window_geometry(), NORMAL)
        self.assertEqual(self.window_writes(), [NORMAL])

    def test_the_write_is_not_a_pending_settings_change(self):
        entered = Event()

        def slow(changes):
            entered.set()
            if not self.release.wait(4):
                raise TimeoutError("fixture was not released")

        self.settings.update.side_effect = slow
        self.controller.rememberWindow(140, 90, 1100, 760, False)
        self.assertTrue(entered.wait(2))
        self.application.processEvents()
        # While the file is being written nothing waits for it and nothing is locked.
        self.assertEqual(self.controller._pending_settings, {})
        self.assertFalse(self.controller.busy)
        self.assertTrue(self.controller.canStart)
        self.settings.update.side_effect = None
        self.release.set()
        self.assertEqual(self.window_writes(), [NORMAL])
        self.assertEqual(self.controller.error, "")

    def test_the_same_geometry_again_writes_nothing(self):
        self.controller.rememberWindow(140, 90, 1100, 760, False)
        self.controller.rememberWindow(140, 90, 1100, 760, False)
        self.assertEqual(self.window_writes(), [NORMAL])
        self.controller.rememberWindow(141, 90, 1100, 760, False)
        self.assertEqual(self.window_writes(), [NORMAL, dict(NORMAL, x=141)])

    def test_closing_where_it_opened_writes_nothing(self):
        self.start(dict(NORMAL))
        self.controller.rememberWindow(140, 90, 1100, 760, False)
        self.assertEqual(self.window_writes(), [])

    def test_a_maximized_window_keeps_the_normal_size_and_place_it_is_given(self):
        self.controller.rememberWindow(140, 90, 1100, 760, False)
        # Moved and resized as a normal window, then maximized: the window sends that normal place.
        self.controller.rememberWindow(300, 200, 1000, 700, True)
        maximized = {"x": 300, "y": 200, "width": 1000, "height": 700, "maximized": True}
        self.assertEqual(self.controller.window_geometry(), maximized)
        self.controller.rememberWindow(300, 200, 1000, 700, False)
        self.assertEqual(self.window_writes(), [NORMAL, maximized, dict(maximized, maximized=False)])

    def test_a_maximized_window_without_a_usable_normal_size_keeps_the_one_read_at_start(self):
        self.start(dict(NORMAL))
        # No normal place to give (opened maximized on no usable place): a zero size. Any number out of its limits counts the same,
        # and none of them is taken on its own.
        for place in [(0, 0, 0, 0), (-20001, 0, 1000, 700), (0, 20001, 1000, 700), (0, 0, 199, 700),
                      (0, 0, 1000, 20001)]:
            with self.subTest(place=place):
                self.controller.rememberWindow(*place, True)
                self.assertEqual(self.controller.window_geometry(), dict(NORMAL, maximized=True))
        self.assertEqual(self.window_writes(), [dict(NORMAL, maximized=True)])

    def test_maximized_with_nothing_before_keeps_only_that(self):
        self.controller.rememberWindow(0, 0, 0, 0, True)
        self.assertEqual(self.window_writes(), [{"maximized": True}])

    def test_a_normal_window_outside_the_limits_is_not_kept(self):
        self.start(dict(NORMAL))
        for place in [(0, 0, 199, 700), (0, 0, 1000, 199), (0, 0, 20001, 700), (0, 0, 1000, 20001),
                      (-20001, 0, 1000, 700), (20001, 0, 1000, 700), (0, -20001, 1000, 700), (0, 20001, 1000, 700),
                      # Windows' own rectangle for a minimized window. Qt hands the window the place
                      # it was last shown at instead, so a value like this can only come from elsewhere.
                      (-32000, -32000, 160, 28)]:
            with self.subTest(place=place):
                self.controller.rememberWindow(*place, False)
                self.assertEqual(self.controller.window_geometry(), NORMAL)
        self.assertEqual(self.window_writes(), [])
        # The limits themselves are inside.
        self.controller.rememberWindow(-20000, -20000, 200, 200, False)
        self.controller.rememberWindow(20000, 20000, 20000, 20000, False)
        self.assertEqual(self.window_writes(), [
            {"x": -20000, "y": -20000, "width": 200, "height": 200, "maximized": False},
            {"x": 20000, "y": 20000, "width": 20000, "height": 20000, "maximized": False}])

    def test_arguments_of_the_wrong_kind_are_ignored(self):
        self.start(dict(NORMAL))
        for arguments in [(140.0, 90, 1100, 700, False), (140, 90.5, 1100, 700, False), (140, 90, True, 700, False),
                          (True, 90, 1100, 700, False), (140, False, 1100, 700, False),
                          (140, 90, 1100, "700", False), (140, 90, 1100, None, False),
                          (140, 90, 1100, 700, 1), (140, 90, 1100, 700, None), (140, 90, 1100, 700, "false"),
                          (140.0, 90, 1100, 700, True), (140, 90, 1100, 700, 0)]:
            with self.subTest(arguments=arguments):
                self.controller.rememberWindow(*arguments)
                self.assertEqual(self.controller.window_geometry(), NORMAL)
        self.assertEqual(self.window_writes(), [])

    def test_a_damaged_value_is_mended_by_the_next_close(self):
        self.start({"width": "wide", "height": 700, "x": 10, "y": 20, "maximized": "yes", "monitor": 2})
        self.controller.rememberWindow(0, 0, 0, 0, True)
        self.controller.rememberWindow(140, 90, 1100, 760, False)
        self.assertEqual(self.window_writes(), [{"height": 700, "x": 10, "y": 20, "maximized": True}, NORMAL])

    def test_the_window_write_leaves_a_waiting_preference_waiting(self):
        window_entered, other_entered = Event(), Event()
        let_window, let_other = Event(), Event()
        self.addCleanup(let_window.set)
        self.addCleanup(let_other.set)

        def update(changes):
            entered, let = (window_entered, let_window) if "window" in changes else (other_entered, let_other)
            entered.set()
            if not let.wait(4):
                raise TimeoutError("fixture was not released")

        self.settings.update.side_effect = update
        self.controller.rememberWindow(140, 90, 1100, 760, False)
        self.assertTrue(window_entered.wait(2))
        self.controller.setPreference("gallery_density", 2)
        waiting = dict(self.controller._pending_settings)
        self.assertTrue(waiting and self.controller.busy)
        let_window.set()
        # The other write has begun, so the window write has finished and reported back.
        self.assertTrue(other_entered.wait(2))
        deadline = time.monotonic() + .2
        while time.monotonic() < deadline:
            self.application.processEvents()
            time.sleep(.005)
        # That report settles nothing: the thumbnail size is still being written.
        self.assertEqual(self.controller._pending_settings, waiting)
        self.assertTrue(self.controller.busy)
        let_other.set()
        self.wait_for(lambda: not self.controller.busy)
        self.assertEqual(self.controller._committed_preferences["gallery_density"], 2)
        self.assertEqual(self.controller.preferences["gallery_density"], 2)

    def test_closing_waits_for_the_window_write_even_behind_another(self):
        density_entered, window_entered = Event(), Event()
        let_density, let_window = Event(), Event()
        self.addCleanup(let_density.set)
        self.addCleanup(let_window.set)
        finished = []

        def update(changes):
            entered, let = (window_entered, let_window) if "window" in changes else (density_entered, let_density)
            entered.set()
            if not let.wait(4):
                raise TimeoutError("fixture was not released")
            finished.append(set(changes))

        self.settings.update.side_effect = update
        self.controller.setPreference("gallery_density", 2)
        self.assertTrue(density_entered.wait(2))            # the storage thread is busy
        # As MainWindow.qml does it: remember, then close. The window write waits in the queue.
        self.controller.rememberWindow(140, 90, 1100, 760, False)
        self.controller.requestClose()
        self.wait_for(lambda: self.controller._io_shutdown_queued)
        let_density.set()
        self.assertTrue(window_entered.wait(2), "the queued window write was dropped")
        deadline = time.monotonic() + .2
        while time.monotonic() < deadline:
            self.application.processEvents()
            time.sleep(.005)
        # Everything else has closed; the window may not go while its place is still being written.
        self.assertFalse(self.controller.shutdownReady)
        let_window.set()
        self.wait_for(lambda: self.controller.shutdownReady)
        self.assertEqual(finished, [{"gallery_density"}, {"window"}])
        self.assertFalse(self.controller._io_thread.is_alive())

    def test_a_failed_write_does_not_keep_the_window_from_closing(self):
        self.settings.update.side_effect = OSError("a private settings path")
        self.controller.rememberWindow(140, 90, 1100, 760, False)
        self.controller.requestClose()
        self.wait_for(lambda: self.controller.shutdownReady)
        self.settings.update.assert_called_once_with({"window": NORMAL})
        self.assertEqual(self.controller.error, "")


class RoundTripTests(_ControllerCase):
    """What one close keeps is where the next start opens."""

    def place(self, saved, *screens):
        width, height, minimum_width, minimum_height = declared_window()
        screens = screens or (Screen(0, 0, 1920, 1040),)
        window = Window(screens[0], width, height, minimum_width, minimum_height)
        qt_app._place_window(Application(*screens), window, saved)
        return window

    def test_the_next_start_opens_where_the_window_was_closed(self):
        self.controller.rememberWindow(300, 200, 1100, 760, False)
        self.start(self.window_writes()[-1])
        window = self.place(self.controller.window_geometry())
        self.assertEqual(window.rect(), QRect(300, 200, 1100, 760))
        self.assertNotIn("maximized", window.calls)

    def test_a_window_closed_maximized_opens_maximized_over_its_normal_size(self):
        self.controller.rememberWindow(300, 200, 1100, 760, False)
        self.controller.rememberWindow(320, 210, 1000, 700, True)
        self.start(self.window_writes()[-1])
        window = self.place(self.controller.window_geometry())
        self.assertEqual(window.rect(), QRect(320, 210, 1000, 700))
        self.assertEqual(window.calls[-1], "maximized")

    def test_maximized_with_nothing_before_opens_at_the_first_size_then_maximized(self):
        self.controller.rememberWindow(0, 0, 0, 0, True)
        self.start(self.window_writes()[-1])
        width, height, _minimum_width, _minimum_height = declared_window()
        window = self.place(self.controller.window_geometry())
        self.assertEqual(window.rect(), QRect((1920 - width) // 2, (1040 - height) // 2, width, height))
        self.assertEqual(window.calls[-1], "maximized")

    def test_what_is_kept_is_read_back_whole(self):
        # The limits for keeping and for reading are the same, so nothing kept is lost at the next start.
        for place in [(-20000, -20000, 200, 200), (20000, 20000, 20000, 20000), (-8, 0, 1936, 1056)]:
            with self.subTest(place=place):
                self.controller.rememberWindow(*place, False)
                written = self.window_writes()[-1]
                self.assertEqual(written, dict(zip(("x", "y", "width", "height"), place), maximized=False))
                self.start(written)
                self.assertEqual(self.controller.window_geometry(), written)


@unittest.skipUnless(_QT_AVAILABLE, "optional Qt dependencies are not installed")
class OtherScreenTests(unittest.TestCase):
    """A window that closes maximized on another screen than its normal place (qt_app._onto_screen)."""

    # This PC's kind of layout: a 1440p monitor, and a 1080p one to its right, lower down.
    FIRST = (QRect(0, 0, 2560, 1440), QRect(0, 0, 2560, 1392)) if _QT_AVAILABLE else None
    SECOND = (QRect(2560, 585, 1920, 1080), QRect(2560, 585, 1920, 1032)) if _QT_AVAILABLE else None

    def test_a_place_on_the_screen_it_closes_on_is_kept(self):
        # Partly off the screen is still on it while the middle is (2500,1350 for the last).
        for place in [(300, 200, 1000, 700), (-200, -100, 1000, 700), (2000, 1000, 1000, 700)]:
            with self.subTest(place=place):
                self.assertEqual(qt_app._onto_screen(place, *self.FIRST), place)

    def test_the_middle_decides_and_the_far_edges_are_outside(self):
        geometry, available = QRect(0, 0, 1000, 800), QRect(0, 0, 1000, 760)
        # Middles at 999.5 and 799.5 are on the screen; at 1000 or 800 they are not.
        self.assertEqual(qt_app._onto_screen((499, 100, 1001, 600), geometry, available), (499, 100, 1001, 600))
        self.assertEqual(qt_app._onto_screen((100, 499, 600, 601), geometry, available), (100, 499, 600, 601))
        self.assertNotEqual(qt_app._onto_screen((500, 100, 1000, 600), geometry, available), (500, 100, 1000, 600))
        self.assertNotEqual(qt_app._onto_screen((100, 500, 600, 600), geometry, available), (100, 500, 600, 600))
        self.assertNotEqual(qt_app._onto_screen((-501, 100, 1000, 600), geometry, available), (-501, 100, 1000, 600))

    def test_a_place_on_another_screen_is_centred_in_the_part_the_taskbar_leaves(self):
        self.assertEqual(qt_app._onto_screen((300, 200, 980, 680), *self.SECOND),
                         (2560 + (1920 - 980) // 2, 585 + (1032 - 680) // 2, 980, 680))
        # A taskbar at the top or on the left: the place starts below or beside it.
        geometry = QRect(0, 0, 1920, 1080)
        for available in (QRect(0, 40, 1920, 1040), QRect(60, 0, 1860, 1080)):
            with self.subTest(available=available):
                x, y, width, height = qt_app._onto_screen((3000, 200, 980, 680), geometry, available)
                self.assertEqual((width, height), (980, 680))
                self.assertEqual((x, y), (available.left() + (available.width() - 980) // 2,
                                          available.top() + (available.height() - 680) // 2))

    def test_a_place_too_large_for_the_other_screen_is_limited_as_at_a_first_start(self):
        laptop = (QRect(0, 0, 1536, 864), QRect(0, 0, 1536, 816))    # 1920x1080 at 125%
        self.assertEqual(qt_app._onto_screen((2000, 100, 1800, 1300), *laptop), (12, 24, 1512, 768))
        # Room is left above for the title bar, which sits over the window's own top edge.
        self.assertEqual(qt_app._onto_screen((2000, 100, 1500, 800), *laptop), (18, 24, 1500, 768))

    def test_without_a_size_the_place_comes_back_as_it_was(self):
        for place in [(0, 0, 0, 0), (5000, 5000, 0, 700), (5000, 5000, 900, 0)]:
            with self.subTest(place=place):
                self.assertEqual(qt_app._onto_screen(place, *self.SECOND), place)

    def test_the_next_start_takes_the_moved_place_as_it_is(self):
        # What is remembered after the move opens there, the same size, on that screen.
        first, second = Screen(0, 0, 2560, 1392), Screen(2560, 585, 1920, 1032)
        laptop = Screen(2560, 0, 1536, 816)
        width, height, minimum_width, minimum_height = declared_window()
        for target, geometry in ((second, self.SECOND[0]), (laptop, QRect(2560, 0, 1536, 864))):
            with self.subTest(screen=target.area):
                x, y, across, down = qt_app._onto_screen((300, 200, 1800, 1300), geometry, target.area)
                window = Window(first, width, height, minimum_width, minimum_height)
                qt_app._place_window(Application(first, target), window,
                                     {"x": x, "y": y, "width": across, "height": down, "maximized": True})
                self.assertEqual(window.rect(), QRect(x, y, across, down))
                self.assertEqual(window.calls[-1], "maximized")

    def test_the_window_asks_the_controller_which_needs_screens(self):
        # Without a GUI application there are no screens: the place is handed back unchanged.
        application = QCoreApplication.instance() or QCoreApplication([])
        self.assertNotIsInstance(application, QGuiApplication)
        # The method uses nothing of the controller itself.
        self.assertEqual(qt_app.ApplicationController.normalPlaceOnScreen(None, 300, 200, 980, 680, 3500, 1100),
                         [300, 200, 980, 680])


@unittest.skipUnless(_QT_AVAILABLE, "optional Qt dependencies are not installed")
class SettingsFileTests(unittest.TestCase):
    """Through the real settings file: the window is kept beside every other preference."""

    @classmethod
    def setUpClass(cls):
        cls.application = QCoreApplication.instance() or QCoreApplication([])

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="luvbox-window-file-")
        self.addCleanup(temporary.cleanup)
        for target, options in (
                ("luvbox.qt_accounts.AccountController._read_state",
                 {"new": staticmethod(lambda: {"accounts": [], "identity": {}, "connected": True})}),
                ("luvbox.qt_schedule.task_status", {"return_value": {"registered": False}})):
            patcher = patch(target, **options)
            patcher.start()
            self.addCleanup(patcher.stop)
        environment = patch.dict(os.environ, {"LOCALAPPDATA": temporary.name})
        environment.start()
        self.addCleanup(environment.stop)

    def wait_for(self, condition, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.application.processEvents()
            if condition():
                return
            time.sleep(.002)
        self.fail("the controller did not settle")

    def run_controller(self, act):
        controller = ApplicationController()
        try:
            self.wait_for(lambda: not controller.busy)
            return act(controller)
        finally:
            controller.requestClose()
            self.wait_for(lambda: controller.shutdownReady)

    def test_the_window_is_written_to_the_file_and_read_back_from_it(self):
        path = settings_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"gallery_density": 2, "kept_for_a_newer_version": {"a": 1}}), encoding="utf-8")
        self.run_controller(lambda controller: controller.rememberWindow(140, 90, 1100, 760, False))
        stored = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(stored["window"], NORMAL)
        self.assertIs(stored["window"]["maximized"], False)
        self.assertEqual(stored["gallery_density"], 2)
        self.assertEqual(stored["kept_for_a_newer_version"], {"a": 1})
        self.assertEqual(self.run_controller(lambda controller: controller.window_geometry()), NORMAL)
        # Maximized and closed again: the same file, the same normal size.
        self.run_controller(lambda controller: controller.rememberWindow(0, 0, 0, 0, True))
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["window"], dict(NORMAL, maximized=True))
        self.assertEqual(self.run_controller(lambda controller: controller.window_geometry()),
                         dict(NORMAL, maximized=True))


if __name__ == "__main__":
    unittest.main()

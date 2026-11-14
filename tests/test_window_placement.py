"""Where the main window opens: it has to fit the screen it is given.

On a display set to 125% or 150% the screen is smaller in the units a window is measured
in: a 1920x1080 panel at 150% is 1280x720, less than the window's declared 1280x860. The
start-up code (``qt_app._place_window``) makes the window fit, restores a remembered size
and place while they are still usable, and otherwise centres it.

The code only reads and sets a handful of things on the window and asks screens for their
free area, so it is exercised here with stand-ins that have exactly those things. The free
areas are real ``QRect`` objects, whose right and bottom edges are one less than
left + width; the rule about a reachable title bar depends on that.
"""

from importlib.util import find_spec
from pathlib import Path
import re
import unittest
try:
    from tests import app_data_isolation  # noqa: F401  (never this PC's real app data)
except ModuleNotFoundError:      # run as a script: the tests folder is the path
    import app_data_isolation  # noqa: F401


_QT_AVAILABLE = find_spec("PySide6") is not None
if _QT_AVAILABLE:
    from PySide6.QtCore import QRect
    from luvbox import qt_app

ROOT = Path(__file__).resolve().parents[1]


def declared_window():
    """(width, height, minimum width, minimum height) as the product window declares them."""
    found = {}
    for line in (ROOT / "assets/qt/MainWindow.qml").read_text(encoding="utf-8").splitlines()[:40]:
        match = re.fullmatch(r"    (width|height|minimumWidth|minimumHeight): (\d+)", line.rstrip())
        if match and match[1] not in found:
            found[match[1]] = int(match[2])
    return found["width"], found["height"], found["minimumWidth"], found["minimumHeight"]


class Screen:
    """A connected screen: the area left free by the taskbar, in the window's units."""

    def __init__(self, left, top, width, height):
        self.area = QRect(left, top, width, height)

    def availableGeometry(self):
        return self.area


class Application:
    def __init__(self, *screens):
        self._screens = list(screens)

    def screens(self):
        return self._screens


class Window:
    """What the placement reads and sets on the main window, and nothing else."""

    def __init__(self, screen, width, height, minimum_width, minimum_height):
        self._screen = screen
        self._width, self._height = width, height
        self._minimum = (minimum_width, minimum_height)
        self._x = self._y = None
        self.calls = []

    def width(self):
        return self._width

    def height(self):
        return self._height

    def minimumWidth(self):
        return self._minimum[0]

    def minimumHeight(self):
        return self._minimum[1]

    def screen(self):
        return self._screen

    def setWidth(self, value):
        self.calls.append("width")
        self._width = value

    def setHeight(self, value):
        self.calls.append("height")
        self._height = value

    def setX(self, value):
        self.calls.append("x")
        self._x = value

    def setY(self, value):
        self.calls.append("y")
        self._y = value

    def showMaximized(self):
        self.calls.append("maximized")

    # For the assertions only.
    def rect(self):
        return QRect(self._x, self._y, self._width, self._height)

    def size(self):
        return self._width, self._height

    def place(self):
        return self._x, self._y


@unittest.skipUnless(_QT_AVAILABLE, "optional Qt dependencies are not installed")
class PlacementCase(unittest.TestCase):
    def setUp(self):
        self.declared = declared_window()

    def window(self, screen, *, size=None, minimum=None):
        width, height, minimum_width, minimum_height = self.declared
        if size is not None:
            width, height = size
        if minimum is not None:
            minimum_width, minimum_height = minimum
        return Window(screen, width, height, minimum_width, minimum_height)

    def place(self, window, saved=None, *, screens=None):
        application = Application(*(screens or [window.screen()]))
        # Through the module, as the app calls it.
        qt_app._place_window(application, window, dict(saved or {}))
        return window

    def centred(self, window, area):
        width, height = window.size()
        return (area.left() + max(0, (area.width() - width) // 2), area.top() + max(0, (area.height() - height) // 2))

    def assert_inside(self, window, area):
        self.assertTrue(area.contains(window.rect()), f"window {window.rect()} is not inside the free area {area}")

    def assert_centred(self, window, area):
        self.assertEqual(window.place(), self.centred(window, area))


class DeclaredWindowTests(PlacementCase):
    def test_the_product_window_declares_a_size_and_a_smaller_minimum(self):
        width, height, minimum_width, minimum_height = self.declared
        self.assertGreaterEqual(width, minimum_width)
        self.assertGreaterEqual(height, minimum_height)
        self.assertGreater(minimum_width, 0)
        self.assertGreater(minimum_height, 0)
        # Every layout is checked down to the minimum by the scene tests; a 1080p panel at 150%
        # must leave at least that much, or the cases below would describe a window that cannot fit.
        self.assertLessEqual(minimum_width, 1280 - 24)
        self.assertLessEqual(minimum_height, 720 - 48)


class FirstStartTests(PlacementCase):
    """Nothing remembered: the declared window, made to fit and centred."""

    def test_on_a_roomy_screen_the_declared_size_is_kept_and_centred(self):
        screen = Screen(0, 0, 2560, 1440)
        window = self.place(self.window(screen))
        self.assertEqual(window.size(), self.declared[:2])
        self.assert_centred(window, screen.area)
        self.assert_inside(window, screen.area)
        self.assertNotIn("maximized", window.calls)

    def test_on_a_1080p_panel_at_150_percent_the_window_is_made_to_fit(self):
        # 1920x1080 at 150% is 1280x720: not as high as the declared window.
        screen = Screen(0, 0, 1280, 720)
        window = self.place(self.window(screen))
        self.assert_inside(window, screen.area)
        self.assert_centred(window, screen.area)
        self.assertLessEqual(window.size()[0], self.declared[0])
        self.assertLess(window.size()[1], self.declared[1])
        self.assertGreaterEqual(window.size(), self.declared[2:])

    def test_on_a_1080p_panel_at_125_percent_the_window_is_made_to_fit(self):
        screen = Screen(0, 0, 1536, 864)
        window = self.place(self.window(screen))
        self.assert_inside(window, screen.area)
        self.assert_centred(window, screen.area)
        self.assertGreaterEqual(window.size(), self.declared[2:])

    def test_the_taskbar_is_left_free_wherever_it_is(self):
        for name, area in (("at the bottom", (0, 0, 1280, 672)), ("at the top", (0, 48, 1280, 672)),
                           ("at the left", (72, 0, 1208, 720)), ("at the right", (0, 0, 1208, 720))):
            with self.subTest(taskbar=name):
                screen = Screen(*area)
                window = self.place(self.window(screen))
                self.assert_inside(window, screen.area)
                self.assert_centred(window, screen.area)

    def test_with_several_screens_the_window_fits_the_one_it_is_on(self):
        # Nothing remembered and the window is on the second, smaller screen: that screen decides.
        first, second = Screen(0, 0, 2560, 1440), Screen(2560, 0, 1280, 720)
        window = self.place(self.window(second), screens=[first, second])
        self.assert_inside(window, second.area)
        self.assert_centred(window, second.area)

    def test_room_is_left_around_a_window_that_would_fill_the_screen(self):
        # 24 across and 48 down are kept free for the frame and the title bar.
        screen = Screen(0, 0, 1280, 720)
        window = self.place(self.window(screen, size=(2000, 2000), minimum=(680, 480)))
        self.assertEqual(window.size(), (1256, 672))
        self.assertEqual(window.place(), (12, 24))

    def test_the_window_is_never_made_smaller_than_it_allows(self):
        # A screen with less room than the minimum: the window keeps its minimum and starts at the
        # corner of the free area, never left of it or above it.
        screen = Screen(100, 50, 640, 400)
        window = self.place(self.window(screen, size=(1280, 860), minimum=(680, 480)))
        self.assertEqual(window.size(), (680, 480))
        self.assertEqual(window.place(), (100, 50))


class RememberedTests(PlacementCase):
    """What the settings kept from the last time the window was closed."""

    def setUp(self):
        super().setUp()
        self.screen = Screen(0, 0, 1920, 1080)

    def test_a_size_and_place_that_fit_are_restored_exactly(self):
        window = self.place(self.window(self.screen), {"width": 1100, "height": 600, "x": 40, "y": 30})
        self.assertEqual((window.size(), window.place()), ((1100, 600), (40, 30)))
        self.assert_inside(window, self.screen.area)
        self.assertNotIn("maximized", window.calls)

    def test_a_size_larger_than_the_screen_is_reduced_to_fit(self):
        screen = Screen(0, 0, 1280, 720)
        window = self.place(self.window(screen), {"width": 1600, "height": 1000})
        self.assertEqual(window.size(), (1256, 672))
        self.assert_centred(window, screen.area)
        self.assert_inside(window, screen.area)

    def test_a_place_on_no_connected_screen_is_dropped_and_the_window_centred(self):
        # The monitor it was on is gone.
        window = self.place(self.window(self.screen), {"width": 1100, "height": 600, "x": 5000, "y": 3000})
        self.assertEqual(window.size(), (1100, 600))
        self.assert_centred(window, self.screen.area)
        self.assert_inside(window, self.screen.area)

    def test_a_place_is_kept_while_the_title_bar_can_still_be_grabbed(self):
        # Parked mostly off the screen on purpose: that is where it opens again.
        for name, x, y in (("200 px showing at the right edge", 1920 - 200, 300),
                           ("200 px showing at the left edge", -800, 300),
                           ("top edge 200 px above the bottom", 300, 1080 - 200)):
            with self.subTest(place=name):
                window = self.place(self.window(self.screen), {"width": 1000, "height": 600, "x": x, "y": y})
                self.assertEqual(window.place(), (x, y))

    def test_a_place_is_dropped_when_the_title_bar_could_not_be_reached(self):
        for name, x, y in (("60 px showing at the right edge", 1920 - 60, 300),
                           ("60 px showing at the left edge", -940, 300),
                           ("top edge 40 px above the bottom", 300, 1080 - 40),
                           ("title bar above the screen", 300, -30)):
            with self.subTest(place=name):
                window = self.place(self.window(self.screen), {"width": 1000, "height": 600, "x": x, "y": y})
                self.assert_centred(window, self.screen.area)
                self.assert_inside(window, self.screen.area)

    def test_the_title_bar_counts_as_reachable_from_120_px_across_and_80_px_down(self):
        # The exact edges as the rule has them today, in the screen's own edge values.
        area = self.screen.area
        width = 1000
        for name, x, y, kept in (("right, at the limit", area.right() - 120, 300, True),
                                 ("right, one past it", area.right() - 119, 300, False),
                                 ("left, at the limit", area.left() - width + 120, 300, True),
                                 ("left, one past it", area.left() - width + 119, 300, False),
                                 ("down, at the limit", 300, area.bottom() - 80, True),
                                 ("down, one past it", 300, area.bottom() - 79, False),
                                 ("up, at the limit", 300, area.top(), True),
                                 ("up, one past it", 300, area.top() - 1, False)):
            with self.subTest(place=name):
                window = self.place(self.window(self.screen), {"width": width, "height": 600, "x": x, "y": y})
                if kept:
                    self.assertEqual(window.place(), (x, y))
                else:
                    self.assert_centred(window, area)

    def test_after_the_display_scale_was_raised_a_place_beyond_the_smaller_screen_is_dropped(self):
        # Closed at 100% near the right edge of 1920x1080; the display is 150% now (1280x720).
        screen = Screen(0, 0, 1280, 720)
        window = self.place(self.window(screen), {"width": 1280, "height": 860, "x": 1300, "y": 700})
        self.assertEqual(window.size(), (1256, 672))
        self.assert_centred(window, screen.area)
        self.assert_inside(window, screen.area)

    def test_a_place_that_still_passes_on_the_smaller_screen_is_kept_though_most_of_the_window_is_off_it(self):
        # The same rule as at 100%: the title bar can be grabbed (180 px across, 120 px down are on
        # the screen), so the place is kept and only the size is reduced. The window then has to be
        # dragged back. Whoever wants such a window pulled fully onto the screen changes this test.
        screen = Screen(0, 0, 1280, 720)
        window = self.place(self.window(screen), {"width": 1280, "height": 860, "x": 1100, "y": 600})
        self.assertEqual((window.size(), window.place()), ((1256, 672), (1100, 600)))
        self.assertFalse(screen.area.contains(window.rect()))

    def test_a_place_on_a_second_screen_is_restored_with_that_screens_room(self):
        first, second = Screen(0, 0, 1920, 1080), Screen(1920, 0, 1280, 720)
        window = self.place(self.window(first), {"width": 1600, "height": 1000, "x": 2000, "y": 50},
                            screens=[first, second])
        # The size is limited by the screen the window goes to, not the one it happens to be on.
        self.assertEqual((window.size(), window.place()), ((1256, 672), (2000, 50)))

    def test_a_maximized_window_is_maximized_again_after_its_normal_size_was_set(self):
        window = self.place(self.window(self.screen),
                            {"width": 1100, "height": 600, "x": 40, "y": 30, "maximized": True})
        # The normal size and place come first, so leaving the maximized state gives them back.
        self.assertEqual((window.size(), window.place()), ((1100, 600), (40, 30)))
        self.assertEqual(window.calls[-1], "maximized")
        self.assertEqual(window.calls.count("maximized"), 1)
        for saved in ({"width": 1100, "height": 600, "maximized": False}, {"width": 1100, "height": 600}):
            self.assertNotIn("maximized", self.place(self.window(self.screen), saved).calls)

    def test_only_part_of_a_geometry_may_have_been_kept(self):
        # The settings drop any value that is not a sensible whole number.
        width, height = self.declared[:2]
        window = self.place(self.window(self.screen), {"width": 1500})
        self.assertEqual(window.size(), (1500, height))
        self.assert_centred(window, self.screen.area)
        # A place needs both of its numbers.
        for saved in ({"x": 100}, {"y": 100}):
            window = self.place(self.window(self.screen), saved)
            self.assertEqual(window.size(), (width, height))
            self.assert_centred(window, self.screen.area)
        window = self.place(self.window(self.screen), {"maximized": True})
        self.assertEqual(window.size(), (width, height))
        self.assertEqual(window.calls[-1], "maximized")


if __name__ == "__main__":
    unittest.main()

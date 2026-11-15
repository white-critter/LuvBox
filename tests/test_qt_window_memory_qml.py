"""The real main window remembers its own size and place when it closes.

tests/test_window_memory.py checks the controller's side with direct calls. This runs the
real MainWindow.qml and controller against a private settings file, and ten times starts
the window, uses it and closes it the way its close button does. Each time the window must
stay on screen until the controller has finished closing.

1. A first start. The window is moved and resized, then grown for a saved-file card from the
   lowest height it allows; the grown height is temporary, so that lowest height is the one
   remembered.
2. The next start opens there. Maximized and closed, it is remembered as maximized and keeps
   the normal size and place.
3. The next start opens maximized. Made normal again, moved and resized, and closed while the
   grown-height mark holds a height one below the window's minimum, which cannot be the
   person's own, so the real height is remembered.
4. Closed twice before the first close has finished: only the first is remembered. An old
   card's mark is left behind (a height at or above the minimum, with the card gone): the
   real height is remembered.
5. Full screen and closed: remembered as maximized, again over the normal size.
6. The next start opens maximized and is closed at once: the settings file is not written.
7. Made normal, moved and resized, maximized, then minimized and closed (as from the
   taskbar): remembered as maximized over the moved normal place. A minimized window still
   reports the maximized area, which is not its own size.
8. Made normal, then given the maximized area as a normal window a moment before the
   maximized state, the order in which Windows reports a maximize made by a person: the
   normal place from before is kept.
9. A normal window minimized and closed: its own size and place, not maximized.
10. A card grew the normal window, which was then maximized, and the card went while it
    stayed maximized: the height from before the card is kept.

A run on two simulated screens side by side (the offscreen platform's screen file): a window
dragged to the other screen and maximized there in one go is remembered on that screen, the
same size (it fits), centred in its free area, and opens maximized there; one maximized where
it was keeps its place. The arithmetic itself (taskbars, sizes too large for the screen) is
checked in tests/test_window_memory.py; offscreen screens have no taskbar.

The card's growth is set on the window's own properties rather than produced by a save. The
windows are offscreen, on the platform's 800x800 screen, which every size here fits.

A second test starts the app through ``qt_app.launch`` itself, as entry.py does, once on a
remembered window and once on a damaged one, and closes it through the real event loop.
"""

from importlib.util import find_spec
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
try:
    from tests import app_data_isolation  # noqa: F401  (never this PC's real app data)
except ModuleNotFoundError:      # run as a script: the tests folder is the path
    import app_data_isolation  # noqa: F401


ROOT = Path(__file__).resolve().parents[1]


def scene(layout="one"):
    from contextlib import ExitStack
    import tempfile
    import time
    from unittest.mock import patch

    from PySide6.QtCore import qInstallMessageHandler
    from PySide6.QtGui import QWindow
    from PySide6.QtQml import QQmlApplicationEngine
    from PySide6.QtQuickControls2 import QQuickStyle
    from PySide6.QtWidgets import QApplication

    sys.path.insert(0, str(ROOT))
    from luvbox import qt_app
    from luvbox.qt_app import ApplicationController
    from luvbox.qt_wheel import WheelInputFilter
    from luvbox.settings import settings_path

    QQuickStyle.setStyle("Basic")
    application = QApplication([])
    messages = []
    previous = qInstallMessageHandler(lambda _kind, _context, message: messages.append(message))
    report = {"platform": application.platformName(), "sessions": []}
    kept = []

    def wait(predicate, what, timeout=15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            application.processEvents()
            if predicate():
                return
            time.sleep(.003)
        raise AssertionError("The scene did not reach: " + what)

    def pause(milliseconds):
        deadline = time.monotonic() + milliseconds / 1000
        while time.monotonic() < deadline:
            application.processEvents()
            time.sleep(.003)

    def geometry(window):
        return {"x": window.x(), "y": window.y(), "width": window.width(), "height": window.height(),
                "maximized": window.visibility() == QWindow.Visibility.Maximized}

    def session(act):
        """Start as the app does, let `act` use and close the window, and say what was kept."""
        controller = ApplicationController()
        wheel = WheelInputFilter()
        engine = QQmlApplicationEngine()
        engine.addImageProvider("thumbs", controller.library.provider)
        engine.setInitialProperties({"appController": controller, "galleryModel": controller.library.model,
                                     "wheelInputFilter": wheel})
        engine.load(ROOT / "assets/qt" / "MainWindow.qml")
        assert engine.rootObjects(), messages
        window = engine.rootObjects()[0]
        controller.shutdownFinished.connect(window.close)   # as luvbox.qt_app.launch does
        read = controller.window_geometry()
        qt_app._place_window(application, window, read)    # as luvbox.qt_app.launch does
        wait(lambda: not controller.busy, "idle application")
        pause(150)                                          # the window's own start-up fitting has run
        opened = geometry(window)
        before = settings_path().read_bytes(), settings_path().stat().st_mtime_ns
        notes = act(window) or {}
        # The close button only asks; the window goes once the controller has closed.
        stayed = window.isVisible() and controller.closing and not controller.shutdownReady
        wait(lambda: controller.shutdownReady and not window.isVisible(), "the closed window")
        wheel.close()
        controller._library.provider = None
        kept.extend((engine, controller, wheel))
        after = settings_path().read_bytes(), settings_path().stat().st_mtime_ns
        remembered = json.loads(after[0].decode("utf-8")).get("window")
        report["sessions"].append({"read": read, "opened": opened, "remembered": remembered,
                                   "rewritten": after != before, "stayed_open": stayed, **notes})
        return remembered

    def first(window):
        lowest = window.minimumHeight()
        window.setX(30)
        window.setY(20)
        window.setWidth(720)
        window.setHeight(lowest + 120)
        # A saved-file card grew the window from the lowest height it allows; then the person
        # went to the gallery, where the grown height stays.
        window.setProperty("currentScreen", 1)
        pause(150)
        window.setProperty("saveRestoreHeight", lowest)
        window.setProperty("saveHeightExpanded", True)
        before = geometry(window)
        window.close()
        return {"closed": before, "minimum": lowest}

    def second(window):
        window.showMaximized()
        wait(lambda: window.visibility() == QWindow.Visibility.Maximized, "the maximized window")
        before = geometry(window)
        window.close()
        return {"closed": before}

    def third(window):
        window.showNormal()
        wait(lambda: window.visibility() == QWindow.Visibility.Windowed, "the normal window")
        normal = geometry(window)
        window.setX(50)
        window.setY(40)
        window.setWidth(740)
        window.setHeight(560)
        window.setProperty("saveRestoreHeight", window.minimumHeight() - 1)
        window.setProperty("saveHeightExpanded", True)
        before = geometry(window)
        window.close()
        return {"made_normal": normal, "closed": before}

    def fourth(window):
        window.setX(60)
        window.setY(50)
        window.setWidth(700)
        window.setHeight(500)
        # A card that grew the window earlier and gave the height back leaves its mark.
        window.setProperty("saveRestoreHeight", window.minimumHeight() + 50)
        window.setProperty("saveHeightExpanded", False)
        window.close()
        # Still closing: the close button pressed again after a resize changes nothing.
        window.setHeight(640)
        window.close()

    def fifth(window):
        window.showFullScreen()
        wait(lambda: window.visibility() == QWindow.Visibility.FullScreen, "the full-screen window")
        window.close()

    def sixth(window):
        window.close()

    def seventh(window):
        # Made normal, moved and resized; after a moment maximized, then minimized, and closed
        # from the taskbar. A minimized window still reports the maximized area.
        window.showNormal()
        wait(lambda: window.visibility() == QWindow.Visibility.Windowed, "the normal window")
        pause(400)

        def tracked():
            place = window.property("normalPlace")
            return [round(place.x()), round(place.y()), round(place.width()), round(place.height())]

        # One change at a time: the window follows each of them.
        followed = [tracked()]
        for change, value in ((window.setX, 90), (window.setY, 70), (window.setWidth, 760), (window.setHeight, 540)):
            change(value)
            pause(400)
            followed.append(tracked())
        window.showMaximized()
        wait(lambda: window.visibility() == QWindow.Visibility.Maximized, "the maximized window")
        pause(400)                                  # it stays maximized for a while
        followed.append(tracked())
        window.showMinimized()
        wait(lambda: window.visibility() == QWindow.Visibility.Minimized, "the minimized window")
        before = geometry(window)
        window.close()
        return {"closed": before, "followed": followed}

    def eighth(window):
        # Windows reports a maximize made by a person in this order: the new size while the window is
        # still a normal window, the maximized state a moment later.
        window.showNormal()
        wait(lambda: window.visibility() == QWindow.Visibility.Windowed, "the normal window")
        pause(400)
        normal = geometry(window)
        area = window.screen().availableGeometry()
        window.setGeometry(area)
        # Let the new size arrive while the window is a normal one: a few passes, not a span of time,
        # so a busy machine cannot stretch this to the window's 250 ms wait.
        for _ in range(5):
            application.processEvents()
        assert window.visibility() == QWindow.Visibility.Windowed and window.width() == area.width(), geometry(window)
        window.showMaximized()
        wait(lambda: window.visibility() == QWindow.Visibility.Maximized, "the maximized window")
        pause(400)
        window.close()
        return {"made_normal": normal}

    def ninth(window):
        # A normal window minimized and closed from the taskbar: it reports its own size and place.
        window.showNormal()
        wait(lambda: window.visibility() == QWindow.Visibility.Windowed, "the normal window")
        window.setX(100)
        window.setY(80)
        window.setWidth(700)
        window.setHeight(520)
        pause(400)
        window.showMinimized()
        wait(lambda: window.visibility() == QWindow.Visibility.Minimized, "the minimized window")
        window.close()

    def tenth(window):
        # A saved-file card grew the normal window from the lowest height it allows; the window was
        # maximized, and the card went while it stayed maximized (a new save started, or the details
        # were switched off), which clears the card's mark.
        lowest = window.minimumHeight()
        window.setProperty("currentScreen", 1)
        pause(150)
        window.setHeight(lowest + 150)
        window.setProperty("saveRestoreHeight", lowest)
        window.setProperty("saveHeightExpanded", True)
        pause(400)
        window.showMaximized()
        wait(lambda: window.visibility() == QWindow.Visibility.Maximized, "the maximized window")
        pause(400)
        window.setProperty("saveHeightExpanded", False)
        window.close()
        return {"minimum": lowest}

    def tracked(window):
        place = window.property("normalPlace")
        return [round(place.x()), round(place.y()), round(place.width()), round(place.height())]

    def across(window):
        # A normal window on the left screen, then dragged to the right one and maximized there in one
        # go, without resting there as a normal window: the size it had, centred on the right screen.
        window.setGeometry(100, 80, 900, 600)
        pause(400)
        right = next(screen for screen in application.screens() if screen.name() == "right")
        area = right.availableGeometry()
        window.setGeometry(area.x() + 40, area.y() + 40, 900, 600)
        for _ in range(5):
            application.processEvents()
        if window.screen().name() != "right":
            window.setScreen(right)
            for _ in range(5):
                application.processEvents()
        window.showMaximized()
        wait(lambda: window.visibility() == QWindow.Visibility.Maximized, "the maximized window")
        pause(400)
        notes = {"screen": window.screen().name(), "tracked": tracked(window),
                 "area": [area.x(), area.y(), area.width(), area.height()]}
        window.close()
        return notes

    def stayed(window):
        # Opened maximized on the right screen. Made normal, moved back to the left screen, and
        # maximized there without leaving it: that normal place is kept as it is.
        window.showNormal()
        wait(lambda: window.visibility() == QWindow.Visibility.Windowed, "the normal window")
        window.setGeometry(120, 90, 880, 580)
        pause(400)
        window.showMaximized()
        wait(lambda: window.visibility() == QWindow.Visibility.Maximized, "the maximized window")
        pause(400)
        notes = {"screen": window.screen().name()}
        window.close()
        return notes

    resources = ExitStack()
    with resources:
        folder = Path(resources.enter_context(tempfile.TemporaryDirectory(prefix="luvbox-window-memory-qml-")))
        resources.enter_context(patch.dict(os.environ, {"LOCALAPPDATA": str(folder)}))
        resources.enter_context(patch("luvbox.qt_schedule.task_status", return_value={"registered": False}))
        path = settings_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"gallery_density": 2}), encoding="utf-8")

        screen = application.primaryScreen().availableGeometry()
        report["screen"] = [screen.x(), screen.y(), screen.width(), screen.height()]
        assert screen.width() >= 800 and screen.height() >= 800, report["screen"]

        def no_warnings():
            warnings = [message for message in messages if any(
                mark in message for mark in (".qml", "QQml", "ReferenceError", "TypeError", "Binding loop"))]
            assert not warnings, warnings

        if layout == "two":
            names = [entry.name() for entry in application.screens()]
            assert names == ["left", "right"], names
            remembered = session(across)
            notes = report["sessions"][0]
            assert notes["screen"] == "right" and notes["tracked"] == [100, 80, 900, 600], report
            left, top, width, height = notes["area"]
            assert remembered == {"x": left + (width - 900) // 2, "y": top + (height - 600) // 2,
                                  "width": 900, "height": 600, "maximized": True}, report
            assert session(stayed) == {"x": 120, "y": 90, "width": 880, "height": 580, "maximized": True}, report
            # The next start opened maximized on the right screen, where the window was closed.
            opened = report["sessions"][1]["opened"]
            assert opened["maximized"] and opened["x"] >= left, report
            assert report["sessions"][1]["screen"] == "left", report
            no_warnings()
            report["ok"] = True
            qInstallMessageHandler(previous)
            return report

        remembered = session(first)
        lowest = report["sessions"][0]["minimum"]
        assert remembered == {"x": 30, "y": 20, "width": 720, "height": lowest, "maximized": False}, report
        assert report["sessions"][0]["read"] == {}, report

        assert session(second) == {"x": 30, "y": 20, "width": 720, "height": lowest, "maximized": True}, report
        opened = report["sessions"][1]["opened"]
        assert opened == {"x": 30, "y": 20, "width": 720, "height": lowest, "maximized": False}, report
        closed = report["sessions"][1]["closed"]
        assert closed["maximized"] and closed["width"] > 720 and closed["height"] > lowest, report

        assert session(third) == {"x": 50, "y": 40, "width": 740, "height": 560, "maximized": False}, report
        assert report["sessions"][2]["opened"]["maximized"], report
        normal = report["sessions"][2]["made_normal"]
        assert normal == {"x": 30, "y": 20, "width": 720, "height": lowest, "maximized": False}, report

        assert session(fourth) == {"x": 60, "y": 50, "width": 700, "height": 500, "maximized": False}, report
        assert report["sessions"][3]["opened"] == {
            "x": 50, "y": 40, "width": 740, "height": 560, "maximized": False}, report

        # Full screen is kept as maximized over the normal size, and the next start opens maximized.
        assert session(fifth) == {"x": 60, "y": 50, "width": 700, "height": 500, "maximized": True}, report
        assert session(sixth) == {"x": 60, "y": 50, "width": 700, "height": 500, "maximized": True}, report
        assert report["sessions"][5]["opened"]["maximized"], report

        # Moved, maximized, minimized, closed: the moved normal place and "maximized" are kept.
        assert session(seventh) == {"x": 90, "y": 70, "width": 760, "height": 540, "maximized": True}, report
        closed = report["sessions"][6]["closed"]
        assert not closed["maximized"] and closed["width"] > 760, report      # what the window reported
        # Made normal at the remembered 60,50 700x500, changed one number at a time, then maximized.
        assert report["sessions"][6]["followed"] == [
            [60, 50, 700, 500], [90, 50, 700, 500], [90, 70, 700, 500], [90, 70, 760, 500], [90, 70, 760, 540],
            [90, 70, 760, 540]], report
        # The maximized size arriving before the state is not taken for the normal one.
        assert session(eighth) == {"x": 90, "y": 70, "width": 760, "height": 540, "maximized": True}, report
        assert report["sessions"][7]["made_normal"] == {
            "x": 90, "y": 70, "width": 760, "height": 540, "maximized": False}, report
        # Minimized from a normal window: its own size and place, not maximized.
        assert session(ninth) == {"x": 100, "y": 80, "width": 700, "height": 520, "maximized": False}, report
        # The card went while the window was maximized: the person's own height is still the one kept.
        remembered = session(tenth)
        lowest = report["sessions"][9]["minimum"]
        assert remembered == {"x": 100, "y": 80, "width": 700, "height": lowest, "maximized": True}, report

        # Closed as it opened: the settings file is not written again.
        assert [entry["rewritten"] for entry in report["sessions"]] == [True] * 5 + [False, True, False, True, True], report
        assert all(entry["stayed_open"] for entry in report["sessions"]), report

        stored = json.loads(path.read_text(encoding="utf-8"))
        assert stored["gallery_density"] == 2, stored          # the other preferences stay
        no_warnings()
        report["ok"] = True
    qInstallMessageHandler(previous)
    return report


REMEMBERED = {
    "normal": {"x": 40, "y": 30, "width": 700, "height": 500, "maximized": False},
    # From an older version or a hand edit: only x and the height are usable.
    "damaged": {"x": 40, "y": "30", "width": "wide", "height": 500, "maximized": "no"},
}


def launched(kind):
    """Start through qt_app.launch on a settings file with a remembered window; move it and close it."""
    from contextlib import ExitStack
    import tempfile
    from unittest.mock import patch

    from PySide6.QtCore import QTimer

    sys.path.insert(0, str(ROOT))
    from luvbox import qt_app
    from luvbox.settings import settings_path

    report = {"kind": kind}
    with ExitStack() as resources:
        folder = Path(resources.enter_context(tempfile.TemporaryDirectory(prefix="luvbox-window-launch-")))
        resources.enter_context(patch.dict(os.environ, {"LOCALAPPDATA": str(folder)}))
        resources.enter_context(patch("luvbox.qt_schedule.task_status", return_value={"registered": False}))
        path = settings_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"window": REMEMBERED[kind], "gallery_density": 2}), encoding="utf-8")
        real_place = qt_app._place_window

        def place(application, window, saved):
            # launch's own call, with what launch hands over.
            report["given"] = dict(saved)
            real_place(application, window, saved)
            report["placed"] = [window.x(), window.y(), window.width(), window.height()]

            def move_and_close():
                window.setX(70)
                window.setY(60)
                window.setWidth(720)
                window.setHeight(520)
                window.close()
                report["open_after_close"] = window.isVisible()

            QTimer.singleShot(400, move_and_close)
            QTimer.singleShot(30000, lambda: application.exit(2))    # never hang the test run

        resources.enter_context(patch.object(qt_app, "_place_window", place))
        report["result"] = qt_app.launch()
        report["stored"] = json.loads(path.read_text(encoding="utf-8"))
    return report


@unittest.skipUnless(find_spec("PySide6") is not None, "Qt dependencies are not installed")
class WindowMemoryLaunchTests(unittest.TestCase):
    def run_launch(self, kind):
        environment = dict(os.environ, QT_QPA_PLATFORM="offscreen", QT_QUICK_BACKEND="software",
                           PYTHONIOENCODING="utf-8")
        try:
            run = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--launch", kind], cwd=ROOT,
                                 env=environment, capture_output=True, text=True, encoding="utf-8",
                                 errors="replace", timeout=120)
        except subprocess.TimeoutExpired as error:
            self.fail("launch timed out: " + str(error.stdout or "") + str(error.stderr or ""))
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        return json.loads(run.stdout.strip().splitlines()[-1])

    def test_the_real_start_opens_at_the_remembered_place_and_the_real_close_writes_the_new_one(self):
        report = self.run_launch("normal")
        self.assertEqual(report["given"], REMEMBERED["normal"])
        self.assertEqual(report["placed"], [40, 30, 700, 500])
        self.assertTrue(report["open_after_close"])
        self.assertEqual(report["result"], 0)
        self.assertEqual(report["stored"]["window"], {"x": 70, "y": 60, "width": 720, "height": 520,
                                                      "maximized": False})
        self.assertEqual(report["stored"]["gallery_density"], 2)

    def test_a_damaged_remembered_window_starts_the_app_and_is_mended_by_the_close(self):
        report = self.run_launch("damaged")
        self.assertEqual(report["given"], {"x": 40, "height": 500})
        width = report["placed"][2]
        # A lone x is no place: the window is centred, at the first-start width the screen allows.
        self.assertEqual(report["placed"], [(800 - width) // 2, (800 - 500) // 2, width, 500])
        self.assertEqual(report["result"], 0)
        self.assertEqual(report["stored"]["window"], {"x": 70, "y": 60, "width": 720, "height": 520,
                                                      "maximized": False})


@unittest.skipUnless(find_spec("PySide6") is not None, "Qt dependencies are not installed")
class WindowMemorySceneTests(unittest.TestCase):
    def test_the_window_opens_where_and_how_it_was_closed(self):
        environment = dict(os.environ, QT_QPA_PLATFORM="offscreen", QT_QUICK_BACKEND="software",
                           PYTHONIOENCODING="utf-8")
        try:
            run = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scene"], cwd=ROOT,
                                 env=environment, capture_output=True, text=True, encoding="utf-8",
                                 errors="replace", timeout=120)
        except subprocess.TimeoutExpired as error:
            self.fail("scene timed out: " + str(error.stdout or "") + str(error.stderr or ""))
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        report = json.loads(run.stdout.strip().splitlines()[-1])
        self.assertTrue(report["ok"])
        self.assertEqual([session["remembered"]["maximized"] for session in report["sessions"]],
                         [False, True, False, False, True, True, True, True, False, True])

    def test_a_window_maximized_on_another_screen_is_remembered_there(self):
        # Two screens side by side, as the offscreen platform makes them from a file; it reads the
        # file's name relative to the folder it starts in (a drive letter breaks the option).
        with tempfile.TemporaryDirectory(prefix="luvbox-two-screens-") as folder:
            Path(folder, "screens.json").write_text(json.dumps({
                "synchronousWindowSystemEvents": False, "windowFrameMargins": False,
                "screens": [{"name": "left", "x": 0, "y": 0, "width": 1280, "height": 800, "logicalDpi": 96,
                             "logicalBaseDpi": 96, "dpr": 1},
                            {"name": "right", "x": 1280, "y": 100, "width": 1024, "height": 768, "logicalDpi": 96,
                             "logicalBaseDpi": 96, "dpr": 1}]}), encoding="utf-8")
            environment = dict(os.environ, QT_QPA_PLATFORM="offscreen:configfile=screens.json",
                               QT_QUICK_BACKEND="software", PYTHONIOENCODING="utf-8")
            try:
                run = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scene", "two"], cwd=folder,
                                     env=environment, capture_output=True, text=True, encoding="utf-8",
                                     errors="replace", timeout=120)
            except subprocess.TimeoutExpired as error:
                self.fail("scene timed out: " + str(error.stdout or "") + str(error.stderr or ""))
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        report = json.loads(run.stdout.strip().splitlines()[-1])
        self.assertTrue(report["ok"])
        self.assertEqual([session["screen"] for session in report["sessions"]], ["right", "left"])


if __name__ == "__main__":
    if "--scene" in sys.argv:
        layout = sys.argv[sys.argv.index("--scene") + 1] if len(sys.argv) > sys.argv.index("--scene") + 1 else "one"
        print(json.dumps(scene(layout if layout in ("one", "two") else "one"), ensure_ascii=False))
    elif "--launch" in sys.argv:
        print(json.dumps(launched(sys.argv[sys.argv.index("--launch") + 1]), ensure_ascii=False))
    else:
        unittest.main()

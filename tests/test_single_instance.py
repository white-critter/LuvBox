"""A second LuvBox start shows the program already running instead of opening another one.

Since 1.11.0 closing the window leaves LuvBox in the tray; starting it again used to open a
second program on the same library. Each case runs its programs as separate processes with a
private LOCALAPPDATA (the guard's names come from that folder), so the real LuvBox is never met.
"""

from importlib.util import find_spec
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tests import app_data_isolation  # noqa: E402,F401

HERE = Path(__file__).resolve()


def _wait_for(path, seconds=20):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(.02)
    return False


def first(data, folder, mode):
    """A first program: claim, then serve (or be closing, or crash) until `end` appears."""
    os.environ["LOCALAPPDATA"] = data
    from PySide6.QtCore import QCoreApplication, QTimer
    from luvbox.single_instance import InstanceGuard

    folder = Path(folder)
    application = QCoreApplication([])
    guard = InstanceGuard()
    report = {"claimed": guard.claim(deadline=2), "owner": guard.owner}

    def show():
        if mode == "refuse":
            return False                          # closing, before it stopped listening
        (folder / "shown").write_text(str(guard.requests), encoding="utf-8")
        return True

    guard.attach(show)
    if mode == "closing":
        guard.stopListening()
    (folder / "ready").write_text(json.dumps(report), encoding="utf-8")
    started = time.monotonic()
    released = []

    def tick():
        if released:
            # Still running a while after letting go, as a program does after an update's restart.
            if time.monotonic() - released[0] > 2.5:
                (folder / "exited_at").write_text(repr(time.time()), encoding="utf-8")
                application.quit()
        elif (folder / "end").exists() or time.monotonic() - started > 40:
            if mode == "crash":
                os._exit(3)                       # ends without releasing anything
            guard.release()
            released.append(time.monotonic())
            (folder / "released_at").write_text(repr(time.time()), encoding="utf-8")

    timer = QTimer()
    timer.timeout.connect(tick)
    timer.start(20)
    application.exec()
    return report


def second(data, deadline, linger="0"):
    os.environ["LOCALAPPDATA"] = data
    from PySide6.QtCore import QCoreApplication, QTimer
    from luvbox.single_instance import InstanceGuard

    application = QCoreApplication([])
    guard = InstanceGuard()
    started_at = time.time()
    started = time.monotonic()
    claimed = guard.claim(deadline=float(deadline))
    report = {"claimed": claimed, "owner": guard.owner, "seconds": round(time.monotonic() - started, 3),
              "started_at": started_at, "decided_at": time.time()}
    if float(linger) > 0:
        # Keep running, as a program that opened after the deadline does.
        QTimer.singleShot(int(float(linger) * 1000), application.quit)
        application.exec()
        report["owner_later"] = guard.owner
    guard.release()
    return report


def launched(data, folder, role):
    """The real qt_app.launch: the first hides its window to the tray, the second start shows it."""
    os.environ["LOCALAPPDATA"] = data
    from unittest.mock import patch
    from PySide6.QtCore import QTimer
    from luvbox import qt_app

    folder = Path(folder)
    report = {"role": role}
    made = []
    real_controller = qt_app.ApplicationController
    restarting = []

    def controller(*args, **kwargs):
        made.append(True)
        return real_controller(*args, **kwargs)

    def restart_program():
        return Path(sys.executable) if restarting else None

    def spawn(_command):
        # What the version an update starts would find at this moment.
        run = subprocess.run([sys.executable, str(HERE), "--second", data, "0"], cwd=ROOT,
                             capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
        report["restart_found"] = json.loads(run.stdout.strip().splitlines()[-1]) if run.returncode == 0 else run.stderr

    real_place = qt_app._place_window

    def place(application, window, saved):
        real_place(application, window, saved)
        if role != "first":
            return
        app = window.property("appController")

        def hide():
            window.close()                       # the caption close: into the tray
            report["hidden"] = not window.isVisible() and not app.closing
            (folder / "hidden").write_text("1", encoding="utf-8")
            watch.start(20)

        def check():
            if window.isVisible():
                watch.stop()
                report["shown_again"] = True
                # End as "지금 다시 시작" after an update does.
                app.updates._restart = True
                app.updates._state = "ready"
                restarting.append(True)
                app.requestClose()

        watch = QTimer(window)
        watch.timeout.connect(check)
        QTimer.singleShot(300, hide)
        QTimer.singleShot(40000, lambda: application.exit(2))     # never hang the test run

    started = time.monotonic()
    with patch.object(qt_app, "ApplicationController", controller), \
            patch.object(qt_app, "_place_window", place), \
            patch("luvbox.launcher.launcher_path", restart_program), \
            patch("luvbox.launcher.spawn_detached", spawn), \
            patch("luvbox.qt_schedule.task_status", return_value={"registered": False}):
        report["result"] = qt_app.launch()
    report["seconds"] = round(time.monotonic() - started, 3)
    report["controllers"] = len(made)
    return report


@unittest.skipUnless(find_spec("PySide6") is not None, "Qt dependencies are not installed")
class InstanceNameTests(unittest.TestCase):
    """Every later version must find a running earlier one: the names are a contract between versions."""

    def test_the_names_come_from_the_data_folder_and_the_session_only(self):
        import hashlib
        from unittest.mock import patch
        from luvbox import single_instance

        with tempfile.TemporaryDirectory(prefix="luvbox-one-name-") as data:
            with patch.dict(os.environ, {"LOCALAPPDATA": data}):
                identity = single_instance._identity()
                guard = single_instance.InstanceGuard(identity=identity)
                again = single_instance._identity()
            with patch.dict(os.environ, {"LOCALAPPDATA": data + "-other"}):
                other = single_instance._identity()
        session = 0
        if sys.platform == "win32":
            import ctypes
            value = ctypes.c_ulong()
            ctypes.windll.kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(value))
            session = value.value
        folder = os.path.normcase(os.path.abspath(os.path.join(data, "LuvBox")))
        self.assertEqual(identity, hashlib.sha256(f"{session}\0{folder}".encode("utf-8")).hexdigest()[:32])
        self.assertEqual(identity, again)
        self.assertNotEqual(identity, other)
        self.assertEqual(guard.server_name, "LuvBox-instance-" + identity)
        self.assertEqual(guard._mutex_name, "Local\\LuvBox-instance-" + identity)


@unittest.skipUnless(sys.platform == "win32" and find_spec("PySide6") is not None,
                     "the guard is a Windows mutex and a Qt local socket")
class SingleInstanceTests(unittest.TestCase):
    def setUp(self):
        self.data = tempfile.mkdtemp(prefix="luvbox-one-")
        self.folder = Path(tempfile.mkdtemp(prefix="luvbox-one-run-"))
        self.processes = []
        self.environment = dict(os.environ, PYTHONIOENCODING="utf-8")

    def tearDown(self):
        (self.folder / "end").write_text("1", encoding="utf-8")
        for process in self.processes:
            try:
                process.communicate(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
        import shutil
        shutil.rmtree(self.data, ignore_errors=True)
        shutil.rmtree(self.folder, ignore_errors=True)

    def start_first(self, mode, data=None):
        process = subprocess.Popen([sys.executable, str(HERE), "--first", data or self.data, str(self.folder), mode],
                                   cwd=ROOT, env=self.environment, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
        self.processes.append(process)
        self.assertTrue(_wait_for(self.folder / "ready"), "the first program did not start")
        report = json.loads((self.folder / "ready").read_text(encoding="utf-8"))
        self.assertEqual(report, {"claimed": True, "owner": True})
        return process

    def run_second(self, deadline, data=None):
        run = subprocess.run([sys.executable, str(HERE), "--second", data or self.data, str(deadline)], cwd=ROOT,
                             env=self.environment, capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=60)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        return json.loads(run.stdout.strip().splitlines()[-1])

    def test_a_second_start_asks_the_first_to_show_its_window_and_ends(self):
        self.start_first("serve")
        report = self.run_second(10)
        self.assertEqual((report["claimed"], report["owner"]), (False, False), report)
        self.assertLess(report["seconds"], 3, report)
        self.assertTrue((self.folder / "shown").exists())

    def test_a_start_while_the_first_is_closing_waits_and_then_is_the_program(self):
        first_run = self.start_first("closing")
        process = subprocess.Popen([sys.executable, str(HERE), "--second", self.data, "15"], cwd=ROOT,
                                   env=self.environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, encoding="utf-8", errors="replace")
        time.sleep(2)
        (self.folder / "end").write_text("1", encoding="utf-8")       # the first program lets go
        out, err = process.communicate(timeout=30)
        self.assertEqual(process.returncode, 0, out + err)
        first_run.communicate(timeout=20)
        report = json.loads(out.strip().splitlines()[-1])
        self.assertEqual((report["claimed"], report["owner"]), (True, True), report)
        released = float((self.folder / "released_at").read_text(encoding="utf-8"))
        exited = float((self.folder / "exited_at").read_text(encoding="utf-8"))
        # It waited, and took over as soon as the first let go, while that one was still running.
        self.assertLess(report["started_at"], released, report)
        self.assertLess(released - .05, report["decided_at"], report)
        self.assertLess(report["decided_at"], min(exited, released + 1.5), (report, released, exited))
        self.assertFalse((self.folder / "shown").exists())

    def test_a_first_program_that_crashed_does_not_hold_the_next_start(self):
        process = self.start_first("crash")
        (self.folder / "end").write_text("1", encoding="utf-8")
        process.communicate(timeout=20)
        self.assertEqual(process.returncode, 3)
        report = self.run_second(10)
        self.assertEqual((report["claimed"], report["owner"]), (True, True), report)
        self.assertLess(report["seconds"], 1, report)

    def test_a_first_program_that_never_answers_lets_the_start_open_after_the_deadline(self):
        self.start_first("closing")
        report = self.run_second(1.5)
        self.assertEqual((report["claimed"], report["owner"]), (True, False), report)
        self.assertGreaterEqual(report["seconds"], 1.4, report)
        self.assertLess(report["seconds"], 8, report)

    def test_a_program_that_opened_after_the_deadline_takes_the_name_once_the_first_ends(self):
        self.start_first("closing")
        process = subprocess.Popen([sys.executable, str(HERE), "--second", self.data, "1", "7"], cwd=ROOT,
                                   env=self.environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, encoding="utf-8", errors="replace")
        time.sleep(3)
        (self.folder / "end").write_text("1", encoding="utf-8")
        out, err = process.communicate(timeout=30)
        self.assertEqual(process.returncode, 0, out + err)
        report = json.loads(out.strip().splitlines()[-1])
        self.assertEqual((report["claimed"], report["owner"], report["owner_later"]), (True, False, True), report)

    def test_a_first_program_that_cannot_show_its_window_does_not_end_the_start(self):
        self.start_first("refuse")
        report = self.run_second(1.5)
        self.assertEqual((report["claimed"], report["owner"]), (True, False), report)
        self.assertGreaterEqual(report["seconds"], 1.4, report)

    def test_another_data_folder_is_another_program(self):
        self.start_first("serve")
        other = tempfile.mkdtemp(prefix="luvbox-one-other-")
        try:
            report = self.run_second(10, data=other)
        finally:
            import shutil
            shutil.rmtree(other, ignore_errors=True)
        self.assertEqual((report["claimed"], report["owner"]), (True, True), report)
        self.assertLess(report["seconds"], 1, report)
        self.assertFalse((self.folder / "shown").exists())

    def test_the_real_start_shows_the_window_hidden_in_the_tray(self):
        environment = dict(self.environment, QT_QPA_PLATFORM="offscreen", QT_QUICK_BACKEND="software")
        first_run = subprocess.Popen([sys.executable, str(HERE), "--launch", self.data, str(self.folder), "first"],
                                     cwd=ROOT, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     text=True, encoding="utf-8", errors="replace")
        self.processes.append(first_run)
        self.assertTrue(_wait_for(self.folder / "hidden", 60), "the first program never hid its window")
        second_run = subprocess.run([sys.executable, str(HERE), "--launch", self.data, str(self.folder), "second"],
                                    cwd=ROOT, env=environment, capture_output=True, text=True,
                                    encoding="utf-8", errors="replace", timeout=90)
        self.assertEqual(second_run.returncode, 0, second_run.stdout + second_run.stderr)
        second_report = json.loads(second_run.stdout.strip().splitlines()[-1])
        # It ended at once, without opening a library or a window of its own.
        self.assertEqual((second_report["result"], second_report["controllers"]), (0, 0), second_report)
        out, err = first_run.communicate(timeout=60)
        self.assertEqual(first_run.returncode, 0, out + err)
        first_report = json.loads(out.strip().splitlines()[-1])
        self.assertTrue(first_report["hidden"], first_report)
        self.assertTrue(first_report.get("shown_again"), first_report)
        self.assertEqual((first_report["result"], first_report["controllers"]), (0, 1), first_report)
        # The restart after an update is started only once this program has let go of the name.
        found = first_report.get("restart_found")
        self.assertIsInstance(found, dict, first_report)
        self.assertEqual((found["claimed"], found["owner"]), (True, True), first_report)


if __name__ == "__main__":
    if len(sys.argv) == 5 and sys.argv[1] == "--first":
        print(json.dumps(first(*sys.argv[2:])))
    elif len(sys.argv) in (4, 5) and sys.argv[1] == "--second":
        print(json.dumps(second(*sys.argv[2:])))
    elif len(sys.argv) == 5 and sys.argv[1] == "--launch":
        print(json.dumps(launched(*sys.argv[2:])))
    else:
        unittest.main()

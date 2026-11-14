"""Every offline test run uses a private app-data folder, never this PC's."""

import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

# Every test module imports this before anything from luvbox, so a module run
# on its own is isolated too; discovery alone used to be the only protection.
from tests import app_data_isolation
from luvbox import settings

TESTS = Path(__file__).resolve().parent
_ISOLATION = re.compile(r"^\s*(from tests import app_data_isolation|import app_data_isolation)\b")
_LUVBOX = re.compile(r"^\s*(from luvbox\b|import luvbox\b)")

# What a scene test's child process does first: load a QML file from disk.
_LOAD_QML = """
import sys
from PySide6.QtCore import QCoreApplication, QUrl
from PySide6.QtQml import QQmlComponent, QQmlEngine

application = QCoreApplication(sys.argv)
engine = QQmlEngine()
component = QQmlComponent(engine, QUrl.fromLocalFile(sys.argv[1]))
thing = component.create()
loaded = thing is not None and thing.property("doubled") == 8
del thing, component, engine
sys.exit(0 if loaded else 1)
"""


class AppDataIsolationTests(unittest.TestCase):
    def test_suite_app_data_is_a_private_temporary_folder(self):
        folder = Path(app_data_isolation.FOLDER)
        self.assertEqual(Path(os.environ["LOCALAPPDATA"]), folder)
        self.assertEqual(folder.parent, Path(tempfile.gettempdir()))
        self.assertEqual(settings.app_directory(), folder / "LuvBox")

    def test_every_test_module_isolates_before_it_imports_the_application(self):
        for path in sorted(TESTS.glob("test_*.py")):
            lines = path.read_text(encoding="utf-8-sig").splitlines()
            isolation = next((index for index, line in enumerate(lines) if _ISOLATION.match(line)), None)
            application = next((index for index, line in enumerate(lines) if _LUVBOX.match(line)), None)
            with self.subTest(module=path.name):
                self.assertIsNotNone(isolation, "import tests.app_data_isolation at the top of this module")
                if application is not None:
                    self.assertLess(isolation, application, "the isolation import has to come before luvbox")

    def test_the_real_folder_is_refused_for_the_rest_of_the_process(self):
        reached = list(app_data_isolation.REACHED)
        self.addCleanup(lambda: app_data_isolation.REACHED.__setitem__(slice(None), reached))
        real = next(iter(app_data_isolation.REAL_FOLDERS))
        # Pointed back at the real folder, and with the variable gone (the fallback is the real folder).
        for pointed_back in (True, False):
            with self.subTest(pointed_back=pointed_back):
                with patch.dict(os.environ):
                    if pointed_back:
                        os.environ["LOCALAPPDATA"] = real
                    else:
                        del os.environ["LOCALAPPDATA"]
                    with self.assertRaises(app_data_isolation.RealAppDataReached):
                        settings.app_directory()
        self.assertEqual(len(app_data_isolation.REACHED), len(reached) + 2)
        # A private folder a test chooses itself is still fine.
        with tempfile.TemporaryDirectory(prefix="luvbox-isolation-test-") as folder:
            with patch.dict(os.environ, {"LOCALAPPDATA": folder}):
                self.assertEqual(settings.app_directory(), Path(folder) / "LuvBox")

    def test_qt_is_given_a_qml_cache_folder_inside_the_private_folder(self):
        # Qt asks Windows for its cache folder and ignores LOCALAPPDATA; only this variable moves it.
        cache = Path(os.environ["QML_DISK_CACHE_PATH"])
        self.assertEqual(cache, Path(app_data_isolation.QML_CACHE))
        self.assertEqual(cache.parent, Path(app_data_isolation.FOLDER))
        # Beside the application's folder: a test that looks into its app data never meets Qt's files.
        self.assertNotEqual(cache, settings.app_directory())
        self.assertFalse(cache.is_relative_to(settings.app_directory()))

    def test_a_child_process_writes_its_compiled_qml_into_that_folder(self):
        # Compiled QML used to land under the real LocalAppData (Python\cache\qmlcache). Qt has one
        # cache folder, so a new file's cache appearing here means it went nowhere else.
        cache = Path(app_data_isolation.QML_CACHE)
        before = {item.name for item in cache.glob("*.qmlc")} if cache.is_dir() else set()
        with tempfile.TemporaryDirectory(prefix="luvbox-isolation-qml-") as folder:
            source = Path(folder) / "Probe.qml"
            source.write_text("import QtQml\nQtObject {\n    property int size: 4\n"
                              "    property int doubled: size * 2\n    function next(x) { return x + 1 }\n}\n",
                              encoding="utf-8")
            result = subprocess.run([sys.executable, "-c", _LOAD_QML, str(source)], capture_output=True, text=True,
                                    timeout=120, env={**os.environ, "QT_QPA_PLATFORM": "offscreen"})
        self.assertEqual(result.returncode, 0, result.stderr)
        after = {item.name for item in cache.glob("*.qmlc")}
        self.assertEqual(len(after - before), 1, f"one new cache file for the one new QML file, got {after - before}")

    def test_the_collector_runs_between_tests_on_the_main_thread_only(self):
        # Collected on a worker thread, finished Qt controllers were destroyed off their thread and a
        # later test crashed. While a test runs the collector is off; the wrapper runs it afterwards.
        import gc
        import threading

        self.assertFalse(gc.isenabled())
        self.assertIs(unittest.TestCase.run, app_data_isolation._run_then_collect)
        ran_on = []
        note = lambda phase, _info: ran_on.append(threading.current_thread()) if phase == "start" else None
        gc.callbacks.append(note)
        try:
            class Inner(unittest.TestCase):
                def test_garbage(self):
                    cycle = []
                    cycle.append(cycle)
                    worker = threading.Thread(target=lambda: [[] for _ in range(200_000)])
                    worker.start()
                    worker.join()

            outcome = unittest.TestResult()
            Inner("test_garbage").run(outcome)
        finally:
            gc.callbacks.remove(note)
        self.assertTrue(outcome.wasSuccessful())
        self.assertTrue(ran_on, "the collector ran after the test")
        self.assertEqual(set(ran_on), {threading.main_thread()})

    def test_both_names_of_the_module_are_one_module(self):
        self.assertIs(sys.modules["tests.app_data_isolation"], sys.modules["app_data_isolation"])


if __name__ == "__main__":
    unittest.main()

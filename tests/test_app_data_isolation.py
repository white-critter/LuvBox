"""Every offline test run uses a private app-data folder, never this PC's."""

import os
from pathlib import Path
import re
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

    def test_both_names_of_the_module_are_one_module(self):
        import sys

        self.assertIs(sys.modules["tests.app_data_isolation"], sys.modules["app_data_isolation"])


if __name__ == "__main__":
    unittest.main()

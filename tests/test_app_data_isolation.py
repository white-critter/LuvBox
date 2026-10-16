"""Every offline test run uses a private app-data folder, never this PC's."""

import os
from pathlib import Path
import tempfile
import unittest

# Discovery imports this module before any test runs, which isolates the
# whole suite; modules that build records stores also import it directly.
import app_data_isolation
from luvbox import settings


class AppDataIsolationTests(unittest.TestCase):
    def test_suite_app_data_is_a_private_temporary_folder(self):
        folder = Path(app_data_isolation.FOLDER)
        self.assertEqual(Path(os.environ["LOCALAPPDATA"]), folder)
        self.assertEqual(folder.parent, Path(tempfile.gettempdir()))
        self.assertEqual(settings.app_directory(), folder / "LuvBox")


if __name__ == "__main__":
    unittest.main()

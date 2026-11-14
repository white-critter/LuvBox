"""The built-in startup check keeps Qt's compiled-QML cache out of this PC's real cache folder.

Qt does not read LOCALAPPDATA for that cache, so the check's own temporary app data
does not cover it: `--qt-smoke` and `--self-test` run from source used to leave .qmlc
files in the real %LOCALAPPDATA%\\LuvBox\\LuvBox\\cache\\qmlcache.
"""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests import app_data_isolation
from luvbox import qt_app, qt_smoke

_NAMES = ("LOCALAPPDATA", "QML_DISK_CACHE_PATH", "QML_DISABLE_DISK_CACHE")


class SmokeQmlCacheTests(unittest.TestCase):
    def run_smoke(self):
        """What Qt would see when the window starts; the window itself is not opened."""
        seen = {}

        def launch(*, startup_probe=None):
            seen.update({name: os.environ.get(name) for name in _NAMES})
            return 0

        with tempfile.TemporaryDirectory(prefix="luvbox-smoke-isolation-") as folder, \
                patch.object(qt_app, "launch", launch):
            destination = Path(folder) / "report.json"
            qt_smoke.run(destination)
            report = json.loads(destination.read_text(encoding="utf-8"))
        self.assertTrue(seen, "the check reached the point where the window starts")
        self.assertTrue(report["temporary_data_removed"])
        return seen

    def test_a_cache_folder_given_by_the_caller_is_kept(self):
        # The build's check and the tests give Qt a folder of their own.
        given = os.environ["QML_DISK_CACHE_PATH"]
        self.assertEqual(given, app_data_isolation.QML_CACHE)
        with patch.dict(os.environ):
            os.environ.pop("QML_DISABLE_DISK_CACHE", None)
            seen = self.run_smoke()
            left = {name: os.environ.get(name) for name in _NAMES}
        self.assertEqual(seen["QML_DISK_CACHE_PATH"], given)
        self.assertIsNone(seen["QML_DISABLE_DISK_CACHE"])
        self.assertEqual(left, {"LOCALAPPDATA": app_data_isolation.FOLDER, "QML_DISK_CACHE_PATH": given,
                                "QML_DISABLE_DISK_CACHE": None})

    def test_without_one_the_check_writes_no_cache_at_all(self):
        # Started by hand: Qt would use this PC's real cache folder, so the cache is switched off.
        with patch.dict(os.environ):
            del os.environ["QML_DISK_CACHE_PATH"]
            os.environ.pop("QML_DISABLE_DISK_CACHE", None)
            seen = self.run_smoke()
            left = {name: os.environ.get(name) for name in _NAMES}
        self.assertIsNone(seen["QML_DISK_CACHE_PATH"])
        self.assertEqual(seen["QML_DISABLE_DISK_CACHE"], "1")
        # The check's own temporary app data, as before.
        self.assertNotEqual(seen["LOCALAPPDATA"], app_data_isolation.FOLDER)
        # Everything is put back for whatever runs next in this process.
        self.assertEqual(left, {"LOCALAPPDATA": app_data_isolation.FOLDER, "QML_DISK_CACHE_PATH": None,
                                "QML_DISABLE_DISK_CACHE": None})

    def test_a_value_the_caller_set_itself_is_put_back(self):
        with patch.dict(os.environ, {"QML_DISABLE_DISK_CACHE": "0"}):
            del os.environ["QML_DISK_CACHE_PATH"]
            seen = self.run_smoke()
            self.assertEqual(os.environ["QML_DISABLE_DISK_CACHE"], "0")
        self.assertEqual(seen["QML_DISABLE_DISK_CACHE"], "1")


if __name__ == "__main__":
    unittest.main()

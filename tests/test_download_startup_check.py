"""A successful UI check cannot hide a failed or stale downloader startup."""

import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tools import check_download_startup as gate


class DownloadStartupGateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="luvbox-download-gate-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.executable = self.root / "LuvBox.exe"
        self.executable.write_bytes(b"synthetic frozen executable")
        self.destination = self.root / "reports" / "download.json"

    def report(self):
        return {"ok": True, "frozen": True, "isolated": True, "managed_cookies": True,
                "authentication_or_network": False, "prepared_requests": 1,
                "extractor": "TwitterLikesExtractor", "version": gate.__version__}

    def child(self, report, code=0):
        def run(arguments, **options):
            self.assertFalse(self.destination.exists())
            self.assertEqual(arguments, [str(self.executable.resolve()), "--download-smoke",
                                         str(self.destination.resolve())])
            self.assertEqual(options["timeout"], 30)
            self.destination.write_text(json.dumps(report), encoding="utf-8")
            return subprocess.CompletedProcess(arguments, code, b"", b"")
        return run

    def test_complete_report_is_bound_to_exact_executable(self):
        with patch.object(gate.subprocess, "run", side_effect=self.child(self.report())):
            report = gate.check(self.executable, self.destination)
        self.assertEqual(report["executable_sha256"], hashlib.sha256(self.executable.read_bytes()).hexdigest())
        self.assertEqual(report, json.loads(self.destination.read_text(encoding="utf-8")))

    def test_incomplete_or_nonzero_result_cannot_be_bound(self):
        changes = [{name: False} for name in ("ok", "frozen", "isolated", "managed_cookies")]
        changes += [{"authentication_or_network": True}, {"prepared_requests": True},
                    {"error": "OSError"}, {"initialization_error": {"type": "FileNotFoundError"}},
                    {"extractor": "FakeExtractor"}, {"version": "0.0.0"}, {}]
        for change in changes:
            with self.subTest(change=change):
                with patch.object(gate.subprocess, "run", side_effect=self.child(
                        {**self.report(), **change}, 0 if change else 1)):
                    with self.assertRaises((RuntimeError, ValueError)):
                        gate.check(self.executable, self.destination)
                self.assertNotIn("executable_sha256", json.loads(self.destination.read_text(encoding="utf-8")))

    def test_stale_report_is_removed_when_child_does_not_write_or_times_out(self):
        self.destination.parent.mkdir()
        for failure in (None, subprocess.TimeoutExpired([], 30)):
            with self.subTest(failure=type(failure).__name__):
                self.destination.write_text(json.dumps(self.report()), encoding="utf-8")
                with patch.object(gate.subprocess, "run", return_value=subprocess.CompletedProcess([], 1),
                                  side_effect=failure):
                    with self.assertRaises((RuntimeError, subprocess.TimeoutExpired)):
                        gate.check(self.executable, self.destination)
                self.assertFalse(self.destination.exists())


if __name__ == "__main__":
    unittest.main()

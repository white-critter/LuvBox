"""Exercise the frozen startup gate with synthetic child-process reports."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tools import check_qt_startup


class FrozenStartupCheckTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="luvbox-startup-check-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.executable = self.root / "LuvBox-test.exe"
        self.executable.write_bytes(b"synthetic frozen executable")
        self.destination = self.root / "reports" / "startup.json"

    def complete_report(self):
        return {
            "ok": True,
            "frozen": True,
            "isolated": True,
            "window_visible": True,
            "window_exposed": True,
            "gallery_thumbnail_ready": True,
            "photo_preview_ready": True,
            "settings_and_confirmation": True,
            "connection_dialog": True,
            "shutdown_ready": True,
            "temporary_data_removed": True,
            "version": check_qt_startup.__version__,
            "platform": "windows",
            "qml_errors": [],
        }

    def child_result(self, report, returncode=0):
        def run(arguments, **options):
            self.assertFalse(self.destination.exists(), "Old reports must be removed before launch")
            self.destination.write_text(json.dumps(report), encoding="utf-8")
            return subprocess.CompletedProcess(arguments, returncode, b"", b"")
        return run

    def test_complete_native_frozen_startup_is_bound_to_exact_executable(self):
        with patch.object(check_qt_startup.subprocess, "run", side_effect=self.child_result(self.complete_report())) as run:
            report = check_qt_startup.check(self.executable, self.destination)
        arguments, options = run.call_args
        self.assertEqual(arguments[0], [str(self.executable.resolve()), "--qt-smoke", str(self.destination.resolve())])
        self.assertEqual(options["timeout"], 60)
        self.assertEqual(report["executable_sha256"], hashlib.sha256(self.executable.read_bytes()).hexdigest())
        self.assertEqual(json.loads(self.destination.read_text(encoding="utf-8")), report)

    def test_source_incomplete_or_error_reports_are_rejected(self):
        valid = self.complete_report()
        changes = [{name: False} for name, value in valid.items() if value is True]
        changes += [{"version": "0.0.0"}, {"qml_errors": ["missing QML dependency"]}]
        if os.name == "nt":
            changes.append({"platform": "offscreen"})
        for change in changes:
            with self.subTest(change=change):
                report = {**valid, **change}
                with patch.object(check_qt_startup.subprocess, "run", side_effect=self.child_result(report)):
                    with self.assertRaisesRegex(RuntimeError, "startup failed"):
                        check_qt_startup.check(self.executable, self.destination)
                self.assertNotIn("executable_sha256", json.loads(self.destination.read_text(encoding="utf-8")))

    def test_nonzero_exit_is_rejected_even_with_complete_report(self):
        with patch.object(check_qt_startup.subprocess, "run", side_effect=self.child_result(self.complete_report(), 1)):
            with self.assertRaisesRegex(RuntimeError, "startup failed"):
                check_qt_startup.check(self.executable, self.destination)

    def test_missing_report_is_rejected(self):
        with patch.object(check_qt_startup.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)):
            with self.assertRaisesRegex(RuntimeError, "did not write a report"):
                check_qt_startup.check(self.executable, self.destination)

    def test_previous_success_report_cannot_mask_child_failure(self):
        self.destination.parent.mkdir()
        self.destination.write_text(json.dumps(self.complete_report()), encoding="utf-8")
        with patch.object(check_qt_startup.subprocess, "run", return_value=subprocess.CompletedProcess([], 1)):
            with self.assertRaisesRegex(RuntimeError, "did not write a report"):
                check_qt_startup.check(self.executable, self.destination)
        self.assertFalse(self.destination.exists())

    def test_timeout_does_not_leave_a_previous_success_report(self):
        self.destination.parent.mkdir()
        self.destination.write_text(json.dumps(self.complete_report()), encoding="utf-8")
        timeout = subprocess.TimeoutExpired([str(self.executable)], 60)
        with patch.object(check_qt_startup.subprocess, "run", side_effect=timeout):
            with self.assertRaises(subprocess.TimeoutExpired):
                check_qt_startup.check(self.executable, self.destination)
        self.assertFalse(self.destination.exists())


if __name__ == "__main__":
    unittest.main()

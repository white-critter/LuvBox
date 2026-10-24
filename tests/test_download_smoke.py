"""The frozen download gate initializes the real extractor without network."""

import json
from contextlib import contextmanager
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import entry


ROOT = Path(__file__).resolve().parents[1]


class DownloadStartupSmokeTests(unittest.TestCase):
    def test_cli_reaches_real_authenticated_request_without_network(self):
        with tempfile.TemporaryDirectory(prefix="luvbox-smoke-report-") as folder:
            destination = Path(folder) / "report.json"
            child = subprocess.run([sys.executable, str(ROOT / "entry.py"),
                                    "--download-smoke", str(destination)],
                                   capture_output=True, timeout=30,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.assertEqual(child.returncode, 0, child.stderr.decode(errors="replace"))
            report = json.loads(destination.read_text(encoding="utf-8"))
            self.assertTrue(report["ok"])
            self.assertTrue(report["isolated"])
            self.assertTrue(report["managed_cookies"])
            self.assertEqual(report["extractor"], "TwitterLikesExtractor")
            self.assertEqual(report["prepared_requests"], 1)
            self.assertFalse(report["authentication_or_network"])

    def test_initialization_failure_cannot_pass_the_gate_or_leak_exception_text(self):
        from gallery_dl.extractor.common import Extractor

        def fail_initialization(self):
            raise ModuleNotFoundError("private-cookie-value")

        with patch.object(Extractor, "_init_session", fail_initialization):
            report = entry._check_download_start()
        self.assertFalse(report["ok"])
        self.assertEqual(report["prepared_requests"], 0)
        self.assertEqual(report["worker_exit_code"], 1)
        self.assertEqual(report["initialization_error"]["type"], "ModuleNotFoundError")
        self.assertTrue(any(frame["function"] == "fail_initialization"
                            for frame in report["initialization_error"]["frames"]))
        self.assertNotIn("private-cookie-value", json.dumps(report))

    def test_cleanup_failure_cannot_leave_a_successful_report(self):
        original_directory = tempfile.TemporaryDirectory

        @contextmanager
        def fail_after_cleanup(*args, **kwargs):
            with original_directory(*args, **kwargs) as folder:
                yield folder
            raise OSError("private cleanup details")

        with patch.object(tempfile, "TemporaryDirectory", fail_after_cleanup):
            report = entry._check_download_start()
        self.assertEqual(report["prepared_requests"], 1)
        self.assertFalse(report["ok"])
        self.assertEqual(report["error"], "OSError")
        self.assertNotIn("private cleanup details", json.dumps(report))

    def test_unexpected_network_path_is_blocked_and_all_process_state_is_restored(self):
        from gallery_dl.extractor.common import Extractor
        from gallery_dl.extractor.twitter import TwitterAPI
        from requests.sessions import Session
        from luvbox import login, metadata

        before = (Session.send, login.load_session_cookies, metadata.available,
                  socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo,
                  TwitterAPI.client_transaction, Extractor.initialize)
        environment = {name: os.environ.get(name) for name in ("LOCALAPPDATA", "APPDATA")}

        def unexpected_request(self):
            socket.getaddrinfo("this-must-never-resolve.invalid", 443)

        with patch.object(Extractor, "_init_session", unexpected_request):
            report = entry._check_download_start()
        self.assertFalse(report["ok"])
        self.assertIn("blocked", report["error"])
        self.assertEqual(report["prepared_requests"], 0)
        self.assertEqual(before, (Session.send, login.load_session_cookies, metadata.available,
                                 socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo,
                                 TwitterAPI.client_transaction, Extractor.initialize))
        self.assertEqual(environment, {name: os.environ.get(name) for name in environment})


if __name__ == "__main__":
    unittest.main()

"""Qt cancellation reaches a real isolated worker without user data or network."""

from dataclasses import replace
from contextlib import closing
from importlib.util import find_spec
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from tests import app_data_isolation  # noqa: F401
from luvbox import download_service
from luvbox.core import JobOptions
from luvbox.storage import resolve_storage


_QT_AVAILABLE = find_spec("PySide6") is not None
if _QT_AVAILABLE:
    from PySide6.QtCore import QCoreApplication
    from luvbox.qt_download import DownloadController


_WORKER = r'''
import json
import os
from pathlib import Path
import sys
import time
from unittest.mock import patch

request, events, stop = map(Path, sys.argv[1:])
values = json.loads(request.read_text(encoding="utf-8"))
output = Path(values["output_dir"])
root = output.parent
os.environ["LOCALAPPDATA"] = str(root / "user-data")
from luvbox.core import JobOptions, run_worker
from tests.test_core import FakeLikesExtractor, FakeHttpDownloader, SECRET

def before(extractor, row):
    if row[0] != "101":
        return
    scenario = (root / "scenario.txt").read_text()
    if scenario == "resume":
        return
    (output / "resumable.part").write_bytes(b"retained partial input")
    if scenario == "forced-conversion":
        from luvbox.media import _run
        child = ("import os,time;from pathlib import Path;"
                 f"Path({str(root / 'helper.pid')!r}).write_text(str(os.getpid()));time.sleep(60)")
        _run([sys.executable, "-c", child],
             check_stop=lambda: (root / "waiting").touch(), deadline=time.monotonic()+60)
    elif scenario == "cooperative":
        (root / "waiting").touch()
        extractor.sleep(20, "request")
    else:
        (root / "waiting").touch()
        while True:
            time.sleep(.05)

cookies = [{"name": "auth_token", "value": SECRET, "domain": ".x.com", "path": "/",
            "expires": -1, "secure": True, "httpOnly": True}]
rows = [("100", 1, "photo", "alice"), ("101", 1, "photo", "alice")]
with events.open("a", encoding="utf-8", buffering=1) as stream:
    def emit(event):
        stream.write(json.dumps(event, ensure_ascii=False) + "\n")
    with patch("gallery_dl.extractor.find", side_effect=lambda url: FakeLikesExtractor(url, rows, before_row=before)), \
            patch("gallery_dl.downloader.find", return_value=FakeHttpDownloader), \
            patch("luvbox.login.load_session_cookies", return_value=cookies), \
            patch("luvbox.metadata.available", return_value=False):
        code = run_worker(JobOptions(**values), emit, stop)
raise SystemExit(code)
'''


@unittest.skipUnless(_QT_AVAILABLE, "Qt dependencies are unavailable")
class RealWorkerCancellationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QCoreApplication.instance() or QCoreApplication([])

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="luvbox-stop-worker-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = patch.dict(os.environ, {"LOCALAPPDATA": str(self.root / "user-data")})
        environment.start()
        self.addCleanup(environment.stop)
        self.options = JobOptions("offline", str(self.root / "media"), include_videos=False,
                                  include_gifs=False)
        command = patch.object(download_service, "worker_command",
                               side_effect=lambda request, events, stop:
                               [sys.executable, "-u", "-c", _WORKER, str(request), str(events), str(stop)])
        command.start()
        self.addCleanup(command.stop)
        self.service = download_service.DownloadService()
        self.controller = DownloadController(service=self.service)
        self.events = []
        self.controller.eventReceived.connect(self.events.append)
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.controller.shutdown()
        self.service.close()
        # Only this service's exact disposable worker is retired on a failed
        # assertion; no process names or unrelated application PIDs are used.
        process = self.service._process
        if process is not None and process.poll() is None:
            process.kill()
        if self.service._thread is not None:
            self.service._thread.join(timeout=5)
        self.controller._pump()
        self.application.processEvents()

    def wait_for(self, predicate, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.application.processEvents()
            if predicate():
                return
            time.sleep(.01)
        self.fail("The isolated worker did not reach the expected cancellation state")

    def start_waiting(self, scenario):
        (self.root / "scenario.txt").write_text(scenario)
        self.assertTrue(self.controller.start(self.options))
        self.wait_for(lambda: (self.root / "waiting").is_file() and self.controller.savedCount == 1)

    def read_history(self, sql):
        location = resolve_storage(self.options.output_dir, self.options.account)
        with closing(sqlite3.connect(location.record_dir / ".luvbox-history.sqlite3")) as db:
            return db.execute(sql).fetchall()

    def test_qt_stop_preserves_completed_archive_partial_and_allows_restart(self):
        self.start_waiting("cooperative")
        self.controller.stop()
        self.controller.stop()
        self.assertTrue(self.controller.stopping)
        self.wait_for(lambda: not self.controller.running)
        self.assertEqual(self.controller.error, "")
        self.assertIn("중단했습니다", self.controller.status)
        self.assertEqual(self.read_history("SELECT status,saved FROM runs"), [("stopped", 1)])
        terminal = next(event for event in self.events if event["type"] == "finished")
        self.assertIsInstance(terminal["history_run_id"], int)
        self.assertFalse(self.events[-1]["forced"])
        self.assertTrue((self.root / "media/alice_100_1.jpg").is_file())
        self.assertEqual((self.root / "media/resumable.part").read_bytes(), b"retained partial input")
        self.restart_and_verify()

    def test_forced_stop_is_reported_and_next_run_reconciles_interrupted_history(self):
        with patch.object(download_service, "_STOP_GRACE_SECONDS", .3), \
                patch.object(download_service, "_KILL_GRACE_SECONDS", .1):
            self.start_waiting("unresponsive")
            self.controller.stop()
            self.wait_for(lambda: not self.controller.running)
        self.assertIn("강제로 종료", self.controller.error)
        self.assertTrue(self.events[-1]["forced"])
        self.assertTrue((self.root / "media/alice_100_1.jpg").is_file())
        self.assertEqual((self.root / "media/resumable.part").read_bytes(), b"retained partial input")
        self.restart_and_verify()
        self.assertEqual(self.read_history("SELECT status FROM runs ORDER BY id"),
                         [("interrupted",), ("finished",)])

    @unittest.skipUnless(os.name == "nt", "Windows conversion process ownership")
    def test_forced_worker_exit_retires_its_owned_conversion_helper(self):
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        with patch.object(download_service, "_STOP_GRACE_SECONDS", .3), \
                patch.object(download_service, "_KILL_GRACE_SECONDS", .1):
            self.start_waiting("forced-conversion")
            self.wait_for(lambda: (self.root / "helper.pid").is_file())
            handle = kernel.OpenProcess(0x00100000 | 0x1, False,
                                        int((self.root / "helper.pid").read_text()))
            self.assertTrue(handle)
            try:
                self.controller.stop()
                self.wait_for(lambda: not self.controller.running)
                self.assertIn("강제로 종료", self.controller.error)
                self.assertEqual(kernel.WaitForSingleObject(handle, 5000), 0,
                                 "A conversion helper survived its worker's forced stop")
                self.assertTrue((self.root / "media/alice_100_1.jpg").is_file())
            finally:
                kernel.TerminateProcess(handle, 1)
                kernel.WaitForSingleObject(handle, 5000)
                kernel.CloseHandle(handle)

    def restart_and_verify(self):
        (self.root / "scenario.txt").write_text("resume")
        self.events.clear()
        self.assertTrue(self.controller.start(replace(self.options, scan_mode="full")))
        self.wait_for(lambda: not self.controller.running)
        self.assertEqual(self.controller.error, "")
        terminal = next(event for event in self.events if event["type"] == "finished")
        self.assertEqual((terminal["status"], terminal["saved"], terminal["skipped"]), ("finished", 1, 1))
        self.assertEqual(self.read_history("SELECT tweet_id,status FROM media ORDER BY tweet_id"),
                         [("100", "saved"), ("101", "saved")])
        self.assertTrue((self.root / "media/alice_101_1.jpg").is_file())


if __name__ == "__main__":
    unittest.main()

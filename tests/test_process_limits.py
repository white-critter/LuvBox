"""Exercise the real private job only against disposable child processes."""

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from luvbox.process_limits import MetadataJob


@unittest.skipUnless(os.name == "nt", "Windows private process jobs")
class MetadataJobTests(unittest.TestCase):
    def child(self, code):
        process = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
        def cleanup():
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)
        self.addCleanup(cleanup)
        return process

    def test_memory_limit_refuses_an_oversized_child_allocation(self):
        process = self.child("import sys; sys.stdin.buffer.read(1)\n"
                             "try: data = bytearray(128 * 1024 * 1024)\n"
                             "except MemoryError: print('limited')\n"
                             "else: print('unlimited')\n")
        job = MetadataJob(process, 64 * 1024 * 1024)
        self.addCleanup(job.close)
        stdout, stderr = process.communicate(b"x", timeout=10)
        self.assertEqual(process.returncode, 0, stderr)
        self.assertEqual(stdout.strip(), b"limited")

    def test_job_close_retires_only_its_own_waiting_child(self):
        process = self.child("import sys, time; sys.stdin.buffer.read(1); time.sleep(30)")
        job = MetadataJob(process)
        job.close()
        process.wait(timeout=5)
        self.assertIsNotNone(process.returncode)
        job.close()


@unittest.skipUnless(os.name == "nt", "Windows private process jobs")
class ConversionProcessTests(unittest.TestCase):
    def test_forced_worker_exit_retires_its_running_conversion_helper(self):
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel.TerminateProcess.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        with tempfile.TemporaryDirectory(prefix="luvbox-conversion-job-") as folder:
            marker, ready = Path(folder) / "child.pid", Path(folder) / "worker-ready"
            child_code = ("import os,time;from pathlib import Path;"
                          f"p=Path({str(marker)!r});p.with_suffix('.tmp').write_text(str(os.getpid()));"
                          "p.with_suffix('.tmp').replace(p);time.sleep(30)")
            worker_code = ("import sys,time;from pathlib import Path;from luvbox.media import _run;"
                           f"_run([sys.executable,'-c',{child_code!r}],"
                           f"check_stop=lambda:Path({str(ready)!r}).touch(),deadline=time.monotonic()+60)")
            worker = subprocess.Popen([sys.executable, "-c", worker_code], cwd=Path(__file__).resolve().parents[1],
                                      stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                      creationflags=subprocess.CREATE_NO_WINDOW)
            child_handle = None
            try:
                deadline = time.monotonic() + 5
                while (not marker.is_file() or not ready.is_file()) and worker.poll() is None:
                    if time.monotonic() >= deadline:
                        self.fail("Disposable conversion helper did not start")
                    time.sleep(.02)
                self.assertTrue(marker.is_file() and ready.is_file(), "Disposable worker exited before readiness")
                # Retain this exact process handle; never find or kill by name.
                child_handle = kernel.OpenProcess(0x00100000 | 0x1, False, int(marker.read_text()))
                self.assertTrue(child_handle)
                worker.kill()
                worker.wait(timeout=5)
                self.assertEqual(kernel.WaitForSingleObject(child_handle, 5000), 0,
                                 "Conversion helper survived its owning worker")
            finally:
                if worker.poll() is None:
                    worker.kill()
                    worker.wait(timeout=5)
                if child_handle:
                    kernel.TerminateProcess(child_handle, 1)
                    kernel.WaitForSingleObject(child_handle, 5000)
                    kernel.CloseHandle(child_handle)

"""Exercise the real private job only against disposable child processes."""

import os
import subprocess
import sys
import unittest

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

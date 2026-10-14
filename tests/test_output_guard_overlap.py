"""Concurrent jobs cannot write through ancestor/descendant output roots."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from luvbox.protection import ProtectionError
from luvbox.storage import OutputGuard


class OutputGuardOverlapTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.locks = self.root / "locks"
        isolated = patch("luvbox.protection.state_directory", return_value=self.locks)
        isolated.start()
        self.addCleanup(isolated.stop)

    def acquire(self, root, **options):
        guard = OutputGuard(root, **options)
        self.addCleanup(guard.close)
        guard.acquire()
        return guard

    def test_overlapping_roots_are_excluded_in_both_orders(self):
        parent, child = self.root / "media", self.root / "media" / "author"
        for first, second in ((parent, child), (child, parent)):
            guard = self.acquire(first)
            with self.assertRaises(ProtectionError):
                self.acquire(second)
            guard.close()

    def test_siblings_can_run_together(self):
        self.acquire(self.root / "media" / "alice")
        self.acquire(self.root / "media" / "bob")

    def test_explicit_job_group_can_lock_overlapping_former_roots(self):
        for index, reverse in enumerate((False, True)):
            parent = self.root / str(index)
            child = parent / "nested"
            first, second = (child, parent) if reverse else (parent, child)
            primary = self.acquire(first)
            self.acquire(second, group=primary)
            with self.assertRaises(ProtectionError):
                self.acquire(parent / "nested" / "other")

    def test_inactive_unknown_metadata_never_blocks_a_new_job(self):
        stale = OutputGuard(self.root / "old")
        stale.path.parent.mkdir()
        stale.path.write_bytes(b"\0unreadable stale metadata")
        self.acquire(self.root / "new")

    def test_active_unknown_metadata_fails_closed_until_os_lock_releases(self):
        active = self.acquire(self.root / "old")
        # Updating bytes after zero emulates an older program's opaque lock.
        with active.path.open("r+b") as record:
            record.seek(1)
            record.write(b"invalid metadata")
            record.truncate()
        with self.assertRaises(ProtectionError):
            self.acquire(self.root / "unrelated")
        active.close()
        self.acquire(self.root / "unrelated")

    def test_another_process_cannot_acquire_a_nested_root(self):
        self.acquire(self.root / "media")
        program = """
import sys
from pathlib import Path
from unittest.mock import patch
from luvbox.protection import ProtectionError
from luvbox.storage import OutputGuard
with patch('luvbox.protection.state_directory', return_value=Path(sys.argv[1])):
    guard = OutputGuard(Path(sys.argv[2]))
    try:
        guard.acquire()
    except ProtectionError:
        raise SystemExit(0)
    else:
        guard.close()
        raise SystemExit(9)
"""
        result = subprocess.run([sys.executable, "-c", program, str(self.locks),
                                 str(self.root / "media" / "nested")],
                                capture_output=True, timeout=10, check=False)
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", errors="replace"))

"""Transient Windows metadata locks and fixed, secret-free failure reasons."""

import ctypes
import json
import os
import stat
import struct
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_metadata import JPEG, URL, SECRET, image_payload
from luvbox import metadata


def sharing_error():
    error = PermissionError(13, SECRET)
    error.winerror = 32
    return error


class MetadataResilienceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "원본.jpg"
        self.path.write_bytes(JPEG)
        self.original = self.path.read_bytes()
        self.mtime = self.path.stat().st_mtime_ns

    def valid_tags(self):
        return [{}, {"XMP-dc:Source": URL, "XMP-dc:Description": ""}]

    def assert_original(self):
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertEqual(self.path.stat().st_mtime_ns, self.mtime)
        self.assertEqual(list(self.root.iterdir()), [self.path])

    def test_initial_tag_read_without_a_proven_sharing_lock_is_not_retried(self):
        with patch.object(metadata, "available", return_value=True), \
                patch.object(metadata, "_read_tags", side_effect=metadata._TagReadError(SECRET)) as read, \
                patch.object(metadata, "is_read_locked", return_value=False), \
                patch.object(metadata, "_copy_for_metadata") as copy, \
                patch.object(metadata.time, "sleep") as sleep:
            result = metadata.embed_source(self.path, "100")
        self.assertEqual((result["status"], result["reason"]), ("failed", "read"))
        self.assertEqual(read.call_count, 1)
        copy.assert_not_called()
        sleep.assert_not_called()
        self.assertNotIn(SECRET, str(result))
        self.assert_original()

    def test_initial_copy_permission_error_is_not_retried(self):
        error = PermissionError(13, SECRET)
        error.winerror = 5
        with patch.object(metadata, "available", return_value=True), \
                patch.object(metadata, "_read_tags", return_value={}), \
                patch.object(metadata, "_copy_for_metadata", side_effect=error) as copy, \
                patch.object(metadata, "_run") as run, patch.object(metadata.time, "sleep") as sleep:
            result = metadata.embed_source(self.path, "100")
        self.assertEqual((result["status"], result["reason"]), ("failed", "permission"))
        self.assertEqual(copy.call_count, 1)
        run.assert_not_called()
        sleep.assert_not_called()
        self.assertNotIn(SECRET, str(result))
        self.assert_original()

    def test_initial_read_retries_do_not_extend_the_overall_deadline(self):
        clock = [10.0]
        with patch.object(metadata, "available", return_value=True), \
                patch.object(metadata, "TIMEOUT_SECONDS", .15), \
                patch.object(metadata, "_read_tags", side_effect=metadata._TagReadError(SECRET)) as read, \
                patch.object(metadata, "is_read_locked", return_value=True), \
                patch.object(metadata.time, "sleep", side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)), \
                patch.object(metadata.time, "monotonic", side_effect=lambda: clock[0]):
            result = metadata.embed_source(self.path, "100")
        self.assertEqual((result["status"], result["reason"]), ("failed", "timeout"))
        self.assertLessEqual(clock[0], 10.151)
        self.assertLessEqual(read.call_count, 2)
        self.assert_original()

    def test_initial_copy_sharing_wait_is_bounded(self):
        clock = [10.0]
        with patch.object(metadata, "available", return_value=True), \
                patch.object(metadata, "_read_tags", return_value={}), \
                patch.object(metadata, "_copy_for_metadata", side_effect=sharing_error()) as copy, \
                patch.object(metadata.time, "sleep", side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)), \
                patch.object(metadata.time, "monotonic", side_effect=lambda: clock[0]):
            result = metadata.embed_source(self.path, "100")
        self.assertEqual((result["status"], result["reason"]), ("failed", "busy"))
        self.assertLessEqual(clock[0], 11.001)
        self.assertLessEqual(copy.call_count, 12)
        self.assert_original()

    def test_initial_copy_retry_rechecks_changes_and_cancellation_before_another_attempt(self):
        for outcome in ("changed", "cancelled"):
            cancelled = [False]

            def during_wait(_seconds):
                if outcome == "changed":
                    self.path.write_bytes(b"outside-change")
                else:
                    cancelled[0] = True

            with self.subTest(outcome=outcome), patch.object(metadata, "available", return_value=True), \
                    patch.object(metadata, "_read_tags", return_value={}), \
                    patch.object(metadata, "_copy_for_metadata", side_effect=sharing_error()) as copy, \
                    patch.object(metadata.time, "sleep", side_effect=during_wait):
                result = metadata.embed_source(self.path, "100", cancelled=lambda: cancelled[0])
            self.assertEqual((result["status"], result["reason"]),
                             ("failed", "changed" if outcome == "changed" else "busy"))
            self.assertEqual(copy.call_count, 1)
            self.assertEqual(list(self.root.iterdir()), [self.path])
            if outcome == "changed":
                self.assertEqual(self.path.read_bytes(), b"outside-change")
                self.path.write_bytes(self.original)
                os.utime(self.path, ns=(self.path.stat().st_atime_ns, self.mtime))
            self.assert_original()

    def test_transient_sharing_violation_retries_the_verified_copy(self):
        replace = os.replace
        calls = []

        def briefly_busy(source, target):
            calls.append((source, target))
            if len(calls) == 1:
                raise sharing_error()
            replace(source, target)

        with patch.object(metadata, "available", return_value=True), \
                patch.object(metadata, "_read_tags", side_effect=self.valid_tags()), \
                patch.object(metadata, "_run", return_value=subprocess.CompletedProcess([], 0, "", "")), \
                patch.object(metadata.os, "replace", side_effect=briefly_busy), \
                patch.object(metadata.time, "sleep"):
            result = metadata.embed_source(self.path, "100")
        self.assertEqual(result["status"], "embedded")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], calls[1])
        self.assert_original()

    def test_changed_original_during_lock_retry_is_preserved(self):
        with patch.object(metadata, "available", return_value=True), \
                patch.object(metadata, "_read_tags", side_effect=self.valid_tags()), \
                patch.object(metadata, "_run", return_value=subprocess.CompletedProcess([], 0, "", "")), \
                patch.object(metadata.os, "replace", side_effect=sharing_error()) as replace, \
                patch.object(metadata.time, "sleep", side_effect=lambda seconds: self.path.write_bytes(b"outside-change")):
            result = metadata.embed_source(self.path, "100")
        self.assertEqual((result["status"], result["reason"]), ("failed", "changed"))
        self.assertEqual(self.path.read_bytes(), b"outside-change")
        self.assertEqual(replace.call_count, 1)
        self.assertEqual(list(self.root.iterdir()), [self.path])

    def test_permanent_permission_error_does_not_retry_or_expose_error(self):
        error = PermissionError(13, SECRET)
        error.winerror = 5
        with patch.object(metadata, "available", return_value=True), \
                patch.object(metadata, "_read_tags", side_effect=self.valid_tags()), \
                patch.object(metadata, "_run", return_value=subprocess.CompletedProcess([], 0, "", "")), \
                patch.object(metadata.os, "replace", side_effect=error) as replace, \
                patch.object(metadata, "_sharing_violation", return_value=False), \
                patch.object(metadata.time, "sleep") as sleep:
            result = metadata.embed_source(self.path, "100")
        self.assertEqual((result["status"], result["reason"]), ("failed", "permission"))
        self.assertNotIn(SECRET, str(result))
        self.assertEqual(replace.call_count, 1)
        sleep.assert_not_called()
        self.assert_original()

    def test_persistent_sharing_violation_has_a_bounded_wait(self):
        clock = [10.0]

        def advance(seconds):
            clock[0] += seconds

        with patch.object(metadata, "available", return_value=True), \
                patch.object(metadata, "_read_tags", side_effect=self.valid_tags()), \
                patch.object(metadata, "_run", return_value=subprocess.CompletedProcess([], 0, "", "")), \
                patch.object(metadata.os, "replace", side_effect=sharing_error()) as replace, \
                patch.object(metadata.time, "sleep", side_effect=advance), \
                patch.object(metadata.time, "monotonic", side_effect=lambda: clock[0]):
            result = metadata.embed_source(self.path, "100")
        self.assertEqual((result["status"], result["reason"]), ("failed", "busy"))
        self.assertLessEqual(clock[0], 11.001)
        self.assertLessEqual(replace.call_count, 12)
        self.assert_original()

    def test_read_write_verification_and_timeout_reasons_are_fixed(self):
        for reason in ("read", "write", "verification", "timeout"):
            reads = [RuntimeError(SECRET)] if reason == "read" else [{}, {} if reason == "verification" else self.valid_tags()[1]]
            failure = TimeoutError(SECRET) if reason == "timeout" else subprocess.CompletedProcess([], 1 if reason == "write" else 0, "", SECRET)
            with self.subTest(reason=reason), patch.object(metadata, "available", return_value=True), \
                    patch.object(metadata, "_read_tags", side_effect=reads), \
                    patch.object(metadata, "_run", side_effect=failure if isinstance(failure, Exception) else None,
                                 return_value=failure):
                result = metadata.embed_source(self.path, "100")
            self.assertEqual((result["status"], result["reason"]), ("failed", reason))
            self.assertNotIn(SECRET, str(result))
            self.assertTrue(result["reason_message"])
            self.assert_original()

    @unittest.skipUnless(os.name == "nt", "Windows read-only file attributes required")
    def test_read_only_original_fails_before_tool_or_temporary_copy(self):
        os.chmod(self.path, stat.S_IREAD)
        try:
            with patch.object(metadata, "available", return_value=True), \
                    patch.object(metadata, "_run") as run:
                result = metadata.embed_source(self.path, "100")
            self.assertEqual((result["status"], result["reason"]), ("failed", "permission"))
            run.assert_not_called()
            self.assert_original()
        finally:
            os.chmod(self.path, stat.S_IWRITE)

    @unittest.skipUnless(metadata.available(), "Bundled ExifTool required")
    def test_minor_jpeg_exif_warnings_do_not_require_forced_repair(self):
        variants = (b"Exif\0\0bad-tiff-data", b"Exif\0\0II*\0\xff\xff\xff\x7f")
        with metadata.persistent():
            for app1 in variants:
                with self.subTest(app1=app1):
                    data = JPEG[:2] + b"\xff\xe1" + struct.pack(">H", len(app1) + 2) + app1 + JPEG[2:]
                    self.path.write_bytes(data)
                    stamp = self.path.stat().st_mtime_ns
                    result = metadata.embed_source(self.path, "100", author="writer", author_id="123")
                    self.assertEqual((result["status"], result["author_status"]), ("embedded", "embedded"))
                    self.assertEqual(metadata.read_source(self.path), URL)
                    self.assertEqual(metadata.read_author(self.path)["author"], "writer")
                    self.assertEqual(image_payload(self.path.read_bytes(), ".jpg"), image_payload(data, ".jpg"))
                    self.assertEqual(self.path.stat().st_mtime_ns, stamp)
                    self.assertEqual(list(self.root.iterdir()), [self.path])

    @unittest.skipUnless(os.name == "nt" and metadata.available(), "Bundled Windows ExifTool required")
    def test_actual_windows_initial_read_and_copy_locks_clear_before_retry(self):
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
                                      ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
        kernel.CreateFileW.restype = ctypes.c_void_p
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel.CloseHandle.restype = ctypes.c_int
        for stage in ("read", "copy"):
            handles, threads, attempts, failures = [], [], [], []
            method = "_read_tags" if stage == "read" else "_copy_for_metadata"
            operation = getattr(metadata, method)

            def initially_busy(*args, **kwargs):
                attempts.append(args[0])
                if len(attempts) == 1:
                    handle = kernel.CreateFileW(str(self.path), 0x80000000, 0, None, 3, 0, None)
                    self.assertNotEqual(handle, ctypes.c_void_p(-1).value)
                    handles.append(handle)
                try:
                    return operation(*args, **kwargs)
                except Exception:
                    if len(attempts) == 1:
                        failures.append(True)

                        def release():
                            time.sleep(.15)
                            kernel.CloseHandle(handle)
                            handles.remove(handle)

                        thread = threading.Thread(target=release)
                        threads.append(thread)
                        thread.start()
                    raise

            try:
                with self.subTest(stage=stage), metadata.persistent():
                    # The retry must keep unrelated tags as well as encoded
                    # image bytes; the writer is already ready before locking.
                    result = metadata._run(["-overwrite_original_in_place", "-P",
                                            "-XMP-dc:Title=External title", str(self.path)],
                                           time.monotonic() + metadata.TIMEOUT_SECONDS)
                    self.assertEqual(result.returncode, 0)
                    os.utime(self.path, ns=(self.path.stat().st_atime_ns, self.mtime))
                    with patch.object(metadata, method, side_effect=initially_busy):
                        result = metadata.embed_source(self.path, "100", author="writer", author_id="123")
                    self.assertEqual((result["status"], result["author_status"]), ("embedded", "embedded"), str(result))
                    self.assertEqual(len(failures), 1)
                    self.assertGreater(sum(path == self.path for path in attempts), 1)
                    self.assertEqual(metadata.read_source(self.path), URL)
                    self.assertEqual(metadata.read_author(self.path)["author"], "writer")
                    tags = metadata._run(["-j", "-G1", "-XMP-dc:Title", str(self.path)],
                                         time.monotonic() + metadata.TIMEOUT_SECONDS)
                    self.assertEqual(json.loads(tags.stdout)[0]["XMP-dc:Title"], "External title")
                    self.assertEqual(image_payload(self.path.read_bytes(), ".jpg"), image_payload(self.original, ".jpg"))
                    self.assertEqual(self.path.stat().st_mtime_ns, self.mtime)
                    self.assertEqual(list(self.root.iterdir()), [self.path])
            finally:
                for thread in threads:
                    thread.join(timeout=2)
                for handle in handles:
                    kernel.CloseHandle(handle)

    @unittest.skipUnless(os.name == "nt" and metadata.available(), "Bundled Windows ExifTool required")
    def test_actual_windows_delete_sharing_lock_can_clear_before_retry(self):
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
                                      ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
        kernel.CreateFileW.restype = ctypes.c_void_p
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel.CloseHandle.restype = ctypes.c_int
        replace = os.replace
        calls, handles, threads = [], [], []

        def briefly_busy(source, target):
            calls.append((source, target))
            if len(calls) == 1:
                handle = kernel.CreateFileW(str(target), 0, 1 | 2, None, 3, 0, None)
                self.assertNotEqual(handle, ctypes.c_void_p(-1).value)
                handles.append(handle)

                def release():
                    time.sleep(0.15)
                    kernel.CloseHandle(handle)
                    handles.remove(handle)

                thread = threading.Thread(target=release)
                threads.append(thread)
                thread.start()
            replace(source, target)

        try:
            with metadata.persistent(), patch.object(metadata.os, "replace", side_effect=briefly_busy):
                result = metadata.embed_source(self.path, "100", author="writer", author_id="123")
            self.assertEqual((result["status"], result["author_status"]), ("embedded", "embedded"), str(result))
            self.assertGreater(len(calls), 1)
            self.assertEqual(metadata.read_source(self.path), URL)
            self.assertEqual(metadata.read_author(self.path)["author"], "writer")
            self.assertEqual(image_payload(self.path.read_bytes(), ".jpg"), image_payload(self.original, ".jpg"))
            self.assertEqual(self.path.stat().st_mtime_ns, self.mtime)
            self.assertEqual(list(self.root.iterdir()), [self.path])
        finally:
            for thread in threads:
                thread.join(timeout=2)
            for handle in handles:
                kernel.CloseHandle(handle)


if __name__ == "__main__":
    unittest.main()

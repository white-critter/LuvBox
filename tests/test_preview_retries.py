"""Only a precise Windows sharing/lock failure is a transient preview error."""

from contextlib import contextmanager
import ctypes
import gc
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import warnings
from unittest.mock import Mock, patch

from PIL import Image

from luvbox import file_access, preview
from luvbox.file_access import is_read_locked


class PreviewRetryClassificationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="luvbox-preview-retry-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.photo = self.root / "local.png"
        with Image.new("RGB", (40, 20), "red") as image:
            image.save(self.photo)

    def test_existing_thumbnail_callers_default_to_no_retry(self):
        self.assertFalse(preview.Thumbnail(None, "normal failure").retryable)
        result = preview.thumbnail(self.photo)
        try:
            self.assertIsNotNone(result.image)
            self.assertFalse(result.retryable)
        finally:
            if result.image is not None:
                result.image.close()

    def test_only_windows_sharing_and_lock_codes_request_retry(self):
        for code in (2, 5, 32, 33):
            with self.subTest(winerror=code):
                error = OSError("private system details")
                error.winerror = code
                with patch.object(preview.Image, "open", side_effect=error):
                    result = preview.thumbnail(self.photo)
                self.assertIsNone(result.image)
                self.assertEqual(result.retryable, code in {32, 33})
                self.assertNotIn("private", result.note)

    def test_cloud_missing_unsupported_and_oversized_sources_do_not_retry(self):
        with patch.object(preview, "cloud_only", return_value=True):
            result = preview.thumbnail(self.photo)
            self.assertIsNone(result.image)
            self.assertFalse(result.retryable)
        self.assertFalse(preview.thumbnail(self.root / "missing.png").retryable)
        unsupported = self.root / "unsupported.txt"
        unsupported.write_bytes(b"unsupported local file")
        self.assertFalse(preview.thumbnail(unsupported).retryable)
        with patch.object(preview, "MAX_PIXELS", 1):
            result = preview.thumbnail(self.photo)
            self.assertIsNone(result.image)
            self.assertFalse(result.retryable)

    def test_corrupt_photo_and_video_failure_or_timeout_do_not_retry(self):
        corrupt = self.root / "corrupt.png"
        corrupt.write_bytes(b"not an image")
        self.assertFalse(preview.thumbnail(corrupt).retryable)
        video = self.root / "corrupt.mp4"
        video.write_bytes(b"not a video")
        for outcome in (subprocess.CompletedProcess([], 1, b"", b"private error"),
                        subprocess.TimeoutExpired("synthetic tool", 8)):
            with self.subTest(outcome=type(outcome).__name__):
                options = {"side_effect": outcome} if isinstance(outcome, Exception) else {"return_value": outcome}
                with patch("luvbox.media.ffmpeg_path", return_value=Path("synthetic-ffmpeg.exe")), \
                        patch.object(preview.subprocess, "run", **options):
                    result = preview.thumbnail(video)
                self.assertIsNone(result.image)
                self.assertFalse(result.retryable)


@unittest.skipUnless(os.name == "nt", "Windows sharing modes")
class NativePreviewLockTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="luvbox-preview-native-lock-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    @contextmanager
    def exclusive_reader(self, path):
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                      ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.CreateFileW(str(path), 0x80000000, 0, None, 3, 0x80, None)
        self.assertNotIn(handle, (None, wintypes.HANDLE(-1).value))
        try:
            yield
        finally:
            self.assertTrue(kernel.CloseHandle(handle))

    def test_locked_png_is_retryable_and_decodes_after_release(self):
        photo = self.root / "locked.png"
        with Image.new("RGB", (40, 20), "red") as image:
            image.save(photo)
        before = photo.read_bytes()
        with self.exclusive_reader(photo):
            self.assertTrue(is_read_locked(photo))
            result = preview.thumbnail(photo)
            self.assertIsNone(result.image)
            self.assertTrue(result.retryable)
        result = preview.thumbnail(photo)
        try:
            self.assertIsNotNone(result.image)
            self.assertFalse(result.retryable)
        finally:
            if result.image is not None:
                result.image.close()
        self.assertEqual(photo.read_bytes(), before)
        self.assertFalse(is_read_locked(photo))

    def test_byte_range_lock_is_retryable_until_released(self):
        from ctypes import wintypes

        class Overlapped(ctypes.Structure):
            _fields_ = (("internal", ctypes.c_size_t), ("internal_high", ctypes.c_size_t),
                        ("offset", wintypes.DWORD), ("offset_high", wintypes.DWORD), ("event", wintypes.HANDLE))

        photo = self.root / "byte-locked.png"
        with Image.new("RGB", (40, 20), "red") as image:
            image.save(photo)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                      ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.LockFileEx.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                                      wintypes.DWORD, ctypes.POINTER(Overlapped))
        kernel.LockFileEx.restype = wintypes.BOOL
        kernel.UnlockFileEx.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                        wintypes.DWORD, ctypes.POINTER(Overlapped))
        kernel.UnlockFileEx.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel.CreateFileW(str(photo), 0xC0000000, 7, None, 3, 0x80, None)
        self.assertNotIn(handle, (None, wintypes.HANDLE(-1).value))
        overlapped = Overlapped()
        locked = False
        try:
            self.assertTrue(kernel.LockFileEx(handle, 3, 0, 1, 0, ctypes.byref(overlapped)))
            locked = True
            self.assertTrue(is_read_locked(photo))
            with warnings.catch_warnings(record=True) as notices:
                warnings.simplefilter("always", ResourceWarning)
                result = preview.thumbnail(photo)
                gc.collect()
            self.assertEqual([item for item in notices if issubclass(item.category, ResourceWarning)], [])
            self.assertIsNone(result.image)
            self.assertTrue(result.retryable)
        finally:
            if locked:
                self.assertTrue(kernel.UnlockFileEx(handle, 0, 1, 0, ctypes.byref(overlapped)))
            kernel.CloseHandle(handle)
        self.assertFalse(is_read_locked(photo))

    def test_missing_cloud_only_and_unsafe_paths_do_not_open_a_handle(self):
        photo = self.root / "ordinary.png"
        photo.write_bytes(b"ordinary local bytes")
        with patch.object(file_access.ctypes, "WinDLL") as native:
            self.assertFalse(is_read_locked(self.root / "missing.png"))
            with patch.object(file_access, "cloud_only", return_value=True):
                self.assertFalse(is_read_locked(photo))
            with patch.object(file_access, "file_signature", side_effect=OSError("redirected parent")):
                self.assertFalse(is_read_locked(photo))
            native.assert_not_called()

    def test_redirected_or_cloud_only_open_handle_is_not_read(self):
        from ctypes import wintypes

        photo = self.root / "ordinary.png"
        photo.write_bytes(b"ordinary local bytes")
        for attributes, tag in ((0x40000, 0), (0x400, 0xA000000C)):
            with self.subTest(attributes=attributes, tag=tag):
                kernel = Mock()
                kernel.CreateFileW.return_value = 123
                def info(_handle, _kind, result, _size):
                    fields = ctypes.cast(result, ctypes.POINTER(wintypes.DWORD))
                    fields[0], fields[1] = attributes, tag
                    return 1
                kernel.GetFileInformationByHandleEx.side_effect = info
                with patch.object(file_access.ctypes, "WinDLL", return_value=kernel):
                    self.assertFalse(is_read_locked(photo))
                kernel.ReadFile.assert_not_called()
                kernel.CloseHandle.assert_called_once_with(123)

    def test_locked_mp4_does_not_launch_decoder_and_decodes_after_release(self):
        from luvbox.media import ffmpeg_path

        video = self.root / "locked.mp4"
        subprocess.run([str(ffmpeg_path()), "-nostdin", "-v", "error", "-f", "lavfi", "-i",
                        "testsrc2=size=64x48:rate=8", "-t", "0.5", "-an", "-c:v", "mpeg4", str(video)],
                       check=True, capture_output=True, timeout=20,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        before = video.read_bytes()
        with self.exclusive_reader(video), patch.object(preview.subprocess, "run") as decoder:
            self.assertTrue(is_read_locked(video))
            result = preview.thumbnail(video)
            self.assertIsNone(result.image)
            self.assertTrue(result.retryable)
            decoder.assert_not_called()
        result = preview.thumbnail(video)
        try:
            self.assertIsNotNone(result.image)
            self.assertFalse(result.retryable)
        finally:
            if result.image is not None:
                result.image.close()
        self.assertEqual(video.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()

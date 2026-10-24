"""Offline regressions for media identity, legacy authors and Windows streams."""

import os
from pathlib import Path
import queue
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PIL import Image

from luvbox import metadata
from luvbox.gallery import _GridDecoder
from luvbox.preview import PreviewLoader


class PreviewFileIdentityTests(unittest.TestCase):
    def test_replaced_equal_size_and_mtime_file_is_decoded_again(self):
        for kind in ("selection", "gallery"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory(prefix="luvbox-preview-identity-") as root:
                path, replacement = Path(root) / "image.png", Path(root) / "replacement.png"
                Image.new("RGB", (16, 16), "red").save(path)
                Image.new("RGB", (16, 16), "blue").save(replacement)
                # PNG permits trailing padding; keep the stat values used by
                # the old cache key equal while replacing the actual file.
                size = max(path.stat().st_size, replacement.stat().st_size)
                for candidate in (path, replacement):
                    candidate.write_bytes(candidate.read_bytes() + b"\0" * (size - candidate.stat().st_size))
                original = path.stat()
                os.utime(replacement, ns=(original.st_atime_ns, original.st_mtime_ns))
                results = queue.Queue(maxsize=240)
                loader = PreviewLoader(results) if kind == "selection" else _GridDecoder(results)

                def image(token):
                    if kind == "selection":
                        loader.request(token, path, lambda _: True)
                        return results.get(timeout=5)[1].image
                    loader.request(token, [("row", str(path))], set)
                    return results.get(timeout=5)[2]

                try:
                    self.assertEqual(image(1).getpixel((0, 0))[:3], (255, 0, 0))
                    os.replace(replacement, path)
                    self.assertNotEqual(path.stat().st_ino, original.st_ino)
                    self.assertEqual((path.stat().st_size, path.stat().st_mtime_ns),
                                     (original.st_size, original.st_mtime_ns))
                    self.assertEqual(image(2).getpixel((0, 0))[:3], (0, 0, 255))
                finally:
                    loader.close()
                    loader.thread.join(timeout=5)
                    self.assertFalse(loader.thread.is_alive())


class LegacyAuthorIdentityTests(unittest.TestCase):
    def test_old_and_mixed_markers_read_but_conflicting_authors_remain_unknown(self):
        tags = {"XMP-dc:Creator": ["@old_author"],
                "XMP-dc:Relation": ["https://x.com/old_author", "https://x.com/i/user/123"],
                "XMP-xmp:Identifier": ["xlikesaver:x-author:old_author", "xlikesaver:x-user-id:123"]}
        expected = metadata.author_identity("old_author", "123")
        self.assertEqual(metadata._author_from_tags(tags), expected)
        tags["XMP-xmp:Identifier"] += ["luvbox:x-author:old_author", "luvbox:x-user-id:123"]
        self.assertEqual(metadata._author_from_tags(tags), expected)
        tags["XMP-xmp:Identifier"].append("luvbox:x-author:other_author")
        self.assertEqual(metadata._author_from_tags(tags), metadata.author_identity("", ""))

    @unittest.skipUnless(metadata.available(), "bundled ExifTool required")
    def test_update_migrates_old_owned_values_and_keeps_unrelated_metadata(self):
        with tempfile.TemporaryDirectory(prefix="luvbox-legacy-author-") as root:
            path = Path(root) / "legacy.jpg"
            Image.new("RGB", (2, 2), "red").save(path)
            description = Path(root) / "description.txt"
            description.write_text("Original caption\nX author: @old_author (https://x.com/old_author)\n"
                                   "X user ID: 123 (https://x.com/i/user/123)", encoding="utf-8")
            arguments = ["-overwrite_original", "-XMP-dc:Creator=Studio Photographer",
                         "-XMP-dc:Creator+=@old_author", "-XMP-dc:Relation=https://x.com/old_author",
                         "-XMP-dc:Relation+=https://x.com/i/user/123",
                         f"-XMP-dc:Description-x-default<={description}",
                         "-XMP-xmp:Identifier=xlikesaver:x-author:old_author",
                         "-XMP-xmp:Identifier+=xlikesaver:x-user-id:123",
                         "-XMP-xmp:Identifier+=xlikesaver:owned-author:v1:creator:old_author",
                         "-XMP-xmp:Identifier+=xlikesaver:owned-author:v1:profile:old_author",
                         "-XMP-xmp:Identifier+=xlikesaver:owned-author:v1:user-url:123",
                         "-XMP-xmp:Identifier+=xlikesaver:owned-author:v1:description-handle:old_author",
                         "-XMP-xmp:Identifier+=xlikesaver:owned-author:v1:description-id:123",
                         "-XMP-xmp:Identifier+=luvbox:x-author:old_author",
                         "-XMP-xmp:Identifier+=unrelated-private-identifier", str(path)]
            self.assertEqual(metadata._run(arguments, time.monotonic() + 20).returncode, 0)
            self.assertEqual(metadata.read_author(path), metadata.author_identity("old_author", "123"))
            result = metadata.embed_source(path, "100", "new_author", "456")
            self.assertEqual(result["status"], "embedded")
            self.assertEqual(metadata.read_author(path), metadata.author_identity("new_author", "456"))
            tags = metadata._read_tags(path, time.monotonic() + 20)
            self.assertEqual(set(metadata._values(tags["XMP-dc:Creator"])), {"Studio Photographer", "@new_author"})
            self.assertEqual(set(metadata._values(tags["XMP-dc:Relation"])),
                             {"https://x.com/new_author", "https://x.com/i/user/456"})
            self.assertEqual(tags["XMP-dc:Description"], description.read_bytes().decode("utf-8"))
            self.assertNotIn("new_author", tags["XMP-dc:Description"])
            self.assertNotIn("X user ID: 456", tags["XMP-dc:Description"])
            identifiers = metadata._values(tags["XMP-xmp:Identifier"])
            self.assertIn("unrelated-private-identifier", identifiers)
            self.assertIn("luvbox:x-author:new_author", identifiers)
            self.assertFalse(any(value.startswith("xlikesaver:") for value in identifiers))


@unittest.skipUnless(os.name == "nt" and metadata.available(), "Windows and bundled ExifTool required")
class WindowsStreamPreservationTests(unittest.TestCase):
    def test_source_write_preserves_named_streams_and_creation_time(self):
        with tempfile.TemporaryDirectory(prefix="luvbox-stream-preservation-") as root:
            path = Path(root) / "media.jpg"
            Image.new("RGB", (2, 2), "red").save(path)
            streams = {":Zone.Identifier": b"[ZoneTransfer]\r\nZoneId=3\r\n",
                       ":private-fixture": b"auxiliary user data\0\xff"}
            for suffix, content in streams.items():
                Path(str(path) + suffix).write_bytes(content)
            before = path.stat()
            result = metadata.embed_source(path, "100", "author", "123")
            self.assertEqual(result["status"], "embedded")
            for suffix, content in streams.items():
                self.assertEqual(Path(str(path) + suffix).read_bytes(), content)
            after = path.stat()
            self.assertEqual(getattr(after, "st_birthtime_ns", after.st_ctime_ns),
                             getattr(before, "st_birthtime_ns", before.st_ctime_ns))
            self.assertEqual(after.st_mtime_ns, before.st_mtime_ns)
            self.assertEqual(list(Path(root).iterdir()), [path])

    def test_creation_time_supports_the_python_310_311_stat_fields(self):
        with tempfile.TemporaryDirectory(prefix="luvbox-old-python-stat-") as root:
            path = Path(root) / "media.jpg"
            path.write_bytes(b"private fixture")
            original = SimpleNamespace(st_ctime_ns=946684800123456700)
            metadata._preserve_creation_time(path, original)
            status = path.stat()
            self.assertEqual(getattr(status, "st_birthtime_ns", status.st_ctime_ns), original.st_ctime_ns)

    def test_source_write_keeps_custom_file_access_permissions(self):
        import ctypes
        from ctypes import wintypes
        security = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        pointer = ctypes.c_void_p
        convert = security.ConvertStringSecurityDescriptorToSecurityDescriptorW
        convert.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(pointer), ctypes.POINTER(wintypes.DWORD))
        convert.restype = wintypes.BOOL
        set_security = security.SetFileSecurityW
        set_security.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, pointer)
        set_security.restype = wintypes.BOOL
        get_security = security.GetFileSecurityW
        get_security.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, pointer, wintypes.DWORD,
                                ctypes.POINTER(wintypes.DWORD))
        get_security.restype = wintypes.BOOL
        free = kernel.LocalFree
        free.argtypes = (pointer,)
        free.restype = pointer

        def access_descriptor(path):
            required = wintypes.DWORD()
            get_security(str(path), 4, None, 0, ctypes.byref(required))
            buffer = ctypes.create_string_buffer(required.value)
            self.assertTrue(get_security(str(path), 4, buffer, len(buffer), ctypes.byref(required)))
            # Windows may set the auto-inherited bookkeeping bit when it
            # copies a DACL. ACEs and inheritance protection must stay equal.
            data = bytearray(buffer.raw)
            data[3] &= ~4  # SE_DACL_AUTO_INHERITED (0x0400)
            return bytes(data)

        with tempfile.TemporaryDirectory(prefix="luvbox-permissions-") as root:
            path = Path(root) / "media.jpg"
            Image.new("RGB", (2, 2), "red").save(path)
            descriptor = pointer()
            self.assertTrue(convert("D:P(A;;FA;;;WD)", 1, ctypes.byref(descriptor), None))
            try:
                self.assertTrue(set_security(str(path), 0x80000004, descriptor))
            finally:
                free(descriptor)
            before = access_descriptor(path)
            result = metadata.embed_source(path, "100")
            self.assertEqual(result["status"], "embedded")
            self.assertEqual(access_descriptor(path), before)

    def test_permission_copy_failure_preserves_original_and_named_stream(self):
        with tempfile.TemporaryDirectory(prefix="luvbox-permission-failure-") as root:
            path = Path(root) / "media.jpg"
            Image.new("RGB", (2, 2), "red").save(path)
            stream = Path(str(path) + ":private-fixture")
            stream.write_bytes(b"auxiliary user data")
            before = path.read_bytes(), path.stat().st_mtime_ns, stream.read_bytes()
            with patch.object(metadata, "_preserve_permissions", side_effect=PermissionError("private error")):
                result = metadata.embed_source(path, "100")
            self.assertEqual((result["status"], result["reason"]), ("failed", "permission"))
            self.assertNotIn("private error", str(result))
            self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns, stream.read_bytes()), before)
            self.assertEqual(list(Path(root).iterdir()), [path])


if __name__ == "__main__":
    unittest.main()

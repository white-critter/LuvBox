"""Offline transactional metadata tests and actual bundled ExifTool round trips."""

import base64
import importlib.util
import json
import os
import struct
import subprocess
import tempfile
import threading
import time
import unittest
import zipfile
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

from xlikesaver import metadata


URL = "https://x.com/i/web/status/100"
SECRET = "never-log-raw-tool-error-cookie-token"
# Generated 2x2 fixtures; no imaging library is required at test/runtime.
JPEG = base64.b64decode("/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJCQwLDBgNDRgyIRwhMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjL/wAARCAACAAIDASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwDk6KKK+mPAP//Z")
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAEklEQVR4nGNUjl/AwMDAxAAGAA1FASYPxkisAAAAAElFTkSuQmCC")
WEBP = base64.b64decode("UklGRjgAAABXRUJQVlA4ICwAAADQAQCdASoCAAIAAUAmJaACdLoB+AADsAD+8NcD/zNExzH7V//kFywuuIwAAA==")


def image_payload(data, suffix):
    """Compare encoded image bytes, a stronger check than decoded pixel equality."""
    if suffix == ".jpg":
        return data[data.index(b"\xff\xda"):]
    if suffix == ".png":
        result, offset = [], 8
        while offset < len(data):
            size = struct.unpack(">I", data[offset:offset + 4])[0]
            kind = data[offset + 4:offset + 8]
            if kind in {b"IHDR", b"IDAT"}:
                result.append((kind, data[offset + 8:offset + 8 + size]))
            offset += size + 12
        return result
    result, offset = [], 12
    while offset < len(data):
        kind, size = data[offset:offset + 4], struct.unpack("<I", data[offset + 4:offset + 8])[0]
        if kind in {b"VP8 ", b"VP8L", b"ALPH"}:
            result.append((kind, data[offset + 8:offset + 8 + size]))
        offset += size + 8 + size % 2
    return result


def atoms(data):
    result, offset = [], 0
    while offset + 8 <= len(data):
        size, kind = struct.unpack(">I4s", data[offset:offset + 8])
        header = 8
        if size == 1:
            size, header = struct.unpack(">Q", data[offset + 8:offset + 16])[0], 16
        if size == 0:
            size = len(data) - offset
        if size < header:
            raise AssertionError("invalid atom")
        result.append((kind, data[offset + header:offset + size]))
        offset += size
    return result


class MetadataTransactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "한글😀 원본.jpg"
        self.path.write_bytes(JPEG)
        os.utime(self.path, ns=(946684800000000000, 946684800123456700))
        self.original = self.path.read_bytes()
        self.mtime = self.path.stat().st_mtime_ns

    def assert_original(self):
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertEqual(self.path.stat().st_mtime_ns, self.mtime)
        self.assertEqual(list(self.root.iterdir()), [self.path])

    def test_missing_helper_fails_without_modifying_original(self):
        with patch.object(metadata, "available", return_value=False), patch.object(metadata, "_run") as run:
            result = metadata.embed_source(self.path, "100")
        self.assertEqual(result["status"], "failed")
        run.assert_not_called()
        self.assert_original()

    def test_invalid_id_cannot_inject_metadata_or_process_arguments(self):
        for ident in ("100\n-token=" + SECRET, "100;run", "１２３", "", "1" * 21, 100):
            with self.subTest(ident=ident), patch.object(metadata, "_run") as run:
                result = metadata.embed_source(self.path, ident)
                self.assertEqual((result["status"], result["url"]), ("failed", ""))
                self.assertNotIn(SECRET, str(result))
                run.assert_not_called()
        self.assert_original()

    def test_invalid_author_is_dropped_and_never_reaches_tool_arguments(self):
        calls = []

        def record(arguments, deadline):
            if "-j" in arguments:
                return subprocess.CompletedProcess(arguments, 0, "[{}]", "")
            calls.append(arguments)
            raise ValueError("offline stop after capturing the write arguments")

        for author in ("@", "@@name", "name-with-dash", "a" * 16, "사용자", "ｎａｍｅ",
                       "name\n-token=" + SECRET, "name\0extra", "name/other", None, 123):
            with self.subTest(author=author), patch.object(metadata, "available", return_value=True), \
                    patch.object(metadata, "_run", side_effect=record):
                calls.clear()
                result = metadata.embed_source(self.path, "100", author=author)
                # Treated as unknown: the source is still attempted.
                self.assertEqual((result["author"], result["author_id"], result["author_status"]), ("", "", "unknown"))
                self.assertTrue(calls)
                self.assertNotIn(SECRET, str(result) + str(calls))
                self.assertFalse(any("x-author" in argument or argument.startswith("-XMP-dc:Creator")
                                     for call in calls for argument in call))
        for author_id in ("１２３", "1" * 21, "1\n-token=" + SECRET, "123/other", None):
            with self.subTest(author_id=author_id), patch.object(metadata, "available", return_value=True), \
                    patch.object(metadata, "_run", side_effect=record):
                calls.clear()
                result = metadata.embed_source(self.path, "100", author="writer", author_id=author_id)
                self.assertEqual((result["author"], result["author_id"]), ("writer", ""))
                self.assertNotIn(SECRET, str(result) + str(calls))
        self.assertEqual(metadata.author_identity("writer", 123)["author_id"], "123")
        self.assert_original()

    def test_author_reader_requires_explicit_validated_markers(self):
        empty = {"author": "", "author_id": "", "profile_url": ""}
        for tags in (
            {"XMP-dc:Creator": "@writer", "XMP-dc:Relation": "https://x.com/writer"},
            {"XMP-xmp:Identifier": "xlikesaver:x-author:writer"},
            {"XMP-xmp:Identifier": ["xlikesaver:x-author:writer", "xlikesaver:x-author:other"],
             "XMP-dc:Creator": ["@writer", "@other"],
             "XMP-dc:Relation": ["https://x.com/writer", "https://x.com/other"]},
            {"XMP-xmp:Identifier": "xlikesaver:x-author:writer\n" + SECRET},
            {"XMP-xmp:Identifier": "xlikesaver:x-user-id:123"},
        ):
            with self.subTest(tags=tags), patch.object(metadata, "available", return_value=True), \
                    patch.object(metadata, "_read_tags", return_value=tags):
                self.assertEqual(metadata.read_author(self.path), empty)
        with patch.object(metadata, "available", return_value=True), \
                patch.object(metadata, "_read_tags", side_effect=ValueError(SECRET)):
            self.assertEqual(metadata.read_author(self.path), empty)

    def test_author_verification_failure_preserves_original_even_with_valid_source(self):
        valid = {"XMP-dc:Source": URL,
                 "XMP-dc:Description": URL + "\nX author: @writer (https://x.com/writer)",
                 "XMP-dc:Creator": ["Original creator", "@writer"],
                 "XMP-dc:Relation": ["https://original.example/", "https://x.com/writer"],
                 "XMP-xmp:Identifier": ["original-id", "xlikesaver:x-author:writer"]}
        for tag in ("XMP-dc:Creator", "XMP-dc:Relation", "XMP-xmp:Identifier"):
            incomplete = {**valid, tag: []}
            def fake(arguments, deadline):
                Path(arguments[-1]).write_bytes(b"changed-only-temp")
                return subprocess.CompletedProcess([], 0, "", "")
            with self.subTest(tag=tag), patch.object(metadata, "available", return_value=True), \
                    patch.object(metadata, "_read_tags", side_effect=[{}, incomplete]), \
                    patch.object(metadata, "_run", side_effect=fake):
                result = metadata.embed_source(self.path, "100", "writer")
                self.assertEqual((result["status"], result["author_status"]), ("failed", "error"))
                self.assert_original()

    def test_missing_author_remains_unknown_with_source_only_metadata(self):
        with patch.object(metadata, "available", return_value=True), \
                patch.object(metadata, "_read_tags", side_effect=[{}, {"XMP-dc:Source": URL, "XMP-dc:Description": URL}]), \
                patch.object(metadata, "_run", return_value=subprocess.CompletedProcess([], 0, "", "")):
            result = metadata.embed_source(self.path, "100")
        self.assertEqual((result["status"], result["author_status"], result["author"], result["author_id"]),
                         ("embedded", "unknown", "", ""))

    def test_unsupported_gif_has_explicit_status_without_tool_call(self):
        gif = self.root / "100_1.gif"
        gif.write_bytes(b"GIF89a")
        with patch.object(metadata, "_run") as run:
            result = metadata.embed_source(gif, "100")
        self.assertEqual((result["status"], result["url"]), ("unsupported", URL))
        run.assert_not_called()
        self.assertEqual(gif.read_bytes(), b"GIF89a")

    def test_external_failure_timeout_and_verification_failure_preserve_original(self):
        for failure in ("write", "timeout", "read", "mismatch"):
            def fake(arguments, deadline):
                if "-overwrite_original" in arguments:
                    Path(arguments[-1]).write_bytes(b"changed-only-temp")
                    if failure == "timeout":
                        raise subprocess.TimeoutExpired(SECRET, 20, output=SECRET)
                    return subprocess.CompletedProcess([], 1 if failure == "write" else 0, "", SECRET)
                if arguments[-1] == str(self.path):
                    return subprocess.CompletedProcess([], 0, json.dumps([{}]), "")
                return subprocess.CompletedProcess([], 1 if failure == "read" else 0,
                                                   json.dumps([{"XMP-dc:Source": "wrong"}]), SECRET)
            with self.subTest(failure=failure), patch.object(metadata, "available", return_value=True), patch.object(metadata, "_run", side_effect=fake):
                result = metadata.embed_source(self.path, "100")
                self.assertEqual(result["status"], "failed")
                self.assertNotIn(SECRET, str(result))
                self.assert_original()

    def test_file_changed_by_other_process_is_not_replaced(self):
        reads = 0
        def fake(arguments, deadline):
            nonlocal reads
            if "-overwrite_original" in arguments:
                self.path.write_bytes(b"concurrent-local-change")
                return subprocess.CompletedProcess([], 0, "", "")
            reads += 1
            return subprocess.CompletedProcess([], 0, json.dumps([{} if reads == 1 else {"XMP-dc:Source": URL, "XMP-dc:Description": URL}]), "")
        with patch.object(metadata, "available", return_value=True), patch.object(metadata, "_run", side_effect=fake):
            result = metadata.embed_source(self.path, "100")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.path.read_bytes(), b"concurrent-local-change")
        self.assertEqual(list(self.root.iterdir()), [self.path])

    def test_replace_failure_removes_temporaries_and_preserves_original(self):
        with patch.object(metadata, "available", return_value=True), patch.object(metadata, "_read_tags", side_effect=[{}, {"XMP-dc:Source": URL, "XMP-dc:Description": URL}]), patch.object(metadata, "_run", return_value=subprocess.CompletedProcess([], 0, "", "")), patch.object(metadata.os, "replace", side_effect=OSError(SECRET)):
            result = metadata.embed_source(self.path, "100")
        self.assertEqual(result["status"], "failed")
        self.assertNotIn(SECRET, str(result))
        self.assert_original()

    def test_process_uses_hidden_shell_free_utf8_stdin_and_shared_deadline(self):
        with patch.object(metadata.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            metadata._run(["-j", str(self.path)], time.monotonic() + 10)
        arguments, kwargs = run.call_args
        self.assertEqual(arguments[0][-4:], ["-charset", "filename=UTF8", "-@", "-"])
        self.assertFalse(kwargs["shell"])
        self.assertEqual(kwargs["encoding"], "utf-8")
        self.assertIn(str(self.path), kwargs["input"])
        self.assertGreater(kwargs["timeout"], 0)
        self.assertLessEqual(kwargs["timeout"], 10)
        self.assertEqual(kwargs["creationflags"], getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def test_reader_returns_only_canonical_source_and_hides_failures(self):
        with patch.object(metadata, "available", return_value=True), patch.object(metadata, "_read_tags", return_value={"XMP-dc:Source": "https://invalid.example/?token=" + SECRET}):
            self.assertEqual(metadata.read_source(self.path), "")
        with patch.object(metadata, "available", return_value=True), patch.object(metadata, "_read_tags", side_effect=ValueError(SECRET)):
            self.assertEqual(metadata.read_source(self.path), "")

    def test_frozen_bundle_is_resolved_next_to_application(self):
        with patch.object(metadata.sys, "frozen", True, create=True), patch.object(metadata.sys, "executable", str(self.root / "XlikeSaver.exe")):
            self.assertEqual(metadata.exiftool_path(), self.root / "tools" / "exiftool" / "runtime" / "exiftool.exe")

    def test_description_and_comment_do_not_confuse_longer_url_with_source(self):
        for old_url in (URL + "0", URL + "?ref=other", URL + "/extra", URL + "#fragment", URL):
            old_text = "Existing source: " + old_url
            expected = old_text if old_url == URL else old_text + "\nSource: " + URL
            captured = {}
            def fake(arguments, deadline):
                for argument in arguments:
                    if "<=" in argument:
                        tag, value_file = argument.split("<=", 1)
                        captured[tag] = Path(value_file).read_text(encoding="utf-8")
                return subprocess.CompletedProcess([], 0, "", "")
            path = self.root / "100_video_1.mp4"
            path.write_bytes(b"offline-container")
            with self.subTest(old_url=old_url), patch.object(metadata, "available", return_value=True), patch.object(metadata, "_run", side_effect=fake), patch.object(metadata, "_read_tags", side_effect=[{"XMP-dc:Description": old_text, "ItemList:Comment": old_text}, {"XMP-dc:Source": URL, "XMP-dc:Description": expected, "ItemList:Comment": expected}]):
                self.assertEqual(metadata.embed_source(path, "100")["status"], "embedded")
            self.assertEqual(captured["-XMP-dc:Description-x-default"], expected)
            self.assertEqual(captured["-ItemList:Comment"], expected)
            self.assertEqual(path.read_bytes(), b"offline-container")


@unittest.skipUnless(os.name == "nt" and metadata.available(), "Prepare the bundled Windows ExifTool with setup.ps1.")
class ActualExifToolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "한글😀 저장 폴더"
        self.root.mkdir()

    def media_payload(self, suffix):
        if suffix != ".mp4":
            return {".jpg": JPEG, ".png": PNG, ".webp": WEBP}[suffix]
        fixture = metadata.exiftool_path().parent / "exiftool_files" / "t" / "images" / "QuickTime.mov"
        movie = [(kind, data) for kind, data in atoms(fixture.read_bytes()) if kind != b"mdat"]
        movie.insert(0, (b"ftyp", b"isom\x00\x00\x02\x00isomiso2mp41"))
        movie.append((b"mdat", b"\x00\x00\x00\x04\x65\x88\x84\x00" * 32))
        return b"".join(struct.pack(">I4s", len(data) + 8, kind) + data for kind, data in movie)

    def test_author_roundtrips_all_formats_and_modes_preserve_prior_metadata_and_payload(self):
        expected_author = {"author": "Example_User", "author_id": "1234567890",
                           "profile_url": "https://x.com/Example_User"}
        for resident in (False, True):
            with metadata.persistent() if resident else nullcontext():
                for suffix in (".jpg", ".png", ".webp", ".mp4"):
                    with self.subTest(resident=resident, suffix=suffix):
                        path = self.root / f"100_author_{resident}{suffix}"
                        path.write_bytes(self.media_payload(suffix))
                        initial = ["-overwrite_original", "-XMP-dc:Creator=원본 작가", "-XMP-dc:Creator+=@photographer",
                                   "-XMP-dc:Relation=https://original.example/source", "-XMP-xmp:Identifier=original:123",
                                   "-XMP-dc:Identifier=original image id", "-XMP-dc:Description=원본 설명",
                                   "-XMP-dc:Description-fr=Texte original", "-XMP-dc:Description-ja=元の説明"]
                        if suffix == ".mp4":
                            initial.extend(["-ItemList:Comment=Original comment", "-UserData:Comment=Separate comment"])
                        self.assertEqual(metadata._run(initial + [str(path)], time.monotonic() + 20).returncode, 0)
                        # Existing downloaded files may already have their source URL.
                        self.assertEqual(metadata.embed_source(path, "100")["status"], "embedded")
                        os.utime(path, ns=(946684800000000000, 946684800123456700))
                        mtime = path.stat().st_mtime_ns
                        payload = ([data for kind, data in atoms(path.read_bytes()) if kind == b"mdat"]
                                   if suffix == ".mp4" else image_payload(path.read_bytes(), suffix))
                        result = metadata.embed_source(path, "100", author="@Example_User", author_id="1234567890")
                        self.assertEqual((result["status"], result["author_status"]), ("embedded", "embedded"))
                        self.assertEqual(metadata.read_author(path), expected_author)
                        first = metadata._read_tags(path, time.monotonic() + 20)
                        self.assertEqual(metadata._values(first["XMP-dc:Creator"]), ["원본 작가", "@photographer", "@Example_User"])
                        self.assertEqual(set(metadata._values(first["XMP-dc:Relation"])),
                                         {"https://original.example/source", "https://x.com/Example_User", "https://x.com/i/user/1234567890"})
                        self.assertEqual(set(metadata._values(first["XMP-xmp:Identifier"])),
                                         {"original:123", "xlikesaver:x-author:Example_User", "xlikesaver:x-user-id:1234567890"})
                        self.assertIn("원본 설명\nSource: " + URL, first["XMP-dc:Description"])
                        self.assertIn("X author: @Example_User (https://x.com/Example_User)", first["XMP-dc:Description"])
                        self.assertEqual(metadata.embed_source(path, "100", "Example_User", "1234567890")["author_status"], "embedded")
                        second = metadata._read_tags(path, time.monotonic() + 20)
                        self.assertEqual(first, second)
                        preserved = metadata._run(["-j", "-XMP-dc:Description-fr", "-XMP-dc:Description-ja", "-XMP-dc:Identifier", str(path)], time.monotonic() + 20)
                        tags = json.loads(preserved.stdout)[0]
                        self.assertEqual((tags["Description-fr"], tags["Description-ja"], tags["Identifier"]),
                                         ("Texte original", "元の説明", "original image id"))
                        current_payload = ([data for kind, data in atoms(path.read_bytes()) if kind == b"mdat"]
                                           if suffix == ".mp4" else image_payload(path.read_bytes(), suffix))
                        self.assertEqual(current_payload, payload)
                        self.assertEqual(path.stat().st_mtime_ns, mtime)
                        if suffix == ".mp4":
                            self.assertIn("X author: @Example_User", first["ItemList:Comment"])
                            self.assertEqual(first["UserData:Comment"], "Separate comment")
                        self.assertFalse(list(self.root.glob(".xlikesaver-source-*")))

    def test_numeric_author_id_without_handle_is_recorded_without_guessing_a_handle(self):
        path = self.root / "100_numeric.png"
        path.write_bytes(PNG)
        result = metadata.embed_source(path, "100", author_id="12345")
        self.assertEqual((result["status"], result["author_status"]), ("embedded", "embedded"))
        self.assertEqual(metadata.read_author(path), {"author": "", "author_id": "12345", "profile_url": "https://x.com/i/user/12345"})
        self.assertNotIn("XMP-dc:Creator", metadata._read_tags(path, time.monotonic() + 20))

    def test_updated_handle_replaces_only_this_apps_previous_author_entries(self):
        path = self.root / "100_renamed.jpg"
        path.write_bytes(JPEG)
        result = metadata._run(["-overwrite_original", "-XMP-dc:Creator+=Studio Photographer",
                                "-XMP-dc:Relation+=https://example.com/album", "-XMP-xmp:Identifier+=urn:other:1",
                                str(path)], time.monotonic() + 20)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(metadata.embed_source(path, "100", "previous_name", "12345")["author_status"], "embedded")
        self.assertEqual(metadata.embed_source(path, "100", "current_name", "67890")["author_status"], "embedded")
        self.assertEqual(metadata.read_author(path), {"author": "current_name", "author_id": "67890", "profile_url": "https://x.com/current_name"})
        tags = metadata._read_tags(path, time.monotonic() + 20)
        self.assertEqual(metadata._values(tags["XMP-dc:Creator"]), ["Studio Photographer", "@current_name"])
        self.assertEqual(set(metadata._values(tags["XMP-dc:Relation"])),
                         {"https://example.com/album", "https://x.com/current_name", "https://x.com/i/user/67890"})
        self.assertEqual(set(metadata._values(tags["XMP-xmp:Identifier"])),
                         {"urn:other:1", "xlikesaver:x-author:current_name", "xlikesaver:x-user-id:67890"})
        self.assertEqual(tags["XMP-dc:Description"], URL + "\nX author: @current_name (https://x.com/current_name)\n"
                                                      "X user ID: 67890 (https://x.com/i/user/67890)")

    def test_handle_case_change_leaves_one_creator(self):
        path = self.root / "100_case.png"
        path.write_bytes(PNG)
        self.assertEqual(metadata.embed_source(path, "100", "Writer")["author_status"], "embedded")
        self.assertEqual(metadata.embed_source(path, "100", "writer")["author_status"], "embedded")
        tags = metadata._read_tags(path, time.monotonic() + 20)
        self.assertEqual(metadata._values(tags["XMP-dc:Creator"]), ["@writer"])
        self.assertEqual(metadata._values(tags["XMP-dc:Relation"]), ["https://x.com/writer"])
        self.assertEqual(metadata.read_author(path)["author"], "writer")

    def test_invalid_author_text_still_records_the_source(self):
        for index, author in enumerate(("고양이", "a" * 16, "Alice Kim")):
            with self.subTest(author=author):
                path = self.root / f"10{index}_invalid.png"
                path.write_bytes(PNG)
                result = metadata.embed_source(path, f"10{index}", author)
                self.assertEqual((result["status"], result["author_status"]), ("embedded", "unknown"))
                self.assertEqual(metadata.read_source(path), f"https://x.com/i/web/status/10{index}")
                self.assertEqual(metadata.read_author(path)["author"], "")

    def test_jpeg_png_webp_roundtrip_preserves_compressed_pixels_and_mtime(self):
        for suffix, payload in ((".jpg", JPEG), (".png", PNG), (".webp", WEBP)):
            with self.subTest(suffix=suffix):
                path = self.root / ("100_1" + suffix)
                path.write_bytes(payload)
                os.utime(path, ns=(946684800000000000, 946684800123456700))
                mtime = path.stat().st_mtime_ns
                before = image_payload(payload, suffix)
                self.assertTrue(before)
                self.assertEqual(metadata.embed_source(path, "100")["status"], "embedded")
                self.assertEqual(metadata.read_source(path), URL)
                self.assertEqual(image_payload(path.read_bytes(), suffix), before)
                self.assertEqual(path.stat().st_mtime_ns, mtime)
                self.assertFalse(list(self.root.glob(".xlikesaver-source-*")))
                self.assertFalse(list(self.root.glob("*_original")))

    def test_existing_multilingual_description_is_preserved_and_url_not_duplicated(self):
        path = self.root / "100_1.jpg"
        path.write_bytes(JPEG)
        original_description = "원래 설명\nSecond line"
        # Value files also exercise embedded newlines and UTF-8 metadata values.
        text = self.root / "설명.txt"
        text.write_text(original_description, encoding="utf-8", newline="")
        result = metadata._run(["-overwrite_original", f"-XMP-dc:Description-x-default<={text}", "-XMP-dc:Description-fr=Texte original", str(path)], time.monotonic() + 20)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(metadata.embed_source(path, "100")["status"], "embedded")
        first = metadata._read_tags(path, time.monotonic() + 20)
        self.assertEqual(first["XMP-dc:Description"], original_description + "\nSource: " + URL)
        self.assertEqual(metadata.embed_source(path, "100")["status"], "embedded")
        self.assertEqual(metadata._read_tags(path, time.monotonic() + 20)["XMP-dc:Description"], first["XMP-dc:Description"])
        result = metadata._run(["-j", "-XMP-dc:Description-fr", str(path)], time.monotonic() + 20)
        self.assertEqual(json.loads(result.stdout)[0]["Description-fr"], "Texte original")

    def test_mp4_container_roundtrip_preserves_mdat_and_existing_comments(self):
        # ExifTool's own movie test fixture has real movie/track headers with an
        # empty mdat. Supply nonempty opaque bytes to verify payload preservation.
        fixture = metadata.exiftool_path().parent / "exiftool_files" / "t" / "images" / "QuickTime.mov"
        movie = [(kind, data) for kind, data in atoms(fixture.read_bytes()) if kind != b"mdat"]
        mdat = b"\x00\x00\x00\x04\x65\x88\x84\x00" * 32
        movie.insert(0, (b"ftyp", b"isom\x00\x00\x02\x00isomiso2mp41"))
        movie.append((b"mdat", mdat))
        path = self.root / "100_gif_1.mp4"
        path.write_bytes(b"".join(struct.pack(">I4s", len(data) + 8, kind) + data for kind, data in movie))
        result = metadata._run(["-overwrite_original", "-ItemList:Comment=Original comment", "-UserData:Comment=Separate comment", str(path)], time.monotonic() + 20)
        self.assertEqual(result.returncode, 0)
        original_payload = [data for kind, data in atoms(path.read_bytes()) if kind == b"mdat"]
        self.assertEqual(original_payload, [mdat])
        self.assertEqual(metadata.embed_source(path, "100")["status"], "embedded")
        self.assertEqual(metadata.read_source(path), URL)
        self.assertEqual([data for kind, data in atoms(path.read_bytes()) if kind == b"mdat"], original_payload)
        tags = metadata._read_tags(path, time.monotonic() + 20)
        self.assertEqual(tags["ItemList:Comment"], "Original comment\nSource: " + URL)
        self.assertEqual(tags["UserData:Comment"], "Separate comment")
        self.assertEqual(metadata.embed_source(path, "100")["status"], "embedded")
        self.assertEqual(metadata._read_tags(path, time.monotonic() + 20)["ItemList:Comment"], tags["ItemList:Comment"])

    def test_resident_exiftool_matches_one_shot_results_with_a_single_process(self):
        files = []
        for index, (suffix, payload) in enumerate(((".jpg", JPEG), (".png", PNG), (".webp", WEBP))):
            path = self.root / f"{200 + index}_1{suffix}"
            path.write_bytes(payload)
            files.append((path, payload, suffix))
        real_popen = subprocess.Popen
        with patch.object(metadata.subprocess, "Popen", side_effect=real_popen) as popen, \
                patch.object(metadata.subprocess, "run", side_effect=AssertionError("one-shot process used")):
            with metadata.persistent():
                for index, (path, payload, suffix) in enumerate(files):
                    self.assertEqual(metadata.embed_source(path, str(200 + index))["status"], "embedded")
                    self.assertEqual(metadata.read_source(path), f"https://x.com/i/web/status/{200 + index}")
                    self.assertEqual(image_payload(path.read_bytes(), suffix), image_payload(payload, suffix))
        self.assertEqual(popen.call_count, 1)
        self.assertIsNone(metadata._session)
        self.assertFalse(list(self.root.glob(".xlikesaver-source-*")))

    def test_resident_exiftool_reports_failures_and_falls_back_when_its_process_dies(self):
        path = self.root / "300_1.png"
        path.write_bytes(PNG)
        with metadata.persistent():
            missing = metadata._run(["-j", str(self.root / "없는 파일.png")], time.monotonic() + 20)
            self.assertNotEqual(missing.returncode, 0)
            session = metadata._session
            session._process.kill()
            session._process.wait()
            self.assertEqual(metadata.embed_source(path, "300")["status"], "embedded")
        self.assertEqual(metadata.read_source(path), "https://x.com/i/web/status/300")

    def test_resident_exiftool_timeout_stops_the_process(self):
        path = self.root / "301_1.png"
        path.write_bytes(PNG)
        with metadata.persistent():
            session = metadata._session
            with self.assertRaises(TimeoutError):
                session.execute(["-j", str(path)], time.monotonic())
            self.assertIsNotNone(session._process.poll())
            # Later calls still work through one-shot processes.
            self.assertEqual(metadata.embed_source(path, "301")["status"], "embedded")
        self.assertEqual(path.read_bytes()[:8], PNG[:8])

    def test_overlapping_resident_contexts_keep_process_until_last_thread_finishes(self):
        path = self.root / "400_1.png"
        path.write_bytes(PNG)
        entered, proceed = threading.Event(), threading.Event()
        results = []
        def work():
            try:
                with metadata.persistent():
                    entered.set()
                    if not proceed.wait(10):
                        raise AssertionError("metadata test coordination timed out")
                    results.append(metadata.embed_source(path, "400", "writer", "12345"))
            except Exception as error:
                results.append(error)
        real_popen = subprocess.Popen
        with patch.object(metadata.subprocess, "Popen", side_effect=real_popen) as popen, \
                patch.object(metadata.subprocess, "run", side_effect=AssertionError("one-shot process used")):
            with metadata.persistent():
                session = metadata._session
                worker = threading.Thread(target=work)
                worker.start()
                self.assertTrue(entered.wait(10))
            try:
                self.assertIs(metadata._session, session)
                self.assertIsNone(session._process.poll())
            finally:
                proceed.set()
                worker.join(20)
            self.assertFalse(worker.is_alive())
            self.assertEqual(results[0]["author_status"], "embedded")
            self.assertEqual(popen.call_count, 1)
        self.assertIsNone(metadata._session)
        self.assertIsNotNone(session._process.poll())
        self.assertEqual(metadata.read_author(path)["author"], "writer")

    def test_standalone_command_borrows_resident_process_until_command_finishes(self):
        path = self.root / "401_1.png"
        path.write_bytes(PNG)
        entered, proceed = threading.Event(), threading.Event()
        results = []
        with metadata.persistent():
            session = metadata._session
            execute = session.execute
            def delayed(arguments, deadline):
                entered.set()
                if not proceed.wait(10):
                    raise AssertionError("metadata test coordination timed out")
                return execute(arguments, deadline)
            def work():
                try:
                    results.append(metadata._run(["-j", str(path)], time.monotonic() + 20))
                except Exception as error:
                    results.append(error)
            patcher = patch.object(session, "execute", side_effect=delayed)
            patcher.start()
            worker = threading.Thread(target=work)
            worker.start()
            self.assertTrue(entered.wait(10))
        try:
            self.assertIs(metadata._session, session)
            self.assertIsNone(session._process.poll())
        finally:
            proceed.set()
            worker.join(20)
            patcher.stop()
        self.assertFalse(worker.is_alive())
        self.assertEqual(results[0].returncode, 0)
        self.assertIsNone(metadata._session)
        self.assertIsNotNone(session._process.poll())

    def test_waiting_command_honors_deadline_without_killing_another_command(self):
        path = self.root / "402_1.png"
        path.write_bytes(PNG)
        entered, proceed = threading.Event(), threading.Event()
        with metadata.persistent():
            session = metadata._session
            def hold():
                with session._lock:
                    entered.set()
                    proceed.wait(10)
            worker = threading.Thread(target=hold)
            worker.start()
            self.assertTrue(entered.wait(10))
            try:
                started = time.monotonic()
                with self.assertRaises(TimeoutError):
                    session.execute(["-j", str(path)], time.monotonic() + 0.05)
                self.assertLess(time.monotonic() - started, 1)
                self.assertIsNone(session._process.poll())
            finally:
                proceed.set()
                worker.join(10)
            self.assertEqual(metadata.embed_source(path, "402", "writer")["author_status"], "embedded")


class BundleExtractionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        source = Path(__file__).resolve().parent.parent / "tools" / "exiftool" / "download.py"
        spec = importlib.util.spec_from_file_location("offline_exiftool_download", source)
        self.download = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.download)

    def archive(self, name):
        archive = self.root / "synthetic.zip"
        with zipfile.ZipFile(archive, "w") as package:
            package.writestr(name, b"fake-offline-bundle")
        return archive

    def test_extracts_nested_bundle_files(self):
        destination = self.root / "runtime"
        self.download.extract_bundle(self.archive("exiftool_files/lib/Image/ExifTool.pm"), destination)
        self.assertEqual((destination / "exiftool_files/lib/Image/ExifTool.pm").read_bytes(), b"fake-offline-bundle")

    def test_rejects_traversal_absolute_drive_and_backslash_paths(self):
        for name in ("../outside.exe", "/outside.exe", "C:/outside.exe", "..\\outside.exe"):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    self.download.extract_bundle(self.archive(name), self.root / "runtime")
        self.assertFalse((self.root / "outside.exe").exists())

    def test_rejects_zip_symlink_and_excess_expanded_size(self):
        archive = self.root / "link.zip"
        info = zipfile.ZipInfo("exiftool_files/linked")
        info.create_system = 3
        info.external_attr = 0o120777 << 16
        with zipfile.ZipFile(archive, "w") as package:
            package.writestr(info, "../outside")
        with self.assertRaises(ValueError):
            self.download.extract_bundle(archive, self.root / "runtime")
        with patch.object(self.download, "MAX_EXPANDED", 1), self.assertRaises(ValueError):
            self.download.extract_bundle(self.archive("exe"), self.root / "runtime")

    def test_pinned_digest_and_source_are_fixed(self):
        self.assertEqual(len(self.download.SHA256), 64)
        self.assertEqual(self.download.VERSION, "13.59")
        self.assertTrue(self.download.URL.startswith("https://oliverbetz.de/"))

    def test_frozen_helper_uses_extracted_bundle_not_exe_directory(self):
        with patch.object(metadata.sys, "frozen", True, create=True), \
                patch.object(metadata.sys, "_MEIPASS", str(self.root / "extracted"), create=True), \
                patch.object(metadata.sys, "executable", str(self.root / "app.exe")):
            self.assertEqual(metadata.exiftool_path(),
                             self.root / "extracted/tools/exiftool/runtime/exiftool.exe")


if __name__ == "__main__":
    unittest.main()

"""Separate post fields without replacing unrelated media descriptions."""

import hashlib
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from luvbox import metadata
from tests import test_metadata as fixtures
from tests.test_metadata import JPEG, URL, atoms, image_payload


class StructuredTextValidationTests(unittest.TestCase):
    def test_invalid_text_never_starts_tool_or_changes_original(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "original.jpg"
            path.write_bytes(JPEG)
            for text in (123, False, "a\0b", "한" * (metadata._TEXT_LIMIT_BYTES // 3 + 1), "\ud800"):
                with self.subTest(kind=type(text).__name__), patch.object(metadata, "_run") as run:
                    result = metadata.embed_source(path, "100", text=text)
                    self.assertEqual((result["status"], result["reason"], result["text_status"]),
                                     ("failed", "invalid", "error"))
                    run.assert_not_called()
                    self.assertEqual(path.read_bytes(), JPEG)
                    self.assertEqual(list(Path(directory).iterdir()), [path])


@unittest.skipUnless(os.name == "nt" and metadata.available(), "Prepare bundled Windows ExifTool.")
class StructuredExifToolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "한글😀 저장"
        self.root.mkdir()

    def make_media(self, suffix):
        path = self.root / ("100_1" + suffix)
        path.write_bytes(fixtures.ActualExifToolTests.media_payload(self, suffix))
        if suffix == ".mp4":
            self.assertEqual(metadata._run(["-overwrite_original", "-XMP-dc:Creator=", str(path)],
                                            time.monotonic() + 20).returncode, 0)
        return path

    def tags(self, path):
        return metadata._read_tags(path, time.monotonic() + 20)

    def write_description(self, path, text):
        value = self.root / "설명.txt"
        value.write_text(text, encoding="utf-8", newline="")
        self.assertEqual(metadata._run(["-overwrite_original", f"-XMP-dc:Description-x-default<={value}",
                                        str(path)], time.monotonic() + 20).returncode, 0)

    def test_body_source_author_and_id_are_separate_in_all_containers(self):
        body = "사진 본문 😀\r\n두 번째 줄\n-execute999\n-XMP-dc:Creator=attacker"
        with metadata.persistent():
            for suffix in (".jpg", ".png", ".webp", ".mp4"):
                with self.subTest(suffix=suffix):
                    path = self.make_media(suffix)
                    previous_tags = self.tags(path)
                    original = path.read_bytes()
                    os.utime(path, ns=(946684800000000000, 946684800123456700))
                    mtime = path.stat().st_mtime_ns
                    result = metadata.embed_source(path, "100", "writer", "12345", text=body)
                    self.assertEqual((result["status"], result["author_status"], result["text_status"]),
                                     ("embedded", "embedded", "embedded"))
                    tags = self.tags(path)
                    self.assertEqual(tags["XMP-dc:Description"], body)
                    self.assertEqual(tags["XMP-dc:Source"], URL)
                    self.assertEqual(metadata._values(tags["XMP-dc:Creator"]), ["@writer"])
                    self.assertIn("luvbox:x-user-id:12345", metadata._values(tags["XMP-xmp:Identifier"]))
                    self.assertEqual(tags.get("ItemList:Comment", ""), previous_tags.get("ItemList:Comment", ""))
                    self.assertEqual(metadata.read_author(path)["author_id"], "12345")
                    if suffix == ".mp4":
                        self.assertEqual([data for kind, data in atoms(path.read_bytes()) if kind == b"mdat"],
                                         [data for kind, data in atoms(original) if kind == b"mdat"])
                    else:
                        self.assertEqual(image_payload(path.read_bytes(), suffix), image_payload(original, suffix))
                    self.assertEqual(path.stat().st_mtime_ns, mtime)
                    self.assertEqual(metadata.embed_source(path, "100", "writer", "12345", text=body)["status"], "embedded")
                    self.assertEqual(self.tags(path), tags)
                    self.assertFalse(list(self.root.glob(".luvbox-source-*")))

    def test_owned_body_can_update_but_manual_edits_are_never_overwritten(self):
        path = self.make_media(".jpg")
        with metadata.persistent():
            self.assertEqual(metadata.embed_source(path, "100", text="첫 본문")["text_status"], "embedded")
            self.assertEqual(metadata.embed_source(path, "100", text="새 본문")["text_status"], "embedded")
            self.assertEqual(self.tags(path)["XMP-dc:Description"], "새 본문")
            markers = metadata._values(self.tags(path)["XMP-xmp:Identifier"])
            self.assertEqual(markers, [metadata._OWNED_DESCRIPTION_MARKER + hashlib.sha256("새 본문".encode()).hexdigest()])
            self.write_description(path, "수동으로 바꾼 원본 설명")
            self.assertEqual(metadata.embed_source(path, "100", "writer", text="덮어쓰면 안 됨")["text_status"], "preserved")
            self.assertEqual(self.tags(path)["XMP-dc:Description"], "수동으로 바꾼 원본 설명")

    def test_external_matching_body_is_not_claimed(self):
        path = self.make_media(".png")
        with metadata.persistent():
            self.write_description(path, "외부 설명")
            self.assertEqual(metadata.embed_source(path, "100", text="외부 설명")["text_status"], "embedded")
            self.assertFalse(any(value.startswith(metadata._OWNED_DESCRIPTION_MARKER)
                                 for value in metadata._values(self.tags(path).get("XMP-xmp:Identifier"))))
            self.assertEqual(metadata.embed_source(path, "100", text="다른 본문")["text_status"], "preserved")
            self.assertEqual(self.tags(path)["XMP-dc:Description"], "외부 설명")

    def test_unknown_body_does_not_add_source_or_author_to_description(self):
        path = self.make_media(".jpg")
        with metadata.persistent():
            self.assertEqual(metadata.embed_source(path, "100", "writer")["text_status"], "not_requested")
            self.assertEqual(self.tags(path).get("XMP-dc:Description", ""), "")
            self.assertEqual(metadata.embed_source(path, "100", "writer", text="")["text_status"], "embedded")
            self.assertEqual(self.tags(path).get("XMP-dc:Description", ""), "")

    def test_readback_failure_preserves_original_and_removes_staging_files(self):
        path = self.make_media(".jpg")
        original = path.read_bytes()
        read = metadata._read_tags

        def wrong_description(target, deadline):
            tags = read(target, deadline)
            if target != path:
                tags["XMP-dc:Description"] = "wrong body"
            return tags

        with metadata.persistent(), patch.object(metadata, "_read_tags", side_effect=wrong_description):
            result = metadata.embed_source(path, "100", text="검증할 본문")
        self.assertEqual((result["status"], result["reason"], result["text_status"]),
                         ("failed", "verification", "error"))
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(list(self.root.iterdir()), [path])

    def test_native_mp4_creator_is_preserved_or_original_replacement_is_rejected(self):
        path = self.root / "native.mp4"
        path.write_bytes(fixtures.ActualExifToolTests.media_payload(self, ".mp4"))
        with metadata.persistent():
            before_tags = self.tags(path)
            original = path.read_bytes()
            result = metadata.embed_source(path, "100", "writer", text="본문")
            if result["status"] == "embedded":
                self.assertTrue(set(metadata._values(before_tags.get("XMP-dc:Creator"))).issubset(
                    metadata._values(self.tags(path).get("XMP-dc:Creator"))))
            else:
                self.assertEqual(result["reason"], "verification")
                self.assertEqual(path.read_bytes(), original)
        self.assertFalse(list(self.root.glob(".luvbox-source-*")))


if __name__ == "__main__":
    unittest.main()

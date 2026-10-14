"""Offline recycle receipts and one opt-in native test using a new temp file."""

import os
import struct
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from luvbox import recycling
from luvbox.recycling import RecyclingError, file_signature, recycle_file, verify_receipt


class RecyclingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="luvbox-recycle-test-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.source = self.base / "원본 사진.jpg"
        self.source.write_bytes(b"fixture-only media")
        self.signature = file_signature(self.source)
        self.destination = self.base / "$RECYCLE.BIN" / "fixture-user" / "$Rfixture.jpg"

    def recycled(self, version=2, original=None, size=None):
        self.destination.parent.mkdir(parents=True, exist_ok=True)
        self.source.rename(self.destination)
        name = str(original or self.source) + "\0"
        size = self.signature[2] if size is None else size
        content = struct.pack("<QQQ", version, size, 0)
        if version == 2:
            content += struct.pack("<I", len(name.encode("utf-16-le")) // 2)
        content += name.encode("utf-16-le")
        self.destination.with_name("$Ifixture.jpg").write_bytes(content)
        return recycling._Outcome(post_result=0, destination=str(self.destination))

    def test_signature_accepts_json_roundtrip_list(self):
        with patch.object(recycling, "_run_native", side_effect=lambda *_: self.recycled()):
            self.assertEqual(recycle_file(self.source, list(self.signature)), str(self.destination))

    def test_changed_file_is_rejected_before_native_operation(self):
        self.source.write_bytes(b"changed")
        with patch.object(recycling, "_run_native") as native:
            with self.assertRaisesRegex(RecyclingError, "바뀌어"):
                recycle_file(self.source, self.signature)
            native.assert_not_called()
        self.assertTrue(self.source.exists())

    def test_directory_is_never_queued(self):
        with patch.object(recycling, "_run_native") as native:
            with self.assertRaises(RecyclingError):
                recycle_file(self.base)
            native.assert_not_called()

    @staticmethod
    def reparse(info, tag):
        # Python reports a name-surrogate link with its tag. It opens other
        # reparse kinds (cloud placeholders) as files, reporting no tag.
        return SimpleNamespace(st_mode=info.st_mode, st_dev=info.st_dev, st_ino=info.st_ino,
                               st_size=info.st_size, st_mtime_ns=info.st_mtime_ns,
                               st_file_attributes=0x400, st_reparse_tag=tag)

    def test_linked_file_is_rejected(self):
        for tag in (0xA000000C, 0xA0000003):  # symbolic link, junction/mount point
            with self.subTest(tag=hex(tag)):
                info = self.reparse(self.source.lstat(), tag)
                with patch.object(Path, "lstat", return_value=info):
                    with self.assertRaises(RecyclingError):
                        file_signature(self.source)

    def test_linked_parent_is_rejected(self):
        actual = Path.lstat

        def lookup(path, *args, **kwargs):
            info = actual(path, *args, **kwargs)
            return self.reparse(info, 0xA0000003) if path == self.base else info

        with patch.object(Path, "lstat", lookup):
            with self.assertRaisesRegex(RecyclingError, "연결된 폴더"):
                file_signature(self.source)

    def test_cloud_placeholder_file_and_folder_are_ordinary_files(self):
        actual = Path.lstat
        for tag in (0, 0x9000601A):  # as Python reports it; a raw OneDrive cloud tag
            def lookup(path, *args, **kwargs):
                info = actual(path, *args, **kwargs)
                return self.reparse(info, tag) if path in (self.source, self.base) else info

            with self.subTest(tag=hex(tag)), patch.object(Path, "lstat", lookup):
                self.assertEqual(file_signature(self.source), self.signature)

    @unittest.skipUnless(os.name == "nt", "Windows junction")
    def test_real_junction_parent_is_rejected(self):
        import _winapi

        target = self.base / "real-folder"
        target.mkdir()
        (target / "inside.jpg").write_bytes(b"fixture behind a junction")
        junction = self.base / "joined-folder"
        _winapi.CreateJunction(str(target), str(junction))
        self.addCleanup(os.rmdir, junction)  # Removes only the link, not its target.
        with self.assertRaisesRegex(RecyclingError, "연결된 폴더"):
            file_signature(junction / "inside.jpg")
        self.assertTrue((target / "inside.jpg").is_file())

    def test_invalid_signature_never_queues_file(self):
        for value in ((), [True, 1, 2, 3], [1, 2, -1, 3], "abcd", None):
            if value is None:
                continue
            with self.subTest(value=value), patch.object(recycling, "_run_native") as native:
                with self.assertRaises(RecyclingError):
                    recycle_file(self.source, value)
                native.assert_not_called()

    def test_operation_failure_preserves_file_and_reports_safe_failure(self):
        with patch.object(recycling, "_run_native", return_value=recycling._Outcome(operation_result=-1)):
            with self.assertRaises(RecyclingError) as caught:
                recycle_file(self.source)
        self.assertFalse(caught.exception.uncertain)
        self.assertTrue(self.source.exists())

    def test_aborted_operation_is_not_success(self):
        with patch.object(recycling, "_run_native", return_value=recycling._Outcome(aborted=True)):
            with self.assertRaises(RecyclingError) as caught:
                recycle_file(self.source)
        self.assertFalse(caught.exception.uncertain)
        self.assertTrue(self.source.exists())

    def test_per_item_result_is_required_even_with_successful_operation(self):
        with patch.object(recycling, "_run_native", return_value=recycling._Outcome()):
            with self.assertRaises(RecyclingError):
                recycle_file(self.source)
        self.assertTrue(self.source.exists())

    def test_positive_shell_success_code_still_requires_verified_receipt(self):
        def success(*_):
            outcome = self.recycled()
            outcome.post_result = 0x270008  # COPYENGINE_S_* success from the real Shell.
            return outcome

        with patch.object(recycling, "_run_native", side_effect=success):
            self.assertEqual(recycle_file(self.source), str(self.destination))

    def test_no_recycle_destination_is_uncertain_when_original_is_missing(self):
        def missing(*_):
            self.source.rename(self.base / "kept-for-review.jpg")
            return recycling._Outcome(post_result=0)

        with patch.object(recycling, "_run_native", side_effect=missing):
            with self.assertRaises(RecyclingError) as caught:
                recycle_file(self.source)
        self.assertTrue(caught.exception.uncertain)

    def test_error_after_original_disappears_is_always_uncertain(self):
        def error(*_):
            self.source.rename(self.base / "kept-for-review.jpg")
            raise RecyclingError("mock native COM failure")

        with patch.object(recycling, "_run_native", side_effect=error):
            with self.assertRaises(RecyclingError) as caught:
                recycle_file(self.source)
        self.assertTrue(caught.exception.uncertain)

    def test_native_exception_before_operation_is_controlled(self):
        with patch.object(recycling, "_run_native", side_effect=OSError("mock COM failure")):
            with self.assertRaises(RecyclingError) as caught:
                recycle_file(self.source)
        self.assertFalse(caught.exception.uncertain)

    def test_wrong_receipt_path_never_counts_as_success(self):
        with patch.object(recycling, "_run_native", side_effect=lambda *_: self.recycled(original=self.base / "other.jpg")), \
                patch.object(recycling.time, "sleep"):
            with self.assertRaises(RecyclingError) as caught:
                recycle_file(self.source)
        self.assertTrue(caught.exception.uncertain)
        self.assertEqual(caught.exception.recycled_path, str(self.destination))

    def test_receipt_wrong_size_is_rejected(self):
        self.recycled(size=self.signature[2] + 1)
        self.assertFalse(verify_receipt(self.source, self.destination, self.signature))

    def test_different_file_identity_is_rejected(self):
        self.recycled()
        replacement = self.base / "replacement.jpg"
        replacement.write_bytes(b"fixture-only media")
        self.destination.unlink()
        replacement.rename(self.destination)
        self.assertFalse(verify_receipt(self.source, self.destination, self.signature))

    def test_legacy_and_current_receipts_are_supported(self):
        self.recycled(version=1)
        self.assertTrue(verify_receipt(self.source, self.destination, self.signature))
        name = (str(self.source) + "\0").encode("utf-16-le")
        self.destination.with_name("$Ifixture.jpg").write_bytes(
            struct.pack("<QQQI", 2, self.signature[2], 0, len(name) // 2) + name)
        self.assertTrue(verify_receipt(self.source, self.destination, self.signature))

    def test_short_invalid_or_missing_receipts_are_rejected(self):
        self.recycled()
        receipt = self.destination.with_name("$Ifixture.jpg")
        for content in (b"invalid", struct.pack("<QQQ", 9, self.signature[2], 0),
                        struct.pack("<QQQI", 2, self.signature[2], 0, 99999)):
            receipt.write_bytes(content)
            self.assertFalse(verify_receipt(self.source, self.destination, self.signature))
        receipt.unlink()
        self.assertFalse(verify_receipt(self.source, self.destination, self.signature))

    def test_non_recycle_destination_is_rejected(self):
        self.source.rename(self.base / "$Routside.jpg")
        self.assertFalse(verify_receipt(self.source, self.base / "$Routside.jpg", self.signature))

    def test_receipt_behind_linked_parent_is_rejected(self):
        self.recycled()
        actual = Path.lstat

        def lookup(path, *args, **kwargs):
            info = actual(path, *args, **kwargs)
            return self.reparse(info, 0xA0000003) if path == self.destination.parent else info

        with patch.object(Path, "lstat", lookup):
            self.assertFalse(verify_receipt(self.source, self.destination, self.signature))

    def test_oversized_corrupt_receipt_is_rejected(self):
        self.recycled()
        receipt = self.destination.with_name("$Ifixture.jpg")
        with receipt.open("ab") as output:
            output.write(b"\0" * (28 + 32768 * 2))
        self.assertFalse(verify_receipt(self.source, self.destination, self.signature))

    def test_missing_receipt_is_retried_before_giving_up(self):
        with patch.object(recycling, "_run_native", side_effect=lambda *_: self.recycled()), \
                patch.object(recycling, "verify_receipt", side_effect=[False, True]) as verify, \
                patch.object(recycling.time, "sleep") as sleep:
            self.assertEqual(recycle_file(self.source), str(self.destination))
        self.assertEqual(verify.call_count, 2)
        sleep.assert_called_once_with(0.05)

    def test_permanent_delete_callback_is_aborted(self):
        outcome = recycling._Outcome()
        sink = recycling._Sink(self.source, self.signature, outcome, Mock())
        with patch.object(recycling, "_display_name", return_value=str(self.source)):
            self.assertEqual(sink.pre_delete(None, 0, None), recycling._E_ABORT)
        self.assertIn("휴지통", outcome.pre_error)
        self.assertTrue(self.source.exists())
        self.assertTrue(recycling._RECYCLE_FLAGS & 0x00080000)
        # Skip Explorer's optional per-file prompt, but keep its warning before
        # any permanent deletion rather than answering it Yes automatically.
        self.assertTrue(recycling._RECYCLE_FLAGS & 0x10)
        self.assertTrue(recycling._RECYCLE_FLAGS & 0x4000)

    def test_predelete_rechecks_source_identity(self):
        outcome = recycling._Outcome()
        sink = recycling._Sink(self.source, self.signature, outcome, Mock())
        self.source.write_bytes(b"edited after the selection")
        with patch.object(recycling, "_display_name", return_value=str(self.source)):
            self.assertEqual(sink.pre_delete(None, 0x80, None), recycling._E_ABORT)
        self.assertIn("바뀌어", outcome.pre_error)

    def test_predelete_rechecks_shell_item_path(self):
        outcome = recycling._Outcome()
        sink = recycling._Sink(self.source, self.signature, outcome, Mock())
        with patch.object(recycling, "_display_name", return_value=str(self.base / "other.jpg")):
            self.assertEqual(sink.pre_delete(None, 0x80, None), recycling._E_ABORT)
        self.assertTrue(self.source.exists())


@unittest.skipUnless(os.name == "nt" and os.environ.get("LUVBOX_NATIVE_RECYCLE_TEST") == "1",
                     "native Shell test is opt-in and uses only its own new temp file")
class NativeRecyclingTest(unittest.TestCase):
    def test_one_new_temporary_file_has_verified_shell_receipt(self):
        # This test never enumerates or modifies existing user media/Recycle Bin items.
        with tempfile.TemporaryDirectory(prefix="luvbox-native-recycle-") as folder:
            original = Path(folder) / "LuvBox 휴지통 기능 테스트.txt"
            original.write_bytes(b"LuvBox native recycle fixture only.\n")
            expected = file_signature(original)
            destination = recycle_file(original, expected)
            self.assertFalse(original.exists())
            self.assertTrue(verify_receipt(original, destination, expected))
            self.assertEqual(Path(destination).read_bytes(), b"LuvBox native recycle fixture only.\n")


if __name__ == "__main__":
    unittest.main()

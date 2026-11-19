"""Preference updates must preserve an existing file they cannot read."""

from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

from tests import app_data_isolation
from luvbox.app_settings import SettingsRepository


class SettingsIntegrityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="luvbox-settings-integrity-")
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "settings.json"
        for name in ("luvbox.settings.settings_path", "luvbox.app_settings.settings_path"):
            replacement = patch(name, return_value=self.path)
            replacement.start()
            self.addCleanup(replacement.stop)
        self.repository = SettingsRepository()

    def test_corrupt_existing_preferences_are_kept_under_another_name(self):
        # 1.11.7: refusing every later change left the app stuck with an error nobody could act on.
        for original in (b'{"account":"saved-account",', b"[]", b"\xff\xfe"):
            with self.subTest(original=original):
                for path in self.path.parent.iterdir():
                    path.unlink()
                self.path.write_bytes(original)
                self.repository.update({"confirm_each": True})
                kept = [path for path in self.path.parent.iterdir() if path != self.path]
                self.assertEqual([path.read_bytes() for path in kept], [original])
                self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), {"confirm_each": True})

    def test_read_permission_failure_cannot_erase_preferences(self):
        original = b'{"account":"saved-account","custom":42}'
        self.path.write_bytes(original)
        with patch.object(Path, "read_text", side_effect=PermissionError("private details")):
            with self.assertRaises(OSError) as raised:
                self.repository.update({"confirm_each": True})
        self.assertNotIn("private details", str(raised.exception))
        self.assertEqual(self.path.read_bytes(), original)

    def test_first_save_and_updates_preserve_unknown_and_unicode_values(self):
        self.repository.update({"account": "fixture", "custom": {"한글": 42}})
        self.repository.update({"confirm_each": True})
        stored = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(stored, {"account": "fixture", "custom": {"한글": 42}, "confirm_each": True})

    def test_failed_atomic_replace_preserves_original_and_removes_temporary_file(self):
        original = b'{"account":"saved-account","custom":42}'
        self.path.write_bytes(original)
        with patch("luvbox.app_settings.os.replace", side_effect=PermissionError):
            with self.assertRaises(OSError):
                self.repository.update({"confirm_each": True})
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])




if __name__ == "__main__":
    unittest.main()

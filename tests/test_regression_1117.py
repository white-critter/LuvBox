"""Regression checks for the 1.11.7 inspection fixes."""

from importlib.util import find_spec
import io
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
import tokenize
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tests import app_data_isolation  # noqa: E402,F401


class DamagedSettingsFileTests(unittest.TestCase):
    """A settings file that holds no readable settings must not block every later change."""

    def setUp(self):
        from luvbox.app_settings import SettingsRepository

        temporary = tempfile.TemporaryDirectory(prefix="luvbox-settings-damaged-")
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "settings.json"
        for name in ("luvbox.settings.settings_path", "luvbox.app_settings.settings_path"):
            replacement = patch(name, return_value=self.path)
            replacement.start()
            self.addCleanup(replacement.stop)

        self.repository = SettingsRepository()

    def kept(self):
        return sorted(path for path in self.path.parent.iterdir() if path != self.path)

    def test_damaged_contents_are_kept_aside_and_the_change_is_saved(self):
        from luvbox.settings import settings_damaged

        for original in (b'{"account":"saved-account",', b"[]", b"null", b"\xff\xfe", b""):
            with self.subTest(original=original):
                for path in self.path.parent.iterdir():
                    path.unlink()
                self.path.write_bytes(original)
                self.assertTrue(settings_damaged())
                self.repository.update({"confirm_each": True})
                self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), {"confirm_each": True})
                kept = self.kept()
                self.assertEqual(len(kept), 1)
                self.assertRegex(kept[0].name, r"^settings\.damaged-\d{8}-\d{6}(-\d+)?\.json$")
                self.assertEqual(kept[0].read_bytes(), original)
                self.assertFalse(settings_damaged())
                # The next change is an ordinary update of the new file.
                self.repository.update({"custom": 1})
                self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")),
                                 {"confirm_each": True, "custom": 1})
                self.assertEqual(len(self.kept()), 1)

    def test_a_second_damaged_file_in_the_same_second_gets_its_own_name(self):
        self.path.write_bytes(b"{")
        self.repository.update({"confirm_each": True})
        self.path.write_bytes(b"[")
        self.repository.update({"confirm_each": False})
        self.assertEqual(sorted(path.read_bytes() for path in self.kept()), sorted([b"{", b"["]))

    def test_a_byte_order_mark_is_read_as_settings(self):
        from luvbox.settings import load_settings, settings_damaged

        self.path.write_bytes(b'\xef\xbb\xbf{"account":"saved-account","custom":42}')
        self.assertFalse(settings_damaged())
        self.assertEqual(load_settings()["account"], "saved-account")
        self.assertEqual(self.repository.load()["account"], "saved-account")
        self.repository.update({"confirm_each": True})
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")),
                         {"account": "saved-account", "custom": 42, "confirm_each": True})
        self.assertEqual(self.kept(), [])

    def test_a_file_that_cannot_be_read_now_is_neither_moved_nor_called_damaged(self):
        from luvbox.settings import settings_damaged

        original = b'{"account":"saved-account"}'
        self.path.write_bytes(original)
        with patch.object(Path, "read_text", side_effect=PermissionError("private details")):
            self.assertFalse(settings_damaged())
            with self.assertRaises(OSError):
                self.repository.update({"confirm_each": True})
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(self.kept(), [])

    def test_a_file_that_cannot_be_moved_aside_stops_the_change(self):
        self.path.write_bytes(b"{")
        with patch("luvbox.app_settings.os.rename", side_effect=PermissionError):
            with self.assertRaises(OSError):
                self.repository.update({"confirm_each": True})
        self.assertEqual(self.path.read_bytes(), b"{")
        self.assertEqual(self.kept(), [])


@unittest.skipUnless(find_spec("PySide6") is not None, "Qt dependencies are not installed")
class DamagedSettingsNoticeTests(unittest.TestCase):
    """The app opens with the first settings; the person is told to choose the folder again."""

    @classmethod
    def setUpClass(cls):
        from PySide6.QtCore import QCoreApplication

        cls.application = QCoreApplication.instance() or QCoreApplication([])

    def start(self, contents):
        from luvbox.qt_app import ApplicationController

        temporary = tempfile.TemporaryDirectory(prefix="luvbox-settings-notice-")
        self.addCleanup(temporary.cleanup)
        folder = Path(temporary.name)
        for target, kwargs in (
                ("luvbox.qt_accounts.AccountController._read_state",
                 {"new": staticmethod(lambda: {"accounts": [], "identity": {}, "connected": True})}),
                ("luvbox.qt_schedule.task_status", {"return_value": {"registered": False}})):
            patcher = patch(target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)
        environment = patch.dict(os.environ, {"LOCALAPPDATA": str(folder)})
        environment.start()
        self.addCleanup(environment.stop)
        settings = folder / "LuvBox" / "settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_bytes(contents)
        controller = ApplicationController()
        self.addCleanup(self.close, controller)
        return controller, settings

    def close(self, controller):
        controller.requestClose()
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and not controller.shutdownReady:
            self.application.processEvents()
            time.sleep(.002)

    def settle(self, condition, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.application.processEvents()
            if condition():
                return
            time.sleep(.002)
        self.fail("the controller did not settle")

    def test_damaged_settings_are_reported_and_the_next_change_is_saved(self):
        controller, settings = self.start(b'{"account": "example", "output_dir": "D:\\\\Lik')
        self.assertIn("폴더 선택", controller.error)
        self.assertIn("설정", controller.error)
        self.settle(lambda: not controller.operationBusy)
        controller.setPreference("confirm_each", True)
        self.settle(lambda: not controller.busy)
        self.assertIn("폴더 선택", controller.error)          # the notice stays until the person acts
        self.assertEqual(json.loads(settings.read_text(encoding="utf-8")).get("confirm_each"), True)
        kept = [path for path in settings.parent.glob("settings.damaged-*.json")]
        self.assertEqual(len(kept), 1)
        self.assertTrue(kept[0].read_bytes().startswith(b'{"account": "example"'))

    def test_readable_settings_give_no_notice(self):
        for contents in (b'{"account": "example"}', b'\xef\xbb\xbf{"account": "example"}'):
            with self.subTest(contents=contents):
                controller, _ = self.start(contents)
                self.assertEqual(controller.error, "")
                self.assertEqual(controller.account, "example")


class MessagesNameRealControlsTests(unittest.TestCase):
    """A message that tells the person what to press names a control the app has."""

    # Labels of Windows and the browsers, quoted as they appear there.
    OUTSIDE = {"문서", "압축해제된 확장 프로그램 로드", "임시 부가 기능 로드"}

    def literals(self):
        found = []
        for path in sorted((ROOT / "luvbox").glob("*.py")):
            source = path.read_text(encoding="utf-8")
            for token in tokenize.generate_tokens(io.StringIO(source).readline):
                if token.type == tokenize.STRING:
                    found.append((path.name, token.string))
        for path in sorted((ROOT / "assets" / "qt").glob("*.qml")):
            for match in re.finditer(r'"((?:[^"\\\n]|\\.)*)"', path.read_text(encoding="utf-8")):
                found.append((path.name, match.group(0)))
        return found

    def extension_labels(self):
        """Buttons of the connection page in the browser, which app texts also name."""
        page = ROOT / "assets" / "browser-extension" / "chromium"
        text = (page / "connect.html").read_text(encoding="utf-8")
        return ['"' + label.strip() for label in re.findall(r">([^<>]+)<", text) if label.strip()]

    def test_quoted_names_are_labels_the_app_shows(self):
        literals = self.literals()
        labels = "\n".join([text for _, text in literals] + self.extension_labels())
        missing = []
        for name, text in literals:
            for match in re.finditer(r"‘([^’]{1,40})’", text):
                quoted = match.group(1)
                if quoted in self.OUTSIDE:
                    continue
                # The name begins a string of its own: a button, menu, tab or heading.
                if not re.search(r'"' + re.escape(quoted), labels):
                    missing.append(f"{name}: ‘{quoted}’")
        self.assertEqual(missing, [])

    def test_busy_saves_ask_to_wait_not_to_end_a_hidden_save(self):
        from luvbox.protection import _BUSY_HELP
        from luvbox.storage import OUTPUT_BUSY_HELP

        for message in (_BUSY_HELP, OUTPUT_BUSY_HELP):
            with self.subTest(message=message):
                self.assertNotIn("종료해 주세요", message)
                self.assertIn("자동 저장", message)
                self.assertIn("끝난 뒤 다시 시도해 주세요", message)

    def test_missing_folder_help_names_the_folder_button(self):
        from luvbox.storage import StoragePaths, missing_media_help

        with tempfile.TemporaryDirectory(prefix="luvbox-missing-help-") as temporary:
            output, records = Path(temporary) / "gone", Path(temporary) / "records"
            paths = StoragePaths(output, output, records, records, "example", False, records)
            message = missing_media_help(paths)
        self.assertIn("‘저장 위치’", message)
        self.assertIn("‘폴더 선택’", message)


if __name__ == "__main__":
    unittest.main()

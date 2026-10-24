"""Default-browser discovery never runs the association command."""

from pathlib import Path
import os
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch

from luvbox import browser_discovery as discovery


class BrowserDiscoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name) / "Browser Application"
        self.directory.mkdir()
        self.chrome = self.directory / "chrome.exe"
        self.edge = self.directory / "msedge.exe"
        self.chrome.write_bytes(b"synthetic executable fixture")
        self.edge.write_bytes(b"synthetic executable fixture")

    @staticmethod
    def registry(values):
        registry = SimpleNamespace(HKEY_CURRENT_USER="HKCU", HKEY_CLASSES_ROOT="HKCR", HKEY_LOCAL_MACHINE="HKLM",
                                   KEY_READ=1, KEY_WOW64_64KEY=256, KEY_WOW64_32KEY=512,
                                   OpenKey=Mock(return_value=MagicMock()), QueryValueEx=Mock(side_effect=values))
        return registry

    def test_default_https_association_resolves_only_browser_executable_and_discards_arguments(self):
        for executable, program_id in ((self.chrome, "ChromeHTML.012345"), (self.edge, "MSEdgeHTM")):
            with self.subTest(browser=executable.name):
                command = f'"{executable}" --profile-directory="Existing Personal Profile" --single-argument "%1"'
                registry = self.registry([(program_id, 1), (command, 1)])
                with patch.dict(sys.modules, {"winreg": registry}), \
                        patch("luvbox.browser_discovery.os", SimpleNamespace(name="nt", path=os.path)), \
                        patch("subprocess.Popen", side_effect=AssertionError("association command was executed")):
                    self.assertEqual(discovery.find_default_browser(), executable)
                self.assertEqual(registry.OpenKey.call_args_list[0].args,
                                 ("HKCU", r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice"))
                self.assertEqual(registry.OpenKey.call_args_list[1].args,
                                 ("HKCR", program_id + r"\shell\open\command"))

    def test_unsupported_missing_relative_or_malformed_association_is_rejected(self):
        firefox = self.directory / "firefox.exe"
        firefox.write_bytes(b"synthetic unsupported browser")
        commands = (None, 3, "", f'"{firefox}" "%1"', f'"{self.directory / "missing" / "chrome.exe"}" "%1"',
                    'chrome.exe "%1"', f'"{self.chrome}"suffix "%1"', f'"{self.chrome}\n" "%1"',
                    f'"{self.chrome}"\0 "%1"', f'cmd.exe /c "{self.chrome}" "%1"',
                    f'{self.chrome} "%1"', '"' + "x" * 32769 + '"')
        for command in commands:
            with self.subTest(command_type=type(command).__name__):
                self.assertIsNone(discovery._association_executable(command))

    def test_invalid_program_id_and_unreadable_default_fail_without_a_fallback(self):
        for program_id in (None, "", "../ChromeHTML", "ChromeHTML\\shell", "a" * 201):
            registry = self.registry([(program_id, 1)])
            with self.subTest(program_id_type=type(program_id).__name__), \
                    patch.dict(sys.modules, {"winreg": registry}), \
                    patch("luvbox.browser_discovery.os", SimpleNamespace(name="nt", path=os.path)):
                self.assertIsNone(discovery.find_default_browser())
            self.assertEqual(registry.OpenKey.call_count, 1)
        registry = self.registry([])
        registry.OpenKey.side_effect = PermissionError("synthetic inaccessible association")
        with patch.dict(sys.modules, {"winreg": registry}), \
                patch("luvbox.browser_discovery.os", SimpleNamespace(name="nt", path=os.path)):
            self.assertIsNone(discovery.find_default_browser())


if __name__ == "__main__":
    unittest.main()

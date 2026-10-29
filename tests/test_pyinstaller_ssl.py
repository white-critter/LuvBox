"""Keep the build interpreter's optional SSL trust policy in frozen releases."""

from pathlib import Path
import runpy
import shutil
import ssl
import sys
import tempfile
import unittest
from unittest.mock import patch

from tools import package_release


ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "tools" / "pyinstaller" / "hook-ssl.py"


class PyInstallerSSLTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="luvbox-ssl-hook-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.standard_library = self.root / "Python" / "Lib"
        self.standard_library.mkdir(parents=True)
        self.ssl_module = self.standard_library / "ssl.py"
        self.ssl_module.write_text("# disposable interpreter fixture\n", encoding="utf-8")
        self.dlls = self.standard_library.parent / "DLLs"
        self.dlls.mkdir()
        for name in ("libssl-3-x64.dll", "libcrypto-3-x64.dll"):
            (self.dlls / name).write_bytes(b"synthetic interpreter library " + name.encode())

    def run_hook(self, platform="win32"):
        with patch.object(ssl, "__file__", str(self.ssl_module)), \
                patch.object(sys, "base_prefix", str(self.standard_library.parent)), \
                patch.object(sys, "platform", platform):
            return runpy.run_path(str(HOOK))

    def test_openssl_comes_from_the_interpreter_instead_of_path_tools(self):
        foreign = self.root / "unrelated-tool"
        foreign.mkdir()
        for name in ("libssl-3-x64.dll", "libcrypto-3-x64.dll"):
            (foreign / name).write_bytes(b"foreign same-named library")
        with patch.dict("os.environ", {"PATH": str(foreign)}):
            binaries = self.run_hook()["binaries"]
        self.assertEqual(binaries, [(str(self.dlls / name), ".")
                                    for name in ("libssl-3-x64.dll", "libcrypto-3-x64.dll")])

    def test_missing_interpreter_openssl_cannot_fall_back_to_path(self):
        (self.dlls / "libcrypto-3-x64.dll").unlink()
        with self.assertRaisesRegex(RuntimeError, "interpreter's OpenSSL DLLs are missing"):
            self.run_hook()

    def test_non_windows_hook_does_not_collect_windows_libraries(self):
        self.assertEqual(self.run_hook(platform="linux")["binaries"], [])

    def test_optional_ssl_policy_survives_at_frozen_ssl_sibling_path(self):
        policy = self.standard_library / "codex-rejected-cas.pem"
        original = b"synthetic rejection policy, preserved byte for byte\n"
        policy.write_bytes(original)

        data = self.run_hook()["datas"]
        self.assertEqual(data, [(str(policy), ".")])

        # PyInstaller puts frozen ssl.__file__ at its runtime root. Recreate
        # data collection and check the path SSLContext opens remains valid.
        runtime = self.root / "frozen" / "_internal"
        for source, destination in data:
            target = runtime / destination / Path(source).name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        frozen_ssl = runtime / "ssl.py"
        self.assertEqual(frozen_ssl.with_name(policy.name).read_bytes(), original)

    def test_standard_python_without_optional_policy_needs_no_data(self):
        self.assertEqual(self.run_hook()["datas"], [])

    def test_release_source_archive_contains_ssl_hook(self):
        relative = HOOK.relative_to(ROOT).as_posix()
        self.assertIn(relative, package_release.SOURCE_FILES)
        self.assertTrue(HOOK.is_file())


if __name__ == "__main__":
    unittest.main()

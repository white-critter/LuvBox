"""Keep the build interpreter's optional SSL trust policy in frozen releases."""

from pathlib import Path
import runpy
import shutil
import ssl
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

    def run_hook(self):
        with patch.object(ssl, "__file__", str(self.ssl_module)):
            return runpy.run_path(str(HOOK))

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

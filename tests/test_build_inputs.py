"""Offline build checks use disposable artifacts, never real tool execution."""

from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tools import check_build_inputs


class PythonSecurityBaselineTests(unittest.TestCase):
    def test_each_supported_series_rejects_the_previous_patch(self):
        for version in ((2, 7, 18), (3, 11, 99), (3, 12, 15), (3, 13, 17),
                        (3, 14, 9), (3, 15, 1)):
            with self.subTest(version=version), self.assertRaisesRegex(ValueError, "security baseline"):
                check_build_inputs.require_secure_python(version)

    def test_each_exact_baseline_later_patch_and_future_series_are_allowed(self):
        for version in ((3, 12, 16), (3, 12, 17), (3, 13, 18), (3, 13, 19),
                        (3, 14, 10), (3, 14, 11), (3, 15, 2), (3, 15, 3),
                        (3, 16, 0), (4, 0, 0)):
            with self.subTest(version=version):
                check_build_inputs.require_secure_python(version)

    def test_all_cli_modes_reject_old_runtime_before_accessing_assets(self):
        for arguments in ([], ["--assets-only"], ["--python-only"]):
            output = io.StringIO()
            with self.subTest(arguments=arguments), \
                    patch.object(check_build_inputs.sys, "version_info", (3, 12, 14)), \
                    patch.object(check_build_inputs, "require_inputs") as inputs, redirect_stderr(output):
                self.assertEqual(check_build_inputs.main(arguments), 1)
                inputs.assert_not_called()
            message = output.getvalue()
            self.assertIn("Python 3.12.14", message)
            self.assertIn("3.12.16", message)
            self.assertIn("recreate .venv", message)
            self.assertNotIn("Traceback", message)

    def test_python_only_accepts_secure_runtime_without_prepared_assets(self):
        output = io.StringIO()
        with patch.object(check_build_inputs.sys, "version_info", (3, 12, 16)), \
                patch.object(check_build_inputs, "require_inputs") as inputs, redirect_stdout(output):
            self.assertEqual(check_build_inputs.main(["--python-only"], root=Path("unprepared-inputs")), 0)
            inputs.assert_not_called()
        self.assertIn("Python security baseline verified", output.getvalue())


class BuildInputTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="luvbox-build-inputs-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        fonts = {}
        for name in ("Pretendard-Regular.ttf", "Pretendard-Bold.ttf", "OFL.txt"):
            data = ("synthetic " + name).encode()
            self.write("assets/fonts/Pretendard/" + name, data)
            fonts[name] = {"bytes": len(data), "sha256": self.digest(data)}
        self.write("assets/fonts/Pretendard/manifest.json", json.dumps({"files": fonts}).encode())
        source, runtime = b"synthetic upstream source", b"MZ synthetic Windows FFmpeg"
        self.write("tools/ffmpeg/ffmpeg-7.1.tar.xz", source)
        self.write("tools/ffmpeg/runtime/ffmpeg.exe", runtime)
        recipes = {}
        for name in ("build.py", "configure-flags.json"):
            data = ("synthetic recipe " + name).encode()
            self.write("tools/ffmpeg/" + name, data)
            recipes[name] = self.digest(data)
        manifest = {"source": {"sha256": self.digest(source)},
                    "runtime": {"sha256": self.digest(runtime), "size": len(runtime)},
                    "build_files": recipes}
        self.write("tools/ffmpeg/manifest.json", json.dumps(manifest).encode())
        self.write("tools/exiftool/runtime/exiftool.exe", b"MZ synthetic ExifTool")
        self.write("tools/exiftool/runtime/exiftool_files/perl.exe", b"MZ synthetic runtime")

    @staticmethod
    def digest(data):
        return hashlib.sha256(data).hexdigest()

    def write(self, relative, data):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def test_valid_pinned_inputs_pass_without_downloading_or_running_tools(self):
        with patch("urllib.request.urlopen", side_effect=AssertionError("network")) as network, \
                patch("subprocess.run", side_effect=AssertionError("tool execution")) as execute:
            self.assertEqual(check_build_inputs.check_inputs(self.root), [])
            check_build_inputs.require_inputs(self.root)
        network.assert_not_called()
        execute.assert_not_called()

    def test_pure_input_checks_remain_independent_of_the_host_python(self):
        with patch.object(check_build_inputs.sys, "version_info", (3, 12, 14)):
            self.assertEqual(check_build_inputs.check_inputs(self.root), [])
            check_build_inputs.require_inputs(self.root)

    def test_font_and_runtime_lfs_pointers_report_specific_repair(self):
        pointer = (b"version https://git-lfs.github.com/spec/v1\n"
                   b"oid sha256:" + b"0" * 64 + b"\nsize 1000\n")
        for relative in ("assets/fonts/Pretendard/Pretendard-Regular.ttf",
                         "assets/fonts/Pretendard/Pretendard-Bold.ttf",
                         "tools/ffmpeg/runtime/ffmpeg.exe"):
            self.write(relative, pointer)
        errors = check_build_inputs.check_inputs(self.root, assets_only=True)
        self.assertEqual(len(errors), 3)
        self.assertTrue(all("Git LFS pointer" in error and "git lfs pull" in error for error in errors))
        with self.assertRaisesRegex(ValueError, "Pretendard-Regular.ttf"):
            check_build_inputs.require_inputs(self.root)

    def test_changed_font_recipe_and_runtime_are_rejected_by_manifest(self):
        changed = ("assets/fonts/Pretendard/Pretendard-Bold.ttf",
                   "tools/ffmpeg/configure-flags.json", "tools/ffmpeg/runtime/ffmpeg.exe")
        for relative in changed:
            self.write(relative, b"modified fixture")
        errors = check_build_inputs.check_inputs(self.root)
        self.assertEqual(len(errors), len(changed))
        for relative in changed:
            self.assertTrue(any(relative in error and "SHA-256" in error for error in errors))

    def test_early_pass_allows_setup_inputs_but_complete_pass_reports_every_missing_input(self):
        (self.root / "tools/ffmpeg/ffmpeg-7.1.tar.xz").unlink()
        shutil.rmtree(self.root / "tools/exiftool/runtime")
        self.assertEqual(check_build_inputs.check_inputs(self.root, assets_only=True), [])
        errors = check_build_inputs.check_inputs(self.root)
        self.assertEqual(len(errors), 2)
        self.assertTrue(any("ffmpeg-7.1.tar.xz" in error for error in errors))
        self.assertTrue(any("ExifTool" in error or "exiftool/runtime" in error for error in errors))

    def test_incomplete_exiftool_bundle_is_not_ready(self):
        (self.root / "tools/exiftool/runtime/exiftool_files/perl.exe").unlink()
        self.write("tools/exiftool/runtime/exiftool_files/README.txt", b"a directory alone is insufficient")
        errors = check_build_inputs.check_inputs(self.root)
        self.assertEqual(len(errors), 1)
        self.assertIn("exiftool/runtime", errors[0])

    @unittest.skipUnless(shutil.which("git"), "Git checkout behavior")
    def test_pinned_input_line_endings_survive_both_autocrlf_settings(self):
        attributes = (Path(__file__).resolve().parents[1] / ".gitattributes").read_bytes()
        self.write(".gitattributes", attributes)
        recipes = ("tools/ffmpeg/build.py", "tools/ffmpeg/configure-flags.json")
        license_name = "assets/fonts/Pretendard/OFL.txt"
        names = (*recipes, license_name)
        for autocrlf in ("false", "true"):
            with self.subTest(autocrlf=autocrlf):
                for name in names:
                    self.write(name, b"first line\nsecond line\n")
                command = ["git", "-c", "core.autocrlf=" + autocrlf, "-C", str(self.root)]
                subprocess.run([*command, "init", "--quiet"], check=True, capture_output=True)
                subprocess.run([*command, "add", "--", ".gitattributes", *names], check=True, capture_output=True)
                for name in names:
                    (self.root / name).unlink()
                subprocess.run([*command, "checkout-index", "--all", "--force"], check=True, capture_output=True)
                for name in recipes:
                    self.assertEqual((self.root / name).read_bytes(), b"first line\r\nsecond line\r\n")
                self.assertEqual((self.root / license_name).read_bytes(), b"first line\nsecond line\n")

    @patch.object(check_build_inputs.sys, "version_info", (3, 12, 16))
    def test_invalid_manifest_and_cli_failure_are_clear_without_traceback(self):
        for name in ("assets/fonts/Pretendard/manifest.json", "tools/ffmpeg/manifest.json"):
            original = (self.root / name).read_bytes()
            for value in (b"not JSON", b"null", b"[]"):
                with self.subTest(name=name, value=value):
                    self.write(name, value)
                    output = io.StringIO()
                    with redirect_stderr(output):
                        self.assertEqual(check_build_inputs.main([], root=self.root), 1)
                    self.assertIn(name, output.getvalue())
                    self.assertNotIn("Traceback", output.getvalue())
            self.write(name, original)
        self.write("assets/fonts/Pretendard/manifest.json", b"{}")
        self.assertTrue(any("incomplete font metadata" in error
                            for error in check_build_inputs.check_inputs(self.root)))

    @patch.object(check_build_inputs.sys, "version_info", (3, 12, 16))
    def test_cli_success_is_read_only(self):
        before = {path.relative_to(self.root): path.read_bytes()
                  for path in self.root.rglob("*") if path.is_file()}
        with redirect_stdout(io.StringIO()):
            self.assertEqual(check_build_inputs.main([], root=self.root), 0)
        after = {path.relative_to(self.root): path.read_bytes()
                 for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()

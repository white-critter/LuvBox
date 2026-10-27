"""Check shipped browser assets and disposable release-manifest fixtures."""

from html.parser import HTMLParser
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tools import package_release


class _PageResources(HTMLParser):
    def __init__(self):
        super().__init__()
        self.files = set()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        resource = attrs.get("src") if tag == "script" else attrs.get("href") if tag == "link" else None
        if resource:
            self.files.add(resource)


class BrowserExtensionPackagingTests(unittest.TestCase):
    def test_manifests_and_connection_pages_have_all_resources_in_source_archive(self):
        root = Path(__file__).resolve().parents[1]
        shipped = set(package_release.SOURCE_FILES)
        self.assertEqual(len(shipped), len(package_release.SOURCE_FILES))
        for name in ("assets/browser-extension/README.md", "docs/browser-login.md",
                     "tests/browser_bridge_extension_harness.js"):
            self.assertIn(name, shipped)
            self.assertTrue((root / name).is_file(), name)
        for browser in ("chromium", "firefox"):
            with self.subTest(browser=browser):
                folder = f"assets/browser-extension/{browser}"
                manifest = json.loads((root / folder / "manifest.json").read_text(encoding="utf-8"))
                background = manifest["background"]
                resources = {"manifest.json", "connect.html", *background.get("scripts", [])}
                if "service_worker" in background:
                    resources.add(background["service_worker"])
                for script in manifest["content_scripts"]:
                    resources.update(script["js"])
                    resources.update(script.get("css", []))
                page = _PageResources()
                page.feed((root / folder / "connect.html").read_text(encoding="utf-8"))
                resources.update(page.files)
                for resource in resources:
                    name = f"{folder}/{resource}"
                    self.assertIn(name, shipped, name)
                    self.assertTrue((root / name).is_file(), name)

    def test_windows_build_bundles_each_extension_resource_and_login_guide_explicitly(self):
        root = Path(__file__).resolve().parents[1]
        build = (root / "build.ps1").read_text(encoding="utf-8-sig")
        data = {}
        for _, argument in re.findall(r"--add-data\s+(['\"])(.*?)\1", build):
            source, destination = argument.split(";", 1)
            data[source.replace("\\", "/").removeprefix("$PSScriptRoot/")] = destination.replace("\\", "/")
        required = [name for name in package_release.SOURCE_FILES
                    if name.startswith("assets/browser-extension/") or name == "docs/browser-login.md"]
        self.assertGreater(len(required), 2)
        for name in required:
            self.assertEqual(data.get(name), Path(name).parent.as_posix(), name)
        self.assertNotIn("assets/browser-extension", data)  # Never bundle unknown files recursively.


class QtOnlyPackagingTests(unittest.TestCase):
    def test_build_uses_only_current_ui_runtime_and_assets(self):
        root = Path(__file__).resolve().parents[1]
        build = (root / "build.ps1").read_text(encoding="utf-8-sig")
        self.assertIn("--exclude-module tkinter", build)
        self.assertIn("--exclude-module _tkinter", build)
        self.assertIn("--exclude-module PIL._imagingtk", build)
        self.assertNotIn("--collect-binaries PIL", build)
        self.assertNotIn("assets\\theme", build)
        self.assertNotIn("build_theme.py", build)
        self.assertFalse(any("assets/theme" in name or name.endswith("build_theme.py")
                             for name in package_release.SOURCE_FILES))


class BuildInputPackagingTests(unittest.TestCase):
    def test_build_checks_lfs_before_prepare_then_complete_inputs_before_pyinstaller(self):
        root = Path(__file__).resolve().parents[1]
        build = (root / "build.ps1").read_text(encoding="utf-8-sig")
        early = build.index("'tools\\check_build_inputs.py' '--assets-only'")
        prepare = build.index("'tools\\exiftool\\download.py'")
        complete = build.index("'tools\\check_build_inputs.py'\n")
        self.assertLess(early, prepare)
        self.assertLess(prepare, complete)
        self.assertLess(complete, build.index("-m PyInstaller"))
        self.assertIn("tools/check_build_inputs.py", package_release.SOURCE_FILES)
        self.assertIn(".gitattributes", package_release.SOURCE_FILES)
        self.assertTrue((root / "tools/check_build_inputs.py").is_file())
        # Python test files are shipped by the existing tests/*.py allowlist.
        self.assertTrue((root / "tests/test_build_inputs.py").is_file())

    def test_build_records_inputs_before_pyinstaller_and_seals_before_startup(self):
        root = Path(__file__).resolve().parents[1]
        build = (root / "build.ps1").read_text(encoding="utf-8-sig")
        record = build.index("'tools\\build_snapshot.py' 'record'")
        freeze = build.index("-m PyInstaller")
        seal = build.index("'tools\\build_snapshot.py' 'seal'")
        startup = build.index("'tools\\check_qt_startup.py'")
        self.assertLess(record, freeze)
        self.assertLess(freeze, seal)
        self.assertLess(seal, startup)
        self.assertIn("--specpath build\\spec", build)
        self.assertIn("tools/build_snapshot.py", package_release.SOURCE_FILES)

    def test_package_refuses_missing_pinned_inputs_before_prepare_or_output(self):
        with tempfile.TemporaryDirectory(prefix="luvbox-package-preflight-") as folder:
            root = Path(folder)
            with patch.object(package_release, "ROOT", root), \
                    patch.object(package_release, "require_inputs", side_effect=ValueError("missing pinned inputs")) as check, \
                    patch.object(package_release.subprocess, "run") as prepare:
                with self.assertRaisesRegex(ValueError, "missing pinned inputs"):
                    package_release.package()
            check.assert_called_once_with(root)
            prepare.assert_not_called()
            self.assertFalse((root / "release").exists())


class StartupReportPackagingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="luvbox-startup-package-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        root = patch.object(package_release, "ROOT", self.root)
        root.start()
        self.addCleanup(root.stop)
        self.executable = self.root / "LuvBox-test.exe"
        self.executable.write_bytes(b"synthetic frozen executable")
        self.report_path = self.root / "build" / f"qt-startup-{package_release.__version__}.json"
        self.report_path.parent.mkdir()
        self.download_path = self.report_path.with_name(f"download-startup-{package_release.__version__}.json")

    def write_report(self, **changes):
        report = {
            "ok": True,
            "frozen": True,
            "isolated": True,
            "window_visible": True,
            "window_exposed": True,
            "gallery_thumbnail_ready": True,
            "photo_preview_ready": True,
            "settings_and_confirmation": True,
            "connection_dialog": True,
            "shutdown_ready": True,
            "temporary_data_removed": True,
            "platform": "windows",
            "qml_errors": [],
            "version": package_release.__version__,
            "executable_sha256": hashlib.sha256(self.executable.read_bytes()).hexdigest(),
        }
        report.update(changes)
        self.report_path.write_text(json.dumps(report), encoding="utf-8")
        download = {"ok": True, "frozen": True, "isolated": True, "managed_cookies": True,
                    "authentication_or_network": False, "prepared_requests": 1,
                    "extractor": "TwitterLikesExtractor", "version": package_release.__version__,
                    "executable_sha256": hashlib.sha256(self.executable.read_bytes()).hexdigest()}
        self.download_path.write_text(json.dumps(download), encoding="utf-8")

    def test_missing_startup_report_blocks_packaging(self):
        with self.assertRaisesRegex(ValueError, "startup check before packaging"):
            package_release._require_startup_report(self.executable)

    def test_malformed_startup_report_blocks_packaging(self):
        self.report_path.write_text("not JSON", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "startup check before packaging"):
            package_release._require_startup_report(self.executable)

    def test_failed_or_unfrozen_startup_report_blocks_packaging(self):
        for changes in ({"ok": False}, {"frozen": False}):
            with self.subTest(changes=changes):
                self.write_report(**changes)
                with self.assertRaisesRegex(ValueError, "did not pass"):
                    package_release._require_startup_report(self.executable)

    def test_another_version_startup_report_blocks_packaging(self):
        self.write_report(version="0.0.0")
        with self.assertRaisesRegex(ValueError, "this version"):
            package_release._require_startup_report(self.executable)

    def test_executable_changed_since_startup_report_blocks_packaging(self):
        self.write_report()
        self.executable.write_bytes(b"rebuilt executable after startup check")
        with self.assertRaisesRegex(ValueError, "executable changed"):
            package_release._require_startup_report(self.executable)

    def test_successful_startup_report_for_exact_executable_allows_packaging(self):
        self.write_report()
        package_release._require_startup_report(self.executable)

    def test_direct_packaging_rejects_incomplete_or_failed_qt_checks(self):
        changes = [{name: False} for name in (
            "isolated", "window_visible", "window_exposed", "gallery_thumbnail_ready",
            "photo_preview_ready", "settings_and_confirmation", "connection_dialog",
            "shutdown_ready", "temporary_data_removed",
        )]
        changes += [{"qml_errors": ["missing dependency"]}, {"qml_errors": None},
                    {"error": "startup failed"}]
        if os.name == "nt":
            changes.append({"platform": "offscreen"})
        for change in changes:
            with self.subTest(change=change):
                self.write_report(**change)
                with self.assertRaisesRegex(ValueError, "did not pass"):
                    package_release._require_startup_report(self.executable)

    def test_direct_packaging_rejects_a_legacy_summary_without_lifecycle_results(self):
        self.write_report()
        report = json.loads(self.report_path.read_text(encoding="utf-8"))
        report = {name: report[name] for name in ("ok", "frozen", "version", "executable_sha256")}
        self.report_path.write_text(json.dumps(report), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "did not pass"):
            package_release._require_startup_report(self.executable)

    def test_qt_success_cannot_bypass_missing_download_startup_check(self):
        self.write_report()
        self.download_path.unlink()
        with self.assertRaisesRegex(ValueError, "download startup check before packaging"):
            package_release._require_startup_report(self.executable)

    def test_failed_or_stale_download_report_blocks_direct_packaging(self):
        for change in ({"ok": False}, {"authentication_or_network": True},
                       {"prepared_requests": True}, {"version": "0.0.0"},
                       {"executable_sha256": "another executable"}):
            with self.subTest(change=change):
                self.write_report()
                report = json.loads(self.download_path.read_text(encoding="utf-8"))
                self.download_path.write_text(json.dumps({**report, **change}), encoding="utf-8")
                with self.assertRaises(ValueError):
                    package_release._require_startup_report(self.executable)


class ReleaseManifestTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="luvbox-package-manifest-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        root = patch.object(package_release, "ROOT", self.root)
        root.start()
        self.addCleanup(root.stop)
        self.name = "LuvBox-test"
        self.application = self.root / "release" / self.name
        self.application.mkdir(parents=True)
        self.toc = self.root / "build" / "onedir" / self.name / "COLLECT-00.toc"
        self.toc.parent.mkdir(parents=True)
        self.entries = [
            (self.name + ".exe", str(self.toc.parent / (self.name + ".exe")), "EXECUTABLE"),
            ("PIL\\_imaging.pyd", str(self.root / "dependency" / "_imaging.pyd"), "EXTENSION"),
            ("tools\\ffmpeg\\runtime\\ffmpeg.exe", str(self.root / "tools/ffmpeg/runtime/ffmpeg.exe"), "BINARY"),
            ("tools\\exiftool\\runtime\\exiftool_files\\t\\images\\Geotag.log",
             str(self.root / "tools/exiftool/runtime/exiftool_files/t/images/Geotag.log"), "DATA"),
        ]
        self.write_toc()
        for destination, source, kind in self.entries:
            relative = Path(*destination.split("\\"))
            if kind != "EXECUTABLE":
                relative = Path("_internal") / relative
            path = self.application / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"synthetic built file")
            source = Path(source)
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(b"synthetic build source")

    def write_toc(self, entries=None):
        self.toc.write_text(repr((self.entries if entries is None else entries,)), encoding="utf-8")

    def manifest(self):
        return package_release._collect_manifest(self.toc, self.name)

    def test_literal_manifest_matches_windows_onedir_layout_and_keeps_fixture_logs(self):
        manifest = self.manifest()
        files = package_release._verified_files(self.application, manifest)
        self.assertEqual(len(files), 4)
        self.assertIn(self.application / (self.name + ".exe"), files)
        self.assertIn(self.application / "_internal/tools/exiftool/runtime/exiftool_files/t/images/Geotag.log", files)
        for component in ("ffmpeg", "exiftool"):
            self.assertEqual(len(package_release._runtime_files(manifest, component)), 1)

    def test_build_through_another_path_to_the_project_still_finds_bundled_tools(self):
        # A junction, substituted drive or mapped drive gives PyInstaller a
        # different spelling of the same folder than the resolved ROOT.
        alias = self.root.parent / (self.root.name + "-alias")
        try:
            os.symlink(self.root, alias, target_is_directory=True)
        except (OSError, NotImplementedError):
            # Symbolic links need a Windows privilege; a junction does not.
            try:
                import _winapi
                _winapi.CreateJunction(str(self.root), str(alias))
            except (ImportError, OSError):
                self.skipTest("directory links are not available")
        self.addCleanup(os.rmdir if os.name == "nt" else Path.unlink, alias)
        entries = [(name, source.replace(str(self.root), str(alias), 1), kind)
                   for name, source, kind in self.entries]
        self.write_toc(entries)
        manifest = self.manifest()
        self.assertFalse(any(source.is_relative_to(self.root) for source in manifest.values()))
        for component in ("ffmpeg", "exiftool"):
            self.assertEqual(len(package_release._runtime_files(manifest, component)), 1)
        # A manifest that names no bundled tool at all is still refused.
        self.write_toc([entry for entry in self.entries if "runtime" not in entry[0]])
        with self.assertRaisesRegex(ValueError, "missing the bundled media tools"):
            package_release._runtime_files(self.manifest(), "ffmpeg")

    def test_unexpected_personal_file_in_application_or_source_runtime_blocks_release(self):
        manifest = self.manifest()
        for directory, expected in ((self.application, manifest),
                                    (self.root / "tools/exiftool/runtime", [Path("exiftool_files/t/images/Geotag.log")])):
            with self.subTest(directory=directory.name):
                extra = directory / "personal-session.json"
                extra.write_bytes(b"synthetic private fixture")
                try:
                    with self.assertRaisesRegex(ValueError, "absent from the build manifest"):
                        package_release._verified_files(directory, expected)
                finally:
                    extra.unlink()

    def test_missing_built_file_blocks_release(self):
        (self.application / "_internal/PIL/_imaging.pyd").unlink()
        with self.assertRaisesRegex(ValueError, "missing files"):
            package_release._verified_files(self.application, self.manifest())

    def test_manifest_is_literal_data_and_cannot_execute_python(self):
        marker = self.root / "executed.txt"
        self.toc.write_text(f"__import__('pathlib').Path({str(marker)!r}).write_text('not allowed')", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.manifest()
        self.assertFalse(marker.exists())

    def test_malformed_manifest_or_unsupported_entry_blocks_release(self):
        for value in ([], (), ([], []), ({},), ([None],), ([('name', 'relative-source', 'DATA')],),
                      ([('name', str(self.root / 'input'), 'SYMLINK')],)):
            with self.subTest(value=value):
                self.toc.write_text(repr(value), encoding="utf-8")
                with self.assertRaises(ValueError):
                    self.manifest()

    def test_foreign_icu_runtime_cannot_shadow_windows_qt_dependency(self):
        for name in ("icuuc.dll", "ICUDT78.DLL", "PySide6\\ICUUC.dll"):
            with self.subTest(name=name):
                self.write_toc([*self.entries, (name, str(self.root / "foreign-icu.dll"), "BINARY")])
                with self.assertRaisesRegex(ValueError, "unexpected ICU runtime"):
                    self.manifest()

    def test_removed_ui_runtime_cannot_return_through_dependencies(self):
        for name in ("_tkinter.pyd", "tcl86t.dll", "tk86t.dll", "_tk_data/init.tcl",
                     "_tcl_data/encoding/ascii.enc", "PIL/_imagingtk.cp312-win_amd64.pyd"):
            with self.subTest(name=name):
                self.write_toc([*self.entries, (name, str(self.root / "obsolete-input"), "DATA")])
                with self.assertRaisesRegex(ValueError, "removed UI runtime"):
                    self.manifest()

    def test_destination_escape_and_windows_aliases_are_rejected(self):
        names = ("../private.txt", "sub/../../private.txt", "C:\\private.txt", "C:private.txt",
                 "\\\\server\\share\\private.txt", "/private.txt", "name:stream", "name.",
                 "name ", "folder//file", "folder/./file", "NUL.txt")
        for name in names:
            with self.subTest(name=name):
                self.write_toc([*self.entries, (name, str(self.root / "input"), "DATA")])
                with self.assertRaises(ValueError):
                    self.manifest()

    def test_duplicate_windows_destinations_and_wrong_executable_are_rejected(self):
        for entry in (("pil/_IMAGING.pyd", str(self.root / "input"), "DATA"),
                      ("another.exe", str(self.root / "input"), "EXECUTABLE")):
            with self.subTest(entry=entry):
                self.write_toc([*self.entries, entry])
                with self.assertRaises(ValueError):
                    self.manifest()
        self.write_toc(self.entries[1:])
        with self.assertRaises(ValueError):
            self.manifest()

    def test_input_directories_and_manifest_cannot_escape_project_with_dotdot(self):
        for directory in (self.root.parent, self.root / ".."):
            with self.assertRaises(ValueError):
                package_release._verified_files(directory, [])
        with self.assertRaises(ValueError):
            package_release._collect_manifest(self.root / "../COLLECT-00.toc", self.name)

    def test_hardlinked_expected_file_is_rejected(self):
        target = self.application / "_internal/PIL/_imaging.pyd"
        outside = self.root / "outside-private.bin"
        outside.write_bytes(b"private synthetic fixture")
        target.unlink()
        try:
            os.link(outside, target)
        except OSError:
            self.skipTest("Hard links unavailable in disposable fixture")
        with self.assertRaisesRegex(ValueError, "linked files"):
            package_release._verified_files(self.application, self.manifest())
        self.assertEqual(outside.read_bytes(), b"private synthetic fixture")

    def test_symbolic_file_link_is_rejected_even_when_its_name_is_expected(self):
        target = self.application / "_internal/PIL/_imaging.pyd"
        outside = self.root / "outside-private.bin"
        outside.write_bytes(b"private synthetic fixture")
        target.unlink()
        try:
            target.symlink_to(outside)
        except OSError:
            self.skipTest("Symbolic links unavailable in disposable fixture")
        with self.assertRaisesRegex(ValueError, "linked files"):
            package_release._verified_files(self.application, self.manifest())

    @unittest.skipUnless(os.name == "nt", "Windows junction fixture")
    def test_junction_is_rejected_before_descending_into_its_target(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "private.txt").write_bytes(b"private synthetic fixture")
        linked = self.application / "_internal" / "redirected"
        result = subprocess.run(["cmd.exe", "/c", "mklink", "/J", str(linked), str(outside)],
                                capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode:
            self.skipTest("Cannot create a private junction fixture")
        try:
            with self.assertRaisesRegex(ValueError, "linked files"):
                package_release._verified_files(self.application, self.manifest())
        finally:
            os.rmdir(linked)
        self.assertEqual(list(outside.iterdir()), [outside / "private.txt"])


if __name__ == "__main__":
    unittest.main()

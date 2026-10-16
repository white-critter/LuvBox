"""Release input validation uses only disposable build-manifest fixtures."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tools import package_release


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
            self.skipTest("directory links are not available")
        self.addCleanup(alias.unlink)
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

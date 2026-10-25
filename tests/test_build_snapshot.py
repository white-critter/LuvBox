"""A release cannot pair a stale executable with newly edited project sources."""

import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

from tools import build_snapshot, package_release


class BuildSnapshotTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="luvbox-build-snapshot-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        source_files = patch.object(package_release, "SOURCE_FILES", ("entry.py",))
        source_files.start()
        self.addCleanup(source_files.stop)
        dependencies = patch.object(package_release, "DEPENDENCIES", ("synthetic-dependency",))
        dependencies.start()
        self.addCleanup(dependencies.stop)
        versions = patch.object(build_snapshot.importlib.metadata, "version", return_value="1.0")
        self.versions = versions.start()
        self.addCleanup(versions.stop)
        for name in ("entry.py", "luvbox/core.py", "assets/qt/Main.qml", "docs/사용안내.txt",
                     "docs/development.md", "docs/verification.md", "docs/performance.md", "docs/security-review.md"):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("original project input", encoding="utf-8")
        self.executable = self.root / "release" / "LuvBox-test.exe"
        self.executable.parent.mkdir()
        self.executable.write_bytes(b"synthetic frozen executable")

    def complete_build(self):
        build_snapshot.record(self.root)
        build_snapshot.seal(self.executable, self.root)

    def test_unchanged_completed_build_can_be_packaged(self):
        self.complete_build()
        build_snapshot.require_snapshot(self.executable, self.root)

    def test_changed_python_qml_or_shipped_document_requires_rebuild(self):
        for name in ("luvbox/core.py", "assets/qt/Main.qml", "docs/development.md"):
            with self.subTest(name=name):
                self.complete_build()
                path = self.root / name
                original = path.read_bytes()
                path.write_bytes(b"edited after PyInstaller finished")
                with self.assertRaisesRegex(ValueError, "sources, build inputs or dependencies changed"):
                    build_snapshot.require_snapshot(self.executable, self.root)
                path.write_bytes(original)

    def test_source_change_during_build_cannot_be_sealed(self):
        build_snapshot.record(self.root)
        (self.root / "luvbox/core.py").write_text("changed while building", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "rebuild"):
            build_snapshot.seal(self.executable, self.root)

    def test_direct_packaging_refuses_stale_executable_before_startup_or_archive(self):
        name = f"LuvBox-{build_snapshot.__version__}"
        self.executable = self.root / "release" / name / f"{name}.exe"
        self.executable.parent.mkdir()
        self.executable.write_bytes(b"synthetic frozen executable")
        self.complete_build()
        (self.root / "luvbox/core.py").write_text("new code with the same version", encoding="utf-8")
        with patch.object(package_release, "ROOT", self.root), \
                patch.object(package_release, "require_inputs"), \
                patch.object(package_release.subprocess, "run"), \
                patch.object(package_release, "_require_startup_report") as startup:
            with self.assertRaisesRegex(ValueError, "sources, build inputs or dependencies changed"):
                package_release.package()
        startup.assert_not_called()
        self.assertFalse((self.root / "release" / f"{name}.zip").exists())

    def test_changes_during_compression_preserve_previous_zip_and_remove_staging(self):
        name = f"LuvBox-{build_snapshot.__version__}"
        self.executable = self.root / "release" / name / f"{name}.exe"
        self.executable.parent.mkdir()
        self.executable.write_bytes(b"synthetic frozen executable")
        runtime = self.executable.parent / "_internal/Qt6Core.dll"
        runtime.parent.mkdir()
        runtime.write_bytes(b"built runtime input")
        manifest = {Path(self.executable.name): self.executable,
                    Path("_internal/Qt6Core.dll"): runtime}
        for relative in ("LICENSE", "THIRD_PARTY_NOTICES.md", "CHANGELOG.md",
                         "assets/qt/licenses/GPL-2.0-only.txt", "assets/qt/licenses/GPL-3.0-only.txt",
                         "assets/qt/licenses/LGPL-3.0-only.txt", "assets/fonts/Pretendard/OFL.txt",
                         "tools/ffmpeg/LICENSE.LGPLv2.1.txt", "tools/ffmpeg/COPYING.MinGW-w64-runtime.txt",
                         "tools/ffmpeg/COPYING.GCC-runtime-exception.txt", "tools/ffmpeg/ffmpeg-7.1.tar.xz"):
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"synthetic redistribution material")
        destination = self.root / "release" / f"{name}.zip"
        checksums = self.root / "release" / f"SHA256-{build_snapshot.__version__}.txt"
        distribution = SimpleNamespace(_path=self.root / "metadata",
                                       locate_file=lambda _: self.root / "gallery_dl")
        write = zipfile.ZipFile.write
        for changed in (self.root / "luvbox/core.py", runtime):
            with self.subTest(changed=changed.name):
                self.complete_build()
                original = changed.read_bytes()
                destination.write_bytes(b"previous complete release")
                checksums.write_bytes(b"previous checksums")

                def edit_during_write(archive, filename, arcname=None, **kwargs):
                    result = write(archive, filename, arcname, **kwargs)
                    if Path(filename) == self.executable:
                        changed.write_bytes(b"edited while ZIP compression is running")
                    return result

                with patch.object(package_release, "ROOT", self.root), \
                        patch.object(package_release, "require_inputs"), \
                        patch.object(package_release.subprocess, "run"), \
                        patch.object(package_release, "_require_startup_report"), \
                        patch.object(package_release, "_collect_manifest", return_value=manifest), \
                        patch.object(package_release, "_runtime_files", return_value=[]), \
                        patch.object(package_release, "qt_source_files", return_value=[]), \
                        patch.object(package_release.importlib.metadata, "distribution", return_value=distribution), \
                        patch.object(zipfile.ZipFile, "write", edit_during_write):
                    with self.assertRaisesRegex(ValueError, "rebuild"):
                        package_release.package()
                self.assertEqual(destination.read_bytes(), b"previous complete release")
                self.assertEqual(checksums.read_bytes(), b"previous checksums")
                self.assertEqual(list(destination.parent.glob(".release-*.zip")), [])
                changed.write_bytes(original)

    def test_new_python_module_and_dependency_version_are_detected(self):
        self.complete_build()
        (self.root / "luvbox/new_module.py").write_text("new code", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "rebuild"):
            build_snapshot.require_snapshot(self.executable, self.root)
        (self.root / "luvbox/new_module.py").unlink()
        self.versions.return_value = "2.0"
        with self.assertRaisesRegex(ValueError, "rebuild"):
            build_snapshot.require_snapshot(self.executable, self.root)

    def test_changed_bundled_qml_or_dll_is_rejected(self):
        for name in ("_internal/assets/qt/Main.qml", "_internal/Qt6Core.dll"):
            with self.subTest(name=name):
                runtime = self.executable.parent / name
                runtime.parent.mkdir(parents=True, exist_ok=True)
                runtime.write_bytes(b"built runtime input")
                self.complete_build()
                runtime.write_bytes(b"changed after sealing")
                with self.assertRaisesRegex(ValueError, "runtime changed after sealing"):
                    build_snapshot.require_snapshot(self.executable, self.root)
                runtime.write_bytes(b"built runtime input")

    def test_added_or_missing_runtime_file_is_rejected(self):
        runtime = self.executable.parent / "_internal/Qt6Core.dll"
        runtime.parent.mkdir()
        runtime.write_bytes(b"built runtime input")
        self.complete_build()
        extra = runtime.with_name("personal.txt")
        extra.write_bytes(b"not part of the completed build")
        with self.assertRaisesRegex(ValueError, "runtime changed after sealing"):
            build_snapshot.require_snapshot(self.executable, self.root)
        extra.unlink()
        runtime.unlink()
        with self.assertRaisesRegex(ValueError, "runtime changed after sealing"):
            build_snapshot.require_snapshot(self.executable, self.root)

    def test_linked_runtime_file_is_rejected_at_seal(self):
        private = self.root / "private.txt"
        private.write_bytes(b"private material")
        os.link(private, self.executable.parent / "personal.txt")
        build_snapshot.record(self.root)
        with self.assertRaisesRegex(ValueError, "linked files"):
            build_snapshot.seal(self.executable, self.root)

    def test_unsealed_record_and_replaced_executable_are_rejected(self):
        build_snapshot.record(self.root)
        with self.assertRaisesRegex(ValueError, "sealed build input record"):
            build_snapshot.require_snapshot(self.executable, self.root)
        build_snapshot.seal(self.executable, self.root)
        self.executable.write_bytes(b"different executable")
        with self.assertRaisesRegex(ValueError, "sealed build input record"):
            build_snapshot.require_snapshot(self.executable, self.root)

    def test_sealed_record_cannot_be_rebound_to_an_old_executable(self):
        self.complete_build()
        with self.assertRaisesRegex(ValueError, "already sealed"):
            build_snapshot.seal(self.executable, self.root)

    def test_malformed_and_missing_records_require_a_real_build(self):
        with self.assertRaisesRegex(ValueError, "before building"):
            build_snapshot.require_snapshot(self.executable, self.root)
        build_snapshot.record(self.root)
        build_snapshot._path(self.root).write_text(json.dumps([]), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "record is invalid"):
            build_snapshot.require_snapshot(self.executable, self.root)

    def test_allowlisted_source_cannot_be_a_hardlink(self):
        outside = self.root / "private.txt"
        outside.write_text("private material", encoding="utf-8")
        source = self.root / "entry.py"
        source.unlink()
        os.link(outside, source)
        with self.assertRaisesRegex(ValueError, "linked files"):
            build_snapshot.record(self.root)
        with self.assertRaisesRegex(ValueError, "linked files"):
            package_release.project_source_files(self.root)

    def test_allowlisted_source_parent_cannot_redirect_outside_project(self):
        with tempfile.TemporaryDirectory(prefix="luvbox-outside-sources-") as folder:
            outside = Path(folder)
            (outside / "core.py").write_text("private material", encoding="utf-8")
            source = self.root / "luvbox"
            (source / "core.py").unlink()
            source.rmdir()
            try:
                source.symlink_to(outside, target_is_directory=True)
            except OSError:
                self.skipTest("Directory symlinks are unavailable")
            with self.assertRaisesRegex(ValueError, "linked files"):
                package_release.project_source_files(self.root)


if __name__ == "__main__":
    unittest.main()

"""Publish a release from the explicit allowlist, never personal account data.

A release is three files in `release/`:

    LuvBox.exe                           the launcher with the application appended
    SHA256-<version>.txt                 checksums of the other two
    LuvBox-<version>-source-licenses.zip corresponding sources, license texts, the guide

Installed versions fetch the first two by exactly these names (luvbox/updater.py
owns the names and the checksum format), so they are not this module's to change.
"""

import ast
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path, PureWindowsPath
import re
import shutil
import stat
import sys
import subprocess
import tempfile
import zipfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from luvbox import __version__  # noqa: E402
from luvbox.updater import format_checksums, release_assets  # noqa: E402
from tools.check_build_inputs import require_inputs  # noqa: E402
from tools.qt.prepare_sources import release_files as qt_source_files
from tools.check_download_startup import validate_report as validate_download_report
from tools.check_qt_startup import validate_report as validate_qt_report
from tools.build_snapshot import require_snapshot
from tools import build_launcher

SOURCE_FILES = (
    "assets/icons/luvbox.png", "assets/icons/luvbox.ico", "assets/icons/README.md", "tools/build_icon.py",
    "tools/draw_icon.py",
    "tools/draw_splash.py", "tools/build_launcher.py", "tools/launcher/Launcher.cs", "tools/launcher/app.manifest",
    "tools/launcher/splash.ini", "assets/splash/card.png", "assets/splash/glow.png", "assets/splash/mark.png",
    "assets/splash/README.md",
    "entry.py", "setup.ps1", "build.ps1", "run.bat", "tools/windows_version.py", "tools/check_qt_startup.py", "tools/package_browser_extension.py", ".gitattributes", ".gitignore", "requirements.txt",
    "requirements-dev.txt", "requirements-lock.txt", "requirements-qt.txt", "README.md", "WINDOWS_CONTINUE.md", "LICENSE",
    "CHANGELOG.md", "tools/qt_gallery_preview.py",
    "tools/pyinstaller/hook-PySide6.py", "tools/pyinstaller/hook-PySide6.QtQml.py",
    "tools/pyinstaller/hook-PySide6.QtGui.py",
    "tools/pyinstaller/hook-ssl.py",
    "tools/python/README.md", "tools/python/PYTHON.json",
    "tools/python/licenses/LICENSE.bdb.txt", "tools/python/licenses/LICENSE.bzip2.txt",
    "tools/python/licenses/LICENSE.cpython.txt", "tools/python/licenses/LICENSE.expat.txt",
    "tools/python/licenses/LICENSE.libedit.txt", "tools/python/licenses/LICENSE.libffi.txt",
    "tools/python/licenses/LICENSE.liblzma.txt", "tools/python/licenses/LICENSE.libuuid.txt",
    "tools/python/licenses/LICENSE.libX11.txt", "tools/python/licenses/LICENSE.libXau.txt",
    "tools/python/licenses/LICENSE.libxcb.txt", "tools/python/licenses/LICENSE.mpdecimal.txt",
    "tools/python/licenses/LICENSE.ncurses.txt", "tools/python/licenses/LICENSE.openssl-3.txt",
    "tools/python/licenses/LICENSE.sqlite.txt", "tools/python/licenses/LICENSE.tcl.txt",
    "tools/python/licenses/LICENSE.tix.txt", "tools/python/licenses/LICENSE.zlib-ng.txt",
    "tools/python/licenses/LICENSE.zlib.txt", "tools/python/licenses/LICENSE.zstd.txt",
    "tools/check_download_startup.py",
    "tools/qt/prepare_sources.py", "tools/qt/sources.json", "tools/qt/README.md",
    "assets/qt/licenses/GPL-2.0-only.txt", "assets/qt/licenses/GPL-3.0-only.txt",
    "assets/qt/licenses/LGPL-3.0-only.txt",
    "THIRD_PARTY_NOTICES.md", "LuvBox.code-workspace",
    "tools/exiftool/download.py", "tools/package_release.py", "tools/check_build_inputs.py", "tools/build_snapshot.py",
    "tools/benchmark_history.py", "tools/benchmark_preview.py",
    "tools/bench_clip.py", "tools/clip/download.py", "tools/clip/README.md",
    "tests/browser_bridge_extension_harness.js",
    "tools/ffmpeg/build.py", "tools/ffmpeg/README.md", "tools/ffmpeg/manifest.json",
    "tools/ffmpeg/configure-flags.json", "tools/ffmpeg/build-info.txt", "tools/ffmpeg/LICENSE.LGPLv2.1.txt",
    "tools/ffmpeg/COPYING.MinGW-w64-runtime.txt", "tools/ffmpeg/COPYING.GCC-runtime-exception.txt",
    "assets/fonts/Pretendard/Pretendard-Regular.ttf", "assets/fonts/Pretendard/Pretendard-Bold.ttf",
    "assets/fonts/Pretendard/OFL.txt", "assets/fonts/Pretendard/manifest.json",
    "assets/browser-extension/README.md", "assets/browser-extension/PRIVACY.md", "docs/browser-extension-store.md",
    "assets/browser-extension/chromium/manifest.json", "assets/browser-extension/chromium/background.js",
    "assets/browser-extension/chromium/profile.js", "assets/browser-extension/chromium/connect.js",
    "assets/browser-extension/chromium/connect.html", "assets/browser-extension/chromium/connect.css",
    "assets/browser-extension/chromium/extension-page.js",
    "assets/browser-extension/firefox/manifest.json", "assets/browser-extension/firefox/background.js",
    "assets/browser-extension/firefox/profile.js", "assets/browser-extension/firefox/connect.js",
    "assets/browser-extension/firefox/connect.html", "assets/browser-extension/firefox/connect.css",
    "assets/browser-extension/firefox/extension-page.js", "docs/browser-login.md", "docs/windows-handoff.md",
)
DEPENDENCIES = (
    "gallery_dl", "pillow", "requests", "urllib3", "certifi", "charset-normalizer", "idna",
    "pyinstaller", "pyinstaller-hooks-contrib", "altgraph", "packaging", "pefile",
    "pywin32-ctypes", "setuptools",
    "PySide6", "PySide6_Essentials", "PySide6_Addons", "shiboken6",
    "onnxruntime", "numpy",
)
EXCLUDED_QT_COMPONENTS = {
    "qt6quick3dutils.dll", "qt6virtualkeyboard.dll", "qt6pdf.dll", "qt6pdfwidgets.dll",
    "qpdf.dll", "qtvirtualkeyboardplugin.dll", "qtquick3d.pyd", "qtvirtualkeyboard.pyd",
    "qtpdf.pyd", "qtpdfwidgets.pyd", "opengl32sw.dll",
}


def _plain_entry(path: Path):
    details = path.lstat()
    if (stat.S_ISLNK(details.st_mode) or getattr(details, "st_file_attributes", 0) & 0x400 or
            getattr(path, "is_junction", lambda: False)() or
            (stat.S_ISREG(details.st_mode) and details.st_nlink > 1)):
        raise ValueError("Release inputs must not contain linked files or directories")
    if not (stat.S_ISREG(details.st_mode) or stat.S_ISDIR(details.st_mode)):
        raise ValueError("Release inputs must be regular files or directories")
    return details


def project_file(path: Path, root: Path):
    """Reject redirects in an allowlisted project's file or its parent path."""
    if not path.is_relative_to(root) or ".." in path.relative_to(root).parts:
        raise ValueError("Release inputs must be inside the project")
    for parent in (path, *path.parents):
        _plain_entry(parent)
        if parent == root:
            break


def project_source_files(root: Path) -> list[Path]:
    paths = {root / name for name in SOURCE_FILES}
    paths.update(root / name for name in (
        "docs/사용안내.txt", "docs/development.md", "docs/verification.md",
        "docs/performance.md", "docs/security-review.md",
    ))
    for folder, pattern in (("luvbox", "*.py"), ("tests", "*.py"), (".vscode", "*.json")):
        paths.update((root / folder).glob(pattern))
    paths.update((root / "assets/qt").rglob("*.qml"))
    for path in paths:
        project_file(path, root)
        if not path.is_file():
            raise ValueError("Release source inputs must be regular files")
    return sorted(paths)


def _python_materials(root: Path) -> list[tuple[Path, str]]:
    """Only the explicitly retained upstream inventory; never scan a runtime."""
    materials = []
    for name in SOURCE_FILES:
        if name.startswith("tools/python/"):
            path = root / name
            project_file(path, root)
            if not path.is_file():
                raise ValueError("Python upstream materials must be regular files")
            materials.append((path, "licenses/Python-upstream/" + path.name))
    return materials


def _collect_manifest(toc: Path, application_name: str) -> dict[Path, Path]:
    """Read PyInstaller's literal COLLECT tuple, without executing build code.

    `application_name` is the folder under build/app and the program's name in it.
    """
    if not toc.is_relative_to(ROOT) or ".." in toc.relative_to(ROOT).parts:
        raise ValueError("Build manifest must be inside the project")
    for path in (toc, *toc.parents):
        _plain_entry(path)
        if path == ROOT:
            break
    value = ast.literal_eval(toc.read_text(encoding="utf-8"))
    if not (isinstance(value, tuple) and len(value) == 1 and isinstance(value[0], list)):
        raise ValueError("Invalid PyInstaller COLLECT manifest")
    manifest, seen = {}, set()
    executables = 0
    for row in value[0]:
        if not (isinstance(row, tuple) and len(row) == 3 and all(isinstance(item, str) for item in row)):
            raise ValueError("Invalid PyInstaller COLLECT entry")
        name, source, kind = row
        parts = name.replace("\\", "/").split("/")
        if (PureWindowsPath(name).drive or name.startswith(("/", "\\")) or
                any(not part or part in {".", ".."} or ":" in part or "\0" in part or
                    part.rstrip(" .") != part for part in parts) or PureWindowsPath(name).is_reserved()):
            raise ValueError("Build manifest contains an unsafe destination")
        if kind not in {"EXECUTABLE", "BINARY", "EXTENSION", "DATA"} or "\0" in source or not Path(source).is_absolute():
            raise ValueError("Unsupported PyInstaller COLLECT entry")
        normalized = "/".join(parts).casefold()
        basename = parts[-1].casefold()
        if (parts[0].casefold() in {"tkinter", "_tcl_data", "_tk_data", "tcl", "tk"}
                or basename.startswith(("_tkinter.", "_imagingtk."))
                or (basename.startswith(("tcl8", "tk8")) and basename.endswith(".dll"))):
            raise ValueError("Build manifest contains a removed UI runtime")
        # Qt links to Windows' unversioned ICU API. A PATH tool's ICU build
        # can share its filename while exporting only version-suffixed names.
        if parts[-1].casefold() in {"icuuc.dll", "icudt78.dll"}:
            raise ValueError(f"Build manifest contains an unexpected ICU runtime: {name}")
        if len(parts) == 1 and basename.startswith(("libssl", "libcrypto")) and basename.endswith(".dll"):
            expected_ssl = Path(sys.base_prefix) / "DLLs" / parts[0]
            if (kind != "BINARY" or not expected_ssl.is_file()
                    or Path(os.path.realpath(source)) != Path(os.path.realpath(expected_ssl))):
                raise ValueError(f"Build manifest contains OpenSSL outside the build interpreter: {name}")
            bundled_ssl = ROOT / "build" / "app" / application_name / "_internal" / parts[0]
            project_file(bundled_ssl, ROOT)
            with expected_ssl.open("rb") as original, bundled_ssl.open("rb") as bundled:
                if hashlib.file_digest(original, "sha256").digest() != hashlib.file_digest(bundled, "sha256").digest():
                    raise ValueError(f"Bundled OpenSSL differs from the build interpreter: {name}")
        if normalized.startswith("pyside6/") and (
                parts[-1].casefold() in EXCLUDED_QT_COMPONENTS or
                normalized.startswith(("pyside6/plugins/qmltooling/", "pyside6/translations/"))):
            raise ValueError(f"Build manifest contains an excluded Qt component: {name}")
        if kind == "EXECUTABLE":
            if name != application_name + ".exe":
                raise ValueError("Build manifest contains an unexpected executable")
            executables += 1
            relative = Path(*parts)
        else:
            relative = Path("_internal", *parts)
        canonical = relative.as_posix().casefold()
        if canonical in seen:
            raise ValueError("Build manifest contains duplicate destinations")
        seen.add(canonical)
        manifest[relative] = Path(source)
    if executables != 1:
        raise ValueError("Build manifest must contain exactly one application executable")
    return manifest


def _verified_files(directory: Path, expected) -> list[Path]:
    """Refuse additions, omissions and redirects before creating an archive."""
    if not directory.is_relative_to(ROOT) or ".." in directory.relative_to(ROOT).parts:
        raise ValueError("Release inputs must be inside the project")
    for path in (directory, *directory.parents):
        _plain_entry(path)
        if path == ROOT:
            break
    actual = {}
    pending = [directory]
    while pending:
        for path in pending.pop().iterdir():
            details = _plain_entry(path)
            if stat.S_ISDIR(details.st_mode):
                pending.append(path)
            else:
                canonical = path.relative_to(directory).as_posix().casefold()
                if canonical in actual:
                    raise ValueError("Release inputs contain duplicate Windows paths")
                actual[canonical] = path
    wanted = {path.as_posix().casefold() for path in expected}
    if actual.keys() - wanted:
        raise ValueError("Release inputs contain files absent from the build manifest")
    if wanted - actual.keys():
        raise ValueError("Release inputs are missing files from the build manifest")
    return sorted(actual.values())


def _runtime_files(manifest: dict[Path, Path], component: str) -> list[Path]:
    """The bundled tool's source files that PyInstaller actually collected."""
    runtime = ROOT / "tools" / component / "runtime"
    # PyInstaller keeps each source under the build's working path, which can
    # be a junction or a substituted or mapped drive while ROOT is resolved.
    # Compare real locations; the files themselves are still checked below.
    real_runtime = Path(os.path.realpath(runtime))
    expected = set()
    for source in manifest.values():
        real = Path(os.path.realpath(source))
        if real.is_relative_to(real_runtime):
            expected.add(real.relative_to(real_runtime))
    if not expected:
        raise ValueError("Build manifest is missing the bundled media tools")
    return _verified_files(runtime, expected)


def _require_startup_report(executable: Path) -> None:
    report_path = ROOT / "build" / f"qt-startup-{__version__}.json"
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ValueError("Run the frozen Qt startup check before packaging") from None
    with executable.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    validate_qt_report(report, digest)
    try:
        download_report = json.loads((ROOT / "build" / f"download-startup-{__version__}.json")
                                     .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ValueError("Run the frozen download startup check before packaging") from None
    validate_download_report(download_report, digest)


def require_current_guides(root: Path, version: str) -> None:
    """Refuse guides that still name another version or the former ZIP layout.

    The guide tells people what to start. It shipped naming an older version
    twice because nothing compared it with the build.
    """
    guide = (root / "docs" / "사용안내.txt").read_text(encoding="utf-8")
    development = (root / "docs" / "development.md").read_text(encoding="utf-8")
    readme = (root / "README.md").read_text(encoding="utf-8")
    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    program, checksums, sources = release_assets(version)
    latest = re.search(r"^## (\d+\.\d+\.\d+) ", changelog, re.MULTILINE)
    checks = (
        (guide.splitlines()[0].strip() == f"러브박스 (LuvBox) {version}", "docs/사용안내.txt 첫 줄"),
        (f"{program}를 실행" in guide, "docs/사용안내.txt 실행 안내"),
        (set(re.findall(r"(?:LuvBox|SHA256)-(\d+\.\d+\.\d+)", guide)) == {version}, "docs/사용안내.txt 버전 이름"),
        (development.splitlines()[0].strip() == f"# LuvBox {version} 개발 안내", "docs/development.md 제목"),
        (all(f"release/{name}" in development for name in (program, checksums, sources))
         and set(re.findall(r"release/(?:LuvBox|SHA256)-(\d+\.\d+\.\d+)", development)) == {version},
         "docs/development.md 산출물 이름"),
        (readme.splitlines()[0].strip() == f"# 러브박스 (LuvBox) {version}", "README.md 제목"),
        (latest is not None and latest[1] == version, "CHANGELOG.md 최신 항목"),
    )
    stale = [label for current, label in checks if not current]
    if stale:
        raise ValueError(f"Guides do not name version {version}: " + ", ".join(stale))


def _require_same_application(program: Path, application: Path, application_files) -> None:
    """The archive inside LuvBox.exe is exactly the checked application folder."""
    footer = build_launcher.read_footer(program)
    if footer is None or footer["version"] != __version__:
        raise ValueError("The single program was not built for this version; rebuild with build.ps1")
    packed = build_launcher.packed_files(program)
    expected = {}
    for path in application_files:
        with path.open("rb") as stream:
            expected[path.relative_to(application).as_posix()] = hashlib.file_digest(stream, "sha256").hexdigest()
    if packed != expected:
        raise ValueError("The single program does not hold the built application; rebuild with build.ps1")


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def package() -> None:
    require_inputs(ROOT)
    subprocess.run([sys.executable, str(ROOT / "tools/ffmpeg/build.py"), "--check"], check=True)
    output = ROOT / "release"
    if output.is_symlink() or getattr(output, "is_junction", lambda: False)():
        raise ValueError("Release directory must be inside the project")
    output.mkdir(exist_ok=True)
    # PyInstaller's folder (the program and its _internal runtime) and the one
    # file the launcher tool made from it. Only the latter is published.
    application = ROOT / build_launcher.APPLICATION
    executable = application / build_launcher.INNER_NAME
    single = ROOT / build_launcher.SINGLE
    if not executable.is_file() or not single.is_file():
        raise FileNotFoundError("Build the application first")
    require_snapshot(executable, ROOT)
    _require_startup_report(single)
    manifest = _collect_manifest(ROOT / "build/onedir" / application.name / "COLLECT-00.toc", application.name)
    application_files = _verified_files(application, manifest)
    _require_same_application(single, application, application_files)
    runtimes = {component: _runtime_files(manifest, component) for component in ("ffmpeg", "exiftool")}
    qt_materials = qt_source_files()
    source_files = project_source_files(ROOT)
    python_materials = _python_materials(ROOT)
    program_name, checksum_name, source_name = release_assets(__version__)
    descriptor, filename = tempfile.mkstemp(prefix=".release-", suffix=".zip", dir=output)
    os.close(descriptor)
    staged = Path(filename)
    descriptor, filename = tempfile.mkstemp(prefix=".release-", suffix=".exe", dir=output)
    os.close(descriptor)
    staged_program = Path(filename)
    try:
        with zipfile.ZipFile(staged, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            archive.write(ROOT / "docs" / "사용안내.txt", "사용안내.txt")
            archive.write(ROOT / "LICENSE", "LICENSE")
            archive.write(ROOT / "THIRD_PARTY_NOTICES.md", "THIRD_PARTY_NOTICES.md")
            archive.write(ROOT / "CHANGELOG.md", "CHANGELOG.md")
            for name in ("GPL-2.0-only.txt", "GPL-3.0-only.txt", "LGPL-3.0-only.txt"):
                archive.write(ROOT / "assets/qt/licenses" / name, "licenses/Qt/" + name)
            for path, name in qt_materials:
                archive.write(path, name)
            for path, name in python_materials:
                archive.write(path, name)
            archive.write(ROOT / "assets/fonts/Pretendard/OFL.txt", "licenses/Pretendard-OFL.txt")
            archive.write(ROOT / "tools/ffmpeg/LICENSE.LGPLv2.1.txt", "licenses/FFmpeg-LGPLv2.1.txt")
            for name in ("COPYING.MinGW-w64-runtime.txt", "COPYING.GCC-runtime-exception.txt"):
                archive.write(ROOT / "tools/ffmpeg" / name, "licenses/" + name)
            archive.write(ROOT / "tools/ffmpeg/ffmpeg-7.1.tar.xz", "source/tools/ffmpeg/ffmpeg-7.1.tar.xz")
            for path in runtimes["ffmpeg"]:
                archive.write(path, "source/tools/ffmpeg/runtime/" + path.relative_to(ROOT / "tools/ffmpeg/runtime").as_posix())
            for path in source_files:
                archive.write(path, "source/" + path.relative_to(ROOT).as_posix())
            # The GPL downloader's corresponding Python sources are explicit
            # and easy to find; no caches, browser profile, or workspace scans.
            gallery = Path(importlib.metadata.distribution("gallery_dl").locate_file("gallery_dl"))
            for path in sorted(gallery.rglob("*.py")):
                archive.write(path, "source/vendor/gallery_dl/" + path.relative_to(gallery).as_posix())
            for name in DEPENDENCIES:
                dist = importlib.metadata.distribution(name)
                metadata_dir = Path(dist._path)
                for path in sorted(metadata_dir.rglob("*")):
                    if path.is_file() and path.name not in {"RECORD", "INSTALLER", "REQUESTED", "direct_url.json"}:
                        archive.write(path, "licenses/" + metadata_dir.name + "/" + path.relative_to(metadata_dir).as_posix())
            # ExifTool includes its complete source, Perl modules, runtime and
            # license notices in the official pinned Windows package.
            runtime = ROOT / "tools/exiftool/runtime"
            for path in runtimes["exiftool"]:
                archive.write(path, "source/tools/exiftool/runtime/" + path.relative_to(runtime).as_posix())
            python_root = Path(sys.base_prefix)
            python_license = python_root / "LICENSE.txt"
            if python_license.is_file():
                archive.write(python_license, "licenses/Python-LICENSE.txt")
        shutil.copyfile(single, staged_program)
        rows = [(_digest(staged_program), program_name), (_digest(staged), source_name)]
        # Compression can outlast a concurrent source edit or runtime rebuild.
        # Keep the previous release files unless the exact sealed inputs still
        # exist and the copied program is still the one that was checked.
        require_snapshot(executable, ROOT)
        _require_startup_report(staged_program)
        os.replace(staged_program, output / program_name)
        os.replace(staged, output / source_name)
    finally:
        staged.unlink(missing_ok=True)
        staged_program.unlink(missing_ok=True)
    (output / checksum_name).write_text(format_checksums(rows), encoding="utf-8", newline="\n")
    print(f"Release {__version__}: {program_name}, {checksum_name}, {source_name}")


if __name__ == "__main__":
    # build.ps1 asks for this check alone before the long build; packaging repeats it.
    require_current_guides(ROOT, __version__)
    if "--check-guides" not in sys.argv[1:]:
        package()

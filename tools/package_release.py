"""Package the explicit release allowlist, never personal account data."""

import ast
import hashlib
import importlib.metadata
import os
from pathlib import Path, PureWindowsPath
import stat
import sys
import subprocess
import tempfile
import zipfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from luvbox import __version__  # noqa: E402

SOURCE_FILES = (
    "assets/icons/luvbox.png", "assets/icons/luvbox.ico", "assets/icons/README.md", "tools/build_icon.py",
    "tools/draw_icon.py",
    "entry.py", "setup.ps1", "build.ps1", "run.bat", "requirements.txt",
    "requirements-dev.txt", "requirements-lock.txt", "README.md", "LICENSE",
    "THIRD_PARTY_NOTICES.md", "LuvBox.code-workspace",
    "tools/exiftool/download.py", "tools/package_release.py", "tools/ui_qa.py",
    "tools/benchmark_history.py", "tools/benchmark_preview.py",
    "tools/ffmpeg/build.py", "tools/ffmpeg/README.md", "tools/ffmpeg/manifest.json",
    "tools/ffmpeg/configure-flags.json", "tools/ffmpeg/build-info.txt", "tools/ffmpeg/LICENSE.LGPLv2.1.txt",
    "tools/ffmpeg/COPYING.MinGW-w64-runtime.txt", "tools/ffmpeg/COPYING.GCC-runtime-exception.txt",
    "assets/fonts/Pretendard/Pretendard-Regular.ttf", "assets/fonts/Pretendard/Pretendard-Bold.ttf",
    "assets/fonts/Pretendard/OFL.txt", "assets/fonts/Pretendard/manifest.json",
)
DEPENDENCIES = (
    "gallery_dl", "pillow", "requests", "urllib3", "certifi", "charset-normalizer", "idna",
    "pyinstaller", "pyinstaller-hooks-contrib", "altgraph", "packaging", "pefile",
    "pywin32-ctypes", "setuptools",
)


def _plain_entry(path: Path):
    details = path.lstat()
    if (stat.S_ISLNK(details.st_mode) or getattr(details, "st_file_attributes", 0) & 0x400 or
            getattr(path, "is_junction", lambda: False)() or
            (stat.S_ISREG(details.st_mode) and details.st_nlink > 1)):
        raise ValueError("Release inputs must not contain linked files or directories")
    if not (stat.S_ISREG(details.st_mode) or stat.S_ISDIR(details.st_mode)):
        raise ValueError("Release inputs must be regular files or directories")
    return details


def _collect_manifest(toc: Path, application_name: str) -> dict[Path, Path]:
    """Read PyInstaller's literal COLLECT tuple, without executing build code."""
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


def package() -> None:
    subprocess.run([sys.executable, str(ROOT / "tools/ffmpeg/build.py"), "--check"], check=True)
    output = ROOT / "release"
    if output.is_symlink() or getattr(output, "is_junction", lambda: False)():
        raise ValueError("Release directory must be inside the project")
    output.mkdir(exist_ok=True)
    # Versioned names: a running older executable never blocks a new build.
    # The application is a folder: the exe and its _internal runtime.
    application = output / f"LuvBox-{__version__}"
    executable = application / f"LuvBox-{__version__}.exe"
    if not executable.is_file():
        raise FileNotFoundError("Build the application first")
    manifest = _collect_manifest(ROOT / "build/onedir" / application.name / "COLLECT-00.toc", application.name)
    application_files = _verified_files(application, manifest)
    runtimes = {}
    for component in ("ffmpeg", "exiftool"):
        runtime = ROOT / "tools" / component / "runtime"
        expected = {source.relative_to(runtime) for source in manifest.values() if source.is_relative_to(runtime)}
        if not expected:
            raise ValueError("Build manifest is missing the bundled media tools")
        runtimes[component] = _verified_files(runtime, expected)
    destination = output / f"LuvBox-{__version__}.zip"
    descriptor, filename = tempfile.mkstemp(prefix=".release-", suffix=".zip", dir=output)
    os.close(descriptor)
    staged = Path(filename)
    try:
        with zipfile.ZipFile(staged, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for path in application_files:
                archive.write(path, application.name + "/" + path.relative_to(application).as_posix())
            archive.write(ROOT / "docs" / "사용안내.txt", "사용안내.txt")
            archive.write(ROOT / "LICENSE", "LICENSE")
            archive.write(ROOT / "THIRD_PARTY_NOTICES.md", "THIRD_PARTY_NOTICES.md")
            archive.write(ROOT / "assets/fonts/Pretendard/OFL.txt", "licenses/Pretendard-OFL.txt")
            archive.write(ROOT / "tools/ffmpeg/LICENSE.LGPLv2.1.txt", "licenses/FFmpeg-LGPLv2.1.txt")
            for name in ("COPYING.MinGW-w64-runtime.txt", "COPYING.GCC-runtime-exception.txt"):
                archive.write(ROOT / "tools/ffmpeg" / name, "licenses/" + name)
            archive.write(ROOT / "tools/ffmpeg/ffmpeg-7.1.tar.xz", "source/tools/ffmpeg/ffmpeg-7.1.tar.xz")
            for path in runtimes["ffmpeg"]:
                archive.write(path, "source/tools/ffmpeg/runtime/" + path.relative_to(ROOT / "tools/ffmpeg/runtime").as_posix())
            for name in SOURCE_FILES:
                archive.write(ROOT / name, "source/" + name)
            # Ship the current guide and verification, without obsolete reports.
            for name in (
                "docs/사용안내.txt", "docs/development.md", "docs/verification.md",
                "docs/performance.md", "docs/refactoring-review.md", "docs/security-review.md",
            ):
                archive.write(ROOT / name, "source/" + name)
            for folder, pattern in (("luvbox", "*.py"), ("tests", "*.py"), (".vscode", "*.json")):
                for path in sorted((ROOT / folder).glob(pattern)):
                    if path.is_file():
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
            for component in ("tcl8.6", "tk8.6"):
                license_path = python_root / "tcl" / component / "license.terms"
                if license_path.is_file():
                    archive.write(license_path, f"licenses/{component}-license.terms")
        os.replace(staged, destination)
    finally:
        staged.unlink(missing_ok=True)
    rows = []
    for path in (executable, destination):
        with path.open("rb") as file:
            digest = hashlib.file_digest(file, "sha256").hexdigest()
        rows.append(f"{digest}  {path.name}")
    (output / f"SHA256-{__version__}.txt").write_text("\n".join(rows) + "\n", encoding="utf-8")
    print(f"Release {__version__}: {application.name}/{executable.name}, {destination.name}")


if __name__ == "__main__":
    package()

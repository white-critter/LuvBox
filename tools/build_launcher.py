"""Compile the launcher and append the built application to make one LuvBox.exe.

    LuvBox.exe = launcher program + ZIP of the application folder + 84-byte footer

The launcher (tools/launcher/Launcher.cs) is compiled with the C# compiler
that ships with Windows, so the build needs no extra toolchain. The footer
(luvbox/launcher.py) records where the archive starts, its SHA-256 and the
version; the launcher checks them before it unpacks anything.

Run by build.ps1 after PyInstaller and the build-input seal:
    build/app/LuvBox/  ->  build/single/LuvBox.exe
"""

import hashlib
import io
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from luvbox import __version__  # noqa: E402
from luvbox.launcher import (FOOTER, INNER_NAME, VERSION_SHAPE, make_footer, payload_digest,  # noqa: E402
                             read_footer)

# What the launcher is compiled from; also part of the shipped source.
LAUNCHER_SOURCE = "tools/launcher/Launcher.cs"
LAUNCHER_MANIFEST = "tools/launcher/app.manifest"
LAUNCHER_ICON = "assets/icons/luvbox.ico"
# Embedded files and the names Launcher.cs asks for.
LAUNCHER_RESOURCES = (
    ("tools/launcher/splash.ini", "LuvBox.splash.ini"),
    ("assets/splash/card.png", "LuvBox.card.png"),
    ("assets/splash/glow.png", "LuvBox.glow.png"),
    ("assets/splash/mark.png", "LuvBox.mark.png"),
    ("assets/fonts/Pretendard/Pretendard-Regular.ttf", "LuvBox.font.ttf"),
)
LAUNCHER_REFERENCES = ("System.dll", "System.Core.dll", "System.Drawing.dll", "System.Windows.Forms.dll",
                       "System.IO.Compression.dll")
# Windows refuses paths past 260 characters for most programs. The longest
# file name inside the application plus any realistic profile folder must fit.
MAX_RELATIVE_PATH = 140
APPLICATION = Path("build") / "app" / "LuvBox"
SINGLE = Path("build") / "single" / "LuvBox.exe"
_FIXED_TIME = (1980, 1, 1, 0, 0, 0)
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{n}" for n in range(1, 10)), *(f"LPT{n}" for n in range(1, 10))}


def compiler() -> Path:
    path = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "Microsoft.NET" / "Framework64" / "v4.0.30319" / "csc.exe"
    if not path.is_file():
        raise FileNotFoundError("The .NET Framework C# compiler (csc.exe) was not found in the Windows folder")
    return path


def compile_launcher(destination: Path, version: str = __version__, root: Path = ROOT) -> Path:
    """Build the bare launcher (no application appended) at `destination`."""
    if not VERSION_SHAPE.match(version):
        raise ValueError("Use a numeric MAJOR.MINOR.PATCH version.")
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    # The file's Windows version details come from these attributes.
    details = destination.parent / "Version.cs"
    details.write_text(
        "using System.Reflection;\n"
        '[assembly: AssemblyTitle("LuvBox")]\n'
        '[assembly: AssemblyProduct("LuvBox")]\n'
        f'[assembly: AssemblyVersion("{version}.0")]\n'
        f'[assembly: AssemblyFileVersion("{version}.0")]\n'
        f'[assembly: AssemblyInformationalVersion("{version}")]\n', encoding="utf-8")
    command = [str(compiler()), "/nologo", "/target:winexe", "/platform:x64", "/optimize+", "/debug-",
               "/warnaserror+", "/utf8output", "/codepage:65001",
               f"/out:{destination}", f"/win32icon:{root / LAUNCHER_ICON}",
               f"/win32manifest:{root / LAUNCHER_MANIFEST}"]
    command += [f"/reference:{name}" for name in LAUNCHER_REFERENCES]
    command += [f"/resource:{root / source},{name}" for source, name in LAUNCHER_RESOURCES]
    command += [str(root / LAUNCHER_SOURCE), str(details)]
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode != 0 or not destination.is_file():
        raise RuntimeError("The launcher did not compile:\n" + result.stdout + result.stderr)
    return destination


def _plain(path: Path) -> os.stat_result:
    details = path.lstat()
    if (stat.S_ISLNK(details.st_mode) or getattr(details, "st_file_attributes", 0) & 0x400
            or getattr(path, "is_junction", lambda: False)()
            or (stat.S_ISREG(details.st_mode) and details.st_nlink > 1)):
        raise ValueError("The application folder must not contain linked files or directories")
    if not (stat.S_ISREG(details.st_mode) or stat.S_ISDIR(details.st_mode)):
        raise ValueError("The application folder must contain only files and directories")
    return details


def safe_name(name: str) -> bool:
    """The rule the launcher applies to every archive name before it writes a file."""
    if not name or len(name) > 240 or name.startswith("/") or "\\" in name or ":" in name:
        return False
    for part in name.split("/"):
        if (not part or part in {".", ".."} or part.endswith((" ", "."))
                or any(ord(letter) < 32 or letter in '<>"|?*' for letter in part)
                or part.split(".", 1)[0].upper() in _RESERVED):
            return False
    return True


def application_files(application: Path) -> list[tuple[str, Path]]:
    """Every file of the built application as (archive name, path), in a stable order."""
    application = Path(application)
    if not (application / INNER_NAME).is_file():
        raise FileNotFoundError("Build the application first")
    found = []
    seen = set()
    pending = [application]
    while pending:
        for path in pending.pop().iterdir():
            details = _plain(path)
            if stat.S_ISDIR(details.st_mode):
                pending.append(path)
                continue
            name = path.relative_to(application).as_posix()
            if not safe_name(name) or len(name) > MAX_RELATIVE_PATH:
                raise ValueError(f"The application contains a name the launcher would refuse: {name}")
            if name.casefold() in seen:
                raise ValueError("The application contains duplicate Windows paths")
            seen.add(name.casefold())
            found.append((name, path))
    return sorted(found)


def pack(application: Path, launcher: Path, destination: Path, version: str = __version__) -> dict:
    """Write launcher + archive + footer to `destination`; returns the footer it wrote."""
    files = application_files(application)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    program = Path(launcher).read_bytes()
    if program[:2] != b"MZ" or read_footer(launcher) is not None:
        raise ValueError("The launcher must be a bare Windows program")
    descriptor, filename = tempfile.mkstemp(prefix=".single-", suffix=".tmp", dir=destination.parent)
    staged = Path(filename)
    try:
        with os.fdopen(descriptor, "w+b") as stream:
            stream.write(program)
            offset = stream.tell()
            # zipfile seeks back to fill in sizes, so build the archive beside
            # the program and copy it in once it is complete.
            with tempfile.TemporaryFile(dir=destination.parent) as archive_file:
                with zipfile.ZipFile(archive_file, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
                    for name, path in files:
                        entry = zipfile.ZipInfo(name, date_time=_FIXED_TIME)
                        entry.compress_type = zipfile.ZIP_DEFLATED
                        entry.external_attr = 0x20  # an ordinary file
                        with path.open("rb") as source, archive.open(entry, "w") as target:
                            shutil.copyfileobj(source, target, 1024 * 1024)
                length = archive_file.seek(0, os.SEEK_END)
                archive_file.seek(0)
                digest = hashlib.sha256()
                while block := archive_file.read(1024 * 1024):
                    digest.update(block)
                    stream.write(block)
            stream.write(make_footer(offset, length, digest.digest(), version))
            stream.flush()
            os.fsync(stream.fileno())
        written = read_footer(staged)
        if (written is None or written["version"] != version or written["sha256"] != digest.hexdigest()
                or payload_digest(staged, written) != written["sha256"]):
            raise ValueError("The packed program does not read back as written")
        os.replace(staged, destination)
    finally:
        staged.unlink(missing_ok=True)
    return written


class _Window(io.RawIOBase):
    """Part of a file as a file of its own, so zipfile sees the archive from byte 0."""

    def __init__(self, stream, offset, length):
        self._stream, self._offset, self._length, self._position = stream, offset, length, 0

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self._position

    def seek(self, position, whence=os.SEEK_SET):
        base = {os.SEEK_SET: 0, os.SEEK_CUR: self._position, os.SEEK_END: self._length}[whence]
        self._position = max(0, base + position)
        return self._position

    def readinto(self, buffer):
        count = max(0, min(len(buffer), self._length - self._position))
        self._stream.seek(self._offset + self._position)
        data = self._stream.read(count)
        buffer[:len(data)] = data
        self._position += len(data)
        return len(data)


def packed_files(program: Path) -> dict[str, str]:
    """SHA-256 of every file inside a LuvBox.exe's archive, by its name there."""
    footer = read_footer(program)
    if footer is None:
        raise ValueError("The program carries no application archive")
    if payload_digest(program, footer) != footer["sha256"]:
        raise ValueError("The program's archive does not match its footer")
    result = {}
    with open(program, "rb") as stream:
        window = io.BufferedReader(_Window(stream, footer["offset"], footer["length"]), 1024 * 1024)
        with zipfile.ZipFile(window) as archive:
            for entry in archive.infolist():
                if entry.is_dir() or not safe_name(entry.filename) or entry.filename in result:
                    raise ValueError("The program's archive contains an unexpected entry")
                digest = hashlib.sha256()
                with archive.open(entry) as source:
                    while block := source.read(1024 * 1024):
                        digest.update(block)
                result[entry.filename] = digest.hexdigest()
    return result


def build(root: Path = ROOT, version: str = __version__) -> Path:
    launcher = compile_launcher(root / "build" / "launcher" / "LuvBox.exe", version, root)
    destination = root / SINGLE
    footer = pack(root / APPLICATION, launcher, destination, version)
    size = destination.stat().st_size
    print(f"Single program {version}: {destination.relative_to(root)} "
          f"({size / 1048576:.1f} MB, launcher {footer['offset'] / 1024:.0f} KB, archive {footer['length'] / 1048576:.1f} MB)")
    return destination


if __name__ == "__main__":
    build()

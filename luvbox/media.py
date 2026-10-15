"""Local media conversion using the FFmpeg binary shipped with this app."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable


CONVERSION_TIMEOUT = 600
# GIF size presets: longest side in pixels and maximum frames per second.
# Zero keeps the source value. Smaller GIFs are often several times lighter.
GIF_SIZES = {"original": (0, 0), "720": (720, 15), "480": (480, 12)}


class ConversionError(RuntimeError):
    """A local conversion failed; the downloaded source is still intact."""


class ConversionSizeLimit(ConversionError):
    """The resulting GIF would exceed the user's media size limit."""


def ffmpeg_path() -> Path | None:
    """Find our pinned package's binary, ignoring PATH and environment overrides."""
    bundled = Path(__file__).resolve().parent.parent / "tools" / "ffmpeg" / "runtime" / "ffmpeg.exe"
    if bundled.is_file():
        return bundled
    if getattr(sys, "frozen", False):
        return None
    try:
        import imageio_ffmpeg
    except ImportError:
        return None
    directory = Path(imageio_ffmpeg.__file__).resolve().parent / "binaries"
    for executable in sorted(directory.glob("ffmpeg-*")):
        if executable.is_file() and (os.name != "nt" or executable.suffix == ".exe"):
            return executable
    return None


def _run(arguments: list[str], *, check_stop: Callable[[], None], deadline: float,
         output: Path | None = None, max_bytes: int = 0) -> bytes:
    """Run FFmpeg and return its diagnostics for local parsing only.

    A temporary stderr file cannot block on a full pipe. Raw diagnostics may
    contain local filenames, so callers get only our fixed safe error text.
    """
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(arguments, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=errors,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            while True:
                check_stop()
                if time.monotonic() >= deadline:
                    raise ConversionError("GIF 변환 시간이 너무 오래 걸려 중단했습니다. MP4 임시 파일은 보존했습니다.")
                if output is not None and max_bytes and output.stat().st_size > max_bytes:
                    raise ConversionSizeLimit("변환된 GIF가 용량 제한을 초과했습니다. MP4 임시 파일은 보존했습니다.")
                if process.poll() is not None:
                    if process.returncode:
                        raise ConversionError("GIF로 변환하지 못했습니다. MP4 임시 파일은 보존했습니다. 다시 저장을 시작하면 재시도합니다.")
                    errors.seek(0)
                    return errors.read(1024 * 1024)
                time.sleep(0.1)
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)


def convert_mp4_to_gif(source: Path | str, *, check_stop: Callable[[], None] | None = None,
                       max_bytes: int = 0, size: str = "original") -> Path:
    """Create a complete temporary GIF beside source; never modify source.

    The caller atomically finalizes the returned path and then removes source.
    Both palette passes stream frames; they do not retain the whole animation.
    By default original dimensions and frame timestamps are kept, rounded to
    GIF's 10 ms time unit. A smaller ``size`` preset fits the frames in a
    square box without upscaling and drops frames above its rate. GIF is
    necessarily limited to 256 colors and has no audio.
    """
    if size not in GIF_SIZES:
        raise ValueError("unknown GIF size preset")
    side, fps = GIF_SIZES[size]
    scale = (f"scale=w='min(iw,{side})':h='min(ih,{side})':force_original_aspect_ratio=decrease:"
             f"force_divisible_by=2:flags=lanczos" if side else "")
    # -fpsmax needs FFmpeg's constant-rate output; otherwise keep timestamps.
    timing = ["-fpsmax", str(fps)] if fps else ["-fps_mode", "passthrough"]
    source = Path(source)
    check_stop = check_stop or (lambda: None)
    check_stop()
    executable = ffmpeg_path()
    if executable is None:
        raise ConversionError("GIF 변환 도구를 찾지 못했습니다. MP4 임시 파일은 보존했습니다. 실행 파일을 다시 받아 주세요.")
    palette = output = None
    try:
        if not source.is_file():
            raise ConversionError("GIF로 변환할 MP4 임시 파일을 찾지 못했습니다.")
        fd, name = tempfile.mkstemp(prefix=".luvbox-gif-palette-", suffix=".bmp", dir=source.parent)
        os.close(fd)
        palette = Path(name)
        fd, name = tempfile.mkstemp(prefix=".luvbox-gif-", suffix=".gif", dir=source.parent)
        os.close(fd)
        output = Path(name)
        common = [str(executable), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                  "-protocol_whitelist", "file,pipe", "-threads", "1", "-filter_threads", "1",
                  "-filter_complex_threads", "1"]
        deadline = time.monotonic() + CONVERSION_TIMEOUT
        # With a frame cap, the first pass also reports the source duration.
        report = ["-loglevel", "info"] if fps else []
        # Keep the development decoder on MP4 too; format autodetection can
        # otherwise follow a playlist's references to other local files.
        diagnostics = _run([*common, *report, "-f", "mov", "-i", str(source), "-map", "0:v:0", "-vf",
                            (scale + "," if scale else "") + "palettegen=max_colors=256:reserve_transparent=0",
                            "-frames:v", "1", str(palette)],
                           check_stop=check_stop, deadline=deadline)
        if fps:
            # Constant-rate output keeps up to two frames past the end, which
            # would add a pause to every loop; end exactly with the source.
            found = re.search(rb"Duration: (\d+):(\d\d):(\d\d(?:\.\d+)?)", diagnostics)
            if found:
                hours, minutes, seconds = int(found[1]), int(found[2]), float(found[3])
                timing += ["-t", f"{hours * 3600 + minutes * 60 + seconds:.3f}"]
        frames = f"[0:v:0]{scale}[v];[v][1:v:0]" if scale else "[0:v:0][1:v:0]"
        _run([*common, "-f", "mov", "-i", str(source), "-i", str(palette), "-lavfi",
              frames + "paletteuse=dither=sierra2_4a", "-an", *timing,
              "-loop", "0", "-f", "gif", str(output)],
             check_stop=check_stop, deadline=deadline, output=output, max_bytes=max_bytes)
        check_stop()
        if max_bytes and output.stat().st_size > max_bytes:
            raise ConversionSizeLimit("변환된 GIF가 용량 제한을 초과했습니다. MP4 임시 파일은 보존했습니다.")
        with output.open("rb") as stream:
            header = stream.read(10)
            stream.seek(-1, os.SEEK_END)
            trailer = stream.read(1)
        if (header[:6] not in {b"GIF87a", b"GIF89a"} or trailer != b";" or
                not int.from_bytes(header[6:8], "little") or not int.from_bytes(header[8:10], "little")):
            raise ConversionError("GIF 변환 결과를 확인하지 못했습니다. MP4 임시 파일은 보존했습니다.")
        completed = output
        output = None
        return completed
    except (OSError, subprocess.SubprocessError):
        raise ConversionError("GIF로 변환하지 못했습니다. MP4 임시 파일은 보존했습니다. 저장 폴더와 디스크 여유 공간을 확인해 주세요.") from None
    finally:
        for temporary in (palette, output):
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def move_without_overwrite(source: Path, destination: Path) -> None:
    """Move within the account filesystem without replacing another file."""
    if os.name == "nt":
        # Windows rename is atomic and fails if the destination already exists.
        source.rename(destination)
    else:
        # POSIX rename replaces existing files. Linking is an exclusive create
        # and keeps the original intact until the second operation succeeds.
        os.link(source, destination)
        try:
            source.unlink()
        except OSError:
            try:
                destination.unlink()
            except OSError:
                pass
            raise


def can_recycle(path: Path | str) -> bool:
    """Whether this file's drive has a Windows Recycle Bin to restore from."""
    if os.name != "nt":
        return False
    import ctypes
    from ctypes import wintypes

    class Info(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("i64Size", ctypes.c_longlong),
                    ("i64NumItems", ctypes.c_longlong)]

    info = Info()
    info.cbSize = ctypes.sizeof(info)
    drive = os.path.splitdrive(os.path.abspath(path))[0]
    if not drive:
        return False
    return ctypes.windll.shell32.SHQueryRecycleBinW(drive + "\\", ctypes.byref(info)) == 0


def recycle(path: Path | str) -> bool:
    """Move one file to the Recycle Bin; never delete it permanently unasked.

    If Windows cannot keep the file (e.g. it is larger than the bin), it asks
    the user first. Returns True only when the file is gone afterwards.
    """
    if os.name != "nt":
        return False
    import ctypes
    from ctypes import wintypes

    class Operation(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT),
                    ("pFrom", wintypes.LPCWSTR), ("pTo", wintypes.LPCWSTR),
                    ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wintypes.BOOL),
                    ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]

    path = Path(os.path.abspath(path))
    delete, silent, no_confirmation, allow_undo, no_error_ui, nuke_warning = 3, 0x4, 0x10, 0x40, 0x400, 0x4000
    # pFrom is a double-NUL-terminated list; ctypes adds the final NUL.
    operation = Operation(None, delete, str(path) + "\0", None,
                          silent | no_confirmation | allow_undo | no_error_ui | nuke_warning, False, None, None)
    result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(operation))
    return result == 0 and not operation.fAnyOperationsAborted and not path.exists()

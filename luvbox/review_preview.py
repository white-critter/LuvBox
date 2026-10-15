"""Bounded local playback for a file awaiting the user's save decision.

Only plain Python/Pillow objects cross this worker's three-frame queue. Video
decoding uses the bundled FFmpeg with local input, muted output and one decoder
thread. Closing never waits on Tk's thread; ``finished`` signals closed handles.
"""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
import os
from pathlib import Path
import queue
import stat
import struct
import subprocess
from threading import Event, Lock, Thread
import time
import warnings

from PIL import Image, ImageOps, UnidentifiedImageError

from .preview import MAX_PIXELS, oriented_thumbnail
from .thumbcache import cloud_only


REVIEW_SIZE = (640, 400)
PLAYBACK_FPS = 12
LOOP_SECONDS = 30
FRAME_QUEUE_SIZE = 3
DECODER_TIMEOUT = 8


@dataclass(frozen=True)
class ReviewFrame:
    image: Image.Image | None
    duration_ms: int = 0
    note: str = ""


class _GifSampler:
    """Sample a continuous GIF timeline without rounding away loop tails."""

    def __init__(self, size, interval, send, note):
        self.size = size
        self.interval = interval
        self.send = send
        self.note = note
        self.duration = 0
        self.image = None

    def add(self, original, duration):
        while duration:
            if self.image is None:
                self.image = ImageOps.contain(original, self.size, Image.Resampling.LANCZOS).convert("RGBA")
            # A long source frame needs one held image, rather than a copy
            # for every sampling interval in which its pixels stay the same.
            consumed = (duration // self.interval * self.interval
                        if not self.duration and duration >= self.interval
                        else min(duration, self.interval - self.duration))
            duration -= consumed
            self.duration += consumed
            if self.duration >= self.interval:
                image, self.image = self.image, None
                elapsed, self.duration = self.duration, 0
                if not self.send(ReviewFrame(image, elapsed, self.note)):
                    return False
        return True

    def close(self):
        if self.image is not None:
            self.image.close()
            self.image = None


def _signature(path: Path) -> tuple[int, int, int, int]:
    """Reject redirects; never recall a cached frame from an untrusted path."""
    status = path.lstat()
    if (not stat.S_ISREG(status.st_mode) or path.is_symlink()
            or getattr(status, "st_file_attributes", 0) & 0x400):
        raise OSError("not a plain local media file")
    if cloud_only(path, status=status):
        raise OSError("media is not local")
    return (status.st_dev, status.st_ino, status.st_size, status.st_mtime_ns)


class ReviewPreview:
    """One decoder, at most three frames, and a cancellable process watchdog.

    ``path`` must have passed the caller's allowed-location check on the main
    thread. No UI callback or Tk object is retained here. Photo input uses
    Pillow; animations stream small frames and loop only the first 30s.
    """

    def __init__(self, path: Path | str, media_type: str, *, size=REVIEW_SIZE):
        width, height = (int(value) for value in size)
        if not (16 <= width <= 1280 and 16 <= height <= 720):
            raise ValueError("preview size is out of bounds")
        self.path = Path(path).absolute()
        self.media_type = str(media_type)
        self.size = (width, height)
        self.frames = queue.Queue(maxsize=FRAME_QUEUE_SIZE)
        self.stop = Event()
        self.finished = Event()
        self._lock = Lock()
        self._process = None
        self._heartbeat = time.monotonic()
        self._timed_out = False
        self.release_error = False
        self._used_process = False
        self._worker = Thread(target=self._run, daemon=True, name="save-review-preview")
        self._watchdog = Thread(target=self._watch, daemon=True, name="save-review-preview-watch")
        self._worker.start()
        self._watchdog.start()

    def _send(self, frame: ReviewFrame) -> bool:
        while not self.stop.is_set():
            with self._lock:
                if self.stop.is_set():
                    break
                # Back pressure means the viewer is still consuming playback,
                # so a full queue must not trigger the decoder timeout.
                self._heartbeat = time.monotonic()
                try:
                    self.frames.put_nowait(frame)
                    return True
                except queue.Full:
                    pass
            self.stop.wait(.05)
        if frame.image is not None:
            frame.image.close()
        return False

    def _watch(self):
        while not self.finished.wait(.1) and not self.stop.is_set():
            with self._lock:
                process = self._process
                expired = process is not None and time.monotonic() - self._heartbeat > DECODER_TIMEOUT
                if expired:
                    self._timed_out = True
            if expired:
                try:
                    process.kill()
                except OSError:
                    pass
                return

    def close(self):
        """Cancel immediately; do not join a worker or wait for a process."""
        with self._lock:
            self.stop.set()
            process = self._process
            while True:
                try:
                    frame = self.frames.get_nowait()
                except queue.Empty:
                    break
                if frame.image is not None:
                    frame.image.close()
        if process is not None:
            try:
                process.kill()
            except OSError:
                pass

    def _run(self):
        try:
            signature = _signature(self.path)
            if self.stop.is_set():
                return
            if self.media_type == "photo":
                self._photo(signature)
            elif self.media_type in {"video", "animated_gif", "gif"}:
                self._animation(signature)
            else:
                self._send(ReviewFrame(None, note="이 파일 형식은 미리보기를 지원하지 않습니다."))
        except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError,
                Image.DecompressionBombWarning, subprocess.SubprocessError):
            self._send(ReviewFrame(None, note="미리보기를 읽지 못했습니다. 건너뛰거나 저장을 선택할 수 있습니다."))
        finally:
            if self._used_process and not self._released_file():
                self.release_error = True
            self.finished.set()

    def _released_file(self):
        """Windows can retain a dying process's file lock briefly after wait.

        Observe sharing permission before advertising safe save/skip cleanup.
        No bytes are read or written. Another program holding the candidate
        longer than two seconds makes this a failed review rather than a save.
        """
        if os.name != "nt":
            return True
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        create = kernel.CreateFileW
        create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                           wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        create.restype = wintypes.HANDLE
        close = kernel.CloseHandle
        close.argtypes = [wintypes.HANDLE]
        close.restype = wintypes.BOOL
        deadline = time.monotonic() + 2
        while True:
            # GENERIC_READ, exclusive sharing, OPEN_EXISTING, no link traversal.
            handle = create(str(self.path), 0x80000000, 0, None, 3, 0x00200000, None)
            if handle != ctypes.c_void_p(-1).value:
                close(handle)
                return True
            error = ctypes.get_last_error()
            if error in {2, 3}:
                return True  # The file was already removed outside this viewer.
            if error not in {32, 33} or time.monotonic() >= deadline:
                return False
            time.sleep(.01)

    def _photo(self, signature):
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(self.path) as original:
                if original.width * original.height > MAX_PIXELS:
                    self._send(ReviewFrame(None, note="이미지가 커서 미리보기를 표시할 수 없습니다."))
                    return
                image = oriented_thumbnail(original, self.size)
        if _signature(self.path) != signature:
            image.close()
            self._send(ReviewFrame(None, note="파일이 변경되었습니다. 이번 항목을 건너뛰어 주세요."))
            return
        self._send(ReviewFrame(image, note="사진 미리보기"))

    def _animation(self, signature):
        with self.path.open("rb") as source:
            header = source.read(6)
        if header in {b"GIF87a", b"GIF89a"}:
            self._gif(signature)
            return
        self._video(signature)

    def _gif(self, signature):
        """Read one GIF frame at a time; sample fast GIFs without slowing them."""
        note = f"GIF · 소리 없이 재생 · 처음 {LOOP_SECONDS}초 반복"
        interval = max(1, round(1000 / PLAYBACK_FPS))
        sampler = _GifSampler(self.size, interval, self._send, note)
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(self.path) as original:
                if original.width * original.height > MAX_PIXELS:
                    self._send(ReviewFrame(None, note="이미지가 커서 미리보기를 표시할 수 없습니다."))
                    return
                try:
                    while not self.stop.is_set():
                        if _signature(self.path) != signature:
                            self._send(ReviewFrame(None, note="파일이 변경되었습니다. 이번 항목을 건너뛰어 주세요."))
                            return
                        elapsed = 0
                        seen = False
                        # At most 3,000 10ms input frames per 30-second loop.
                        # Keep the sampler's partial interval across loops:
                        # resetting it would freeze short, fast animations on
                        # the same first frame or stretch their last frame.
                        for index in range(LOOP_SECONDS * 100):
                            if self.stop.is_set():
                                return
                            try:
                                original.seek(index)
                            except EOFError:
                                break
                            seen = True
                            frame_duration = max(10, min(60_000, int(original.info.get("duration") or 100)))
                            frame_duration = min(frame_duration, LOOP_SECONDS * 1000 - elapsed)
                            elapsed += frame_duration
                            if not sampler.add(original, frame_duration):
                                return
                            if elapsed >= LOOP_SECONDS * 1000:
                                break
                        if not seen:
                            raise OSError("empty GIF")
                finally:
                    sampler.close()

    def _video(self, signature):
        from .media import ffmpeg_path
        executable = ffmpeg_path()
        if executable is None:
            self._send(ReviewFrame(None, note="재생 도구를 찾지 못했습니다. 건너뛰거나 저장을 선택할 수 있습니다."))
            return
        width, height = self.size
        max_frame_bytes = width * height * 4 + 4096
        caption = "GIF" if self.media_type in {"animated_gif", "gif"} else "동영상"
        note = (("움직이는 GIF · 원본 영상 미리보기" if caption == "GIF" else "동영상")
                + f" · 소리 없이 재생 · 처음 {LOOP_SECONDS}초 반복")
        arguments = [str(executable), "-nostdin", "-v", "error", "-max_alloc", "67108864",
                     "-protocol_whitelist", "file,pipe", "-threads", "1", "-filter_threads", "1",
                     # A playlist disguised as .part cannot read other files.
                     "-f", "mov", "-i", str(self.path), "-t", str(LOOP_SECONDS), "-an", "-sn", "-dn",
                     "-vf", f"scale={width}:{height}:force_original_aspect_ratio=decrease",
                     "-r", str(PLAYBACK_FPS), "-f", "image2pipe", "-vcodec", "bmp", "pipe:1"]
        while not self.stop.is_set():
            if _signature(self.path) != signature:
                self._send(ReviewFrame(None, note="파일이 변경되었습니다. 이번 항목을 건너뛰어 주세요."))
                return
            process = subprocess.Popen(arguments, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL, bufsize=0,
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            with self._lock:
                self._used_process = True
                self._process = process
                self._heartbeat = time.monotonic()
                self._timed_out = False
                already_closed = self.stop.is_set()
            emitted = 0
            try:
                if already_closed:
                    return
                # Both FFmpeg and this loop cap playback length. Output remains
                # bounded even if a malformed input disregards its timestamps.
                for _ in range(PLAYBACK_FPS * LOOP_SECONDS):
                    header = self._read_exact(process.stdout, 14)
                    if self.stop.is_set() or not header:
                        break
                    if len(header) != 14 or header[:2] != b"BM":
                        raise OSError("invalid preview frame")
                    frame_bytes = struct.unpack("<I", header[2:6])[0]
                    if not 54 <= frame_bytes <= max_frame_bytes:
                        raise OSError("unbounded preview frame")
                    payload = self._read_exact(process.stdout, frame_bytes - 14)
                    if self.stop.is_set() or len(payload) != frame_bytes - 14:
                        break
                    with Image.open(BytesIO(header + payload)) as source:
                        if source.width > width or source.height > height:
                            raise OSError("unbounded preview dimensions")
                        image = source.convert("RGB")
                    if not self._send(ReviewFrame(image, max(1, round(1000 / PLAYBACK_FPS)), note)):
                        return
                    emitted += 1
            finally:
                if process.poll() is None:
                    try:
                        process.kill()
                    except OSError:
                        pass
                while True:
                    try:
                        process.wait(timeout=2)
                        break
                    except subprocess.TimeoutExpired:
                        try:
                            process.kill()
                        except OSError:
                            pass
                if process.stdout is not None:
                    process.stdout.close()
                with self._lock:
                    if self._process is process:
                        self._process = None
            if self.stop.is_set():
                return
            if self._timed_out or not emitted:
                self._send(ReviewFrame(None, note="재생하지 못했습니다. 건너뛰거나 저장을 선택할 수 있습니다."))
                return

    def _read_exact(self, pipe, length):
        raw = bytearray()
        while len(raw) < length and not self.stop.is_set():
            data = pipe.read(length - len(raw))
            if not data:
                break
            raw.extend(data)
        return bytes(raw)

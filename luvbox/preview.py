"""Small local media previews. Decoding never contacts X or writes media."""

from collections import OrderedDict
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
import queue
import subprocess
from threading import Condition, Thread
import warnings

from PIL import Image, ImageOps, UnidentifiedImageError

from .thumbcache import cloud_only


PREVIEW_SIZE = (220, 168)
MAX_PIXELS = 60_000_000


@dataclass(frozen=True)
class Thumbnail:
    image: Image.Image | None
    note: str


def thumbnail(path: Path, size=PREVIEW_SIZE, *, suffix: str | None = None) -> Thumbnail:
    """Decode one image/frame, with bounded output and no external protocols.

    ``suffix`` names the type of a file whose name does not show it yet,
    such as a download's temporary ``.part`` file.
    """
    path = Path(path)
    kind = (suffix if suffix is not None else path.suffix).lower()
    if cloud_only(path):
        return Thumbnail(None, "파일이 클라우드에만 있습니다. 탐색기에서 내려받은 뒤 다시 확인해 주세요.")
    if not path.is_file():
        return Thumbnail(None, "파일이 이동되었거나 삭제되었습니다.")
    try:
        if kind == ".mp4":
            from .media import ffmpeg_path
            executable = ffmpeg_path()
            if executable is None:
                return Thumbnail(None, "동영상 미리보기 도구가 없습니다. 파일 열기로 확인해 주세요.")
            result = subprocess.run(
                [str(executable), "-nostdin", "-v", "error", "-protocol_whitelist", "file,pipe",
                 "-threads", "1", "-i", str(path.resolve()), "-an", "-sn", "-dn", "-frames:v", "1",
                 "-vf", f"scale={size[0]}:{size[1]}:force_original_aspect_ratio=decrease",
                 "-f", "image2pipe", "-vcodec", "bmp", "pipe:1"],
                capture_output=True, timeout=8,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if result.returncode or not result.stdout:
                return Thumbnail(None, "동영상 미리보기를 만들지 못했습니다. 파일 열기로 확인해 주세요.")
            source = BytesIO(result.stdout)
            note = "동영상 · 첫 프레임"
        elif kind in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
            source = path
            note = "GIF · 첫 프레임" if kind == ".gif" else "사진 미리보기"
        else:
            return Thumbnail(None, "미리보기를 지원하지 않는 파일입니다.")
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(source) as original:
                if original.width * original.height > MAX_PIXELS:
                    return Thumbnail(None, "큰 이미지입니다. 파일 열기로 확인해 주세요.")
                original.draft("RGB", size)
                image = ImageOps.exif_transpose(original)
                image.thumbnail(size, Image.Resampling.LANCZOS)
                image = image.convert("RGBA")
        return Thumbnail(image, note)
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError,
            Image.DecompressionBombWarning, subprocess.SubprocessError):
        return Thumbnail(None, "미리보기를 읽지 못했습니다. 파일 열기로 확인해 주세요.")


class PreviewLoader:
    """One daemon decoder, one pending selection and a small in-memory cache."""

    def __init__(self, results, *, decoder=thumbnail, cache_size=24):
        self.results = results
        self.decoder = decoder
        self.cache_size = cache_size
        self.cache = OrderedDict()
        self.condition = Condition()
        self.pending = None
        self.closed = False
        self.thread = None

    def request(self, token, path, permitted, *, remember=None, recall=None):
        """``remember(image)`` keeps a decoded preview; ``recall()`` returns a
        kept one when the file itself cannot be read. Both run on this thread."""
        with self.condition:
            if self.closed:
                return
            self.pending = (token, Path(path), permitted, remember, recall)
            if self.thread is None:
                self.thread = Thread(target=self._work, daemon=True, name="local-media-preview")
                self.thread.start()
            self.condition.notify()

    def close(self):
        with self.condition:
            self.closed = True
            self.pending = None
            self.cache.clear()
            self.condition.notify()

    def _work(self):
        while True:
            with self.condition:
                self.condition.wait_for(lambda: self.closed or self.pending is not None)
                if self.closed:
                    return
                token, path, permitted, remember, recall = self.pending
                self.pending = None
            try:
                if not permitted(path):
                    result = Thumbnail(None, "파일 위치를 확인할 수 없습니다. 기록을 새로고침해 주세요.")
                else:
                    stat = path.stat()
                    key = (str(path.resolve()), stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
                    result = self.cache.get(key)
                    if result is None:
                        result = (Thumbnail(None, "파일이 클라우드에만 있습니다. 탐색기에서 내려받은 뒤 다시 확인해 주세요.")
                                  if cloud_only(path, status=stat) else self.decoder(path))
                        # A cloud file or busy decoder can fail temporarily
                        # without changing the file's size or timestamp.
                        if result.image is not None:
                            self.cache[key] = result
                            while len(self.cache) > self.cache_size:
                                self.cache.popitem(last=False)
                    else:
                        self.cache.move_to_end(key)
            except FileNotFoundError:
                result = Thumbnail(None, "파일이 이동되었거나 삭제되었습니다.")
            except Exception:
                result = Thumbnail(None, "미리보기를 읽지 못했습니다. 파일 열기로 확인해 주세요.")
            try:
                if result.image is not None and remember is not None:
                    remember(result.image)
                elif result.image is None and recall is not None:
                    kept = recall()
                    if kept is not None:
                        result = Thumbnail(kept, "저장해 둔 작은 미리보기 · " + result.note)
            except Exception:
                pass
            with self.condition:
                if self.closed:
                    self.cache.clear()
                    return
                # UI validates this token again. Retain at most a few decoded
                # results even when the window is temporarily busy elsewhere.
                while self.results.full():
                    try:
                        self.results.get_nowait()
                    except queue.Empty:
                        break
                self.results.put_nowait((token, result))

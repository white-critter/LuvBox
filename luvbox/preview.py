"""Small local media previews. Decoding never contacts X or writes media."""

from dataclasses import dataclass
import errno
from functools import cache
from io import BytesIO
from pathlib import Path
import subprocess
import warnings
import struct
import zlib

from PIL import Image, ImageOps, UnidentifiedImageError

from .thumbcache import cloud_only
from .file_access import is_read_locked


PREVIEW_SIZE = (220, 168)
MAX_PIXELS = 60_000_000
PHOTO_FORMATS = ("JPEG", "PNG", "WEBP", "GIF")


def _png_exif_orientation(path: Path) -> int:
    """Read a possibly late eXIf chunk without inflating PNG image data."""
    with path.open("rb") as stream:
        if stream.read(8) != b"\x89PNG\r\n\x1a\n":
            raise ValueError("invalid PNG header")
        length = path.stat().st_size
        if length > 256 * 1024 * 1024:
            raise ValueError("PNG metadata scan limit")
        for _ in range(4096):
            header = stream.read(8)
            if len(header) != 8:
                raise ValueError("short PNG chunk")
            size, kind = struct.unpack(">I4s", header)
            if size > length - stream.tell() - 4:
                raise ValueError("invalid PNG chunk size")
            if kind == b"eXIf":
                if not 0 < size <= 1024 * 1024:
                    raise ValueError("PNG EXIF limit")
                payload = stream.read(size)
                checksum = stream.read(4)
                if len(checksum) != 4 or struct.unpack(">I", checksum)[0] != zlib.crc32(kind + payload):
                    raise ValueError("invalid PNG EXIF checksum")
                metadata = Image.Exif()
                metadata.load(payload)
                return metadata.get(274, 1)
            stream.seek(size + 4, 1)
            if kind == b"IEND":
                return 1
        raise ValueError("PNG chunk count limit")


def photo_info(path: Path, max_pixels: int = 16_000_000) -> dict[str, int]:
    """Return safe raw/oriented still-photo sizes without decoding pixels.

    The caller must validate its catalog path and file signature before and
    after this read. Unknown, animated GIF and oversized sources are excluded
    from original-resolution viewing rather than removing the preview limit.
    """
    path = Path(path)
    if (type(max_pixels) is not int or not 0 < max_pixels <= MAX_PIXELS or
            path.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"} or
            path.is_symlink() or getattr(path, "is_junction", lambda: False)() or cloud_only(path)):
        return {}
    try:
        # WebP's container reader retains compressed bytes even for a header
        # query; bound that allocation as well as the decoded-pixel budget.
        if path.stat().st_size > 64 * 1024 * 1024:
            return {}
        _prepare_decoders(False)
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path, formats=("JPEG", "PNG", "WEBP")) as image:
                width, height = image.size
                if (not 0 < width <= 16384 or not 0 < height <= 16384 or
                        width * height > max_pixels):
                    return {}
                if image.format == "PNG":
                    # PngImageFile.getexif() calls load() when EXIF follows
                    # IDAT. Skip image chunks explicitly to stay header-only.
                    orientation = _png_exif_orientation(path)
                else:
                    orientation = image.getexif().get(274, 1)
                oriented = (height, width) if orientation in (5, 6, 7, 8) else (width, height)
                return {"pixel_width": oriented[0], "pixel_height": oriented[1],
                        "source_width": width, "source_height": height}
    except (OSError, ValueError, TypeError, SyntaxError, struct.error,
            Image.DecompressionBombError, Image.DecompressionBombWarning):
        return {}


def photo_dimensions(path: Path, max_pixels: int = 16_000_000) -> tuple[int, int]:
    """Return the safe display size; unknown or excluded inputs return zeros."""
    details = photo_info(path, max_pixels)
    return details.get("pixel_width", 0), details.get("pixel_height", 0)


@cache
def _prepare_decoders(video: bool) -> None:
    """Register our raster readers on the decoder thread, once per kind.

    Pillow otherwise initializes every plugin when an earlier format in the
    allowlist is not registered, even if the filename's reader is available.
    Keep the full allowlist so a supported image with a renamed extension can
    still be identified from its contents.
    """
    if video:
        from PIL import BmpImagePlugin
    else:
        from PIL import GifImagePlugin, JpegImagePlugin, PngImagePlugin, WebPImagePlugin


@dataclass(frozen=True)
class Thumbnail:
    image: Image.Image | None
    note: str
    retryable: bool = False


def oriented_thumbnail(original: Image.Image, size) -> Image.Image:
    """Shrink before copying/rotating pixels; return a detached RGBA preview.

    EXIF quarter turns swap the decoder's target bounds. Reducing the open
    image first avoids a full-size copy for formats without JPEG's draft
    decoding, while preserving the displayed orientation and aspect ratio.
    """
    orientation = original.getexif().get(274)
    bounds = size[::-1] if orientation in (5, 6, 7, 8) else size
    original.draft("RGB", bounds)
    original.thumbnail(bounds, Image.Resampling.LANCZOS)
    image = ImageOps.exif_transpose(original)
    try:
        return image.convert("RGBA")
    finally:
        if image is not original:
            image.close()


def thumbnail(path: Path, size=PREVIEW_SIZE, *, suffix: str | None = None) -> Thumbnail:
    """Decode one image/frame, with bounded output and no external protocols.

    ``suffix`` names the type of a file whose name does not show it yet,
    such as a download's temporary ``.part`` file.
    """
    path = Path(path)
    # Metadata writes already refuse links. A preview must not follow one either.
    if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
        return Thumbnail(None, "파일이 이동되었거나 삭제되었습니다.")
    kind = (suffix if suffix is not None else path.suffix).lower()
    if cloud_only(path):
        return Thumbnail(None, "파일이 클라우드에만 있습니다. 탐색기에서 내려받은 뒤 다시 확인해 주세요.")
    if not path.is_file():
        return Thumbnail(None, "파일이 이동되었거나 삭제되었습니다.")
    source = None
    try:
        if kind == ".mp4":
            from .media import ffmpeg_path
            executable = ffmpeg_path()
            if executable is None:
                return Thumbnail(None, "동영상 미리보기 도구가 없습니다. 파일 열기로 확인해 주세요.")
            # FFmpeg reports a sharing violation only as a generic exit code.
            # A bounded local read preserves Windows' precise lock error so
            # the optional saved card can retry without guessing from stderr.
            with path.open("rb", buffering=0) as check:
                check.read(1)
            result = subprocess.run(
                [str(executable), "-nostdin", "-v", "error", "-protocol_whitelist", "file,pipe",
                 "-threads", "1", "-f", "mov", "-i", str(path.resolve()), "-an", "-sn", "-dn", "-frames:v", "1",
                 "-vf", f"scale={size[0]}:{size[1]}:force_original_aspect_ratio=decrease",
                 "-f", "image2pipe", "-vcodec", "bmp", "pipe:1"],
                capture_output=True, timeout=8,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if result.returncode or not result.stdout:
                return Thumbnail(None, "동영상 미리보기를 만들지 못했습니다. 파일 열기로 확인해 주세요.")
            source = BytesIO(result.stdout)
            note = "동영상 · 첫 프레임"
        elif kind in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
            # Own the stream even when Pillow fails during its initial read,
            # before it can return an image context manager to close it.
            source = path.open("rb")
            note = "GIF · 첫 프레임" if kind == ".gif" else "사진 미리보기"
        else:
            return Thumbnail(None, "미리보기를 지원하지 않는 파일입니다.")
        _prepare_decoders(kind == ".mp4")
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            # Inspect only our supported raster formats. A renamed EPS file
            # must never dispatch to Pillow's external Ghostscript renderer.
            with Image.open(source, formats=("BMP",) if kind == ".mp4" else PHOTO_FORMATS) as original:
                if original.width * original.height > MAX_PIXELS:
                    return Thumbnail(None, "큰 이미지입니다. 파일 열기로 확인해 주세요.")
                image = oriented_thumbnail(original, size)
        return Thumbnail(image, note)
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError,
            Image.DecompressionBombWarning, subprocess.SubprocessError) as exc:
        retryable = isinstance(exc, OSError) and (
            getattr(exc, "winerror", None) in {32, 33}
            or (exc.errno == errno.EACCES and is_read_locked(path)))
        return Thumbnail(None, "미리보기를 읽지 못했습니다. 파일 열기로 확인해 주세요.", retryable)
    finally:
        if source is not None:
            source.close()

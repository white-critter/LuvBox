"""Small local media previews. Decoding never contacts X or writes media."""

from dataclasses import dataclass
from functools import cache
from io import BytesIO
from pathlib import Path
import subprocess
import warnings

from PIL import Image, ImageOps, UnidentifiedImageError

from .thumbcache import cloud_only


PREVIEW_SIZE = (220, 168)
MAX_PIXELS = 60_000_000
PHOTO_FORMATS = ("JPEG", "PNG", "WEBP", "GIF")


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
        image.close()


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
            source = path
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
            Image.DecompressionBombWarning, subprocess.SubprocessError):
        return Thumbnail(None, "미리보기를 읽지 못했습니다. 파일 열기로 확인해 주세요.")

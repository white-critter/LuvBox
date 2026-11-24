"""Local AI image features and zero-shot scores with a quantized SigLIP 2 vision model.

Every photo is turned into one L2-normalized 768-dimensional feature by the
SigLIP 2 Base Patch16-224 vision encoder (ONNX, 8-bit weights), run offline on
the CPU with onnxruntime. Which AI tags a photo gets is decided from those
features by ``tag_learner``; this module only encodes photos and scores them
against the five default tags:

- 일러스트 (illustration)
- 인물 (real person)
- 동물 (real animal)
- 짤·텍스트 (meme / text screenshot)
- 풍경·배경 (scenery / nature / interior / objects)

The text side of those five is precomputed (``tools/clip/precompute_siglip2_anchors.py``:
several English and Korean prompts per tag, averaged) and embedded below, so no
tokenizer and no text model are needed at runtime. SigLIP scores each image-text
pair on its own with a sigmoid, so removing some defaults does not push a photo
into the ones that are left.

The model weights are Apache-2.0 and are never part of LuvBox's files: they are
downloaded from Hugging Face, pinned to one commit and checked by size and SHA-256.
"""

from __future__ import annotations

import contextlib
import gc
import hashlib
import os
from pathlib import Path
import stat
import struct
import tempfile
import threading
from typing import Sequence
import urllib.parse
import urllib.request

import numpy as np
from PIL import Image

from . import __version__
from .ai_anchors import (  # noqa: F401  (re-exported for existing callers and tests)
    LOGIT_SCALE, ZERO_SHOT_CUTS, ZERO_SHOT_THRESHOLD, _CATEGORY_EMBEDDINGS_B64, _THRESHOLD_LOGIT,
    get_category_embeddings, zero_shot_probabilities,
)
from .clip_model import (  # noqa: F401  (these names are part of this module's interface)
    ACTIVE_MODEL_ID, CATEGORIES, CATEGORY_IDS, CATEGORY_NAMES, EMBEDDING_DIM, MODEL_FILENAME, MODEL_SIZE,
    bundled_model_path, default_cache_dir,
)
from .duplicates import _safe_local_file
from .file_mutex import replace_file
from .preview import MAX_PIXELS, MAX_RASTER_BYTES, PHOTO_FORMATS, _BoundedRasterReader, _prepare_decoders

try:
    import onnxruntime as ort
    _ONNXRUNTIME_AVAILABLE = True
except ImportError:
    ort = None
    _ONNXRUNTIME_AVAILABLE = False


MODEL_REPOSITORY = "onnx-community/siglip2-base-patch16-224-ONNX"
MODEL_COMMIT = "ba1f3b0843f24bc5417d38e19c37b287d719b2f4"
MODEL_URL = (f"https://huggingface.co/{MODEL_REPOSITORY}/resolve/{MODEL_COMMIT}"
             "/onnx/vision_model_int8.onnx")
MODEL_SHA256 = "0dd31785a2713f1113ef2272472165c69d580473dae38d7b47568ac587795e70"
MAX_DOWNLOAD = 120 * 1024 * 1024
INPUT_SIZE = 224

_SESSION_LOCK = threading.Lock()
_VERIFIED_LOCK = threading.Lock()
_DOWNLOAD_LOCK = threading.Lock()
_SESSION = None
_SESSION_USERS = 0
_VERIFIED_MODEL_SIG: tuple[str, int, int] | None = None


def _is_link(path: Path) -> bool:
    try:
        return path.is_symlink() or getattr(path, "is_junction", lambda: False)()
    except OSError:
        return True


def _ensure_safe_parent_dirs(destination: Path) -> None:
    """Reject symlinks or junctions anywhere in the target path hierarchy."""
    for candidate in (destination, *destination.parents):
        if _is_link(candidate):
            raise ValueError("Unsafe symlink or junction in model destination path")


class _HttpsOnlyRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Allow only HTTPS redirects during model weight downloads."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urllib.parse.urlsplit(newurl)
        if parsed.scheme.lower() != "https" or not parsed.netloc:
            raise urllib.error.URLError("Refusing non-HTTPS redirect during model download")
        return super().redirect_request(req, fp, code, msg, headers, newurl)



def resolve_model_path() -> Path:
    """Find existing model file or return target cache destination."""
    bundled = bundled_model_path()
    if bundled.is_file() and is_model_ready(bundled):
        return bundled
    return default_cache_dir() / MODEL_FILENAME


def is_model_ready(path: Path | None = None) -> bool:
    """Verify that the ONNX model exists and matches the expected size and SHA-256.

    Caches the verified (resolved_path, size, mtime_ns) tuple so batch inference
    does not re-hash the 90 MB file on every batch.
    """
    global _VERIFIED_MODEL_SIG
    if path is None:
        bundled = bundled_model_path()
        if bundled.is_file() and is_model_ready(bundled):
            return True
        target = default_cache_dir() / MODEL_FILENAME
    else:
        target = path
    try:
        _ensure_safe_parent_dirs(target)
        if not _safe_local_file(target):
            return False
        with target.open("rb") as f:
            st = os.fstat(f.fileno())
            if not stat.S_ISREG(st.st_mode) or st.st_size != MODEL_SIZE:
                return False
            sig = (os.path.normcase(str(target.resolve())), int(st.st_size), int(st.st_mtime_ns))
            with _VERIFIED_LOCK:
                if _VERIFIED_MODEL_SIG == sig:
                    return True
            h = hashlib.sha256()
            total_read = 0
            while chunk := f.read(1024 * 1024):
                total_read += len(chunk)
                if total_read > MODEL_SIZE:
                    return False
                h.update(chunk)
            if total_read == MODEL_SIZE and h.hexdigest().lower() == MODEL_SHA256.lower():
                with _VERIFIED_LOCK:
                    _VERIFIED_MODEL_SIG = sig
                return True
        return False
    except (OSError, ValueError):
        return False


verify_file_hash = is_model_ready


def is_available() -> bool:
    """Check if onnxruntime is installed and the model is ready for inference."""
    if not _ONNXRUNTIME_AVAILABLE:
        return False
    with _SESSION_LOCK:
        if _SESSION is not None:
            return True
    return is_model_ready()


def download_model(destination: Path | None = None, progress_callback=None) -> Path:
    """Download model with HTTPS enforcement, bounded size, SHA-256 verification, and atomic replace."""
    if destination is None:
        destination = default_cache_dir() / MODEL_FILENAME
    with _DOWNLOAD_LOCK:
        _ensure_safe_parent_dirs(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        _ensure_safe_parent_dirs(destination)
        destination = destination.resolve()

        if is_model_ready(destination):
            return destination

        temp_dir = destination.parent
        hasher = hashlib.sha256()
        bytes_downloaded = 0
        tmp_path: Path | None = None

        try:
            with tempfile.NamedTemporaryFile(dir=temp_dir, prefix="siglip_download_", suffix=".tmp", delete=False) as tmp_file:
                tmp_path = Path(tmp_file.name)
                opener = urllib.request.build_opener(_HttpsOnlyRedirectHandler())
                req = urllib.request.Request(
                    MODEL_URL,
                    headers={"User-Agent": f"LuvBox/{__version__} (Windows; Local SigLIP Model Fetcher)"},
                )
                with opener.open(req, timeout=30) as response:
                    content_length = response.headers.get("Content-Length")
                    total_size = int(content_length) if content_length and content_length.isdigit() else MODEL_SIZE
                    while True:
                        chunk = response.read(64 * 1024)
                        if not chunk:
                            break
                        bytes_downloaded += len(chunk)
                        if bytes_downloaded > MAX_DOWNLOAD:
                            raise ValueError(f"Download exceeded maximum safe size ({MAX_DOWNLOAD} bytes)")
                        hasher.update(chunk)
                        tmp_file.write(chunk)
                        if progress_callback:
                            progress_callback(bytes_downloaded, total_size)
                tmp_file.flush()
                os.fsync(tmp_file.fileno())

            # File handle is now closed (required on Windows before atomic replace).
            digest = hasher.hexdigest().lower()
            if digest != MODEL_SHA256.lower():
                raise ValueError(f"SHA-256 hash mismatch: expected {MODEL_SHA256}, got {digest}")
            if bytes_downloaded != MODEL_SIZE:
                raise ValueError(f"File size mismatch: expected {MODEL_SIZE}, got {bytes_downloaded}")

            _ensure_safe_parent_dirs(destination)
            replace_file(tmp_path, destination)
            is_model_ready(destination)
            return destination
        except BaseException:
            if tmp_path is not None and tmp_path.is_file():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
            raise


def ensure_model(destination: Path | None = None, progress_callback=None) -> Path:
    """Ensure the model file exists, downloading it if necessary."""
    from .clip_model import forget_legacy_model

    target = destination or resolve_model_path()
    if not is_model_ready(target):
        target = download_model(target, progress_callback=progress_callback)
    in_app_cache = destination is None
    if not in_app_cache:
        try:
            in_app_cache = Path(target).resolve().is_relative_to(default_cache_dir().resolve())
        except OSError:
            in_app_cache = False
    if in_app_cache:
        forget_legacy_model()               # the CLIP model of 1.11 and earlier is read no more
    return target


def get_session(model_path: Path | None = None):
    """Return or initialize a cached onnxruntime InferenceSession."""
    global _SESSION
    with _SESSION_LOCK:
        if _SESSION is not None and model_path is None:
            return _SESSION
        if not _ONNXRUNTIME_AVAILABLE:
            return None
    target = model_path or resolve_model_path()
    if not is_model_ready(target):
        return None
    with _SESSION_LOCK:
        if _SESSION is not None and model_path is None:
            return _SESSION
        if not _ONNXRUNTIME_AVAILABLE:
            return None
        opts = ort.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = min(4, os.cpu_count() or 1)
        opts.enable_cpu_mem_arena = False
        sess = ort.InferenceSession(str(target), opts, providers=["CPUExecutionProvider"])
        if model_path is None:
            _SESSION = sess
        return sess


@contextlib.contextmanager
def session_use():
    """Keep the shared ONNX InferenceSession alive across concurrent classification scopes."""
    global _SESSION_USERS
    with _SESSION_LOCK:
        _SESSION_USERS += 1
    try:
        yield
    finally:
        with _SESSION_LOCK:
            _SESSION_USERS = max(0, _SESSION_USERS - 1)


def release_session(force: bool = False) -> None:
    """Release the cached ONNX InferenceSession to free memory when idle."""
    global _SESSION
    dropped = None
    with _SESSION_LOCK:
        if _SESSION is not None and (force or _SESSION_USERS == 0):
            dropped = _SESSION
            _SESSION = None
    if dropped is not None:
        del dropped
        gc.collect()


def feature_output(session) -> str:
    """The name of the pooled image feature among a session's outputs.

    The export lists ``last_hidden_state`` (B, 196, 768) first, so the first
    output is not the feature. ``pooler_output`` is, or ``image_embeds`` in
    another export; failing both, the one output shaped (batch, 768).
    """
    outputs = list(session.get_outputs())
    names = [output.name for output in outputs]
    for wanted in ("pooler_output", "image_embeds"):
        if wanted in names:
            return wanted
    for output in outputs:
        shape = list(getattr(output, "shape", []) or [])
        if len(shape) == 2 and shape[-1] == EMBEDDING_DIM:
            return output.name
    raise RuntimeError("The vision model has no pooled image output")


_EXIF_TRANSPOSE = {
    2: Image.Transpose.FLIP_LEFT_RIGHT,
    3: Image.Transpose.ROTATE_180,
    4: Image.Transpose.FLIP_TOP_BOTTOM,
    5: Image.Transpose.TRANSPOSE,
    6: Image.Transpose.ROTATE_270,
    7: Image.Transpose.TRANSVERSE,
    8: Image.Transpose.ROTATE_90,
}


@contextlib.contextmanager
def _open_rgb_image(path: Path):
    """Safely open a local raster image and yield ``(rgb_image, exif_orientation)``."""
    _prepare_decoders(False)
    with path.open("rb") as source:
        if os.fstat(source.fileno()).st_size > MAX_RASTER_BYTES:
            raise ValueError("Image exceeds maximum raster byte limit")
        with Image.open(_BoundedRasterReader(source), formats=PHOTO_FORMATS) as img:
            if img.width <= 0 or img.height <= 0 or img.width * img.height > MAX_PIXELS:
                raise Image.DecompressionBombError("Image exceeds safe pixel count")
            try:
                orientation = img.getexif().get(274, 1)
            except (OSError, ValueError, SyntaxError, TypeError, struct.error):
                orientation = 1
            rgb = img if img.mode == "RGB" else img.convert("RGB")
            try:
                if rgb.width <= 0 or rgb.height <= 0:
                    raise ValueError("Image has empty dimensions")
                yield rgb, orientation
            finally:
                if rgb is not img:
                    rgb.close()


@contextlib.contextmanager
def _load_rgb_image(path: Path):
    """Safely open a local raster image, apply EXIF orientation, and yield an RGB Image."""
    with _open_rgb_image(path) as (rgb, orientation):
        method = _EXIF_TRANSPOSE.get(orientation)
        transposed = rgb.transpose(method) if method is not None else rgb
        try:
            yield transposed
        finally:
            if transposed is not rgb:
                transposed.close()


def preprocess_image(image_path: Path | str) -> np.ndarray | None:
    """Safely load a photo, resize to 224x224, apply EXIF orientation, and scale to [-1, 1].

    SigLIP's image processor does not crop: a tall picture is squeezed, not cut,
    so the top and bottom of a webtoon panel or a long meme still count.
    Resizing before the EXIF transpose avoids copying multi-megapixel rotated
    buffers while preserving identical square 224x224 output.
    """
    path = Path(image_path)
    if not _safe_local_file(path):
        return None
    try:
        with _open_rgb_image(path) as (rgb, orientation):
            resized = rgb.resize((INPUT_SIZE, INPUT_SIZE), Image.Resampling.BILINEAR)
            try:
                method = _EXIF_TRANSPOSE.get(orientation)
                oriented = resized.transpose(method) if method is not None else resized
                try:
                    arr = np.array(oriented, dtype=np.float32)
                finally:
                    if oriented is not resized:
                        oriented.close()
            finally:
                resized.close()
        arr *= np.float32(2.0 / 255.0)
        arr -= np.float32(1.0)
        return np.ascontiguousarray(np.transpose(arr, (2, 0, 1)))
    except Exception:
        return None


def _transient_io(exc: OSError) -> bool:
    """Sharing, permission, and disappearance should be tried again next time."""
    if isinstance(exc, FileNotFoundError):
        return True
    winerror = getattr(exc, "winerror", None)
    if winerror in {5, 32, 33}:
        return True
    return exc.errno in {1, 11, 13, 16}


def encode_skip_signature(image_path: Path | str) -> tuple[int, int] | None:
    """Return ``(size, mtime_ns)`` when this file cannot be encoded until it changes.

    A locked or missing file returns None so the next scan tries again. A file
    that opens and is still not a usable photo is safe to skip.
    """
    path = Path(image_path)
    if not _safe_local_file(path):
        return None
    try:
        info = path.stat()
    except OSError:
        return None
    signature = (int(info.st_size), int(info.st_mtime_ns))
    if info.st_size > MAX_RASTER_BYTES:
        return signature
    try:
        with _load_rgb_image(path) as rgb:
            rgb.load()
    except (Image.UnidentifiedImageError, Image.DecompressionBombError, Image.DecompressionBombWarning,
            SyntaxError, ValueError, TypeError, EOFError, struct.error):
        return signature
    except OSError as exc:
        return None if _transient_io(exc) else signature
    except Exception:
        return None
    return None


def encode_batch(tensors: np.ndarray, session) -> np.ndarray:
    """L2-normalized features of a ``(n, 3, 224, 224)`` batch."""
    output = feature_output(session)
    embeds = np.asarray(session.run([output], {"pixel_values": tensors})[0], dtype=np.float32)
    if embeds.ndim != 2 or embeds.shape[1] != EMBEDDING_DIM or embeds.shape[0] != tensors.shape[0]:
        raise RuntimeError("The vision model returned an unexpected feature shape")
    if not np.all(np.isfinite(embeds)):
        raise RuntimeError("The vision model returned non-finite features")
    norms = np.maximum(np.linalg.norm(embeds, axis=-1, keepdims=True), 1e-12)
    normalized = embeds / norms
    if not np.all(np.isfinite(normalized)):
        raise RuntimeError("The vision model returned non-finite features")
    return normalized


def classify_detailed(
    image_paths: Sequence[Path | str],
    threshold: float | None = None,
    batch_size: int = 16,
    session=None,
) -> list[tuple[str | None, float, np.ndarray | None]]:
    """Encode images and score them against the default tags.

    Each item is ``(best default tag, its score, embedding)``. The tag is
    ``None`` when the image cannot be read or the best score is below
    ``threshold`` (by default the calibrated zero-shot threshold). The embedding
    is 768-d float32 and L2-normalized, or ``None`` when encoding fails.
    """
    results: list[tuple[str | None, float, np.ndarray | None]] = [(None, 0.0, None)] * len(image_paths)
    if not image_paths:
        return results
    batch_size = max(1, min(int(batch_size), 128))
    threshold = ZERO_SHOT_THRESHOLD if threshold is None else float(threshold)

    sess = session or get_session()
    if sess is None:
        return results

    for batch_start in range(0, len(image_paths), batch_size):
        batch_slice = image_paths[batch_start : batch_start + batch_size]
        valid_indices: list[int] = []
        valid_tensors: list[np.ndarray] = []

        for idx_offset, img_path in enumerate(batch_slice):
            tensor = preprocess_image(img_path)
            if tensor is not None:
                valid_indices.append(batch_start + idx_offset)
                valid_tensors.append(tensor)

        if not valid_tensors:
            continue

        embeds = encode_batch(np.stack(valid_tensors, axis=0), sess)
        probs = zero_shot_probabilities(embeds)

        for i, global_idx in enumerate(valid_indices):
            best_cat_idx = int(np.argmax(probs[i]))
            best_conf = float(probs[i, best_cat_idx])
            vector = np.ascontiguousarray(embeds[i], dtype=np.float32)
            if best_conf >= threshold:
                results[global_idx] = (CATEGORY_NAMES[best_cat_idx], best_conf, vector)
            else:
                results[global_idx] = (None, best_conf, vector)

    return results


def classify_batch(
    image_paths: Sequence[Path | str],
    threshold: float | None = None,
    batch_size: int = 16,
    session=None,
) -> list[tuple[str | None, float]]:
    """Classify a list of image paths into (best default tag, score)."""
    detailed = classify_detailed(image_paths, threshold=threshold, batch_size=batch_size, session=session)
    return [(name, confidence) for name, confidence, _embedding in detailed]


def categories_from_embeddings(
    embeddings: np.ndarray,
    threshold: float | None = None,
) -> list[tuple[str | None, float]]:
    """Score stored image features against the default tags without the vision model.

    ``embeddings`` is one 768-d vector or an ``(n, 768)`` matrix of the same
    L2-normalized features ``classify_detailed`` stores. The best tag of each
    row is named when its score reaches ``threshold``, otherwise ``None``.
    """
    probs = zero_shot_probabilities(embeddings)
    if probs.shape[0] == 0:
        return []
    threshold = ZERO_SHOT_THRESHOLD if threshold is None else float(threshold)
    best = np.argmax(probs, axis=-1)
    scored: list[tuple[str | None, float]] = []
    for row, index in enumerate(best):
        confidence = float(probs[row, int(index)])
        name = CATEGORY_NAMES[int(index)] if confidence >= threshold else None
        scored.append((name, confidence))
    return scored


def classify_image(
    image_path: Path | str,
    threshold: float | None = None,
    session=None,
) -> tuple[str | None, float]:
    """Classify a single image path into (best default tag, score)."""
    res = classify_batch([image_path], threshold=threshold, batch_size=1, session=session)
    return res[0] if res else (None, 0.0)

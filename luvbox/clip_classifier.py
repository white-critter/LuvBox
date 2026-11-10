"""Local AI Image Classification using quantized ONNX CLIP ViT-B/32.

Classifies library images offline into zero-shot categories:
- 일러스트 (illustration)
- 인물 (real person)
- 동물 (real animal)
- 짤·텍스트 (meme / text screenshot)
- 풍경·배경 (scenery / nature / interior / objects)

Text embeddings are pre-computed using multi-prompt ensembles and embedded
directly into this module, requiring zero tokenizer and zero text model at runtime.
"""

from __future__ import annotations

import base64
import gc
import hashlib
import os
from pathlib import Path
import tempfile
import threading
from typing import Sequence
import urllib.parse
import urllib.request
import warnings

import numpy as np
from PIL import Image, ImageOps

from . import __version__
from .duplicates import _safe_local_file
from .preview import MAX_PIXELS, MAX_RASTER_BYTES, PHOTO_FORMATS, _BoundedRasterReader, _prepare_decoders

try:
    import onnxruntime as ort
    _ONNXRUNTIME_AVAILABLE = True
except ImportError:
    ort = None
    _ONNXRUNTIME_AVAILABLE = False


MODEL_FILENAME = "clip_vit_b32_vision.onnx"
MODEL_URL = "https://huggingface.co/Xenova/clip-vit-base-patch32/resolve/main/onnx/vision_model_quantized.onnx"
MODEL_SHA256 = "583fd1110a514667812fee7d684952aaf82a99b959760c8d7dca7e0ab9839299"
MODEL_SIZE = 89117001
MAX_DOWNLOAD = 120 * 1024 * 1024

CATEGORIES = [
    {"id": "illustration", "name": "일러스트"},
    {"id": "real_person", "name": "인물"},
    {"id": "real_animal", "name": "동물"},
    {"id": "meme_text", "name": "짤·텍스트"},
    {"id": "scenery", "name": "풍경·배경"},
]
CATEGORY_NAMES = [c["name"] for c in CATEGORIES]
CATEGORY_IDS = [c["id"] for c in CATEGORIES]

# Precomputed multi-prompt ensemble text embeddings (5 categories x 512-dim float32 L2-normalized)
_CATEGORY_EMBEDDINGS_B64 = "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"
_CATEGORY_EMBEDDINGS: np.ndarray | None = None

CLIP_MEAN = np.array([0.48145466, 0.4578275, 0.40821073], dtype=np.float32)
CLIP_STD = np.array([0.26862954, 0.26130258, 0.27577711], dtype=np.float32)
_CLIP_SCALE = (1.0 / (255.0 * CLIP_STD)).astype(np.float32)
_CLIP_OFFSET = (-CLIP_MEAN / CLIP_STD).astype(np.float32)

_SESSION_LOCK = threading.Lock()
_SESSION: ort.InferenceSession | None = None
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


def get_category_embeddings() -> np.ndarray:
    """Return the precomputed (5, 512) normalized category embeddings array."""
    global _CATEGORY_EMBEDDINGS
    if _CATEGORY_EMBEDDINGS is None:
        raw = base64.b64decode(_CATEGORY_EMBEDDINGS_B64)
        arr = np.frombuffer(raw, dtype=np.float32).reshape(len(CATEGORIES), 512)
        _CATEGORY_EMBEDDINGS = arr
    return _CATEGORY_EMBEDDINGS


def default_cache_dir() -> Path:
    """Return user app data directory for model storage."""
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "LuvBox" / "models" / "clip"
    return Path.home() / ".luvbox" / "models" / "clip"


def bundled_model_path() -> Path:
    """Return project runtime directory for pre-bundled model if present."""
    return Path(__file__).resolve().parents[1] / "tools" / "clip" / "runtime" / MODEL_FILENAME


def resolve_model_path() -> Path:
    """Find existing model file or return target cache destination."""
    bundled = bundled_model_path()
    if bundled.is_file() and is_model_ready(bundled):
        return bundled
    return default_cache_dir() / MODEL_FILENAME


def is_model_ready(path: Path | None = None) -> bool:
    """Verify that the ONNX model exists and matches the expected size and SHA-256.

    Caches the verified (resolved_path, size, mtime_ns) tuple so batch inference
    does not re-hash the 89 MB file on every batch.
    """
    global _VERIFIED_MODEL_SIG
    target = path if path is not None else (
        bundled_model_path() if bundled_model_path().is_file() else default_cache_dir() / MODEL_FILENAME
    )
    try:
        if not _safe_local_file(target):
            return False
        st = target.stat()
        if st.st_size != MODEL_SIZE:
            return False
        sig = (str(target.resolve()), st.st_size, st.st_mtime_ns)
        if _VERIFIED_MODEL_SIG == sig:
            return True
        h = hashlib.sha256()
        with target.open("rb") as f:
            while chunk := f.read(1024 * 1024):
                h.update(chunk)
        if h.hexdigest().lower() == MODEL_SHA256.lower():
            _VERIFIED_MODEL_SIG = sig
            return True
        return False
    except OSError:
        return False


verify_file_hash = is_model_ready


def is_available() -> bool:
    """Check if onnxruntime is installed and the model is ready for inference."""
    if not _ONNXRUNTIME_AVAILABLE:
        return False
    if _SESSION is not None:
        return True
    return is_model_ready()


def download_model(destination: Path | None = None, progress_callback=None) -> Path:
    """Download model with HTTPS enforcement, bounded size, SHA-256 verification, and atomic replace."""
    if destination is None:
        destination = default_cache_dir() / MODEL_FILENAME
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
        with tempfile.NamedTemporaryFile(dir=temp_dir, prefix="clip_download_", suffix=".tmp", delete=False) as tmp_file:
            tmp_path = Path(tmp_file.name)
            opener = urllib.request.build_opener(_HttpsOnlyRedirectHandler())
            req = urllib.request.Request(
                MODEL_URL,
                headers={"User-Agent": f"LuvBox/{__version__} (Windows; Local CLIP Model Fetcher)"},
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

        # File handle is now closed (required on Windows before atomic os.replace).
        digest = hasher.hexdigest().lower()
        if digest != MODEL_SHA256.lower():
            raise ValueError(f"SHA-256 hash mismatch: expected {MODEL_SHA256}, got {digest}")
        if bytes_downloaded != MODEL_SIZE:
            raise ValueError(f"File size mismatch: expected {MODEL_SIZE}, got {bytes_downloaded}")

        _ensure_safe_parent_dirs(destination)
        os.replace(str(tmp_path), str(destination))
        is_model_ready(destination)
        return destination
    except Exception:
        if tmp_path is not None and tmp_path.is_file():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise


def ensure_model(destination: Path | None = None, progress_callback=None) -> Path:
    """Ensure the model file exists, downloading it if necessary."""
    target = destination or resolve_model_path()
    if is_model_ready(target):
        return target
    return download_model(target, progress_callback=progress_callback)


def get_session(model_path: Path | None = None) -> ort.InferenceSession | None:
    """Return or initialize a cached onnxruntime InferenceSession."""
    global _SESSION
    if _SESSION is not None and model_path is None:
        return _SESSION
    with _SESSION_LOCK:
        if _SESSION is not None and model_path is None:
            return _SESSION
        if not _ONNXRUNTIME_AVAILABLE:
            return None
        target = model_path or resolve_model_path()
        if not is_model_ready(target):
            return None
        opts = ort.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = min(4, os.cpu_count() or 1)
        opts.enable_cpu_mem_arena = False
        sess = ort.InferenceSession(str(target), opts, providers=["CPUExecutionProvider"])
        if model_path is None:
            _SESSION = sess
        return sess


def release_session() -> None:
    """Release the cached ONNX InferenceSession to free memory when idle."""
    global _SESSION
    with _SESSION_LOCK:
        if _SESSION is not None:
            _SESSION = None
            gc.collect()


def preprocess_image(image_path: Path | str) -> np.ndarray | None:
    """Safely load, transpose, center-crop to 224x224 and normalize an image for CLIP."""
    path = Path(image_path)
    if not _safe_local_file(path):
        return None
    try:
        _prepare_decoders(False)
        with path.open("rb") as source, warnings.catch_warnings():
            if os.fstat(source.fileno()).st_size > MAX_RASTER_BYTES:
                return None
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(_BoundedRasterReader(source), formats=PHOTO_FORMATS) as img:
                if img.width <= 0 or img.height <= 0 or img.width * img.height > MAX_PIXELS:
                    return None
                transposed = ImageOps.exif_transpose(img)
                try:
                    w, h = transposed.size
                    if w <= 0 or h <= 0:
                        return None
                    short = min(w, h)
                    left = (w - short) * 0.5
                    top = (h - short) * 0.5
                    rgb = transposed if transposed.mode == "RGB" else transposed.convert("RGB")
                    try:
                        cropped = rgb.resize(
                            (224, 224),
                            Image.Resampling.BILINEAR,
                            box=(left, top, left + short, top + short),
                        )
                        try:
                            arr = np.array(cropped, dtype=np.float32)
                        finally:
                            cropped.close()
                    finally:
                        if rgb is not transposed:
                            rgb.close()
                    arr *= _CLIP_SCALE
                    arr += _CLIP_OFFSET
                    return np.ascontiguousarray(np.transpose(arr, (2, 0, 1)))
                finally:
                    if transposed is not img:
                        transposed.close()
    except Exception:
        return None


def classify_batch(
    image_paths: Sequence[Path | str],
    threshold: float = 0.45,
    batch_size: int = 16,
    session: ort.InferenceSession | None = None,
) -> list[tuple[str | None, float]]:
    """Classify a list of image paths into (category_name, confidence).

    Returns [(category_name, confidence), ...] matching image_paths length.
    If image fails to load or top confidence < threshold, category_name is None.
    """
    results: list[tuple[str | None, float]] = [(None, 0.0)] * len(image_paths)
    if not image_paths:
        return results
    batch_size = max(1, min(int(batch_size), 128))
    threshold = float(threshold)

    sess = session or get_session()
    if sess is None:
        return results

    category_embeds = get_category_embeddings()

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

        input_arr = np.stack(valid_tensors, axis=0)
        outputs = sess.run(None, {"pixel_values": input_arr})
        embeds = outputs[0]
        norms = np.linalg.norm(embeds, axis=-1, keepdims=True)
        norms = np.maximum(norms, 1e-12)
        embeds = embeds / norms

        sims = embeds @ category_embeds.T

        # Softmax with logit scale 100.0 (CLIP standard)
        logits = sims * 100.0
        max_logits = np.max(logits, axis=-1, keepdims=True)
        exp_logits = np.exp(logits - max_logits)
        probs = exp_logits / np.sum(exp_logits, axis=-1, keepdims=True)

        for i, global_idx in enumerate(valid_indices):
            best_cat_idx = int(np.argmax(probs[i]))
            best_conf = float(probs[i, best_cat_idx])
            if best_conf >= threshold:
                results[global_idx] = (CATEGORY_NAMES[best_cat_idx], best_conf)
            else:
                results[global_idx] = (None, best_conf)

    return results


def classify_image(
    image_path: Path | str,
    threshold: float = 0.45,
    session: ort.InferenceSession | None = None,
) -> tuple[str | None, float]:
    """Classify a single image path into (category_name, confidence)."""
    res = classify_batch([image_path], threshold=threshold, batch_size=1, session=session)
    return res[0] if res else (None, 0.0)

"""Download and cache the ONNX CLIP vision model.

Downloads the lightweight quantized CLIP ViT-B/32 vision model from Hugging Face
with strict SHA-256 validation into %LOCALAPPDATA%\\LuvBox\\models\\clip.
"""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import shutil
import sys
import tempfile
import urllib.request


MODEL_FILENAME = "clip_vit_b32_vision.onnx"
MODEL_URL = "https://huggingface.co/Xenova/clip-vit-base-patch32/resolve/main/onnx/vision_model_quantized.onnx"
MODEL_SHA256 = "583fd1110a514667812fee7d684952aaf82a99b959760c8d7dca7e0ab9839299"
MODEL_SIZE = 89117001
MAX_DOWNLOAD = 120 * 1024 * 1024


def default_cache_dir() -> Path:
    """Return local app data directory for model storage."""
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "LuvBox" / "models" / "clip"
    return Path.home() / ".luvbox" / "models" / "clip"


def bundled_model_path() -> Path:
    """Return project runtime directory for pre-bundled model if present."""
    return Path(__file__).resolve().parents[1] / "clip" / "runtime" / MODEL_FILENAME


def resolve_model_path() -> Path:
    """Find existing model file or return target cache destination."""
    bundled = bundled_model_path()
    if bundled.is_file() and verify_file_hash(bundled):
        return bundled
    cache_path = default_cache_dir() / MODEL_FILENAME
    return cache_path


def verify_file_hash(path: Path) -> bool:
    """Return True if path matches the expected SHA-256 hash and size."""
    try:
        if not path.is_file():
            return False
        if path.stat().st_size != MODEL_SIZE:
            return False
        h = hashlib.sha256()
        with path.open("rb") as f:
            while chunk := f.read(1024 * 1024):
                h.update(chunk)
        return h.hexdigest().lower() == MODEL_SHA256.lower()
    except OSError:
        return False


def download_model(destination: Path | None = None, progress_callback=None) -> Path:
    """Download model with bounded size, progress tracking, and atomic rename."""
    if destination is None:
        destination = default_cache_dir() / MODEL_FILENAME
    destination = destination.resolve()

    if verify_file_hash(destination):
        return destination

    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_dir = destination.parent
    hasher = hashlib.sha256()
    bytes_downloaded = 0

    with tempfile.NamedTemporaryFile(dir=temp_dir, prefix="clip_download_", suffix=".tmp", delete=False) as tmp_file:
        tmp_path = Path(tmp_file.name)
        try:
            req = urllib.request.Request(
                MODEL_URL,
                headers={"User-Agent": "LuvBox/1.6 (Windows; Python CLIP Downloader)"}
            )
            with urllib.request.urlopen(req, timeout=30) as response:
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
            digest = hasher.hexdigest().lower()
            if digest != MODEL_SHA256.lower():
                raise ValueError(f"SHA-256 hash mismatch: expected {MODEL_SHA256}, got {digest}")
            if bytes_downloaded != MODEL_SIZE:
                raise ValueError(f"File size mismatch: expected {MODEL_SIZE}, got {bytes_downloaded}")

            # Atomic replace
            shutil.move(str(tmp_path), str(destination))
            return destination
        except Exception:
            if tmp_path.is_file():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
            raise


def main():
    parser = argparse.ArgumentParser(description="Download and verify CLIP vision model for LuvBox.")
    parser.add_argument("--check", action="store_true", help="Check if model is present and valid")
    parser.add_argument("--dest", type=Path, default=None, help="Custom destination file path")
    args = parser.parse_args()

    target = args.dest if args.dest else resolve_model_path()

    if args.check:
        if verify_file_hash(target):
            print(f"OK: Model is valid at {target}")
            sys.exit(0)
        else:
            print(f"MISSING: Model is not found or corrupted at {target}")
            sys.exit(1)

    if verify_file_hash(target):
        print(f"Model already present and verified: {target}")
        sys.exit(0)

    print(f"Downloading CLIP vision model to {target}...")

    def progress(cur, tot):
        pct = (cur / tot * 100) if tot else 0
        sys.stdout.write(f"\rDownloading: {cur / (1024*1024):.1f} / {tot / (1024*1024):.1f} MB ({pct:.1f}%)")
        sys.stdout.flush()

    try:
        download_model(target, progress_callback=progress)
        print("\nDownload and SHA-256 verification complete!")
    except Exception as e:
        print(f"\nError downloading model: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

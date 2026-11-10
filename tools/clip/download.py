"""Download and cache the ONNX CLIP vision model.

Downloads the lightweight quantized CLIP ViT-B/32 vision model from Hugging Face
with strict SHA-256 validation into %LOCALAPPDATA%\\LuvBox\\models\\clip.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from luvbox.clip_classifier import (
    MAX_DOWNLOAD,
    MODEL_FILENAME,
    MODEL_SHA256,
    MODEL_SIZE,
    MODEL_URL,
    bundled_model_path,
    default_cache_dir,
    download_model,
    resolve_model_path,
    verify_file_hash,
)

__all__ = [
    "MAX_DOWNLOAD",
    "MODEL_FILENAME",
    "MODEL_SHA256",
    "MODEL_SIZE",
    "MODEL_URL",
    "bundled_model_path",
    "default_cache_dir",
    "download_model",
    "resolve_model_path",
    "verify_file_hash",
]


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

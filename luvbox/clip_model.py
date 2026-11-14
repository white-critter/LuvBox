"""Where the AI classifier keeps its model file.

Kept apart from ``clip_classifier`` so that asking "is the model on this PC?"
does not load numpy and onnxruntime. The gallery asks at start, to decide
whether newly saved photos can be classified on their own; most of the time
the answer is all it needs.
"""

from __future__ import annotations

import os
from pathlib import Path

MODEL_FILENAME = "clip_vit_b32_vision.onnx"
MODEL_SIZE = 89117001


def default_cache_dir() -> Path:
    """Return user app data directory for model storage."""
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "LuvBox" / "models" / "clip"
    return Path.home() / ".luvbox" / "models" / "clip"


def bundled_model_path() -> Path:
    """Return project runtime directory for pre-bundled model if present."""
    return Path(__file__).resolve().parents[1] / "tools" / "clip" / "runtime" / MODEL_FILENAME


def model_file_present() -> bool:
    """A model file of the expected size is here.

    Its content is verified (SHA-256) when the classifier loads it; this only
    says whether there is anything to load, so that nothing is ever downloaded
    or imported just to find out.
    """
    for candidate in (bundled_model_path(), default_cache_dir() / MODEL_FILENAME):
        try:
            if candidate.is_file() and not candidate.is_symlink() and candidate.stat().st_size == MODEL_SIZE:
                return True
        except OSError:
            continue
    return False

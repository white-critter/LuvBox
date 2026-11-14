"""What the app needs to know about the AI classifier without loading it.

Kept apart from ``clip_classifier`` so that asking "is the model on this PC?"
or "what are the categories called?" does not load numpy and onnxruntime. The
gallery asks both at start; most of the time the answers are all it needs.

Loading the classifier costs several hundred milliseconds, and onnxruntime asks
Windows (WMI) what machine this is while it loads. Until 1.9.7 the window read
the category names from the classifier while it was being built, so it waited
for that answer, for as long as Windows took to give it.
"""

from __future__ import annotations

import os
from pathlib import Path

MODEL_FILENAME = "clip_vit_b32_vision.onnx"
MODEL_SIZE = 89117001

# The categories a photo can be put in, in the order every list shows them.
CATEGORIES = [
    {"id": "illustration", "name": "일러스트"},
    {"id": "real_person", "name": "인물"},
    {"id": "real_animal", "name": "동물"},
    {"id": "meme_text", "name": "짤·텍스트"},
    {"id": "scenery", "name": "풍경·배경"},
]
CATEGORY_NAMES = [c["name"] for c in CATEGORIES]
CATEGORY_IDS = [c["id"] for c in CATEGORIES]


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

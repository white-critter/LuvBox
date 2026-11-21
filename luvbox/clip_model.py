"""What the app needs to know about the AI classifier without loading it.

Kept apart from ``clip_classifier`` so that asking "is the model on this PC?"
or "what are the default tags called?" does not load numpy and onnxruntime. The
gallery asks both at start; most of the time the answers are all it needs.

Loading the classifier costs several hundred milliseconds, and onnxruntime asks
Windows (WMI) what machine this is while it loads. Until 1.9.7 the window read
the category names from the classifier while it was being built, so it waited
for that answer, for as long as Windows took to give it.

After 1.11.9 the vision model is SigLIP 2 Base Patch16-224 (768-dimensional
image features). Up to 1.11.9 it was CLIP ViT-B/32 (512 dimensions); features
stored by those versions keep the model id ``clip_vit_b32`` and are never mixed
with the new ones. A photo that has only an old feature is encoded once more.
"""

from __future__ import annotations

import os
from pathlib import Path
import stat

ACTIVE_MODEL_ID = "siglip2_base_224"
LEGACY_MODEL_ID = "clip_vit_b32"
EMBEDDING_DIM = 768
# Bytes of one stored float32 feature, per model id.
_EMBEDDING_BYTES = {ACTIVE_MODEL_ID: 768 * 4, LEGACY_MODEL_ID: 512 * 4}

MODEL_FILENAME = "siglip2_base_patch16_224_vision.onnx"
MODEL_SIZE = 94553333
MODEL_FOLDER = "siglip2"

# The default AI tags, in the order every list shows them. A person can switch
# them off or delete them, and add tags of their own (see ``ai_profile``).
CATEGORIES = [
    {"id": "illustration", "name": "일러스트"},
    {"id": "real_person", "name": "인물"},
    {"id": "real_animal", "name": "동물"},
    {"id": "meme_text", "name": "짤·텍스트"},
    {"id": "scenery", "name": "풍경·배경"},
]
CATEGORY_NAMES = [c["name"] for c in CATEGORIES]
CATEGORY_IDS = [c["id"] for c in CATEGORIES]
DEFAULT_CATEGORIES = CATEGORY_NAMES


def embedding_bytes(model_id: str) -> int:
    """Size of one stored feature of ``model_id``; 0 for a model this version does not know."""
    return _EMBEDDING_BYTES.get(model_id, 0)


def default_cache_dir() -> Path:
    """Return user app data directory for model storage."""
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "LuvBox" / "models" / MODEL_FOLDER
    return Path.home() / ".luvbox" / "models" / MODEL_FOLDER


# The CLIP model of 1.11 and earlier (about 85 MB) and its unfinished downloads: this version
# reads none of them, so they go once its own model is in place (forget_legacy_model).
_LEGACY_FOLDER = "clip"
_LEGACY_FILES = frozenset({"clip_vit_b32_vision.onnx"})
_NAME_SURROGATE = 0x20000000
_CLOUD_ATTRIBUTES = 0x00400000 | 0x00040000 | 0x00001000


def _linked(details) -> bool:
    return bool(stat.S_ISLNK(details.st_mode) or getattr(details, "st_reparse_tag", 0) & _NAME_SURROGATE)


def _path_linked(path: Path) -> bool:
    try:
        details = path.lstat()
    except FileNotFoundError:
        return False
    return _linked(details) or bool(getattr(path, "is_junction", lambda: False)())


def _safe_ancestor_chain(path: Path, stop_at: Path | None) -> bool:
    for folder in (path, *path.parents):
        if _path_linked(folder):
            return False
        if stop_at is not None and folder == stop_at:
            break
    return True


def forget_legacy_model() -> int:
    """Remove the old model's files by their exact names; the folder too when nothing else is in it.

    A folder or file reached through a link or junction is left alone, and so is
    anything else the folder holds. Returns how many files went.
    """
    base = default_cache_dir().parent
    folder = base / _LEGACY_FOLDER
    try:
        if not _safe_ancestor_chain(folder, base.parent) or not folder.is_dir():
            return 0
        entries = list(os.scandir(folder))
    except OSError:
        return 0
    removed = 0
    for entry in entries:
        known = entry.name in _LEGACY_FILES or (entry.name.startswith("clip_download_") and entry.name.endswith(".tmp"))
        try:
            if (known and entry.is_file(follow_symlinks=False)
                    and not entry.is_symlink() and not getattr(entry, "is_junction", lambda: False)()
                    and not _linked(entry.stat(follow_symlinks=False))):
                os.remove(entry.path)
                removed += 1
        except OSError:
            continue
    try:
        folder.rmdir()                      # only when empty
    except OSError:
        pass
    return removed


def bundled_model_path() -> Path:
    """A developer's copy beside the sources (``tools/clip/runtime``), never part of a release."""
    return Path(__file__).resolve().parents[1] / "tools" / "clip" / "runtime" / MODEL_FILENAME


def model_file_present() -> bool:
    """A model file of the expected size is here.

    Its content is verified (SHA-256) when the classifier loads it; this only
    says whether there is anything to load, so that nothing is ever downloaded
    or imported just to find out.
    """
    bundled = bundled_model_path()
    cached = default_cache_dir() / MODEL_FILENAME
    for candidate, stop_at in ((bundled, bundled.parent.parent), (cached, cached.parent.parent)):
        try:
            if not _safe_ancestor_chain(candidate, stop_at):
                continue
            info = candidate.lstat()
            if (stat.S_ISREG(info.st_mode) and not _linked(info)
                    and not (getattr(info, "st_file_attributes", 0) & _CLOUD_ATTRIBUTES)
                    and info.st_size == MODEL_SIZE):
                return True
        except OSError:
            continue
    return False

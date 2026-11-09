"""Duplicate image detection engine for LuvBox.

Uses SHA-256 for exact binary duplicates and dHash (difference hash)
via Pillow for perceptual duplicates (resized, re-encoded, slightly modified images).
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Sequence
from PIL import Image


def compute_dhash(image_path: Path | str, hash_size: int = 8) -> str:
    """Compute a 64-bit difference hash (dHash) as a 16-character hex string.

    Fast, robust against scaling and slight recompression.
    """
    path = Path(image_path)
    if not path.is_file():
        return ""
    try:
        with Image.open(path) as img:
            # Convert to grayscale and resize to (width + 1, height)
            resized = img.convert("L").resize((hash_size + 1, hash_size), Image.Resampling.BILINEAR)
            # Safe pixel extraction across Pillow versions without DeprecationWarning
            pixels = list(resized.tobytes())
            diff = []
            for row in range(hash_size):
                start = row * (hash_size + 1)
                for col in range(hash_size):
                    diff.append(pixels[start + col] > pixels[start + col + 1])
            decimal = 0
            for bit in diff:
                decimal = (decimal << 1) | bit
            # For solid color images (where all adjacent differences are False),
            # mix in the mean luminance so different solid colors don't collide.
            if decimal == 0:
                avg = sum(pixels) // len(pixels)
                decimal = avg
            return f"{decimal:016x}"
    except Exception:
        return ""


def compute_file_hash(file_path: Path | str) -> str:
    """Compute SHA-256 checksum of the file bytes."""
    path = Path(file_path)
    if not path.is_file():
        return ""
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            while chunk := f.read(65536):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return ""


def hamming_distance(hex1: str, hex2: str) -> int:
    """Compute Hamming distance between two 16-hex (64-bit) hashes."""
    if not hex1 or not hex2 or len(hex1) != 16 or len(hex2) != 16:
        return 64
    try:
        v1 = int(hex1, 16)
        v2 = int(hex2, 16)
        return (v1 ^ v2).bit_count()
    except ValueError:
        return 64


def cluster_duplicates(items: Sequence[dict], max_distance: int = 6) -> list[list[dict]]:
    """Group items by duplicate status.

    Each item must contain:
    - 'key': unique identity string
    - 'file_hash': exact SHA256 (optional, empty string if missing)
    - 'dhash': 16-hex perceptual hash (optional)

    Returns list of groups where each group has >= 2 items.
    Items with exact file_hash match are grouped first, then nearby dhash matches.
    """
    if len(items) < 2:
        return []

    # 1. Exact matches by file_hash
    exact_groups: dict[str, list[dict]] = {}
    remaining: list[dict] = []
    for item in items:
        fhash = item.get("file_hash") or ""
        if fhash:
            exact_groups.setdefault(fhash, []).append(item)
        else:
            remaining.append(item)

    groups: list[list[dict]] = []
    clustered_keys: set[str] = set()

    for fhash, group in exact_groups.items():
        if len(group) >= 2:
            groups.append(group)
            clustered_keys.update(it["key"] for it in group)
        else:
            remaining.extend(group)

    # 2. Perceptual matches by dhash for unclustered items
    dhash_items = [it for it in remaining if it.get("dhash") and len(it["dhash"]) == 16]

    # Quick exact dhash grouping first
    dhash_buckets: dict[str, list[dict]] = {}
    for it in dhash_items:
        dhash_buckets.setdefault(it["dhash"], []).append(it)

    for dh, bucket in dhash_buckets.items():
        if len(bucket) >= 2:
            groups.append(bucket)
            clustered_keys.update(it["key"] for it in bucket)

    unassigned = [it for it in dhash_items if it["key"] not in clustered_keys]

    # Pairwise comparison for items with hamming distance <= max_distance
    visited: set[str] = set()
    for i, it1 in enumerate(unassigned):
        k1 = it1["key"]
        if k1 in visited:
            continue
        current_cluster = [it1]
        dh1 = it1["dhash"]
        for j in range(i + 1, len(unassigned)):
            it2 = unassigned[j]
            k2 = it2["key"]
            if k2 in visited:
                continue
            if hamming_distance(dh1, it2["dhash"]) <= max_distance:
                current_cluster.append(it2)
                visited.add(k2)
        if len(current_cluster) >= 2:
            visited.add(k1)
            groups.append(current_cluster)

    return groups

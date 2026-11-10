"""Duplicate image detection engine for LuvBox.

Uses SHA-256 for exact binary duplicates and dHash (difference hash)
via Pillow for perceptual duplicates (resized, re-encoded, slightly modified images).
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Sequence
import warnings
from PIL import Image, ImageOps, UnidentifiedImageError

from .preview import MAX_PIXELS, MAX_RASTER_BYTES, PHOTO_FORMATS, _BoundedRasterReader, _prepare_decoders
from .thumbcache import cloud_only


def _safe_local_file(path: Path) -> bool:
    """Return True only for an ordinary, local, non-linked, non-cloud file."""
    try:
        if path.is_symlink() or getattr(path, "is_junction", lambda: False)() or cloud_only(path):
            return False
        return path.is_file()
    except OSError:
        return False


def compute_dhash(image_path: Path | str, hash_size: int = 8) -> str:
    """Compute a 64-bit difference hash (dHash) as a 16-character hex string.

    Fast, robust against scaling and slight recompression, bounded by the same
    raster security limits as gallery previews.
    """
    if type(hash_size) is not int or not 4 <= hash_size <= 16:
        return ""
    path = Path(image_path)
    if not _safe_local_file(path):
        return ""
    try:
        with path.open("rb") as source:
            if os.fstat(source.fileno()).st_size > MAX_RASTER_BYTES:
                return ""
            return _dhash_from_stream(source, hash_size=hash_size)
    except OSError:
        return ""


def _dhash_from_stream(source, hash_size: int = 8) -> str:
    """Decode an open, size-checked binary stream and return its 16-hex dHash."""
    try:
        _prepare_decoders(False)
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(_BoundedRasterReader(source), formats=PHOTO_FORMATS) as img:
                if img.width * img.height > MAX_PIXELS:
                    return ""
                transposed = ImageOps.exif_transpose(img)
                try:
                    resized = transposed.convert("L").resize(
                        (hash_size + 1, hash_size), Image.Resampling.BILINEAR
                    )
                finally:
                    if transposed is not img:
                        transposed.close()
                pixels = list(resized.tobytes())
        diff = []
        for row in range(hash_size):
            start = row * (hash_size + 1)
            for col in range(hash_size):
                diff.append(pixels[start + col] > pixels[start + col + 1])
        decimal = 0
        for bit in diff:
            decimal = (decimal << 1) | bit
        if decimal == 0 and pixels:
            lum = sum(pixels) // len(pixels)
            decimal = sum(lum << (8 * i) for i in range(8))
        return f"{decimal:016x}"
    except (OSError, ValueError, TypeError, SyntaxError, UnidentifiedImageError,
            Image.DecompressionBombError, Image.DecompressionBombWarning):
        return ""


def compute_file_hash(file_path: Path | str) -> str:
    """Compute SHA-256 checksum of an ordinary local file within raster bounds."""
    path = Path(file_path)
    if not _safe_local_file(path):
        return ""
    try:
        h = hashlib.sha256()
        with path.open("rb") as f:
            if os.fstat(f.fileno()).st_size > MAX_RASTER_BYTES:
                return ""
            while chunk := f.read(65536):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return ""


def compute_image_hashes(file_path: Path | str, hash_size: int = 8) -> tuple[str, str]:
    """Compute ``(file_hash, dhash)`` in a single file open."""
    if type(hash_size) is not int or not 4 <= hash_size <= 16:
        return compute_file_hash(file_path), ""
    path = Path(file_path)
    if not _safe_local_file(path):
        return "", ""
    try:
        h = hashlib.sha256()
        with path.open("rb") as source:
            if os.fstat(source.fileno()).st_size > MAX_RASTER_BYTES:
                return "", ""
            while chunk := source.read(65536):
                h.update(chunk)
            fhash = h.hexdigest()
            source.seek(0)
            dhash = _dhash_from_stream(source, hash_size=hash_size)
        return fhash, dhash
    except OSError:
        return "", ""


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

    Returns list of groups where each group has >= 2 items, sorted so the first
    item in each group is the preferred primary copy (favorited, then largest).
    """
    valid = [it for it in items if isinstance(it, dict) and it.get("key")
             and (it.get("file_hash") or (it.get("dhash") and len(str(it.get("dhash"))) == 16))]
    n = len(valid)
    if n < 2:
        return []

    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri != rj:
            if ri < rj:
                parent[rj] = ri
            else:
                parent[ri] = rj

    # 1. Exact matches by non-empty file_hash
    by_fhash: dict[str, int] = {}
    for idx, item in enumerate(valid):
        fhash = item.get("file_hash") or ""
        if fhash:
            first = by_fhash.get(fhash)
            if first is not None:
                union(first, idx)
            else:
                by_fhash[fhash] = idx

    # 2. Exact matches by valid 16-hex dhash
    by_dhash: dict[str, int] = {}
    for idx, item in enumerate(valid):
        dh = item.get("dhash") or ""
        if len(dh) == 16:
            first = by_dhash.get(dh)
            if first is not None:
                union(first, idx)
            else:
                by_dhash[dh] = idx

    # 3. Near matches across distinct dhash buckets when max_distance > 0.
    # For max_distance <= 7, partitioning the 64-bit hash into (max_distance + 1)
    # disjoint bit slices guarantees by the pigeonhole principle that any two
    # hashes within Hamming distance <= max_distance share at least one slice.
    if max_distance > 0 and len(by_dhash) > 1:
        unique_dhashes = []
        for dh, idx in by_dhash.items():
            try:
                unique_dhashes.append((int(dh, 16), idx))
            except ValueError:
                continue
        if 1 <= max_distance <= 7 and len(unique_dhashes) > 64:
            slice_count = max_distance + 1
            shift = 0
            for s in range(slice_count):
                width = (64 // slice_count) + (1 if s < (64 % slice_count) else 0)
                mask = (1 << width) - 1
                buckets: dict[int, list[tuple[int, int]]] = {}
                for val, idx in unique_dhashes:
                    buckets.setdefault((val >> shift) & mask, []).append((val, idx))
                for bucket in buckets.values():
                    m = len(bucket)
                    if m < 2:
                        continue
                    for i in range(m - 1):
                        v1, idx1 = bucket[i]
                        for j in range(i + 1, m):
                            v2, idx2 = bucket[j]
                            if (v1 ^ v2).bit_count() <= max_distance:
                                union(idx1, idx2)
                shift += width
        else:
            for i, (v1, idx1) in enumerate(unique_dhashes):
                for j in range(i + 1, len(unique_dhashes)):
                    v2, idx2 = unique_dhashes[j]
                    if (v1 ^ v2).bit_count() <= max_distance:
                        union(idx1, idx2)

    clusters_by_root: dict[int, list[dict]] = {}
    for idx, item in enumerate(valid):
        clusters_by_root.setdefault(find(idx), []).append(item)

    groups: list[list[dict]] = []
    for group in clusters_by_root.values():
        if len(group) >= 2:
            group.sort(key=lambda x: (bool(x.get("favorite")), x.get("filesize") or 0), reverse=True)
            groups.append(group)

    return groups

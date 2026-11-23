"""Duplicate image detection engine for LuvBox.

Uses SHA-256 for exact binary duplicates. With the near rule, dHash
(difference hash) via Pillow finds candidates for the same picture saved at
another size or quality, and their pixels decide.
"""

from __future__ import annotations

from collections import OrderedDict
from contextlib import ExitStack
import hashlib
import math
import os
import re
from pathlib import Path
import struct
from typing import Sequence
import warnings
from PIL import Image, ImageOps, UnidentifiedImageError

from .preview import MAX_PIXELS, MAX_RASTER_BYTES, PHOTO_FORMATS, _BoundedRasterReader, _prepare_decoders
from .thumbcache import cloud_only


class ScanCancelled(RuntimeError):
    """A requested stop, distinct from a failed duplicate scan."""


def image_signature(path: Path | str) -> str:
    """Local identity and modification stamp; never reusable on a different file."""
    from .recycling import file_signature
    if cloud_only(path):
        raise OSError("cloud-only image")
    return ":".join(str(value) for value in file_signature(path))


def _stream_hash(source) -> str:
    """Bound the read even when a file grows after the initial stat."""
    digest = hashlib.sha256()
    remaining = MAX_RASTER_BYTES
    while chunk := source.read(min(65536, remaining + 1)):
        remaining -= len(chunk)
        if remaining < 0:
            raise OSError("image exceeded hashing limit")
        digest.update(chunk)
    return digest.hexdigest()


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
        with Image.open(_BoundedRasterReader(source), formats=PHOTO_FORMATS) as img:
            if img.width * img.height > MAX_PIXELS:
                return ""
            # Transpose a one-byte grayscale image instead of copying the
            # full RGB/RGBA raster, and release both intermediates promptly.
            with img.convert("L") as gray:
                ImageOps.exif_transpose(gray, in_place=True)
                with gray.resize((hash_size + 1, hash_size), Image.Resampling.BILINEAR) as resized:
                    pixels = resized.tobytes()
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
    except (OSError, ValueError, TypeError, SyntaxError, EOFError, struct.error, UnidentifiedImageError,
            Image.DecompressionBombError, Image.DecompressionBombWarning):
        return ""


def compute_file_hash(file_path: Path | str) -> str:
    """Compute SHA-256 checksum of an ordinary local file within raster bounds."""
    path = Path(file_path)
    if not _safe_local_file(path):
        return ""
    try:
        with path.open("rb") as f:
            if os.fstat(f.fileno()).st_size > MAX_RASTER_BYTES:
                return ""
            return _stream_hash(f)
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
        signature = image_signature(path)
        with path.open("rb") as source:
            info = os.fstat(source.fileno())
            opened = ":".join(str(value) for value in (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns))
            if info.st_size > MAX_RASTER_BYTES or opened != signature:
                return "", ""
            fhash = _stream_hash(source)
            source.seek(0)
            dhash = _dhash_from_stream(source, hash_size=hash_size)
        if image_signature(path) != signature:
            return "", ""
        return fhash, dhash
    except OSError:
        return "", ""


NEAR_CANDIDATE_BITS = 4
_VECTOR_SIDE = 32
# Measured on 32x32 RGB reductions. The same picture saved again at another
# quality or size stays under 0.4% mean, 1.6% at the 99th percentile and 3.1%
# at its worst pixel. A large change on the same template also stays under 1%
# on average but exceeds 15% locally, so the average alone is not enough.
# Text a few dozen pixels high is a grey smear at this size, so a pair that
# passes is looked at again in detail (``same_in_detail``).
_NEAR_MEAN = 0.01
_NEAR_PERCENTILE = 0.06
_NEAR_WORST = 0.15
_NEAR_ASPECT = 0.01


def pixel_vector(image_path: Path | str):
    """A small RGB reduction and the width/height ratio, or None when unreadable.

    Used only to confirm that two photos with close perceptual hashes are the
    same picture. Bounded like every other decode in the app.
    """
    import numpy as np

    path = Path(image_path)
    if not _safe_local_file(path):
        return None
    try:
        with path.open("rb") as source:
            if os.fstat(source.fileno()).st_size > MAX_RASTER_BYTES:
                return None
            _prepare_decoders(False)
            with Image.open(_BoundedRasterReader(source), formats=PHOTO_FORMATS) as img:
                if img.width * img.height > MAX_PIXELS:
                    return None
                img.draft("RGB", (_VECTOR_SIDE * 4, _VECTOR_SIDE * 4))
                with img.convert("RGB") as color:
                    ImageOps.exif_transpose(color, in_place=True)
                    ratio = color.width / max(1, color.height)
                    with color.resize((_VECTOR_SIDE, _VECTOR_SIDE), Image.Resampling.BILINEAR,
                                      reducing_gap=2.0) as small:
                        return np.asarray(small, dtype=np.int16), ratio
    except (OSError, ValueError, TypeError, SyntaxError, EOFError, struct.error, UnidentifiedImageError,
            Image.DecompressionBombError, Image.DecompressionBombWarning):
        return None


def nearly_identical(first, second) -> bool:
    """True when two ``pixel_vector`` results are the same picture (about 99% and no local change)."""
    import numpy as np

    if first is None or second is None:
        return False
    (pixels_a, ratio_a), (pixels_b, ratio_b) = first, second
    if pixels_a.shape != pixels_b.shape:
        return False
    if abs(ratio_a - ratio_b) / max(ratio_a, ratio_b, 1e-9) > _NEAR_ASPECT:
        return False
    difference = np.abs(pixels_a - pixels_b)
    if float(difference.mean()) / 255 > _NEAR_MEAN:
        return False
    if float(np.percentile(difference, 99)) / 255 > _NEAR_PERCENTILE:
        return False
    return float(difference.max()) / 255 <= _NEAR_WORST


# The second look, at the smaller file's own size. Weekly schedules drawn on
# one template, with every line of 32 px text changed, pass the 32x32 test.
# Here the grey pictures are compared in windows of 10 pixels, half a window
# apart. A window scores its mean difference times how little the two vary
# together (one minus the structure term of SSIM): the same edges drawn a
# little sharper or softer cost little, other glyphs in the same place cost a
# lot. Measured on 1080-2048 px text pages, schedules, chat shots, line art
# and photos: of 921 copies brought to 1/3-0.9 of their size (Lanczos,
# bicubic, bilinear, Hamming or box), to 1/4 as WebP, or saved again at
# quality 60-95, as PNG or as WebP, 911 scored under the limit; line art with
# screentone made smaller by box, Hamming or bilinear did not and is not
# grouped. Another row or week of text on one template and a faint watermark
# did not pass. One changed character in text up to about 18 px high at the
# compared size often does (a word, when the text is only 6-7 px high), and
# no pixel measure tried kept those apart from real copies, so a group made
# this way is never proof for the Recycle Bin (``deletion.kept_copy`` asks
# for the same bytes).
_DETAIL_WINDOW = 10
_DETAIL_LIMIT = 0.035
_DETAIL_FLAT = (0.03 * 255) ** 2      # windows where both pictures barely vary do not decide
_DETAIL_MAX_PIXELS = 2048 * 2048      # compared at most at this size,
_DETAIL_MAX_REDUCTION = 2             # and never below half the smaller file's own size
_DETAIL_BAND = 16                     # rows of cells scored at a time
_DETAIL_CACHE_PIXELS = 64 * 1024 * 1024   # decoded grey pixels one scan keeps
_DETAIL_BLOCK = 8                     # photos whose pairs are compared together (see cluster_images)


def _oriented_size(image) -> tuple[int, int]:
    """Width and height as shown, after the EXIF orientation."""
    if image.getexif().get(0x0112) in (5, 6, 7, 8):
        return image.height, image.width
    return image.width, image.height


def _grey(image, size: tuple[int, int]):
    """The picture in grey and upright; a JPEG is decoded no smaller than ``size``."""
    width, height = size
    if _oriented_size(image) != image.size:
        width, height = height, width
    image.draft("L", (width, height))
    grey = image.convert("L")
    ImageOps.exif_transpose(grey, in_place=True)
    return grey


def _detail_band(first, second, cell: int) -> float:
    """The worst window score of two grey uint8 arrays whose sides are multiples of ``cell``.

    A window scores less than twice its mean difference, so only windows whose
    mean difference reaches half the limit are scored in full; the rest count
    as 0. Most windows of the same picture saved again are left out that way.
    """
    import numpy as np

    rows, cols = first.shape[0] // cell, first.shape[1] // cell
    paired = rows > 1 and cols > 1
    count = cell * cell * (4 if paired else 1)

    def windows(part):
        return part[:-1, :-1] + part[1:, :-1] + part[:-1, 1:] + part[1:, 1:] if paired else part

    distance = np.maximum(first, second) - np.minimum(first, second)
    difference = windows(distance.reshape(rows, cell, cols, cell).sum(axis=(1, 3), dtype=np.int64)) / (count * 255)
    chosen = difference * 2 > _DETAIL_LIMIT
    if not chosen.any():
        return 0.0
    needed = chosen
    if paired:
        needed = np.zeros((rows, cols), dtype=bool)
        for row in (0, 1):
            for col in (0, 1):
                needed[row:row + rows - 1, col:col + cols - 1] |= chosen
    blocks_a = first.reshape(rows, cell, cols, cell).swapaxes(1, 2)[needed].astype(np.int32)
    blocks_b = second.reshape(rows, cell, cols, cell).swapaxes(1, 2)[needed].astype(np.int32)

    def means(values):
        part = np.zeros((rows, cols), dtype=np.int64)
        part[needed] = values.sum(axis=(1, 2))
        return windows(part)[chosen] / count

    mean_a, mean_b = means(blocks_a), means(blocks_b)
    spread = means(blocks_a * blocks_a) - mean_a * mean_a + means(blocks_b * blocks_b) - mean_b * mean_b
    together = means(blocks_a * blocks_b) - mean_a * mean_b
    structure = (2 * together + _DETAIL_FLAT) / (spread + _DETAIL_FLAT)
    return float((difference[chosen] * (1 - structure)).max())


def _detail_difference(first, second) -> float:
    """The worst window score of two grey uint8 arrays of one shape (see ``_DETAIL_LIMIT``).

    Scored a band of rows at a time; the first band over the limit decides, so
    a picture with other text costs only the rows down to that text.
    """
    import numpy as np

    height, width = first.shape
    cell = max(1, min(_DETAIL_WINDOW // 2, height // 2, width // 2))
    rows, cols = -(-height // cell), -(-width // cell)
    padding = ((0, rows * cell - height), (0, cols * cell - width))
    if any(after for _, after in padding):
        # Repeat the last row and column so the edges are compared as well.
        first, second = np.pad(first, padding, mode="edge"), np.pad(second, padding, mode="edge")
    worst = 0.0
    for top in range(0, max(1, rows - 1), _DETAIL_BAND):
        # One more row of cells for the windows across the band's lower edge.
        band = slice(top * cell, min(rows, top + _DETAIL_BAND + 1) * cell)
        worst = max(worst, _detail_band(first[band], second[band], cell))
        if worst > _DETAIL_LIMIT:
            break
    return worst


class _GreyCache:
    """What ``same_in_detail`` read during one scan, kept for the next pairs.

    The shown size of each file, and its grey pixels decoded for a comparison
    size, so one template met in many pairs is read a few times rather than
    once per pair. Pixels are bounded by count; the least recently used
    picture goes first.
    """

    def __init__(self, budget: int | None = None):
        self._pictures: OrderedDict = OrderedDict()
        self._budget = _DETAIL_CACHE_PIXELS if budget is None else budget
        self._used = 0
        self._sizes: dict = {}

    def size(self, key):
        """The shown size of a file, False when it may not be read, None when not known yet."""
        return self._sizes.get(key)

    def remember(self, key, size) -> None:
        self._sizes[key] = size

    def get(self, key):
        picture = self._pictures.get(key)
        if picture is not None:
            self._pictures.move_to_end(key)
            return picture
        if (isinstance(key, tuple) and len(key) == 2 and isinstance(key[1], tuple)
                and len(key[1]) == 2):
            name, (req_w, req_h) = key
            shown = self._sizes.get(name)
            for candidate_key, candidate in list(self._pictures.items()):
                if (isinstance(candidate_key, tuple) and len(candidate_key) == 2
                        and candidate_key[0] == name):
                    cand_h, cand_w = candidate.shape[:2]
                    if ((shown and (cand_w, cand_h) == shown)
                            or (cand_w >= req_w and cand_h >= req_h
                                and isinstance(candidate_key[1], tuple)
                                and candidate_key[1][0] >= req_w and candidate_key[1][1] >= req_h)):
                        self._pictures.move_to_end(candidate_key)
                        return candidate
        return None

    def put(self, key, picture) -> None:
        if key in self._pictures or picture.size > self._budget:
            return
        self._pictures[key] = picture
        self._used += picture.size
        while self._used > self._budget:
            _, oldest = self._pictures.popitem(last=False)
            self._used -= oldest.size


def _resized(picture, size: tuple[int, int], resample):
    """A grey uint8 array brought to ``size`` (width, height)."""
    import numpy as np

    with Image.fromarray(picture) as grey, grey.resize(size, resample) as resized:
        return np.asarray(resized, dtype=np.uint8)


def same_in_detail(first: Path | str, second: Path | str, *, greys: _GreyCache | None = None) -> bool:
    """True when two pictures still agree near the smaller one's own resolution.

    The second look for a pair that passed ``nearly_identical``: other text
    on one template shows here. The larger picture is brought to the smaller
    one's size twice, sharply (Lanczos) and by averaging (box), as a copy may
    have been made either way. Unreadable pictures, and pictures that cannot
    be compared within half the smaller one's size, never agree. It decides
    what is shown as one photo, not what may be deleted: one changed
    character of small text can still pass (see ``_DETAIL_LIMIT``).
    ``greys`` keeps what was read for the other pairs of one scan.
    """
    import numpy as np

    try:
        with ExitStack() as stack:
            _prepare_decoders(False)
            opened: dict = {}

            def picture_of(path: Path):
                """The file opened for decoding, or None when it may not be read."""
                if path not in opened:
                    opened[path] = None
                    if _safe_local_file(path):
                        source = stack.enter_context(path.open("rb"))
                        if os.fstat(source.fileno()).st_size <= MAX_RASTER_BYTES:
                            image = stack.enter_context(
                                Image.open(_BoundedRasterReader(source), formats=PHOTO_FORMATS))
                            if image.width * image.height <= MAX_PIXELS:
                                opened[path] = image
                return opened[path]

            files = [(Path(value), os.path.normcase(os.path.abspath(value))) for value in (first, second)]
            sizes = []
            for path, name in files:
                shown = greys.size(name) if greys is not None else None
                if shown is None:
                    image = picture_of(path)
                    shown = _oriented_size(image) if image is not None else False
                    if greys is not None:
                        greys.remember(name, shown)
                if not shown:
                    return False
                sizes.append(shown)
            width, height = min(sizes, key=lambda shown: shown[0] * shown[1])
            scale = min(1.0, math.sqrt(_DETAIL_MAX_PIXELS / max(1, width * height)))
            if scale * _DETAIL_MAX_REDUCTION < 1:
                return False
            size = (max(1, round(width * scale)), max(1, round(height * scale)))
            shape = (size[1], size[0])
            pictures = []
            for path, name in files:
                picture = greys.get((name, size)) if greys is not None else None
                if picture is None:
                    image = picture_of(path)
                    if image is None:
                        return False
                    with _grey(image, size) as grey:
                        picture = np.asarray(grey, dtype=np.uint8)
                    if greys is not None:
                        greys.put((name, size), picture)
                pictures.append(picture)
            for resample in (Image.Resampling.LANCZOS, Image.Resampling.BOX):
                arrays = [picture if picture.shape == shape else _resized(picture, size, resample)
                          for picture in pictures]
                if _detail_difference(*arrays) <= _DETAIL_LIMIT:
                    return True
                if all(picture.shape == shape for picture in pictures):
                    break                       # nothing was resized, so another filter changes nothing
            return False
    except (OSError, ValueError, TypeError, SyntaxError, EOFError, struct.error, UnidentifiedImageError,
            Image.DecompressionBombError, Image.DecompressionBombWarning):
        return False


def _close_hash_pairs(hashes: list[tuple[int, int]], max_distance: int, check_cancel=None) -> list[tuple[int, int]]:
    """Index pairs whose 64-bit hashes differ by at most ``max_distance`` bits."""
    pairs = set()
    if max_distance < 0 or len(hashes) < 2:
        return []
    grouped: dict[int, list[int]] = {}
    for value, index in hashes:
        grouped.setdefault(value, []).append(index)
    for indices in grouped.values():
        count = len(indices)
        if count < 2:
            continue
        for left in range(count - 1):
            if check_cancel:
                check_cancel()
            index_a = indices[left]
            for right in range(left + 1, count):
                index_b = indices[right]
                pairs.add((index_a, index_b) if index_a < index_b else (index_b, index_a))
    distinct_hashes = list(grouped.items())
    if len(distinct_hashes) >= 2:
        slice_count = max_distance + 1
        # Strided bit slices keep uniform top/bottom edges from collapsing into one bucket.
        masks = [sum(1 << bit for bit in range(part, 64, slice_count)) for part in range(slice_count)]
        for mask in masks:
            buckets: dict[int, list[tuple[int, list[int]]]] = {}
            for value, indices in distinct_hashes:
                buckets.setdefault(value & mask, []).append((value, indices))
            for bucket in buckets.values():
                size = len(bucket)
                if size < 2:
                    continue
                for left in range(size - 1):
                    if check_cancel:
                        check_cancel()
                    value_a, indices_a = bucket[left]
                    for right in range(left + 1, size):
                        value_b, indices_b = bucket[right]
                        if (value_a ^ value_b).bit_count() <= max_distance:
                            for index_a in indices_a:
                                for index_b in indices_b:
                                    pairs.add((index_a, index_b) if index_a < index_b else (index_b, index_a))
    return sorted(pairs)


def cluster_images(items: Sequence[dict], *, near: bool = False, vector_for=pixel_vector,
                   detail_for=None, check_cancel=None, progress=None) -> list[list[dict]]:
    """Group duplicate photos.

    Always: files with the same SHA-256 and size. With ``near``: also photos
    whose perceptual hashes are close AND whose pixels confirm it is the same
    picture saved at another size or quality, first on a small reduction and
    then in detail, where other text on one template shows. A close hash
    alone never groups two photos; that produced groups of merely similar
    pictures. Such a group is what the view shows as one photo; recycling a
    member still needs a file with the same bytes (``deletion.kept_copy``).

    Each item needs 'key', 'file_hash', 'filesize'; ``near`` also uses 'dhash'
    and 'path'. ``progress(done, total)`` reports the pixel checks.
    """
    if check_cancel:
        check_cancel()
    valid = [item for item in items if isinstance(item, dict) and item.get("key")]
    count = len(valid)
    if count < 2:
        return []
    parent = list(range(count))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        root_a, root_b = find(left), find(right)
        if root_a != root_b:
            parent[max(root_a, root_b)] = min(root_a, root_b)

    exact: dict[tuple, int] = {}
    for index, item in enumerate(valid):
        digest = item.get("file_hash") or ""
        if not digest:
            continue
        first = exact.setdefault((digest, item.get("filesize") or 0), index)
        if first != index:
            union(first, index)

    if near:
        hashes = []
        seen_roots = set()
        for index, item in enumerate(valid):
            value = item.get("dhash") or ""
            if isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{16}", value) and item.get("path"):
                root = find(index)
                if root in seen_roots:
                    continue
                seen_roots.add(root)
                hashes.append((int(value, 16), index))
        candidates = [pair for pair in _close_hash_pairs(hashes, NEAR_CANDIDATE_BITS, check_cancel)
                      if find(pair[0]) != find(pair[1])]
        # Compared a block of photos against another at a time, so the pictures
        # of both blocks stay decoded meanwhile: a template met in a hundred
        # pairs is read a few times instead of once per pair. The groups do not
        # depend on the order.
        compared = sorted({index for pair in candidates for index in pair})
        rank = {index: number for number, index in enumerate(compared)}
        candidates.sort(key=lambda pair: (rank[pair[0]] // _DETAIL_BLOCK, rank[pair[1]] // _DETAIL_BLOCK, pair))
        vectors: dict[int, object] = {}
        greys = _GreyCache()
        detail = detail_for or (lambda first, second: same_in_detail(first, second, greys=greys))

        def vector(index: int):
            if index not in vectors:
                vectors[index] = vector_for(valid[index]["path"])
            return vectors[index]

        total = len(candidates)
        for done, (left, right) in enumerate(candidates):
            if check_cancel:
                check_cancel()
            if progress and (done % 8 == 0 or done == total - 1):
                progress(done, total)
            if find(left) == find(right):
                continue
            if (nearly_identical(vector(left), vector(right))
                    and detail(valid[left]["path"], valid[right]["path"])):
                union(left, right)
        if progress and total:
            progress(total, total)

    grouped: dict[int, list[dict]] = {}
    for index, item in enumerate(valid):
        grouped.setdefault(find(index), []).append(item)
    groups = []
    for group in grouped.values():
        if len(group) >= 2:
            group.sort(key=lambda entry: (bool(entry.get("favorite")), entry.get("filesize") or 0,
                                          str(entry.get("key") or "")), reverse=True)
            groups.append(group)
    return groups


def split_distinct(members: Sequence, distinct: Sequence) -> list[list]:
    """What is left of one group once some members were declared "not duplicates".

    ``distinct`` holds sets of members. Two members stop being a pair when one
    set contains both; members still linked to another, directly or through a
    third, stay together. Returns the remaining groups of two or more, each in
    the order of ``members``. A member left alone is in none of them.
    """
    members = list(members)
    if len(members) < 2:
        return []
    marks: dict = {}
    for number, group in enumerate(distinct):
        for member in group:
            marks.setdefault(member, set()).add(number)
    memberships = [marks.get(member) for member in members]
    if not all(memberships):
        # One unmarked member connects the entire group.
        return [members]
    common = set(memberships[0])
    for membership in memberships[1:]:
        common.intersection_update(membership)
        if not common:
            break
    else:
        return []                         # every pair shares at least one mark

    parent = list(range(len(members)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    for first, mine in enumerate(memberships):
        for second in range(first + 1, len(members)):
            if not mine.isdisjoint(memberships[second]):
                continue
            left, right = find(first), find(second)
            if left != right:
                parent[right] = left
    groups: dict[int, list] = {}
    for index, member in enumerate(members):
        groups.setdefault(find(index), []).append(member)
    return [group for group in groups.values() if len(group) >= 2]


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


def cluster_duplicates(items: Sequence[dict], max_distance: int = 6, *, check_cancel=None) -> list[list[dict]]:
    """Group items by duplicate status.

    Each item must contain:
    - 'key': unique identity string
    - 'file_hash': exact SHA256 (optional, empty string if missing)
    - 'dhash': 16-hex perceptual hash (optional)

    Returns list of groups where each group has >= 2 items, sorted so the first
    item in each group is the preferred primary copy (favorited, then largest).
    """
    if type(max_distance) is not int or not 0 <= max_distance <= 64:
        raise ValueError("max_distance must be an integer between 0 and 64")
    if check_cancel:
        check_cancel()
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
        if isinstance(dh, str) and re.fullmatch(r"[0-9a-fA-F]{16}", dh):
            dh = dh.lower()
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
                        if check_cancel:
                            check_cancel()
                        v1, idx1 = bucket[i]
                        for j in range(i + 1, m):
                            v2, idx2 = bucket[j]
                            if v1 == v2 or find(idx1) == find(idx2):
                                continue
                            if (v1 ^ v2).bit_count() <= max_distance:
                                union(idx1, idx2)
                shift += width
        else:
            for i, (v1, idx1) in enumerate(unique_dhashes):
                if check_cancel:
                    check_cancel()
                for j in range(i + 1, len(unique_dhashes)):
                    v2, idx2 = unique_dhashes[j]
                    if v1 == v2 or find(idx1) == find(idx2):
                        continue
                    if (v1 ^ v2).bit_count() <= max_distance:
                        union(idx1, idx2)

    clusters_by_root: dict[int, list[dict]] = {}
    for idx, item in enumerate(valid):
        clusters_by_root.setdefault(find(idx), []).append(item)

    groups: list[list[dict]] = []
    for group in clusters_by_root.values():
        if len(group) >= 2:
            group.sort(
                key=lambda x: (
                    bool(x.get("favorite")),
                    x.get("filesize") or 0,
                    str(x.get("tweet_id") or x.get("key") or ""),
                    int(x.get("num") or 0),
                ),
                reverse=True,
            )
            groups.append(group)

    return groups

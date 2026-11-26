"""Zero-dependency path-link safety and tag-normalization helpers."""

from __future__ import annotations

import os
from pathlib import Path
import stat


# Name-surrogate reparse points (symlinks, junctions, volume mount points)
# redirect elsewhere; cloud-provider placeholders do not set this bit.
NAME_SURROGATE = 0x20000000


def is_link_stat(details: os.stat_result | None) -> bool:
    """Whether an ``lstat`` result represents a symlink or name-surrogate reparse point."""
    if details is None:
        return False
    return bool(stat.S_ISLNK(details.st_mode) or (getattr(details, "st_reparse_tag", 0) & NAME_SURROGATE))


def is_reparse_or_link(
    path: Path,
    *,
    status: os.stat_result | None = None,
    check_junction: bool = True,
) -> bool:
    """Whether ``path`` is a symlink, junction, or name-surrogate mount point.

    Honors ``Path.is_symlink`` / ``Path.is_junction`` first so existing
    path-mocking tests behave consistently while also inspecting ``st_reparse_tag``.
    """
    if path.is_symlink():
        return True
    if check_junction and hasattr(path, "is_junction") and path.is_junction():
        return True
    if status is None:
        try:
            status = path.lstat()
        except OSError:
            return False
    return is_link_stat(status)


def regular_file_stat(path: Path) -> os.stat_result | None:
    """Return ``lstat`` result if ``path`` is a regular non-linked file, else ``None``."""
    try:
        details = path.lstat()
    except OSError:
        return None
    if is_link_stat(details) or not stat.S_ISREG(details.st_mode):
        return None
    return details


def normalize_tag(value) -> str:
    """Strip a leading '#', ASCII control characters, and outer whitespace."""
    if not isinstance(value, str):
        return ""
    cleaned = "".join(char for char in value.strip().lstrip("#") if ord(char) >= 32 and ord(char) != 127)
    return cleaned.strip()

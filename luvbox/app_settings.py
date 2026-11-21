"""Shared preferences for the Qt app, preserving existing and unknown keys."""

from copy import deepcopy
from datetime import datetime
import json
import os
from pathlib import Path
import tempfile
import threading

from .settings import SettingsDamaged, load_settings, settings_path
from .file_mutex import file_mutex, replace_file


_WRITE_LOCK = threading.RLock()
_SAVE_ERROR = "설정을 저장하지 못했습니다. 저장 권한을 확인해 주세요."


def _keep_damaged(target: Path) -> None:
    """Rename a settings file nobody can read to settings.damaged-<time>.json beside it; never replace one."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    for number in range(100):
        aside = target.with_name(f"settings.damaged-{stamp}" + (f"-{number}" if number else "") + ".json")
        try:
            os.rename(target, aside)
            return
        except FileExistsError:
            continue
        except OSError:
            break
    raise OSError(_SAVE_ERROR)


def default_settings() -> dict:
    return {
        "account": "", "output_dir": str(Path.home() / "Pictures" / "LuvBox"),
        "folder_mode": "flat", "scan_mode": "quick",
        "include_videos": True, "include_gifs": True, "max_video_mb": 200,
        "separate_accounts": False, "gif_format": "gif", "gif_size": "original",
        "detailed_progress": False, "confirm_each": False,
        "schedule_time": "09:00", "schedule_prompted": "", "record_locations": {},
        # Gallery thumbnail size: 0 small, 1 normal, 2 large.
        "gallery_density": 1,
        # Last window size and place: {"width", "height", "x", "y", "maximized"}; empty until first closed.
        "window": {},
        # Look for a newer release once a day at start; when that was last done (UTC, ISO 8601);
        # and the newer release it found: {"version", "notes", "installable", "size"} or empty.
        "update_check": True, "update_checked_at": "", "update_found": {},
        # Give newly saved photos their AI category without being asked. It needs the model that the
        # first classification downloads; nothing is fetched for it.
        "auto_classify": True,
        # The 폴더 정리 tab: the folder opened last (opened again only when the tab is shown), the
        # folders opened recently (newest first, at most ten), whether subfolders are read, and
        # whether what is taught there teaches the AI tags of the whole program.
        "local_folder_last": "", "local_folder_recent": [],
        "local_folder_subfolders": True, "local_folder_share_learning": True,
    }


class SettingsRepository:
    """Merge each preference update with the latest on-disk preferences."""

    def load(self) -> dict:
        with _WRITE_LOCK:
            return {**default_settings(), **load_settings()}

    def update(self, changes: dict) -> dict:
        if not isinstance(changes, dict) or any(not isinstance(key, str) for key in changes):
            raise ValueError(_SAVE_ERROR)
        target = settings_path()
        with _WRITE_LOCK, file_mutex(target):
            # A tolerant first display must not turn a damaged or inaccessible settings file into an
            # empty document on the next update. One that cannot be read right now (locked, not
            # permitted) stops the change. One that holds no readable settings (cut short, not JSON)
            # used to stop every change for good; it is kept as it is under another name and the
            # change starts a new file (qt_app tells the person at start).
            try:
                current = load_settings(strict=True)
            except SettingsDamaged:
                _keep_damaged(target)
                current = {}
            values = {**current, **deepcopy(changes)}
            try:
                serialized = json.dumps(values, ensure_ascii=False, indent=2, allow_nan=False)
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.is_symlink() or getattr(target, "is_junction", lambda: False)():
                    raise OSError(_SAVE_ERROR)
                descriptor, filename = tempfile.mkstemp(prefix="settings-", suffix=".tmp", dir=target.parent)
                temporary = Path(filename)
                try:
                    with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
                        stream.write(serialized)
                        stream.flush()
                        os.fsync(stream.fileno())
                    replace_file(temporary, target)
                finally:
                    temporary.unlink(missing_ok=True)
            except (OSError, ValueError, TypeError, UnicodeError):
                raise OSError(_SAVE_ERROR) from None
            return {**default_settings(), **deepcopy(values)}

    def save(self, values: dict) -> dict:
        return self.update(values)

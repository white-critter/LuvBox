"""Shared preferences for the Qt app, preserving existing and unknown keys."""

from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import threading

from .settings import load_settings, settings_path


_WRITE_LOCK = threading.RLock()
_SAVE_ERROR = "설정을 저장하지 못했습니다. 저장 권한을 확인해 주세요."


def default_settings() -> dict:
    return {
        "account": "", "output_dir": str(Path.home() / "Pictures" / "LuvBox"),
        "auth_mode": "managed", "folder_mode": "flat", "scan_mode": "quick",
        "include_videos": True, "include_gifs": True, "max_video_mb": 200,
        "separate_accounts": False, "gif_format": "gif", "gif_size": "original",
        "detailed_progress": False, "confirm_each": False,
        "schedule_time": "09:00", "schedule_prompted": "", "record_locations": {},
    }


class SettingsRepository:
    """Merge each preference update with the latest on-disk preferences."""

    def load(self) -> dict:
        with _WRITE_LOCK:
            return {**default_settings(), **load_settings()}

    def update(self, changes: dict) -> dict:
        if not isinstance(changes, dict) or any(not isinstance(key, str) for key in changes):
            raise ValueError(_SAVE_ERROR)
        with _WRITE_LOCK:
            values = {**load_settings(), **deepcopy(changes)}
            try:
                serialized = json.dumps(values, ensure_ascii=False, indent=2, allow_nan=False)
                target = settings_path()
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
                    os.replace(temporary, target)
                finally:
                    temporary.unlink(missing_ok=True)
            except (OSError, ValueError, TypeError, UnicodeError):
                raise OSError(_SAVE_ERROR) from None
            return {**default_settings(), **deepcopy(values)}

    def save(self, values: dict) -> dict:
        return self.update(values)

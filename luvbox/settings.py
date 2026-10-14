"""Per-user preferences shared by the window and the unattended scheduled run.

Only preferences are stored here. Authentication lives encrypted in
session.bin and is never written to this file.
"""

import json
import os
from pathlib import Path
import threading


_APPDATA_LOCK = threading.RLock()
_MIGRATION_HELP = ("이전 앱 데이터를 LuvBox 폴더로 옮기지 못했습니다. 실행 중인 앱을 모두 닫고 "
                   "다시 실행해 주세요. 계속되면 Windows 사용자 폴더의 권한과 "
                   "기존 XlikeSaver·LuvBox 데이터 폴더를 확인해 주세요.")


class AppDataError(OSError):
    """Existing user data could not safely be adopted; never start fresh."""


def app_directory() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local"))
    target, previous = base / "LuvBox", base / "XlikeSaver"
    with _APPDATA_LOCK:
        try:
            for path in (target, previous):
                if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
                    raise AppDataError(_MIGRATION_HELP)
            if previous.exists():
                if not previous.is_dir():
                    raise AppDataError(_MIGRATION_HELP)
                if target.exists():
                    # Never guess which of two libraries is authoritative.
                    # An empty destination can come from an interrupted start.
                    target.rmdir()
                try:
                    # Keep SQLite databases and any WAL sidecars together. On
                    # Windows this also refuses open handles, leaving them intact.
                    previous.rename(target)
                except OSError:
                    # A second process may have completed the same atomic move.
                    if previous.exists() or not target.is_dir():
                        raise
            return target
        except OSError:
            raise AppDataError(_MIGRATION_HELP) from None


def settings_path() -> Path:
    return app_directory() / "settings.json"


def load_settings() -> dict:
    # A failed migration must not be mistaken for absent preferences.
    path = settings_path()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}

"""Per-user preferences shared by the window and the unattended scheduled run.

Only preferences are stored here. Authentication lives encrypted in
session.bin and is never written to this file.
"""

from datetime import datetime
import json
import os
from pathlib import Path
import threading


_APPDATA_LOCK = threading.RLock()
_MIGRATION_HELP = ("이전 앱 데이터를 LuvBox 폴더로 옮기지 못했습니다. 실행 중인 앱을 모두 닫고 "
                   "다시 실행해 주세요. 계속되면 Windows 사용자 폴더의 권한과 "
                   "기존 XlikeSaver·LuvBox 데이터 폴더를 확인해 주세요.")
CONFLICT_HELP = ("이전 이름의 데이터 폴더(XlikeSaver)가 다시 생겼습니다. 예전 버전의 실행 파일이나 "
                 "자동 저장 등록이 다시 실행된 것으로 보입니다. 지금 쓰는 데이터는 LuvBox 폴더에 있습니다.")
# What an older build writes by itself after the move, before anyone signs in
# to it again: logs, its last scheduled outcome, the window's acknowledgement
# of that outcome (and their interrupted temporary files) and rebuildable caches.
_LEFTOVER_NAMES = frozenset({"logs", "last-scheduled.json", "last-scheduled-seen", "cache",
                             "request-protection", "thumbnails"})


class AppDataError(OSError):
    """Existing user data could not safely be adopted; never start fresh."""


class AppDataConflict(AppDataError):
    """Both data folders hold settings, a sign-in or records; the person decides."""


def _leftover(name: str) -> bool:
    return name in _LEFTOVER_NAMES or (name.startswith("last-scheduled-") and name.endswith(".tmp"))


def _folders() -> tuple[Path, Path]:
    base = Path(os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local"))
    return base / "LuvBox", base / "XlikeSaver"


def _check_links(*paths: Path) -> None:
    for path in paths:
        if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
            raise AppDataError(_MIGRATION_HELP)


def _set_aside(previous: Path, target: Path) -> Path:
    """Keep a reappeared old folder inside the current one; nothing is deleted."""
    folder = target / "legacy"
    folder.mkdir(exist_ok=True)
    _check_links(folder)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    for index in range(1, 100):
        destination = folder / (f"XlikeSaver-{stamp}" + (f"-{index}" if index > 1 else ""))
        if not os.path.lexists(destination):
            previous.rename(destination)
            return destination
    raise FileExistsError(folder)


def set_aside_previous() -> Path | None:
    """After the person chose the LuvBox data, move the old folder out of the way."""
    target, previous = _folders()
    with _APPDATA_LOCK:
        try:
            _check_links(target, previous)
            if not previous.exists():
                return None
            if not previous.is_dir() or not target.is_dir():
                raise AppDataError(_MIGRATION_HELP)
            return _set_aside(previous, target)
        except AppDataError:
            raise
        except OSError:
            raise AppDataError(_MIGRATION_HELP) from None


def app_directory() -> Path:
    target, previous = _folders()
    with _APPDATA_LOCK:
        try:
            _check_links(target, previous)
            if previous.exists():
                if not previous.is_dir():
                    raise AppDataError(_MIGRATION_HELP)
                if target.exists() and any(target.iterdir()):
                    # Never guess which of two libraries is authoritative. An
                    # older build run after the move only leaves logs and caches
                    # behind; those are kept aside so it cannot lock this one out.
                    try:
                        if not all(_leftover(entry.name) for entry in previous.iterdir()):
                            raise AppDataConflict(CONFLICT_HELP)
                        _set_aside(previous, target)
                    except AppDataError:
                        raise
                    except OSError:
                        # A second process may have set the same folder aside.
                        if previous.exists():
                            raise
                    return target
                if target.exists():
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
        except AppDataError:
            raise
        except OSError:
            raise AppDataError(_MIGRATION_HELP) from None


def settings_path() -> Path:
    return app_directory() / "settings.json"


def load_settings(*, strict: bool = False) -> dict:
    # A failed migration must not be mistaken for absent preferences.
    path = settings_path()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(value, dict):
            return value
        if strict:
            raise ValueError("invalid settings object")
        return {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        if strict:
            raise AppDataError("기존 설정을 읽지 못해 변경을 저장하지 않았습니다. "
                               "설정 파일을 확인한 뒤 다시 시도해 주세요.") from None
        return {}

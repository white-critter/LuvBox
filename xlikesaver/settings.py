"""Per-user preferences shared by the window and the unattended scheduled run.

Only preferences are stored here. Authentication lives encrypted in
session.bin and is never written to this file.
"""

import json
import os
from pathlib import Path


def app_directory() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "XlikeSaver"


def settings_path() -> Path:
    return app_directory() / "settings.json"


def load_settings() -> dict:
    try:
        value = json.loads(settings_path().read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}

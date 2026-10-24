"""Locate an installed Chrome or Microsoft Edge for the extension setup helper.

Only registry values and file existence are read. A browser association's
command line is never run, so its arguments cannot open a personal profile.
"""

from __future__ import annotations

import os
import re
from pathlib import Path


def find_edge() -> Path | None:
    """The installed msedge.exe (per-machine or per-user), if any."""
    candidates = []
    for variable in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
        base = os.environ.get(variable)
        if base:
            candidates.append(Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe")
    if os.name == "nt":
        import winreg

        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(hive, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe") as key:
                    candidates.append(Path(winreg.QueryValue(key, None)))
            except OSError:
                pass
    return next((path for path in candidates if path.is_file()), None)


def _association_executable(command: object) -> Path | None:
    if not isinstance(command, str) or len(command) > 32768 or any(char in command for char in "\r\n\0"):
        return None
    quoted = re.match(r'^\s*"([^\"]+)"(?:\s|$)', command)
    unquoted = re.match(r'^\s*([^\s\"]+)(?:\s|$)', command) if quoted is None else None
    match = quoted or unquoted
    if match is None:
        return None
    executable = Path(os.path.expandvars(match.group(1)))
    if (not executable.is_absolute() or executable.name.lower() not in {"chrome.exe", "msedge.exe"}
            or not executable.is_file()):
        return None
    return executable


def find_default_browser() -> Path | None:
    """Resolve the default HTTPS browser executable without running its command."""
    if os.name != "nt":
        return None
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                           r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice") as key:
            program_id, _ = winreg.QueryValueEx(key, "ProgId")
        if not isinstance(program_id, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,200}", program_id):
            return None
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, program_id + r"\shell\open\command") as key:
            command, _ = winreg.QueryValueEx(key, None)
        return _association_executable(command)
    except (OSError, ValueError):
        return None

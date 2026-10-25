"""Locate supported browsers without reading their profiles or running associations.

Only registry values and file existence are read. A browser association's
command line is never run, so its arguments cannot open a personal profile.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from urllib.parse import urlsplit


_BROWSERS = {
    "edge": ("Microsoft Edge", "msedge.exe", ("Microsoft", "Edge", "Application")),
    "chrome": ("Google Chrome", "chrome.exe", ("Google", "Chrome", "Application")),
    "firefox": ("Mozilla Firefox", "firefox.exe", ("Mozilla Firefox",)),
}


def browser_id(executable: Path | None) -> str:
    """Return a stable allowlisted identifier, never an executable from QML."""
    if executable is None:
        return ""
    name = executable.name.lower()
    return next((key for key, (_label, filename, _parts) in _BROWSERS.items()
                 if filename == name), "")


def _installed_browser(identifier: str) -> Path | None:
    _label, filename, parts = _BROWSERS[identifier]
    candidates = []
    for variable in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
        base = os.environ.get(variable)
        if base:
            candidates.append(Path(base).joinpath(*parts, filename))
            if identifier == "firefox" and variable == "LOCALAPPDATA":
                candidates.append(Path(base) / "Mozilla Firefox" / filename)
    if os.name == "nt":
        import winreg

        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(hive, "SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\App Paths\\" + filename) as key:
                    value = winreg.QueryValue(key, None)
                    if isinstance(value, str) and not any(char in value for char in "\r\n\0"):
                        candidates.append(Path(os.path.expandvars(value)))
            except (OSError, ValueError):
                pass
    return next((path for path in candidates if path.is_absolute()
                 and path.name.lower() == filename and path.is_file()), None)


def find_edge() -> Path | None:
    """The installed msedge.exe (per-machine or per-user), if any."""
    return _installed_browser("edge")


def find_chrome() -> Path | None:
    return _installed_browser("chrome")


def find_firefox() -> Path | None:
    return _installed_browser("firefox")


def _association_executable(command: object) -> Path | None:
    if not isinstance(command, str) or len(command) > 32768 or any(char in command for char in "\r\n\0"):
        return None
    quoted = re.match(r'^\s*"([^\"]+)"(?:\s|$)', command)
    unquoted = re.match(r'^\s*([^\s\"]+)(?:\s|$)', command) if quoted is None else None
    match = quoted or unquoted
    if match is None:
        return None
    executable = Path(os.path.expandvars(match.group(1)))
    if (not executable.is_absolute() or not browser_id(executable)
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


def browser_path(identifier: str) -> Path | None:
    """Resolve one supported browser; never fall back to a different browser."""
    if not isinstance(identifier, str) or identifier not in _BROWSERS:
        return None
    default = find_default_browser()
    if browser_id(default) == identifier:
        return default
    finder = {"edge": find_edge, "chrome": find_chrome, "firefox": find_firefox}[identifier]
    return finder()


def available_browsers() -> list[dict[str, str]]:
    """Display choices with the supported default browser first, if installed."""
    default = find_default_browser()
    preferred = browser_id(default)
    identifiers = ([preferred] if preferred else []) + [key for key in _BROWSERS if key != preferred]
    finders = {"edge": find_edge, "chrome": find_chrome, "firefox": find_firefox}
    return [{"id": key, "label": _BROWSERS[key][0]} for key in identifiers
            if (key == preferred and default is not None) or finders[key]() is not None]


def open_browser_url(identifier: str, url: str | None = None) -> bool:
    """Open a controlled web URL in the selected browser's ordinary profile.

    The browser chooses its normal active/default profile. We do not inspect
    profiles, forward association arguments or promise a particular profile.
    """
    executable = browser_path(identifier)
    if executable is None:
        return False
    if url is not None:
        if (not isinstance(url, str) or len(url) > 8192 or
                any(ord(char) < 32 or ord(char) == 127 for char in url)):
            return False
        try:
            parsed = urlsplit(url)
            if (parsed.username is not None or parsed.password is not None or
                    not ((parsed.scheme == "https" and parsed.hostname == "x.com") or
                         (parsed.scheme == "http" and parsed.hostname == "127.0.0.1" and
                          parsed.port is not None and 1 <= parsed.port <= 65535))):
                return False
        except ValueError:
            return False
    try:
        subprocess.Popen([str(executable)] + ([url] if url is not None else []),
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, close_fds=True)
    except OSError:
        return False
    return True

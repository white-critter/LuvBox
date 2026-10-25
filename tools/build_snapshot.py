"""Bind a completed build to the exact project inputs used before PyInstaller."""

import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from luvbox import __version__  # noqa: E402


def _digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _inputs(root):
    # Share the packaging allowlist, so new application sources and QML cannot
    # silently be omitted from the build fence or the corresponding source ZIP.
    from tools.package_release import DEPENDENCIES, project_source_files, project_file

    files = set(project_source_files(root))
    for component in ("exiftool", "ffmpeg"):
        for path in (root / "tools" / component / "runtime").rglob("*"):
            project_file(path, root)
            if path.is_file():
                files.add(path)
    return {
        "files": {path.relative_to(root).as_posix(): _digest(path) for path in sorted(files)},
        "python": sys.version,
        "dependencies": {name: importlib.metadata.version(name) for name in DEPENDENCIES},
    }


def _path(root):
    return root / "build" / f"build-inputs-{__version__}.json"


def _application_inputs(executable, root):
    from tools.package_release import project_file

    application = executable.parent
    project_file(application, root)
    files = {}
    windows_paths = set()
    pending = [application]
    while pending:
        for path in pending.pop().iterdir():
            project_file(path, root)
            if path.is_dir():
                pending.append(path)
            else:
                name = path.relative_to(application).as_posix()
                if name.casefold() in windows_paths:
                    raise ValueError("The application runtime contains duplicate Windows paths")
                windows_paths.add(name.casefold())
                files[name] = _digest(path)
    return files


def _read(root):
    from tools.package_release import project_file

    path = _path(root)
    try:
        project_file(path, root)
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ValueError("Run build.ps1 to record the project inputs before building") from None
    if not isinstance(report, dict) or report.get("schema") != 1 or report.get("version") != __version__:
        raise ValueError("The build input record is invalid; rebuild with build.ps1")
    return report


def _write(root, report):
    from tools.package_release import project_file

    path = _path(root)
    path.parent.mkdir(exist_ok=True)
    project_file(path.parent, root)
    descriptor, filename = tempfile.mkstemp(prefix=".build-inputs-", suffix=".json", dir=path.parent)
    staged = Path(filename)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(report, stream, sort_keys=True)
            stream.write("\n")
        os.replace(staged, path)
    finally:
        staged.unlink(missing_ok=True)


def record(root=ROOT):
    _write(root, {"schema": 1, "version": __version__, "inputs": _inputs(root)})


def _unchanged(root, report):
    if report.get("inputs") != _inputs(root):
        raise ValueError("Project sources, build inputs or dependencies changed; rebuild with build.ps1")


def seal(executable, root=ROOT):
    from tools.package_release import project_file

    report = _read(root)
    if "executable_sha256" in report:
        raise ValueError("The build input record is already sealed; rebuild with build.ps1")
    _unchanged(root, report)
    project_file(executable, root)
    report["executable_sha256"] = _digest(executable)
    report["application_files"] = _application_inputs(executable, root)
    _write(root, report)


def require_snapshot(executable, root=ROOT):
    report = _read(root)
    if report.get("executable_sha256") != _digest(executable):
        raise ValueError("The executable has no matching sealed build input record; rebuild with build.ps1")
    _unchanged(root, report)
    if report.get("application_files") != _application_inputs(executable, root):
        raise ValueError("The application runtime changed after sealing; rebuild with build.ps1")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("record", "seal"))
    options = parser.parse_args(argv)
    if options.action == "record":
        record()
    else:
        executable = ROOT / "release" / f"LuvBox-{__version__}" / f"LuvBox-{__version__}.exe"
        seal(executable)
    print(f"Build input record {options.action} complete.")


if __name__ == "__main__":
    main()

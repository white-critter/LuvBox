"""Check or reproduce LuvBox's small, native-only Windows FFmpeg runtime.

Use a portable w64devkit 2.10.0 directory; no admin install is needed. Its
compiler and make are build tools, never copied into the application release.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import urllib.request


HERE = Path(__file__).resolve().parent
SOURCE_NAME = "ffmpeg-7.1.tar.xz"
SOURCE_SHA256 = "40973d44970dbc83ef302b0609f2e74982be2d85916dd2ee7472d30678a7abe6"
SOURCE_URL = "https://ffmpeg.org/releases/ffmpeg-7.1.tar.xz"
TOOLCHAIN_URL = "https://github.com/skeeto/w64devkit/releases/download/v2.10.0/w64devkit-x64-2.10.0.7z.exe"
TOOLCHAIN_SHA256 = "18d0a4c71a166f8401ab6305781bec5882b40b5e06ba9807c61cb5f3b3c6325e"


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def get_source():
    archive = HERE / SOURCE_NAME
    if not archive.exists():
        temporary = archive.with_suffix(".download")
        try:
            with urllib.request.urlopen(SOURCE_URL, timeout=60) as source, temporary.open("wb") as output:
                shutil.copyfileobj(source, output)
            if sha256(temporary) != SOURCE_SHA256:
                raise RuntimeError("FFmpeg source checksum mismatch")
            temporary.replace(archive)
        finally:
            temporary.unlink(missing_ok=True)
    if sha256(archive) != SOURCE_SHA256:
        raise RuntimeError("FFmpeg source checksum mismatch")
    return archive


def check():
    get_source()
    manifest = json.loads((HERE / "manifest.json").read_text(encoding="utf-8"))
    for item in (manifest["source"], manifest["runtime"]):
        path = HERE / item["filename"]
        if not path.is_file() or sha256(path) != item["sha256"]:
            raise RuntimeError(f"FFmpeg artifact missing or modified: {item['filename']}")
    for filename, digest in manifest["build_files"].items():
        if sha256(HERE / filename) != digest:
            raise RuntimeError(f"FFmpeg build recipe modified: {filename}")
    print("FFmpeg source, build recipe, and runtime checksums match.")


def run(command, *, cwd, env, output=None, timeout=600):
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if output is not None:
        with output.open("wb") as stream:
            subprocess.run(command, cwd=cwd, env=env, stdout=stream, stderr=subprocess.STDOUT,
                           creationflags=flags, timeout=timeout, check=True)
        return ""
    result = subprocess.run(command, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            creationflags=flags, timeout=timeout, check=True)
    return result.stdout.decode("utf-8", errors="replace")


def build(toolchain, workspace, jobs):
    if os.name != "nt":
        raise RuntimeError("This recipe targets Windows x64 using w64devkit.")
    binpath = toolchain.resolve() / "bin"
    for command in ("gcc.exe", "sh.exe", "make.exe", "objdump.exe"):
        if not (binpath / command).is_file():
            raise RuntimeError(f"Missing portable toolchain command: {command}")
    workspace = workspace.resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    source = workspace / "ffmpeg-7.1"
    archive = get_source()
    if not (source / "configure").is_file():
        with tarfile.open(archive) as files:
            files.extractall(workspace, filter="data")
    scratch = workspace / "tmp"
    scratch.mkdir(exist_ok=True)
    env = os.environ.copy()
    env["PATH"] = str(binpath) + os.pathsep + env["PATH"]
    env["LC_ALL"] = "C"
    # busybox sh needs forward slashes; Windows' default TEMP has backslashes.
    env["TMPDIR"] = scratch.as_posix()
    options = json.loads((HERE / "configure-flags.json").read_text(encoding="utf-8"))
    print("Configuring native-only FFmpeg...", flush=True)
    run([str(binpath / "sh.exe"), "configure", *options], cwd=source, env=env,
        output=workspace / "configure.log", timeout=300)
    print("Compiling FFmpeg...", flush=True)
    run([str(binpath / "make.exe"), f"-j{jobs}", "ffmpeg.exe"], cwd=source, env=env,
        output=workspace / "make.log", timeout=600)
    record_runtime(binpath, source, env, options)


def record_runtime(binpath, source, env, options):
    """Record verified metadata after a completed build without recompiling."""
    executable = source / "ffmpeg.exe"
    version = run([str(executable), "-version"], cwd=source, env=env)
    license_info = run([str(executable), "-L"], cwd=source, env=env)
    if "GNU Lesser General Public" not in license_info or "--enable-gpl" in version:
        raise RuntimeError("Unexpected FFmpeg license/configuration")
    dependencies = run([str(binpath / "objdump.exe"), "-p", str(executable)], cwd=source, env=env)
    dlls = [line.split(":", 1)[1].strip() for line in dependencies.splitlines() if "DLL Name:" in line]
    if any(name.lower() not in {"kernel32.dll", "msvcrt.dll", "user32.dll", "advapi32.dll", "shell32.dll", "bcrypt.dll"} for name in dlls):
        raise RuntimeError(f"Unexpected runtime DLL dependency: {dlls}")
    runtime = HERE / "runtime"
    runtime.mkdir(exist_ok=True)
    shutil.copy2(executable, runtime / "ffmpeg.exe")
    shutil.copy2(source / "COPYING.LGPLv2.1", HERE / "LICENSE.LGPLv2.1.txt")
    (HERE / "build-info.txt").write_text(version + "\nRuntime DLLs: " + ", ".join(dlls) + "\n", encoding="utf-8")
    compiler = run([str(binpath / "gcc.exe"), "--version"], cwd=source, env=env).splitlines()[0]
    manifest = {
        "version": "7.1", "license": "LGPL-2.1-or-later", "target": "Windows x64",
        "source": {"filename": SOURCE_NAME, "url": SOURCE_URL, "sha256": SOURCE_SHA256},
        "runtime": {"filename": "runtime/ffmpeg.exe", "sha256": sha256(runtime / "ffmpeg.exe"),
                    "size": (runtime / "ffmpeg.exe").stat().st_size},
        "toolchain": {"name": "w64devkit", "version": "2.10.0", "url": TOOLCHAIN_URL,
                      "sha256": TOOLCHAIN_SHA256, "compiler": compiler},
        "configure": options, "runtime_dlls": dlls, "source_modified": False,
        "build_files": {name: sha256(HERE / name) for name in ("build.py", "configure-flags.json")},
    }
    (HERE / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    check()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Verify existing shipped artifacts")
    parser.add_argument("--toolchain", type=Path, help="Extracted w64devkit 2.10.0 directory")
    parser.add_argument("--workspace", type=Path, default=HERE.parents[1] / ".test-output" / "ffmpeg-build")
    parser.add_argument("--jobs", type=int, default=8)
    arguments = parser.parse_args()
    if arguments.check:
        check()
    elif arguments.toolchain:
        build(arguments.toolchain, arguments.workspace, max(1, arguments.jobs))
    else:
        parser.error("Choose --check or --toolchain PATH")

"""Keep upstream PySide setup while omitting the optional Mesa OpenGL fallback."""

from pathlib import Path
import runpy

import PyInstaller


upstream = runpy.run_path(str(Path(PyInstaller.__file__).parent / "hooks" / "hook-PySide6.py"))
hiddenimports = upstream.get("hiddenimports", [])
datas = upstream.get("datas", [])
binaries = [
    (source, destination)
    for source, destination in upstream.get("binaries", [])
    if Path(source).name.casefold() != "opengl32sw.dll"
]

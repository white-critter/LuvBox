"""Keep upstream PySide setup while omitting the optional Mesa OpenGL fallback and Qt's translations."""

from pathlib import Path
import runpy

import PyInstaller
from PyInstaller.utils.hooks.qt import pyside6_library_info


upstream = runpy.run_path(str(Path(PyInstaller.__file__).parent / "hooks" / "hook-PySide6.py"))
hiddenimports = upstream.get("hiddenimports", [])
datas = upstream.get("datas", [])
binaries = [
    (source, destination)
    for source, destination in upstream.get("binaries", [])
    if Path(source).name.casefold() != "opengl32sw.dll"
]

# LuvBox installs no QTranslator and writes its own Korean text, so Qt's roughly
# 150 .qm files (8 MB) are never read. Each Qt module hook collects its own set
# from this table; empty it before they run. package_release.py rejects a build
# that still contains them, in case a PyInstaller update changes this lookup.
if pyside6_library_info.version is not None:
    for _info in (*pyside6_library_info.python_modules.values(), *pyside6_library_info.shared_libraries.values()):
        _info.translations = []

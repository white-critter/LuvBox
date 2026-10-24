"""Generate the Windows version resource from the application's numeric version."""

from pathlib import Path
import re
import sys

from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo, StringFileInfo, StringStruct, StringTable,
    VarFileInfo, VarStruct, VSVersionInfo,
)


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from luvbox import __version__


def write():
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", __version__):
        raise ValueError("Use a numeric MAJOR.MINOR.PATCH version.")
    components = tuple(int(part) for part in __version__.split("."))
    if any(part > 65535 for part in components):
        raise ValueError("Windows version components must not exceed 65535.")
    version = (*components, 0)
    resource = VSVersionInfo(
        ffi=FixedFileInfo(filevers=version, prodvers=version, mask=0x3F,
                          flags=0, OS=0x40004, fileType=1, subtype=0, date=(0, 0)),
        kids=[
            StringFileInfo([StringTable("040904B0", [
                StringStruct("FileDescription", "LuvBox"),
                StringStruct("FileVersion", __version__),
                StringStruct("InternalName", "LuvBox"),
                StringStruct("OriginalFilename", f"LuvBox-{__version__}.exe"),
                StringStruct("ProductName", "LuvBox"),
                StringStruct("ProductVersion", __version__),
            ])]),
            VarFileInfo([VarStruct("Translation", [1033, 1200])]),
        ],
    )
    destination = ROOT / "build" / "windows-version.txt"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(str(resource), encoding="utf-8")


if __name__ == "__main__":
    write()

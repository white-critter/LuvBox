"""Collect desktop GUI dependencies without unused PDF or virtual-keyboard plugins."""

from pathlib import Path

from PyInstaller.utils.hooks.qt import add_qt6_dependencies


hiddenimports, binaries, datas = add_qt6_dependencies(__file__)

EXCLUDED_PLUGINS = {"qpdf", "qtvirtualkeyboardplugin"}

binaries = [
    (source, destination)
    for source, destination in binaries
    if Path(source).stem.casefold() not in EXCLUDED_PLUGINS
]

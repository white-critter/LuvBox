"""Keep the supported app and worker independent of the removed GUI toolkit."""

import ast
from importlib.util import find_spec
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
OBSOLETE_MODULES = {
    "app_icon", "charts", "dialogs", "fonts", "gallery", "gui", "history_gui",
    "popup_menu", "presentation", "review_gui", "review_preview", "theme",
}


class QtOnlyRuntimeTests(unittest.TestCase):
    def test_runtime_sources_cannot_import_the_removed_toolkit_or_ui(self):
        sources = [ROOT / "entry.py", *(ROOT / "luvbox").glob("*.py")]
        forbidden = []
        for path in sources:
            tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                    if node.level or node.module == "luvbox":
                        names += ["luvbox." + alias.name for alias in node.names]
                for name in names:
                    if (name.split(".", 1)[0] in {"tkinter", "_tkinter"}
                            or name.removeprefix("luvbox.") in OBSOLETE_MODULES):
                        forbidden.append(f"{path.name}:{node.lineno}: {name}")
        self.assertEqual(forbidden, [])
        self.assertEqual([name for name in sorted(OBSOLETE_MODULES)
                          if (ROOT / "luvbox" / (name + ".py")).exists()], [])

    @unittest.skipUnless(find_spec("PySide6") is not None, "Qt dependencies are not installed")
    def test_supported_controllers_and_worker_import_with_toolkit_blocked(self):
        script = """
import builtins, importlib
original = builtins.__import__
def require_qt_only(name, *args, **kwargs):
    if name.split('.', 1)[0] in {'tkinter', '_tkinter'}:
        raise AssertionError('Removed GUI toolkit was imported')
    return original(name, *args, **kwargs)
builtins.__import__ = require_qt_only
for name in ('entry', 'luvbox.qt_app', 'luvbox.download_service', 'luvbox.scheduled'):
    importlib.import_module(name)
"""
        with tempfile.TemporaryDirectory(prefix="luvbox-qt-only-") as folder:
            environment = {**os.environ, "LOCALAPPDATA": folder,
                           "APPDATA": str(Path(folder) / "roaming"), "QT_QPA_PLATFORM": "offscreen"}
            result = subprocess.run([sys.executable, "-c", script], cwd=ROOT, env=environment,
                                    capture_output=True, timeout=30,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))

    def test_removed_ui_switch_is_rejected_without_starting_an_app(self):
        result = subprocess.run([sys.executable, str(ROOT / "entry.py"), "--legacy-ui"],
                                cwd=ROOT, capture_output=True, timeout=10,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(result.returncode, 2, result.stderr.decode(errors="replace"))

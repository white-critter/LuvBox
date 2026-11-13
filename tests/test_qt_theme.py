"""The one theme file: how it is found, that releases carry it, and that screens use it."""

from pathlib import Path
import re
import tempfile
import unittest

from tools import package_release
try:
    from tests import app_data_isolation  # noqa: F401  (never this PC's real app data)
except ModuleNotFoundError:      # run as a script: the tests folder is the path
    import app_data_isolation  # noqa: F401


ROOT = Path(__file__).resolve().parents[1]
QML = ROOT / "assets" / "qt"
TONES = {"primary", "quiet", "ghost", "danger", "nav", "chip", "soft"}


def screens():
    """Every QML file except the theme itself."""
    return [path for path in sorted(QML.glob("*.qml")) if path.name != "Theme.qml"]


class ThemeModuleTests(unittest.TestCase):
    def test_theme_is_one_shared_object_declared_in_the_folder_module_file(self):
        # Every screen reads `Theme.<name>`. That only resolves while the folder's
        # module file names Theme.qml as a singleton and the file says so itself.
        lines = [line.split() for line in (QML / "qmldir").read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertIn(["singleton", "Theme", "Theme.qml"], lines)
        theme = (QML / "Theme.qml").read_text(encoding="utf-8")
        self.assertTrue(theme.lstrip().startswith("pragma Singleton"), theme[:40])

    def test_source_archive_and_build_snapshot_include_the_module_file(self):
        # The snapshot and the source ZIP take their file list from here; the
        # build copies the whole folder, so the program itself always has it.
        shipped = {path.relative_to(ROOT).as_posix() for path in package_release.project_source_files(ROOT)}
        self.assertIn("assets/qt/qmldir", shipped)
        self.assertIn("assets/qt/Theme.qml", shipped)
        build = (ROOT / "build.ps1").read_text(encoding="utf-8-sig")
        self.assertIn('--add-data "$PSScriptRoot\\assets\\qt;assets/qt"', build)

    def test_a_project_without_the_module_file_is_still_listed(self):
        # Fixture projects in other tests have QML and no module file.
        with tempfile.TemporaryDirectory(prefix="luvbox-theme-sources-") as folder:
            root = Path(folder)
            for name in package_release.SOURCE_FILES:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"x")
            for name in ("docs/사용안내.txt", "docs/development.md", "docs/verification.md",
                         "docs/performance.md", "docs/security-review.md", "assets/qt/Main.qml"):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"x")
            names = {path.relative_to(root).as_posix() for path in package_release.project_source_files(root)}
            self.assertIn("assets/qt/Main.qml", names)
            self.assertNotIn("assets/qt/qmldir", names)
            (root / "assets/qt/qmldir").write_text("singleton Theme Theme.qml\n", encoding="utf-8")
            names = {path.relative_to(root).as_posix() for path in package_release.project_source_files(root)}
            self.assertIn("assets/qt/qmldir", names)


class ThemeUseTests(unittest.TestCase):
    """Colors, type sizes and corner radii live in Theme.qml and nowhere else."""

    def lines(self):
        for path in screens():
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                code = line.split("//", 1)[0]
                if code.strip():
                    yield path.name, number, code

    def test_no_color_is_written_out_in_a_screen(self):
        literal = re.compile(r"[\"']#[0-9a-fA-F]{3,8}[\"']")
        found = [f"{name}:{number}: {code.strip()}" for name, number, code in self.lines() if literal.search(code)]
        self.assertEqual(found, [], "use a Theme color")

    def test_type_sizes_and_corner_radii_come_from_the_theme(self):
        plain = re.compile(r"(font\.pixelSize|radius):\s*\d+\s*(;|}|$)")
        found = [f"{name}:{number}: {code.strip()}" for name, number, code in self.lines() if plain.search(code)]
        self.assertEqual(found, [], "use Theme.font… / Theme.radius… (or `height / 2` for a pill)")

    def test_every_name_read_from_the_theme_exists(self):
        theme = (QML / "Theme.qml").read_text(encoding="utf-8")
        declared = set(re.findall(r"readonly property \w+ (\w+):", theme))
        self.assertGreater(len(declared), 40)
        used = {}
        for name, number, code in self.lines():
            for token in re.findall(r"\bTheme\.(\w+)", code):
                used.setdefault(token, f"{name}:{number}")
        missing = {token: where for token, where in used.items() if token not in declared}
        self.assertEqual(missing, {})
        # A name nobody reads is a leftover.
        self.assertEqual(sorted(declared - set(used)), [])

    def test_button_tones_are_the_known_ones(self):
        for name, number, code in self.lines():
            if re.match(r"\s*tone:", code):
                # Also the two sides of `tone: busy ? "quiet" : "primary"`.
                for tone in re.findall(r"\"(\w+)\"", code):
                    self.assertIn(tone, TONES, f"{name}:{number}")
        button = (QML / "FlatButton.qml").read_text(encoding="utf-8")
        for tone in TONES - {"quiet"}:
            self.assertIn(f'"{tone}"', button, tone)

    def test_dialogs_share_one_frame(self):
        # Every dialog is a FlatDialog, except the two full-window viewers, which
        # keep their own header and still take the shared card as a background.
        bare = []
        for path in screens():
            text = path.read_text(encoding="utf-8")
            for match in re.finditer(r"^( *)Dialog \{", text, re.MULTILINE):
                block = text[match.start():match.start() + 12000]
                bare.append((path.name, "background: Card {" in block))
        self.assertEqual(sorted(bare), [("FlatDialog.qml", True), ("MainWindow.qml", True),
                                        ("SaveReviewDialog.qml", True)])
        for path in screens():
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("footer: DialogButtonBox {", text, path.name)
            if path.name not in ("DialogButtons.qml", "FlatMenu.qml", "FlatMenuItem.qml", "FlatMenuSeparator.qml"):
                self.assertIsNone(re.search(r"^\s*(Menu|MenuItem|MenuSeparator) \{", text, re.MULTILINE), path.name)


if __name__ == "__main__":
    unittest.main()

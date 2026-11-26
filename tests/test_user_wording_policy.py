"""Policy checks ensuring user-facing copy and error boundaries never leak internal details."""

import ast
from pathlib import Path
import sqlite3
import tempfile
import unittest

from tests import app_data_isolation  # noqa: F401  (never this PC's real app data)
from luvbox.user_errors import UserRuntimeError, UserValueError, safe_ui_error
from tools import package_release


ROOT = Path(__file__).resolve().parents[1]


def _write_clean_tree(root: Path) -> None:
    (root / "docs").mkdir(parents=True, exist_ok=True)
    (root / "tools" / "launcher").mkdir(parents=True, exist_ok=True)
    (root / "assets" / "qt").mkdir(parents=True, exist_ok=True)
    (root / "assets" / "browser-extension" / "chromium").mkdir(parents=True, exist_ok=True)
    (root / "assets" / "browser-extension" / "firefox").mkdir(parents=True, exist_ok=True)
    (root / ".github" / "ISSUE_TEMPLATE").mkdir(parents=True, exist_ok=True)
    (root / "README.md").write_text("# 러브박스 (LuvBox) 1.12.11\n", encoding="utf-8")
    (root / "CHANGELOG.md").write_text("# 변경 사항\n\n## 1.12.11 — 2026-11-24\n\n- 사용성 개선\n", encoding="utf-8")
    (root / "docs" / "사용안내.txt").write_text("러브박스 (LuvBox) 1.12.11\n", encoding="utf-8")
    (root / "docs" / "browser-login.md").write_text("# X 로그인 연결\n", encoding="utf-8")
    (root / "assets" / "browser-extension" / "README.md").write_text("# 확장 안내\n", encoding="utf-8")
    (root / "assets" / "browser-extension" / "PRIVACY.md").write_text("# 개인정보 안내\n", encoding="utf-8")
    (root / ".github" / "ISSUE_TEMPLATE" / "bug.yml").write_text("name: 버그 신고\n", encoding="utf-8")
    (root / "tools" / "launcher" / "splash.ini").write_text("tagline=좋아요한 사진과 영상을 내 폴더에\n", encoding="utf-8")
    (root / "tools" / "launcher" / "Launcher.cs").write_text(
        'internal static class Messages {\n    public const string Ok = "다시 실행해 주세요.";\n}\n',
        encoding="utf-8",
    )
    (root / "assets" / "qt" / "Sample.qml").write_text(
        'import QtQuick\nItem {\n    // QML SQLite comment is ignored\n    property string labelText: ""\n}\n',
        encoding="utf-8",
    )
    for variant in ("chromium", "firefox"):
        (root / "assets" / "browser-extension" / variant / "connect.html").write_text(
            "<html><body><p>LuvBox 연결</p></body></html>", encoding="utf-8"
        )
        (root / "assets" / "browser-extension" / variant / "extension-page.js").write_text(
            'const msg = "연결을 확인하는 중…";\n', encoding="utf-8"
        )


class UserWordingPolicyTests(unittest.TestCase):
    def test_project_surfaces_pass_wording_policy(self):
        package_release.require_user_facing_wording(ROOT)

    def test_internal_terms_are_rejected_across_user_surfaces(self):
        cases = (
            ("README.md", "# 러브박스\nPySide6로 만들었습니다.\n", "README.md"),
            ("CHANGELOG.md", "## 1.12.11 — 2026-11-24\n- SQLite 속도 개선\n", "CHANGELOG.md"),
            ("docs/사용안내.txt", "러브박스 (LuvBox) 1.12.11\n%LOCALAPPDATA%를 여세요.\n", "docs/사용안내.txt"),
            ("tools/launcher/splash.ini", "status.open=QML 창을 여는 중\n", "tools/launcher/splash.ini"),
            (
                "tools/launcher/Launcher.cs",
                'internal static class Messages {\n    public const string Bad = "PyInstaller 실패";\n}\n',
                "tools/launcher/Launcher.cs",
            ),
            (
                "assets/qt/Sample.qml",
                'import QtQuick\nItem {\n    Text { text: "PermissionError 발생" }\n}\n',
                "assets/qt/Sample.qml",
            ),
            ("assets/browser-extension/chromium/manifest.json", '{"description":"SQLite"}', "manifest.json"),
            ("assets/browser-extension/firefox/background.js", 'const publicError = "SQLite";', "background.js"),
            ("luvbox/browser_bridge.py", '_PAGE = "<p>SQLite</p>".encode("utf-8")', "browser_bridge.py"),
        )
        for rel, bad_content, expected_label in cases:
            with self.subTest(rel=rel), tempfile.TemporaryDirectory(prefix="luvbox-wording-") as folder:
                root = Path(folder)
                _write_clean_tree(root)
                (root / rel).parent.mkdir(parents=True, exist_ok=True)
                (root / rel).write_text(bad_content, encoding="utf-8")
                with self.assertRaisesRegex(ValueError, expected_label):
                    package_release.require_user_facing_wording(root)

    def test_no_isascii_exception_passthrough_in_package(self):
        offenders = []
        for path in sorted((ROOT / "luvbox").glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.ExceptHandler):
                    for child in ast.walk(node):
                        if (isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
                                and child.func.attr == "isascii"):
                            offenders.append(f"{path.name}:{child.lineno}")
                elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "isascii"):
                    # Only HTTP Content-Length digit validation may call .isascii().
                    is_header_digits = isinstance(node.func.value, ast.Name) and node.func.value.id == "announced"
                    if not (path.name == "updater.py" and is_header_digits):
                        offenders.append(f"{path.name}:{node.lineno}")
        self.assertEqual(offenders, [])

    def test_safe_ui_error_masks_paths_and_runtime_exceptions_for_korean_paths(self):
        korean_path = r"C:\Users\홍길동\AppData\Local\Temp\사진\secret.sqlite3"
        fallback = "작업을 완료하지 못했습니다."
        for exc in (
            PermissionError(13, "Access is denied", korean_path),
            sqlite3.OperationalError(f"attempt to write a readonly database at {korean_path}"),
            OSError(f"WinError 32: {korean_path}\nTraceback (most recent call last): ..."),
            ValueError(f"bad token for {korean_path}"),
        ):
            shown = safe_ui_error(exc, fallback)
            self.assertEqual(shown, fallback)
            self.assertNotIn("홍길동", shown)
            self.assertNotIn("sqlite3", shown)
            self.assertNotIn("Traceback", shown)
            self.assertNotIn("WinError", shown)

        self.assertEqual(safe_ui_error(UserValueError("올바른 폴더를 고르세요."), fallback), "올바른 폴더를 고르세요.")
        self.assertEqual(safe_ui_error(UserRuntimeError("잠시 뒤 다시 시도해 주세요."), fallback), "잠시 뒤 다시 시도해 주세요.")


if __name__ == "__main__":
    unittest.main()

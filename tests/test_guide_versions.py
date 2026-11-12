"""The bundled guides name the version that is being built."""

from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from luvbox import __version__
from tools import package_release


ROOT = Path(__file__).resolve().parents[1]


class GuideVersionTests(unittest.TestCase):
    def test_project_guides_name_the_current_version(self):
        package_release.require_current_guides(ROOT, __version__)

    def test_the_build_checks_the_guides_before_the_long_build_and_again_when_packaging(self):
        build = (ROOT / "build.ps1").read_text(encoding="utf-8-sig")
        check = build.index(r"'tools\package_release.py' '--check-guides'")
        self.assertLess(check, build.index("-m PyInstaller"))
        self.assertLess(build.index("-m PyInstaller"), build.rindex(r"'tools\package_release.py'"))
        result = subprocess.run([sys.executable, str(ROOT / "tools" / "package_release.py"), "--check-guides"],
                                cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        # The check alone never builds an archive.
        self.assertNotIn("Release ", result.stdout)

    def write(self, root, *, guide_version="9.8.7", development_version="9.8.7"):
        (root / "docs").mkdir(exist_ok=True)
        (root / "docs" / "사용안내.txt").write_text(
            f"러브박스 (LuvBox) {guide_version}\n\n"
            "LuvBox.exe를 실행하세요.\n"
            f"소스와 라이선스는 릴리스의 LuvBox-{guide_version}-source-licenses.zip에 있습니다.\n",
            encoding="utf-8")
        (root / "docs" / "development.md").write_text(
            f"# LuvBox {development_version} 개발 안내\n\n"
            f"산출물은 `release/LuvBox.exe`, `release/SHA256-{development_version}.txt`, "
            f"`release/LuvBox-{development_version}-source-licenses.zip`입니다.\n", encoding="utf-8")
        (root / "README.md").write_text("# 러브박스 (LuvBox) 9.8.7\n", encoding="utf-8")
        (root / "CHANGELOG.md").write_text("# 변경 사항\n\n## 9.8.7 — 2026-11-12\n\n- 항목\n", encoding="utf-8")

    def test_a_guide_left_on_an_older_version_is_refused_by_name(self):
        with tempfile.TemporaryDirectory(prefix="luvbox-guides-") as folder:
            root = Path(folder)
            self.write(root)
            package_release.require_current_guides(root, "9.8.7")
            self.write(root, guide_version="9.8.5")
            with self.assertRaisesRegex(ValueError, "사용안내.txt 첫 줄.*버전 이름") as caught:
                package_release.require_current_guides(root, "9.8.7")
            self.assertNotIn("실행 안내", str(caught.exception))
            # The former ZIP wording (unpack a folder, start a versioned EXE) is refused by name.
            guide = root / "docs" / "사용안내.txt"
            guide.write_text("러브박스 (LuvBox) 9.8.7\n\nZIP 전체를 푼 뒤 LuvBox-9.8.7 폴더의 LuvBox-9.8.7.exe를 실행하세요.\n",
                             encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "사용안내.txt 실행 안내"):
                package_release.require_current_guides(root, "9.8.7")
            self.write(root)
            development = root / "docs" / "development.md"
            development.write_text(development.read_text(encoding="utf-8").replace("`release/LuvBox.exe`, ", ""),
                                   encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "development.md 산출물 이름"):
                package_release.require_current_guides(root, "9.8.7")
            self.write(root, development_version="9.8.6")
            with self.assertRaisesRegex(ValueError, "development.md 제목.*산출물 이름") as caught:
                package_release.require_current_guides(root, "9.8.7")
            self.assertNotIn("사용안내", str(caught.exception))
            self.write(root)
            with self.assertRaisesRegex(ValueError, "README.md 제목.*CHANGELOG.md 최신 항목"):
                package_release.require_current_guides(root, "9.8.8")


if __name__ == "__main__":
    unittest.main()

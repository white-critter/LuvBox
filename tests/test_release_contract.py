"""What a release must look like for an already installed LuvBox to update to it.

Version 1.8.0 fetches the next release by fixed names and reads its checksum
file in a fixed format. Those copies cannot be changed once they are on
people's PCs, so everything here is written out literally instead of being
imported from the code: a change that would strand installed versions fails
here, before a build.
"""

import hashlib
from pathlib import Path
import re
import shutil
import struct
import tempfile
import unittest

from PIL import Image

from luvbox import launcher, updater
from tests.release_server import ReleaseServer, make_program
from tools import draw_splash, package_release


ROOT = Path(__file__).resolve().parents[1]
# How 1.8.0 recognises a checksum row (a frozen copy of its rule).
INSTALLED_CHECKSUM_ROW = re.compile(r"([0-9a-f]{64})  ([^\s/\\][^/\\\r\n]{0,199})\Z")


class ReleaseContractTests(unittest.TestCase):
    def test_asset_names_are_the_ones_installed_versions_ask_for(self):
        self.assertEqual(updater.EXE_ASSET, "LuvBox.exe")
        self.assertEqual(updater.checksum_asset("1.8.0"), "SHA256-1.8.0.txt")
        self.assertEqual(updater.source_asset("1.8.0"), "LuvBox-1.8.0-source-licenses.zip")
        self.assertEqual(updater.release_assets("1.10.3"),
                         ("LuvBox.exe", "SHA256-1.10.3.txt", "LuvBox-1.10.3-source-licenses.zip"))
        # Packaging takes the names from the updater rather than spelling its own.
        self.assertIs(package_release.release_assets, updater.release_assets)
        self.assertIs(package_release.format_checksums, updater.format_checksums)

    def test_release_addresses_are_the_public_github_ones(self):
        self.assertEqual(updater.REPOSITORY, "white-critter/LuvBox")
        self.assertEqual(updater.GITHUB.latest, "https://api.github.com/repos/white-critter/LuvBox/releases/latest")
        self.assertEqual(updater.GITHUB.asset("1.9.0", "LuvBox.exe"),
                         "https://github.com/white-critter/LuvBox/releases/download/v1.9.0/LuvBox.exe")
        self.assertEqual(updater.GITHUB.asset("1.9.0", "SHA256-1.9.0.txt"),
                         "https://github.com/white-critter/LuvBox/releases/download/v1.9.0/SHA256-1.9.0.txt")
        self.assertEqual(updater.RELEASES_PAGE, "https://github.com/white-critter/LuvBox/releases/latest")
        self.assertTrue(updater.GITHUB.secure)
        self.assertEqual(updater.CLIENT_NAME, "LuvBox-Updater")

    def test_checksum_file_is_what_sha256sum_writes_and_1_8_0_reads(self):
        digest = hashlib.sha256(b"program").hexdigest()
        other = hashlib.sha256(b"sources").hexdigest()
        text = updater.format_checksums([(digest, "LuvBox.exe"), (other, "LuvBox-1.9.0-source-licenses.zip")])
        self.assertEqual(text, f"{digest}  LuvBox.exe\n{other}  LuvBox-1.9.0-source-licenses.zip\n")
        rows = dict(reversed(INSTALLED_CHECKSUM_ROW.match(line).groups()) for line in text.splitlines())
        self.assertEqual(rows["LuvBox.exe"], digest)
        self.assertEqual(updater.parse_checksums(text), rows)
        # Windows line endings and surrounding notes do not hide a row.
        self.assertEqual(updater.parse_checksums("# checksums\r\n" + text.replace("\n", "\r\n"))["LuvBox.exe"], digest)
        for bad in ((digest.upper(), "LuvBox.exe"), (digest[:-1], "LuvBox.exe"), (digest, "folder/LuvBox.exe"),
                    (digest, " LuvBox.exe"), (digest, "")):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                updater.format_checksums([bad])
        # The first row for a name wins; a later duplicate cannot replace it.
        self.assertEqual(updater.parse_checksums(f"{digest}  LuvBox.exe\n{other}  LuvBox.exe\n")["LuvBox.exe"], digest)

    def test_the_program_footer_layout_is_fixed_for_the_launcher_and_the_updater(self):
        self.assertEqual(launcher.FOOTER.format, "<8sIQQ32s16s8s")
        self.assertEqual(launcher.FOOTER.size, 84)
        self.assertEqual((launcher.FOOTER_HEAD, launcher.FOOTER_TAIL, launcher.FOOTER_FORMAT),
                         (b"LUVBOXAR", b"RAXOBVUL", 1))
        footer = launcher.make_footer(8192, 1000, hashlib.sha256(b"archive").digest(), "1.8.0")
        self.assertEqual(struct.unpack("<8sIQQ32s16s8s", footer),
                         (b"LUVBOXAR", 1, 8192, 1000, hashlib.sha256(b"archive").digest(),
                          b"1.8.0".ljust(16, b"\0"), b"RAXOBVUL"))
        # The C# launcher reads the same bytes at the same places.
        source = (ROOT / "tools" / "launcher" / "Launcher.cs").read_text(encoding="utf-8")
        for expected in ('public const int Size = 84;', 'GetBytes("LUVBOXAR")', 'GetBytes("RAXOBVUL")',
                         "BitConverter.ToInt64(data, 12)", "BitConverter.ToInt64(data, 20)",
                         "Array.Copy(data, 28, footer.Hash, 0, 32)", "GetString(data, 60, 16)",
                         'const string Marker = ".luvbox-app";', 'const string InnerName = "LuvBox.exe";',
                         '"\\\\.old(\\\\.[0-9]{1,3})?$"'):
            self.assertIn(expected, source)
        self.assertEqual((launcher.MARKER_NAME, launcher.INNER_NAME), (".luvbox-app", "LuvBox.exe"))
        self.assertEqual(updater.replaced_name(Path("C:/apps/LuvBox.exe")).name, "LuvBox.exe.old")

    def test_what_packaging_publishes_is_what_an_installed_version_fetches_and_installs(self):
        # A release directory as package_release writes it, served the way GitHub serves a release.
        folder = Path(tempfile.mkdtemp(prefix="luvbox-contract-"))
        self.addCleanup(shutil.rmtree, folder, True)
        program = make_program("1.9.0")
        (folder / "LuvBox.exe").write_bytes(program)
        (folder / "LuvBox-1.9.0-source-licenses.zip").write_bytes(b"sources")
        (folder / "SHA256-1.9.0.txt").write_text(package_release.format_checksums([
            (hashlib.sha256(program).hexdigest(), "LuvBox.exe"),
            (hashlib.sha256(b"sources").hexdigest(), "LuvBox-1.9.0-source-licenses.zip")]),
            encoding="utf-8", newline="\n")
        server = ReleaseServer()
        self.addCleanup(server.close)
        server.description = {"tag_name": "v1.9.0", "draft": False, "prerelease": False, "body": "- 변경",
                              "assets": [{"name": path.name, "size": path.stat().st_size, "state": "uploaded"}
                                         for path in sorted(folder.iterdir())]}
        server.files = {f"/download/v1.9.0/{path.name}": path.read_bytes() for path in folder.iterdir()}
        installed = folder / "installed" / "LuvBox.exe"
        installed.parent.mkdir()
        installed.write_bytes(make_program("1.8.0"))
        found = updater.check("1.8.0", server.source)
        self.assertEqual((found["newer"], found["installable"], found["size"]), (True, True, len(program)))
        path, digest = updater.download(found["version"], folder / "updates", server.source, size=found["size"])
        updater.install(path, installed, digest)
        self.assertEqual(installed.read_bytes(), program)
        self.assertEqual([name for name, _headers in server.requests],
                         ["/latest", "/download/v1.9.0/SHA256-1.9.0.txt", "/download/v1.9.0/LuvBox.exe"])

    def test_guides_tell_people_and_maintainers_the_same_names(self):
        from luvbox import __version__

        development = (ROOT / "docs" / "development.md").read_text(encoding="utf-8")
        for name in updater.release_assets(__version__):
            self.assertIn(f"`release/{name}`", development)
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("`LuvBox.exe` 하나", readme)
        self.assertIn("`SHA256-<버전>.txt`", readme)
        self.assertIn("`LuvBox-<버전>-source-licenses.zip`", readme)
        build = (ROOT / "build.ps1").read_text(encoding="utf-8-sig")
        self.assertIn("release\\LuvBox.exe", build)
        self.assertIn("release\\SHA256-$version.txt", build)
        self.assertIn("release\\LuvBox-$version-source-licenses.zip", build)


class SplashAssetTests(unittest.TestCase):
    def test_wording_and_layout_come_from_one_file_with_every_line_the_launcher_asks_for(self):
        settings = draw_splash.read_settings()
        self.assertEqual(settings["status.first"], ["처음 실행 준비 중 · 한 번만 걸려요"])
        self.assertEqual(settings["status.open"], ["보관함을 여는 중"])
        for key in ("status.update", "status.wait", "wordmark", "tagline"):
            self.assertEqual(len(settings[key]), 1, key)
            self.assertTrue(settings[key][0])
        for key in ("tip.unpack", "tip.open"):
            self.assertGreaterEqual(len(settings[key]), 3, key)
            self.assertEqual(len(set(settings[key])), len(settings[key]), key)
            for line in settings[key]:
                # Short lines: they must fit the card at the tip size.
                self.assertLessEqual(len(line), 22, line)
        source = (ROOT / "tools" / "launcher" / "Launcher.cs").read_text(encoding="utf-8")
        asked = set(re.findall(r'settings\.(?:Text|Lines|Number|Numbers)\("([^"]+)"\)', source))
        self.assertTrue(asked)
        self.assertEqual(asked - set(settings), set())

    def test_layers_are_twice_the_layout_size_with_transparent_corners(self):
        settings = draw_splash.read_settings()
        size = (draw_splash.numbers(settings, "width")[0] * 2, draw_splash.numbers(settings, "height")[0] * 2)
        left, top, width, height, _radius = draw_splash.numbers(settings, "card")
        for name in ("card.png", "glow.png", "mark.png"):
            with Image.open(ROOT / "assets" / "splash" / name) as image:
                self.assertEqual((image.mode, image.size), ("RGBA", size), name)
                self.assertEqual(image.getpixel((0, 0))[3], 0, name)
                inside = image.getpixel(((left + width // 2) * 2, (top + height // 2) * 2))[3]
                corner = image.getpixel(((left + 2) * 2, (top + 2) * 2))[3]
                if name == "card.png":
                    # Opaque in the middle, see-through in the rounded corner.
                    self.assertEqual(inside, 255)
                    self.assertLess(corner, 255)
                else:
                    self.assertEqual(corner, 0, name)

    def test_the_launcher_embeds_the_files_it_reads(self):
        from tools import build_launcher

        names = {name for _source, name in build_launcher.LAUNCHER_RESOURCES}
        source = (ROOT / "tools" / "launcher" / "Launcher.cs").read_text(encoding="utf-8")
        self.assertEqual(set(re.findall(r'"(LuvBox\.[a-z.]+\.(?:png|ini|ttf))"', source)), names)
        for path, _name in build_launcher.LAUNCHER_RESOURCES:
            self.assertTrue((ROOT / path).is_file(), path)


if __name__ == "__main__":
    unittest.main()

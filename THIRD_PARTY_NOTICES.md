# Third-party components

LuvBox (러브박스, formerly XlikeSaver) is provided under GNU GPL version 2. The license text is in LICENSE. The release ZIP includes corresponding application source, build scripts and the gallery-dl Python sources under source/, plus dependency metadata and license texts under licenses/.

- gallery-dl 1.32.15 by Mike Fährmann: GPL-2.0-only. https://codeberg.org/mikf/gallery-dl
- Requests and its HTTP dependencies retain their own licenses in the included package metadata. https://github.com/psf/requests
- Python 3.12 and Tcl/Tk retain their upstream licenses. https://www.python.org/ and https://www.tcl.tk/
- Pillow 12.3.0: HPND and upstream component licenses, retained in the release's dependency notices. https://python-pillow.github.io/
- FFmpeg 7.1: LGPL-2.1-or-later for this native-code-only build. The separate local command-line executable has no GPL codecs, external codec libraries, or network protocols. Its matching complete FFmpeg source archive, build recipe, configuration, and license are included in the release ZIP. https://ffmpeg.org/
- Pretendard 1.3.9 by Kil Hyung-jin and upstream contributors: SIL Open Font License 1.1. The unmodified Regular/Bold TrueType fonts and copyright/license text are bundled under assets/fonts/Pretendard; the ZIP also includes licenses/Pretendard-OFL.txt. https://github.com/orioncactus/pretendard
- PyInstaller 6.22.3 includes a bootloader exception allowing application redistribution; its own notices remain applicable. https://pyinstaller.org/en/stable/license.html

The application folder bundles Python/Tk, Pillow, gallery-dl, the minimal local FFmpeg executable and the complete local ExifTool runtime under _internal. The executable and _internal directory must be distributed and kept together; no per-launch runtime extraction is needed. Account authentication, downloaded media, settings and local records are not included in the release. Authentication uses a user-supplied X auth_token or Netscape cookies.txt. No browser runtime or separate FFmpeg installation is required. The optional imageio-ffmpeg development fallback and its Gyan binary are excluded from the executable.

ExifTool 13.59 by Phil Harvey is distributed under the same terms as Perl (Artistic License or GNU GPL). Its complete source/modules, documentation and notices are included in tools/exiftool/runtime/exiftool_files inside the executable and source/tools/exiftool/runtime in the release ZIP. See https://exiftool.org/ and the included README/LICENSE. The official Windows ZIP comes from Oliver Betz, whose launcher/packaging content is CC0: https://oliverbetz.de/pages/Artikel/ExifTool-for-Windows . Portable Strawberry Perl notices are included in exiftool_files/Licenses_Strawberry_Perl.zip. setup.ps1 verifies the pinned package SHA256 and version. The app never downloads tools at runtime.

Videos use the highest bitrate of directly provided MP4 variants. New X animated GIF media is downloaded as MP4 and converted locally to GIF. Existing MP4 files are preserved. ExifTool writes XMP/QuickTime source URLs locally without re-encoding media or requesting X; GIF conversion is a separate local step. Local records use standard-library SQLite.

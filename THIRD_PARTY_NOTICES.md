# Third-party components

XlikeSaver 1.0.1 is provided under GNU GPL version 2. The license text is in LICENSE. The release ZIP includes corresponding application source, build scripts and the gallery-dl Python sources under source/, plus dependency metadata and license texts under licenses/.

- gallery-dl 1.32.15 by Mike Fährmann: GPL-2.0-only. https://codeberg.org/mikf/gallery-dl
- Requests and its HTTP dependencies retain their own licenses in the included package metadata. https://github.com/psf/requests
- Python 3.12 and Tcl/Tk retain their upstream licenses. https://www.python.org/ and https://www.tcl.tk/
- PyInstaller 6.22.3 includes a bootloader exception allowing application redistribution; its own notices remain applicable. https://pyinstaller.org/en/stable/license.html

The single executable bundles Python/Tk, gallery-dl and the complete local ExifTool runtime. It extracts required files to a temporary directory when running. Account authentication, downloaded media, settings and local records are not included in the release. Authentication uses a user-supplied X auth_token or Netscape cookies.txt. No Playwright/browser runtime, yt-dlp, FFmpeg or media transcoder is required.

ExifTool 13.59 by Phil Harvey is distributed under the same terms as Perl (Artistic License or GNU GPL). Its complete source/modules, documentation and notices are included in tools/exiftool/runtime/exiftool_files inside the executable and source/tools/exiftool/runtime in the release ZIP. See https://exiftool.org/ and the included README/LICENSE. The official Windows ZIP comes from Oliver Betz, whose launcher/packaging content is CC0: https://oliverbetz.de/pages/Artikel/ExifTool-for-Windows . Portable Strawberry Perl notices are included in exiftool_files/Licenses_Strawberry_Perl.zip. setup.ps1 verifies the pinned package SHA256 and version. The app never downloads tools at runtime.

Videos use the highest bitrate of directly provided MP4 variants. X animated GIF media is kept as the provided MP4. ExifTool writes XMP/QuickTime source URLs locally without re-encoding media or requesting X. Local records use standard-library SQLite.

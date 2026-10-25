# Qt redistribution source inventory

This inventory covers the Windows Qt Quick runtime bundled with LuvBox 1.1.8. It records upstream material and observed binary build information; it does not change LuvBox's GPL version 2 license or grant a different license to any dependency.

## Preparation and ZIP locations

Run `.venv\Scripts\python.exe tools\qt\prepare_sources.py` from the project root. `build.ps1` also runs this source-preparation step. Missing archives are downloaded from the official URLs pinned in `sources.json`; existing archives are reused only after their SHA256 matches. FFmpeg and zlib are additionally matched to the SHA1 values in Qt's tagged Windows provisioning recipes. The acquisition limit is 200 MiB total and 64 MiB per archive.

- Generated upstream source archives: `tools/qt/sources/`, included in the release ZIP as `source/tools/qt/sources/`.
- Generated original license, copyright, notice, `LICENSES/`, `REUSE.toml` and `qt_attribution.json` files: `tools/qt/notices/`, included as `licenses/Qt-upstream/<component>/<original-relative-path>`.
- Tracked acquisition script, pinned manifest, this inventory and the application build hooks: included under `source/` in the ZIP.

Generated material is excluded from Git. Packaging calls `release_files()` from `prepare_sources.py`; it verifies each archive and checks every copied notice byte for byte against that archive. It includes only the explicit archive and extracted-notice allowlist, without scanning workspace or user data. A missing archive or changed notice stops packaging before replacing the ZIP. Full archives retain all source headers and build files, including notices whose original filenames do not match the excerpt collector. Source-tree notices can also cover upstream examples or platforms that the Windows application does not use; this preservation is not a claim that those components are loaded by LuvBox.

## Runtime to source mapping

All Qt and PySide runtime files come from the pinned PySide6/PySide6_Essentials/PySide6_Addons/shiboken6 **6.10.3** Windows wheels. The installed wheel tag is `cp39-abi3-win_amd64`; PySide's `_config.py` records build date `2026-03-31T06:42:26+00:00`. Static reading of `Qt6Core.dll` reports a shared, release, MSVC 2022, x86_64 Qt 6.10.3 build. These observations require no application launch.

| Collected runtime family | Included source archive |
| --- | --- |
| `Qt6Core`, `Qt6Gui`, `Qt6Network`, `Qt6OpenGL`, `Qt6Widgets`; Windows platform, desktop styles, TLS/network, core image format and SVG icon integration dependencies | `qtbase-everywhere-src-6.10.3.tar.xz`; SVG plugin source is in qtsvg below |
| `Qt6Qml`, `Qt6QmlCore`, `Qt6QmlMeta`, `Qt6QmlModels`, `Qt6QmlWorkerScript`, `Qt6LabsFolderListModel`; `Qt6Quick`, layouts, templates, Basic/Fusion controls and their implementation DLLs; Quick Dialogs and QML plugins | `qtdeclarative-everywhere-src-6.10.3.tar.xz` |
| `Qt6Multimedia`, `Qt6MultimediaQuick`, `Qt6MultimediaWidgets`; FFmpeg and Windows media plugins | `qtmultimedia-everywhere-src-6.10.3.tar.xz` |
| `Qt6Svg`, `qsvg`, `qsvgicon` | `qtsvg-everywhere-src-6.10.3.tar.xz` |
| ICNS/TGA/TIFF/WBMP/WebP image plugins collected by the standard GUI dependency hook | `qtimageformats-everywhere-src-6.10.3.tar.xz` |
| Qt translation catalogs collected by the standard hooks | `qttranslations-everywhere-src-6.10.3.tar.xz` |
| Shader baking build tools referenced by Qt Declarative's build dependencies; no ShaderTools runtime is added | `qtshadertools-everywhere-src-6.10.3.tar.xz` |
| `PySide6/Qt*.pyd`, `pyside6.abi3.dll`, `pyside6qml.abi3.dll`, `shiboken6` runtime and associated Python support | `pyside-setup-everywhere-src-6.10.3.tar.xz` |
| Playback `avcodec-61`, `avformat-61`, `avutil-59`, `swresample-5`, `swscale-8` DLLs | `FFmpeg-n7.1.3.tar.gz` |
| zlib linked by the MSVC FFmpeg playback build | `zlib-1.3.1.tar.gz` |
| Qt supermodule configure/CI build material; Windows FFmpeg and zlib recipes | `qt5-v6.10.3.tar.gz` |

The dependency hook also collects MSVC runtime DLLs. Their redistribution follows Microsoft's runtime terms, rather than a Qt source license. Python, Pillow, ExifTool/Perl and the separate conversion executable keep their own source/license locations documented in `THIRD_PARTY_NOTICES.md`.

## Playback FFmpeg and build material

The playback DLLs identify **FFmpeg 7.1.3**, independently of LuvBox's separate minimal FFmpeg 7.1 conversion executable. `sources.json` preserves the exact configure string read statically from those DLLs. The playback build enables shared linking, network support and zlib; it is not described by the converter's restricted flags.

The included Qt supermodule archive contains these original files under `qt5-6.10.3/`:

- `coin/provisioning/common/windows/install-ffmpeg.ps1`: source tag `n7.1.3`, source checksum, MSVC build setup and shared-library recipe.
- `coin/provisioning/common/shared/ffmpeg_config_options.txt`: the default FFmpeg configuration, matching the playback DLLs.
- `coin/provisioning/common/windows/zlib.ps1`: zlib 1.3.1 source checksum, architecture build steps and applied source/build-file adaptations.
- `coin/provisioning/common/windows/zlib-helpers.ps1` and `helpers.ps1`: supporting Windows environment functions.

No LuvBox patches are applied to those upstream archives. Qt's provisioning recipe describes its own zlib build adaptations; the original recipe and original zlib source are both retained. The FFmpeg archive's SHA1 is `27051817deec88bed3b9652d49f9127d22268d83`, exactly as pinned by Qt's 6.10.3 recipe. The zlib archive's SHA1 is `f535367b1a11e2f9ac3bec723fb007fbc0d189e5`, also matching that recipe.

## Retained attributions and excluded modules

The original Qt source attribution metadata and license files accompany the complete archives. Relevant source locations include `qtbase/src/3rdparty` for core, font, image and rendering code, `qtdeclarative/src/3rdparty` for the QML engine and Quick dependencies, `qtimageformats/src/3rdparty` for TIFF/WebP, and `qtmultimedia/src/3rdparty` for playback support. Metadata from the actual 6.10.3 archives takes priority over a website whose displayed documentation may later change.

The installed PySide distribution metadata contains the licensing alternatives `LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only`, but its `licenses/` directory contains only the commercial-reference text. That metadata is retained separately in the ZIP; it does not replace the newly included upstream GPL/LGPL texts, source copyright headers or third-party attributions. Each Qt component's own source terms remain applicable. The GPL version 2-only gallery-dl dependency cannot be silently relicensed to GPL version 3, and a blanket LGPLv3 claim does not resolve GPLv2 compatibility.

The custom build hooks exclude unused `qpdf`, `qtvirtualkeyboardplugin`, and all QML debugger plugins at collection time. This prevents the unrelated Qt PDF, Virtual Keyboard and Quick3DUtils dependency chains. A packaging guard rejects those DLLs/bindings or QML debugger entries if they reappear in PyInstaller's actual collection manifest. No Qt WebEngine or browser runtime is needed.

The optional `opengl32sw.dll` Mesa/LLVM software OpenGL runtime is also excluded by the base PySide hook and packaging guard. The application keeps Qt's Windows default Direct3D 11 renderer, including Qt's supported WARP fallback behavior. No graphics environment override or application graphics-API change is made. A user-forced software OpenGL mode requiring Mesa is outside this package's included renderer options.

## Distribution handoff

Qt6Core uses the unversioned ICU API provided by supported Windows installations. Build analysis puts System32 first so unrelated PATH tools cannot substitute a versioned Poppler ICU. Packaging rejects `icuuc.dll` and `icudt78.dll` from the application bundle; Windows system ICU is not redistributed. The build also requires an isolated native-window startup from the exact frozen executable before creating its ZIP.

The sources and original notices above are locally supplied with the ZIP, rather than left as an unspecified future download. For public distribution, retain them with the same binary release, preserve the dependency license choices and user library-replacement/source rights, and publish matching source access alongside the binary download. This inventory does not certify every third-party legal obligation or replace review of the applicable original terms. Revisit it when updating PySide/Qt, changing codec options or adding a new runtime module; a new version requires new source archives and hashes.

Official references:

- [Qt 6.10.3 source modules](https://download.qt.io/archive/qt/6.10/6.10.3/submodules/) and [Qt for Python 6.10.3 source](https://download.qt.io/official_releases/QtForPython/pyside6/PySide6-6.10.3-src/).
- [Qt licensing](https://doc.qt.io/qt-6.10/licensing.html) and [Qt for Python third-party licenses](https://doc.qt.io/qtforpython-6.10/licenses.html).
- [Qt's FFmpeg 7.1.3 attribution](https://doc.qt.io/qt-6.10/qtmultimedia-attribution-ffmpeg.html), [tagged Qt Windows FFmpeg recipe](https://github.com/qt/qt5/blob/v6.10.3/coin/provisioning/common/windows/install-ffmpeg.ps1), and [FFmpeg distribution guidance](https://ffmpeg.org/legal.html).
- [Qt Windows graphics behavior](https://doc.qt.io/qt-6.10/windows-graphics.html).

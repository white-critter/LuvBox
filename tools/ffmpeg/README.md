# LuvBox FFmpeg runtime

This is a purpose-built, unmodified FFmpeg **7.1** command-line executable for
Windows x64, licensed **LGPL-2.1-or-later**. It runs as a separate process; the
application does not link against FFmpeg libraries.

The complete upstream FFmpeg source is `ffmpeg-7.1.tar.xz`. The archive and the
build recipe are included in the LuvBox source ZIP. A Git checkout can
download the exact archive from the official FFmpeg server with `build.py`.
The source SHA-256 is:

```
40973d44970dbc83ef302b0609f2e74982be2d85916dd2ee7472d30678a7abe6
```

`configure-flags.json` is the exact build configuration. It disables network
access, autodetection, assembly, GPL/nonfree components, and external libraries.
Only Windows system DLLs are required at runtime. The source code is not patched.
The native decoders handle H.264, HEVC, VP9, MPEG-4, BMP, and GIF. The GIF encoder,
palette filters, BMP palette/thumbnail output, MP4 input, and synthetic test
source are enabled. This executable is intentionally not a general FFmpeg
distribution; unrelated codecs, streaming protocols, and tools are omitted.

## Check the bundle

From the project directory, using Python 3.12 or newer:

```
python tools/ffmpeg/build.py --check
```

This verifies source, runtime, and recipe hashes against `manifest.json`. If the
upstream source archive is missing, it downloads and verifies that archive first.

## Rebuild

1. Download the portable [w64devkit 2.10.0 x64 toolchain](https://github.com/skeeto/w64devkit/releases/tag/v2.10.0).
2. Verify `w64devkit-x64-2.10.0.7z.exe` SHA-256:
   `18d0a4c71a166f8401ab6305781bec5882b40b5e06ba9807c61cb5f3b3c6325e`.
3. Extract it with 7-Zip. No installer, administrator permission, or global PATH
   change is necessary. The toolchain contains GCC 16.2.0, GNU Make, and busybox sh.
4. Run:

```
python tools/ffmpeg/build.py --toolchain .test-output/ffmpeg-build-tools/w64devkit --jobs 8
```

The helper configures and compiles in `.test-output/ffmpeg-build`, copies the
finished executable to `runtime/ffmpeg.exe`, records its compiler/configuration,
checks that it only depends on Windows system DLLs, and updates the manifest.
Configure and compile logs remain in the work directory. Paths containing spaces
are supported; compiler tools are never included in the application release.
Different toolchain versions or build paths may produce different binary hashes.

`LICENSE.LGPLv2.1.txt` is the original FFmpeg LGPL license. FFmpeg's full source
archive preserves all its individual copyright and license notices, including
components not compiled into this executable. MinGW runtime notices and the GCC
Runtime Library Exception are also included. The compiler and its tools are not
redistributed in the application.

Upstream references:

- [FFmpeg source release](https://ffmpeg.org/releases/ffmpeg-7.1.tar.xz)
- [FFmpeg license details](https://ffmpeg.org/legal.html)
- [w64devkit toolchain recipe](https://github.com/skeeto/w64devkit/tree/v2.10.0)
- [GCC Runtime Library Exception](https://www.gnu.org/licenses/gcc-exception-3.1.html)

For public redistribution, provide the source ZIP alongside the application
download, including this archive, build recipe, manifest, and license notices.

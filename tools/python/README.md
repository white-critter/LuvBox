# Python 3.12.16 upstream materials

These unmodified files preserve the upstream provenance and license inventory
used for the LuvBox 1.3.5 Windows x64 build. They come from Astral's
[python-build-standalone 20261026 release](https://github.com/astral-sh/python-build-standalone/releases/tag/20261026).
This inventory describes the full upstream distribution, including optional
components. The actual shipped files are determined by PyInstaller's COLLECT
manifest and LuvBox's sealed build-input record, rather than this inventory.

Verified archives:

| Archive | SHA-256 |
| --- | --- |
| [cpython-3.12.16+20261026-x86_64-pc-windows-msvc-install_only.tar.gz](https://github.com/astral-sh/python-build-standalone/releases/download/20261026/cpython-3.12.16%2B20261026-x86_64-pc-windows-msvc-install_only.tar.gz) | `590dfcbe3a34c07301e6332e13e2188b98655cd072b08afa45a7998dc4c22bc5` |
| [cpython-3.12.16+20261026-x86_64-pc-windows-msvc-pgo-full.tar.zst](https://github.com/astral-sh/python-build-standalone/releases/download/20261026/cpython-3.12.16%2B20261026-x86_64-pc-windows-msvc-pgo-full.tar.zst) | `37b4eb2818fee605fefa75ed156c4f2e1aaeaebaf662f9b30177c842ee16c48f` |

Both downloaded archives matched the release's
[SHA256SUMS](https://github.com/astral-sh/python-build-standalone/releases/download/20261026/SHA256SUMS).
`PYTHON.json` and all 20 files under `licenses/` were copied byte for byte from
`python/PYTHON.json` and `python/licenses/` in the full archive.

The installed runtime from `install_only` was checked against the full archive:
`python.exe`, `python312.dll`, and the DLLs directory's `libcrypto-3-x64.dll`,
`libssl-3-x64.dll`, `libffi-8.dll`, `sqlite3.dll`, `zlib1.dll`, and `pyexpat.pyd`
all had matching SHA-256 hashes. The matching binaries establish which upstream
materials apply to this build; no Python runtime is stored in this directory.

Release packaging includes these explicitly allowed source files and copies
the original licenses and this inventory to `licenses/Python-upstream/`.
The build interpreter's own `sys.base_prefix/LICENSE.txt` is still included as
`licenses/Python-LICENSE.txt`, including when building with a different Python.
Review this inventory when changing the build interpreter or its native
dependencies. The runtime under `.python/` and local virtual environments are
never recursively collected as release sources.

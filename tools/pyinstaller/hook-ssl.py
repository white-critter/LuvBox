"""Collect the build interpreter's SSL libraries and optional trust policy."""

from pathlib import Path
import ssl
import sys


# Dependency discovery can otherwise pick same-named OpenSSL DLLs from PATH
# tools such as Poppler. Keep _ssl.pyd paired with its interpreter's libraries.
binaries = []
if sys.platform == "win32":
    dlls = Path(sys.base_prefix) / "DLLs"
    for pattern in ("libssl*.dll", "libcrypto*.dll"):
        libraries = sorted(path for path in dlls.glob(pattern) if path.is_file())
        if not libraries:
            raise RuntimeError("The build interpreter's OpenSSL DLLs are missing from its DLLs directory")
        binaries.extend((str(path), ".") for path in libraries)


# The managed Python runtime loads this sibling file in SSLContext.__new__.
# PyInstaller freezes ssl.py without collecting its extra data by default.
# Keep the same certificate rejection policy in the EXE; ordinary Python
# installations without this runtime resource need no additional data.
trust_data = Path(ssl.__file__).with_name("codex-rejected-cas.pem")
datas = [(str(trust_data), ".")] if trust_data.is_file() else []

"""Preserve optional trust data required by the build interpreter's ssl module."""

from pathlib import Path
import ssl


# The managed Python runtime loads this sibling file in SSLContext.__new__.
# PyInstaller freezes ssl.py without collecting its extra data by default.
# Keep the same certificate rejection policy in the EXE; ordinary Python
# installations without this runtime resource need no additional data.
trust_data = Path(ssl.__file__).with_name("codex-rejected-cas.pem")
datas = [(str(trust_data), ".")] if trust_data.is_file() else []

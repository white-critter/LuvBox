"""Sign with the release key that lives in this Windows account's key store.

The private part stays in the key store (tools/release_key.ps1 never writes it
anywhere); this module only asks for signatures and for the public part.
Packaging refuses to publish a release that installed versions would not
accept: the key here must be one of luvbox.release_signature.RELEASE_KEYS.

    python tools/release_key.py --check     # the key exists, is trusted, and signs
"""

import base64
import binascii
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from luvbox.release_signature import Key, RELEASE_KEYS, confirms  # noqa: E402

KEY_NAME = "LuvBoxReleaseSigning"
TEST_PREFIX = "LuvBoxReleaseSigningTest-"
SCRIPT = ROOT / "tools" / "release_key.ps1"


class KeyUnavailable(RuntimeError):
    """The release cannot be signed here; the text says what to do."""


def _run(*arguments, name: str) -> str:
    if os.name != "nt":
        raise KeyUnavailable("릴리스 서명은 Windows 키 저장소가 있는 PC에서만 할 수 있습니다.")
    command = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
               "-File", str(SCRIPT), *arguments, "-Name", name]
    try:
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                stdin=subprocess.DEVNULL, timeout=120,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired):
        raise KeyUnavailable("PowerShell로 서명 키를 열지 못했습니다.") from None
    if result.returncode != 0:
        raise KeyUnavailable(
            f"이 Windows 계정에서 서명 키({name})를 쓸 수 없습니다. "
            "키가 없다면 `powershell -File tools\\release_key.ps1 -Create`로 만들 수 있지만, 새 키는 "
            "설치된 버전이 모르는 키이므로 luvbox/release_signature.py에 먼저 넣어 한 번 배포해야 합니다.")
    return result.stdout.strip()


def public_key(name: str = KEY_NAME) -> Key:
    try:
        described = json.loads(_run("-Public", name=name))
        return Key(int(described["modulus"], 16), int(described["exponent"], 16))
    except (ValueError, KeyError, TypeError):
        raise KeyUnavailable("서명 키의 공개 부분을 읽지 못했습니다.") from None


def sign(message: bytes, name: str = KEY_NAME) -> tuple[Key, bytes]:
    """This account's key and its signature of `message`, checked before it is returned."""
    with tempfile.TemporaryDirectory(prefix="luvbox-sign-") as folder:
        path = Path(folder) / "message.bin"
        path.write_bytes(message)
        text = _run("-Sign", str(path), name=name)
    try:
        signature = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        raise KeyUnavailable("서명 결과를 읽지 못했습니다.") from None
    key = public_key(name)
    if not confirms(key, message, signature):
        raise KeyUnavailable("만든 서명이 같은 키의 공개 부분으로 확인되지 않습니다.")
    return key, signature


def require_release_key() -> Key:
    """The key in this account is one that installed versions trust, and it signs."""
    key = public_key()
    if key not in RELEASE_KEYS:
        raise KeyUnavailable(
            "이 PC의 서명 키가 앱에 들어 있는 공개 키(luvbox/release_signature.py)와 다릅니다. "
            "이 키로 서명한 릴리스는 설치된 버전이 받지 않습니다.")
    sign(b"LuvBox release key check\n")
    return key


def release_signer():
    """`sign(message) -> (Key, signature)` for packaging, after the checks above."""
    require_release_key()
    return sign


if __name__ == "__main__":
    if sys.argv[1:] != ["--check"]:
        raise SystemExit("Usage: release_key.py --check")
    try:
        found = require_release_key()
    except KeyUnavailable as error:
        raise SystemExit(str(error)) from None
    print(f"Release signing key ready: RSA {found.modulus.bit_length()}, id {found.identifier}.")

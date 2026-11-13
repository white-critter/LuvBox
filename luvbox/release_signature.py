"""Who may publish an update: the signature on a release's checksum file.

The checksum file comes from the same release as the program, so matching it
only proves a clean transfer. Since 1.9.2 the file also carries a signature
made with a key that exists only in the maintainer's Windows key store
(tools/release_key.ps1); an installed version takes an update only when one
of the public keys below confirms it. Someone who can change a release on
GitHub but does not hold that key cannot make an installed LuvBox accept it.

The check is plain integer arithmetic (RSA, PKCS#1 v1.5, SHA-256), so it
needs no extra package. It covers "LuvBox release <version>\\n" followed by the
checksum rows exactly as the updater reads them ("<sha256>  <name>\\n" each),
and it travels as one more line of the checksum file:

    # luvbox-signature v1 <key id> <base64>

Versions 1.8.0 to 1.9.1 and `sha256sum -c` skip that line as a comment.
"""

import base64
import binascii
from dataclasses import dataclass
import hashlib
import re


_LINE = re.compile(r"# luvbox-signature v1 ([0-9a-f]{16}) ([A-Za-z0-9+/]{340,1400}={0,2})\Z")
_VERSION = re.compile(r"[0-9]{1,5}\.[0-9]{1,5}\.[0-9]{1,5}\Z")
# The fixed bytes that name SHA-256 inside a PKCS#1 v1.5 signature (RFC 8017, 9.2).
_SHA256_INFO = bytes.fromhex("3031300d060960864801650304020105000420")
_SMALLEST_KEY = 2048


@dataclass(frozen=True)
class Key:
    """The public part of a signing key."""

    modulus: int
    exponent: int = 65537

    @property
    def size(self) -> int:
        return (self.modulus.bit_length() + 7) // 8

    @property
    def identifier(self) -> str:
        return hashlib.sha256(self.modulus.to_bytes(self.size, "big")).hexdigest()[:16]


# The keys whose releases installed versions accept. A new key is added here
# one release before it starts signing, so versions in use already trust it.
RELEASE_KEYS = (
    Key(int(
        "c4e8120aa551270b14aaae50db72b3ab49a3f91bfcf20bed522e280f36a0e69063cc35ac02856695b2088a648b64621b"
        "572f3ed8df943121e2a1a104a7e04b1817613541c5e0ca955e5e8ae61ec685cd545892d2bcb6e824e88fc9ac72c9764f"
        "d2249f10af26d670a3ffc19cc8fcaac8d34eaa7bbc2e28bc52b98a52e6ce50f745736af1094c7f98fa59b6dd9f4e39b3"
        "3d74ba9ef77890ce47a60cb74e9b6529c57e9b721bafc69129b989cc60d1fec79a7702a9ba337ce7644551cd172cf1e8"
        "7d0865977aa8cda3ca04a76d5fee5d4da28c1d24435517f3fa02c414a53f0435f1cbf8beb910ac2d4a0d38d14c6ffa98"
        "9cb8a5ada21f86fa1c19e9c5a9037fb1440a62b9644881f6e949317823a8838116d63910945c77563dc73f486b2724a2"
        "4c833a6e27c1af180c62bc2f3f7da04600d69799d1150e50b8418f4ba1efc80bd399894c217e5b1d0a67f1e3aefdab8d"
        "87c0c38662b92b73059772746623acf1ebf4000a4b2af82ca4eff2a64ceacd6b39dd77aac012b27fdcf806a6bcb560b5"
        , 16)),
)


def signed_message(version: str, rows: dict) -> bytes:
    """What a signature covers: the version and the rows (file name -> SHA-256) as the updater uses them."""
    if not isinstance(version, str) or not _VERSION.match(version):
        raise ValueError("Not a release version")
    lines = [f"LuvBox release {version}\n"]
    lines.extend(f"{digest}  {name}\n" for name, digest in rows.items())
    return "".join(lines).encode("utf-8")


def signature_line(key: Key, signature: bytes) -> str:
    line = f"# luvbox-signature v1 {key.identifier} {base64.b64encode(signature).decode('ascii')}"
    if not _LINE.match(line):
        raise ValueError("Not a signature this format can carry")
    return line + "\n"


def _expected_block(message: bytes, size: int) -> bytes:
    digest = hashlib.sha256(message).digest()
    return b"\x00\x01" + b"\xff" * (size - len(_SHA256_INFO) - len(digest) - 3) + b"\x00" + _SHA256_INFO + digest


def confirms(key: Key, message: bytes, signature: bytes) -> bool:
    """Whether `signature` is this key's signature of `message`.

    The whole expected block is rebuilt and compared; nothing inside the
    signed block is parsed, which is where such checks have gone wrong.
    """
    if key.modulus.bit_length() < _SMALLEST_KEY or key.exponent < 3 or key.exponent % 2 == 0:
        return False
    if len(signature) != key.size:
        return False
    value = int.from_bytes(signature, "big")
    if value >= key.modulus:
        return False
    return pow(value, key.exponent, key.modulus).to_bytes(key.size, "big") == _expected_block(message, key.size)


def verify(version: str, text: str, rows: dict, keys=RELEASE_KEYS) -> bool:
    """True when the checksum file's text carries a trusted key's signature over these rows."""
    try:
        message = signed_message(version, rows)
    except ValueError:
        return False
    trusted = {key.identifier: key for key in keys}
    for line in text.splitlines():
        match = _LINE.match(line.strip())
        if match is None or match[1] not in trusted:
            continue
        try:
            signature = base64.b64decode(match[2], validate=True)
        except (binascii.Error, ValueError):
            continue
        if confirms(trusted[match[1]], message, signature):
            return True
    return False

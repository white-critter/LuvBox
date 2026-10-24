import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import zipfile

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "assets" / "browser-extension" / "chromium"
OUTPUT = ROOT / "release" / "browser-extension"
SOURCE_FILES = (
    "manifest.json",
    "background.js",
    "profile.js",
    "connect.js",
    "connect.html",
    "connect.css",
    "extension-page.js",
)
ICON_SIZES = (16, 32, 48, 128)


def _plain_path(path):
    relative = path.relative_to(ROOT)
    current = ROOT
    for component in (None, *relative.parts):
        if component is not None:
            current = current / component
        details = current.lstat()
        if (stat.S_ISLNK(details.st_mode) or getattr(details, "st_file_attributes", 0) & 0x400
                or getattr(current, "is_junction", lambda: False)()
                or (stat.S_ISREG(details.st_mode) and details.st_nlink > 1)):
            raise ValueError("Extension package paths must not contain links")
        if not (stat.S_ISDIR(details.st_mode) or stat.S_ISREG(details.st_mode)):
            raise ValueError("Extension package paths must be regular files or directories")
    return details


def _read_input(path):
    if not stat.S_ISREG(_plain_path(path).st_mode):
        raise ValueError("Extension inputs must be regular files")
    return path.read_bytes()


def _manifest_version(manifest):
    version = manifest.get("version")
    if (manifest.get("manifest_version") != 3 or not isinstance(version, str)
            or re.fullmatch(r"(?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*)){0,3}", version) is None):
        raise ValueError("A numeric Manifest V3 extension version is required")
    components = [int(component) for component in version.split(".")]
    if any(component > 65535 for component in components) or not any(components):
        raise ValueError("Extension version components must be 0..65535 and not all zero")
    return version


def _icon_payloads():
    source = _read_input(ROOT / "assets" / "icons" / "luvbox.png")
    with Image.open(BytesIO(source)) as image:
        if image.width != image.height or image.width < 128:
            raise ValueError("The existing square LuvBox icon must be at least 128 pixels")
        artwork = image.convert("RGBA")
        bounds = artwork.getchannel("A").getbbox()
        if bounds is None:
            raise ValueError("The existing LuvBox icon must contain visible artwork")
        artwork = artwork.crop(bounds)
        payloads = {}
        for size in ICON_SIZES:
            artwork_size = size * 3 // 4
            resized = artwork.resize((artwork_size, artwork_size), Image.Resampling.LANCZOS)
            icon = Image.new("RGBA", (size, size), (0, 0, 0, 0))
            inset = (size - artwork_size) // 2
            icon.paste(resized, (inset, inset))
            buffer = BytesIO()
            icon.save(buffer, format="PNG", optimize=False, compress_level=9)
            payloads[f"icons/icon-{size}.png"] = buffer.getvalue()
        return payloads


def _archive_payload(payloads):
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as archive:
        for name in sorted(payloads):
            entry = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            entry.create_system = 3
            entry.external_attr = (stat.S_IFREG | 0o644) << 16
            entry.compress_type = zipfile.ZIP_STORED
            archive.writestr(entry, payloads[name])
    return buffer.getvalue()


def _write_output(destination, payload):
    descriptor, name = tempfile.mkstemp(prefix=".extension-", dir=OUTPUT)
    staged = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
        staged.replace(destination)
    finally:
        staged.unlink(missing_ok=True)


def package():
    payloads = {name: _read_input(SOURCE / name) for name in SOURCE_FILES}
    manifest = json.loads(payloads["manifest.json"])
    if not isinstance(manifest, dict):
        raise ValueError("The extension manifest must be an object")
    version = _manifest_version(manifest)
    if (manifest.get("permissions") != ["cookies"]
            or manifest.get("host_permissions") != ["https://x.com/*", "http://127.0.0.1/*"]):
        raise ValueError("Permission changes require updating the store submission disclosures")
    icons = _icon_payloads()
    manifest["icons"] = {str(size): f"icons/icon-{size}.png" for size in ICON_SIZES}
    payloads["manifest.json"] = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    payloads.update(icons)
    archive = _archive_payload(payloads)
    for directory in (ROOT / "release", OUTPUT):
        try:
            details = _plain_path(directory)
        except FileNotFoundError:
            directory.mkdir()
            details = _plain_path(directory)
        if not stat.S_ISDIR(details.st_mode):
            raise ValueError("Extension output must be a directory inside the project")
    archive_name = f"LuvBox-browser-extension-{version}.zip"
    outputs = {archive_name: archive, "store-icon-128.png": icons["icons/icon-128.png"]}
    for name, payload in outputs.items():
        _write_output(OUTPUT / name, payload)
    checksums = "".join(f"{hashlib.sha256(payload).hexdigest()}  {name}\n"
                        for name, payload in sorted(outputs.items()))
    _write_output(OUTPUT / "SHA256SUMS.txt", checksums.encode("ascii"))
    print(OUTPUT / archive_name)
    print("Local submission files only; no browser, account, upload or publishing action was performed.")


if __name__ == "__main__":
    package()

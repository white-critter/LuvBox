"""A local stand-in for GitHub releases, and synthetic LuvBox programs to serve from it.

Used by the updater tests. Everything is on 127.0.0.1 and in temporary
folders; no test here reaches the network.
"""

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import threading
import zipfile

from luvbox import launcher, release_signature, updater


# A throwaway key for the stand-in releases served here. No installed version
# trusts it: only the Source that ReleaseServer hands out names it.
_TEST_MODULUS = int(
    "bd0e2d301ceab0ff52bd6fc0191a415405d7049c2d2a3225a97f626754a8698f5563148689e751dc341bcfe7fa2ba539"
    "6141d86f85e6bac4716ebbc2f754c4bbf337348272b98ff26e56a3f7b3903962b7ba9357d68aeb8a8fba58eefbe4e18d"
    "3199ecedf71c8c74c6fdb4e83d1c5af94f7efead0e00aa70cae1b640826496a0de35a1341b79e39c0730cc1c3a67e6c7"
    "fa0a2624bed834d50d29b0a31b2eec533286ac9d0846b43c31aa4e5894121df225ae02ec127451ba5a723b4226b8d6f2"
    "9e5897a012336a9c232a6dc343ec5a64d4862a61b2a75970c78825805390a9ebddcd3c2ff7157205ed007a03f4b8722f"
    "8a8f12c9f9e54ceeacb0615835fe7201",
    16)
_TEST_PRIVATE = int(
    "315711d40cb7f399af2e618bf101392b849aa949594039f63a565488433ff0ccc7ac241f38be764ed20ab0280ad2793c"
    "90756ed72c4b1b5349876cd965536a44e324f53f5651b78fbddc3eced6be3d3bd6578b3cd1715027c97b08dad642d5da"
    "d3a7287eb28f70d8c9564d708f02677bc9938b43fef38d88026b05f5b012307d382ba5f25d991df59806df0e3e87f5a5"
    "ac2fe493ae897673199baa4bb3a69b53d7f9d8084a3cc837fdd61180e123eb71622f985788472ac0a427afd4653fd2be"
    "7b3f2bdc72305603c17481afd9741f701bb4d2a3abd2e62236261a0f741f0a0a9410af10c72a627b0b1d6ea0ad0b5c8b"
    "c419c229c90461705e1a379c8cf5145",
    16)
TEST_KEY = release_signature.Key(_TEST_MODULUS)


def sign_for_tests(message: bytes):
    """Sign as the release key does (RSA, PKCS#1 v1.5, SHA-256), with the throwaway key."""
    block = release_signature._expected_block(message, TEST_KEY.size)
    return TEST_KEY, pow(int.from_bytes(block, "big"), _TEST_PRIVATE, _TEST_MODULUS).to_bytes(TEST_KEY.size, "big")


def make_program(version: str, content: bytes = b"application") -> bytes:
    """Bytes shaped like a release's LuvBox.exe: a program, an archive and a valid footer."""
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as output:
        output.writestr("LuvBox.exe", content)
    payload = archive.getvalue()
    # Larger than the smallest size the updater accepts as a real program.
    program = b"MZ" + b"stand-in launcher".ljust(1024 * 1024 + 8190, b"\0")
    return program + payload + launcher.make_footer(len(program), len(payload), hashlib.sha256(payload).digest(),
                                                    version)


class ReleaseServer:
    """Serves one "latest release" description and that release's files."""

    def __init__(self):
        self.status = 200
        self.description = {}
        self.raw_description = None
        self.files = {}
        self.requests = []
        self.cut_short = set()      # paths that announce more bytes than they send
        self.slow = threading.Event()
        self.slow.set()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *_arguments):
                pass

            def do_GET(self):
                owner.requests.append((self.path, {name.lower(): value for name, value in self.headers.items()}))
                if self.path == "/latest":
                    body = (owner.raw_description if owner.raw_description is not None
                            else json.dumps(owner.description).encode("utf-8"))
                    self.send_response(owner.status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if self.path == "/download/elsewhere":
                    self.send_response(302)
                    self.send_header("Location", "http://192.0.2.1/LuvBox.exe")
                    self.end_headers()
                    return
                data = owner.files.get(self.path)
                if data is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                if self.path in owner.cut_short:
                    data = data[:len(data) // 2]
                for start in range(0, len(data), 64 * 1024):
                    if self.path.endswith(".exe"):
                        owner.slow.wait(20)   # a test can hold the large transfer mid-way
                    try:
                        self.wfile.write(data[start:start + 64 * 1024])
                    except OSError:
                        return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.source = updater.Source(latest=f"http://127.0.0.1:{self.port}/latest",
                                     downloads=f"http://127.0.0.1:{self.port}/download", secure=False,
                                     keys=(TEST_KEY,))

    def close(self):
        self.slow.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)

    def publish(self, version, program=None, *, notes="### 주요 변경 사항\n\n- **첫째**: 고친 것\n- 둘째\n",
                checksum=None, with_program=True, with_checksum=True, signed=True):
        """Make `version` the latest release, with the files a real release carries."""
        program = make_program(version) if program is None else program
        digest = hashlib.sha256(program).hexdigest() if checksum is None else checksum
        program_name, checksum_name, source_name = updater.release_assets(version)
        self.files = {}
        assets = [{"name": source_name, "size": 10, "state": "uploaded"}]
        if with_program:
            self.files[f"/download/v{version}/{program_name}"] = program
            assets.append({"name": program_name, "size": len(program), "state": "uploaded"})
        if with_checksum:
            text = updater.format_checksums([(digest, program_name), ("0" * 64, source_name)])
            if signed:
                text = updater.sign_checksums(version, text, sign_for_tests)
            self.files[f"/download/v{version}/{checksum_name}"] = text.encode("ascii")
            assets.append({"name": checksum_name, "size": len(text), "state": "uploaded"})
        self.status = 200
        self.raw_description = None
        self.description = {"tag_name": "v" + version, "name": "LuvBox v" + version, "draft": False,
                            "prerelease": False, "body": notes, "assets": assets}
        return program

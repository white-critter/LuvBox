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

from luvbox import launcher, updater


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
                                     downloads=f"http://127.0.0.1:{self.port}/download", secure=False)

    def close(self):
        self.slow.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)

    def publish(self, version, program=None, *, notes="### 주요 변경 사항\n\n- **첫째**: 고친 것\n- 둘째\n",
                checksum=None, with_program=True, with_checksum=True):
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
            self.files[f"/download/v{version}/{checksum_name}"] = text.encode("ascii")
            assets.append({"name": checksum_name, "size": len(text), "state": "uploaded"})
        self.status = 200
        self.raw_description = None
        self.description = {"tag_name": "v" + version, "name": "LuvBox v" + version, "draft": False,
                            "prerelease": False, "body": notes, "assets": assets}
        return program

"""Entry point shared by Python and the packaged Windows application."""

import json
import os
import sys
from pathlib import Path


def _check_download_start() -> dict:
    """Reach the real X extractor's first HTTP request, entirely offline.

    Finding an extractor or stopping before authentication does not initialize
    its browser adapter and dynamically imported transaction helper. Frozen
    builds must exercise those too, without reading a real saved connection.
    """
    from luvbox import __version__

    report = {"ok": False, "version": __version__, "frozen": bool(getattr(sys, "frozen", False)),
              "isolated": True, "authentication_or_network": False,
              "managed_cookies": False, "prepared_requests": 0}

    class RequestPrepared(BaseException):
        pass

    class NetworkBlocked(BaseException):
        pass

    try:
        import socket
        import tempfile
        from urllib.parse import urlsplit
        from requests.sessions import Session
        from gallery_dl import extractor
        from gallery_dl.extractor.common import Extractor
        from gallery_dl.extractor.twitter import TwitterAPI
        from luvbox import login, metadata
        from luvbox.core import JobOptions, run_worker

        account = "luvboxsmoke"
        auth = "offline-luvbox-auth-placeholder"
        csrf = "offline-luvbox-csrf-placeholder"
        cookies_loaded = False

        def fake_cookies(*, account=None):
            nonlocal cookies_loaded
            if account != "luvboxsmoke":
                raise ValueError("Unexpected smoke-test account")
            cookies_loaded = True
            return [{"name": name, "value": value, "domain": ".x.com",
                     "path": "/", "secure": True, "httpOnly": name == "auth_token"}
                    for name, value in (("auth_token", auth), ("ct0", csrf))]

        def prepare_request(_session, request, **_kwargs):
            report["prepared_requests"] += 1
            url = urlsplit(request.url)
            prepared_cookies = dict(part.strip().split("=", 1)
                                    for part in request.headers.get("Cookie", "").split(";")
                                    if "=" in part)
            if (request.method != "GET" or url.scheme != "https" or url.hostname != "x.com"
                    or url.path != "/home" or not cookies_loaded
                    or prepared_cookies.get("auth_token") != auth
                    or prepared_cookies.get("ct0") != csrf):
                raise NetworkBlocked()
            report["managed_cookies"] = True
            # Neither gallery-dl nor the worker may convert this into a normal
            # download error and continue. Their finally blocks still run.
            raise RequestPrepared()

        def block_network(*_args, **_kwargs):
            raise NetworkBlocked()

        initialize_extractor = Extractor.initialize

        def check_initialization(self):
            try:
                return initialize_extractor(self)
            except Exception as error:
                frames = []
                trace = error.__traceback__
                while trace is not None:
                    code = trace.tb_frame.f_code
                    frames.append({"file": Path(code.co_filename).name,
                                   "line": trace.tb_lineno, "function": code.co_name})
                    trace = trace.tb_next
                report["initialization_error"] = {"type": type(error).__name__, "frames": frames}
                raise

        originals = (Session.send, login.load_session_cookies, metadata.available,
                     socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo,
                     TwitterAPI.client_transaction, Extractor.initialize)
        environment = {name: os.environ.get(name) for name in ("LOCALAPPDATA", "APPDATA")}
        with tempfile.TemporaryDirectory(prefix="luvbox-download-smoke-") as folder:
            try:
                os.environ["LOCALAPPDATA"] = folder
                os.environ["APPDATA"] = str(Path(folder) / "roaming")
                Session.send = prepare_request
                login.load_session_cookies = fake_cookies
                metadata.available = lambda: False
                socket.socket.connect = socket.socket.connect_ex = block_network
                socket.getaddrinfo = block_network
                TwitterAPI.client_transaction = None
                Extractor.initialize = check_initialization
                match = extractor.find(f"https://x.com/{account}/likes")
                if match is None or type(match).__name__ != "TwitterLikesExtractor":
                    raise RuntimeError("X likes extractor unavailable")
                report["extractor"] = type(match).__name__
                try:
                    code = run_worker(JobOptions(account, str(Path(folder) / "media")), lambda _event: None)
                    report["worker_exit_code"] = code
                    report["error"] = "The X extractor did not prepare its first request."
                except RequestPrepared:
                    report["ok"] = report["managed_cookies"] and report["prepared_requests"] == 1
                except NetworkBlocked:
                    report["error"] = "An unexpected network request was blocked."
            finally:
                (Session.send, login.load_session_cookies, metadata.available,
                 socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo,
                 TwitterAPI.client_transaction, Extractor.initialize) = originals
                for name, value in environment.items():
                    if value is None:
                        os.environ.pop(name, None)
                    else:
                        os.environ[name] = value
    except Exception as error:
        # Even a packaging failure must not write exception text or credentials.
        report["ok"] = False
        report["error"] = type(error).__name__
    return report



def main() -> int:
    # PyInstaller's windowed interpreter has no standard streams. Libraries
    # may still expect these attributes while running GUI or diagnostic code.
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    if len(sys.argv) > 1 and sys.argv[1] == "--download-smoke":
        if len(sys.argv) != 3:
            return 2
        report = _check_download_start()
        Path(sys.argv[2]).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return 0 if report["ok"] else 1
    if len(sys.argv) > 1 and sys.argv[1] == "--qt-smoke":
        if len(sys.argv) != 3:
            return 2
        from luvbox.qt_smoke import run

        return run(Path(sys.argv[2]))
    if len(sys.argv) > 1 and sys.argv[1] == "--self-test":
        if len(sys.argv) != 3:
            return 2
        from luvbox.selftest import run

        return run(Path(sys.argv[2]), _check_download_start)

    if len(sys.argv) > 1 and sys.argv[1] == "--worker":
        if len(sys.argv) != 5:
            return 2
        request_path, events_path, stop_path = map(Path, sys.argv[2:])
        with events_path.open("a", encoding="utf-8", buffering=1) as events:
            def emit(event: dict) -> None:
                events.write(json.dumps(event, ensure_ascii=False) + "\n")
                events.flush()

            try:
                from luvbox.core import JobOptions, run_worker

                options = JobOptions(**json.loads(request_path.read_text(encoding="utf-8")))
                from luvbox.review import CONTROL_NAME, ReviewChannel
                control_path = request_path.parent / CONTROL_NAME
                review = ReviewChannel(control_path, emit) if control_path.exists() else None
                return run_worker(options, emit, stop_path, review=review)
            except Exception as exc:
                # Do not serialize exception contents: they may contain a URL
                # with credentials supplied by an upstream library.
                emit({"type": "finished", "status": "error",
                      "message": f"작업 중 오류가 발생했습니다 ({type(exc).__name__}). 입력과 작업 로그를 확인해 주세요."})
                return 1

    if len(sys.argv) > 1 and sys.argv[1] == "--run-saved":
        # Unattended run for Windows Task Scheduler; see luvbox/scheduled.py.
        if len(sys.argv) > 3 or (len(sys.argv) == 3 and sys.argv[2] != "--full"):
            return 2
        from luvbox.scheduled import run_saved

        return run_saved(full=len(sys.argv) == 3)

    if len(sys.argv) > 1:
        return 2

    from luvbox.qt_app import launch

    return launch()


if __name__ == "__main__":
    raise SystemExit(main())

"""UI-independent ownership of one isolated download worker and its IPC."""

from contextlib import ExitStack
from collections import deque
from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from threading import Event, Lock, Thread
import time

from .core import JobOptions
from .review import CONTROL_NAME, ReviewControl


_READ_BYTES = 64 * 1024
_MAX_EVENT_BYTES = 256 * 1024
_PROTOCOL_ERROR = "저장 작업의 진행 정보를 읽지 못해 중단했습니다. 앱을 다시 시작한 뒤 다시 시도해 주세요."
_START_ERROR = "저장 작업을 시작하지 못했습니다. 잠시 뒤 다시 시도해 주세요."


class _ProtocolError(ValueError):
    pass


def worker_command(request, events, stop):
    prefix = [sys.executable]
    if not getattr(sys, "frozen", False):
        prefix.append(str(Path(__file__).resolve().parents[1] / "entry.py"))
    return prefix + ["--worker", str(request), str(events), str(stop)]


class DownloadService:
    def __init__(self):
        self._lock = Lock()
        self._events = deque()
        self._stop = Event()
        self._thread = None
        self._process = None
        self._running = False
        self._closed = False
        self._final = False
        self._job_id = 0
        self._review_id = None
        self._review_response = None
        self._review_enabled = False
        self._review_dirty = False
        self._control = ReviewControl()

    @property
    def running(self):
        with self._lock:
            return self._running

    @property
    def job_id(self):
        with self._lock:
            return self._job_id

    def start(self, options):
        if not isinstance(options, JobOptions):
            raise ValueError("저장 설정을 확인해 주세요.")
        with self._lock:
            if self._closed or self._running:
                return False
            self._job_id += 1
            job_id = self._job_id
            self._running = True
            self._final = False
            self._review_id = None
            self._review_response = None
            self._review_enabled = bool(options.confirm_each)
            self._review_dirty = False
            self._stop.clear()
            self._thread = Thread(target=self._run, args=(job_id, options),
                                  name="download-process-owner", daemon=True)
            try:
                self._thread.start()
            except RuntimeError:
                self._thread = None
                self._running = False
                raise ValueError(_START_ERROR) from None
        return True

    def stop(self):
        with self._lock:
            if not self._running or self._final:
                return
            self._stop.set()
            self._review_id = None

    def close(self):
        with self._lock:
            self._closed = True
        self.stop()
        return not self.running

    def drain(self, limit=200):
        limit = max(0, min(int(limit), 1000))
        with self._lock:
            return [self._events.popleft() for _ in range(min(limit, len(self._events)))]

    def resolve_review(self, ident, action, *, enabled=None):
        if action not in {"save", "skip", "never", "stop"}:
            return False
        with self._lock:
            if not self._running or self._stop.is_set() or ident != self._review_id:
                return False
            self._review_id = None
            self._review_response = (str(ident), action, enabled)
            if action == "stop":
                self._stop.set()
        return True

    def set_review_enabled(self, enabled):
        with self._lock:
            self._review_enabled = bool(enabled)
            self._review_dirty = True

    def _emit(self, job_id, event):
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            return
        with self._lock:
            if job_id != self._job_id:
                return
            if event["type"] == "finished":
                self._final = True
                self._review_id = None
            elif event["type"] == "review-request":
                self._review_id = event.get("id")
            event = {**event, "job_id": job_id}
            # A paused UI needs the newest transfer counters, while saved,
            # review and terminal events must retain their exact ordering.
            if (event["type"] == "progress" and self._events and
                    self._events[-1]["type"] == "progress" and self._events[-1]["job_id"] == job_id):
                self._events[-1] = event
            else:
                self._events.append(event)

    def _read_lines(self, job_id, data):
        lines = data.split(b"\n")
        for line in lines[:-1]:
            if len(line) > _MAX_EVENT_BYTES:
                raise _ProtocolError(_PROTOCOL_ERROR)
            try:
                self._emit(job_id, json.loads(line))
            except (ValueError, UnicodeError):
                pass
        if len(lines[-1]) > _MAX_EVENT_BYTES:
            raise _ProtocolError(_PROTOCOL_ERROR)
        return lines[-1]

    def _update_control(self):
        with self._lock:
            dirty, enabled = self._review_dirty, self._review_enabled
            response = self._review_response
            self._review_dirty = False
            self._review_response = None
        if dirty:
            self._control.set_enabled(enabled)
        if response is not None:
            ident, action, enabled = response
            self._control.respond(ident, action, enabled=enabled)

    def _run(self, job_id, options):
        process = None
        return_code = 1
        try:
            options = options.validate()
            with ExitStack() as resources:
                folder = resources.enter_context(tempfile.TemporaryDirectory(prefix="luvbox-"))
                directory = Path(folder)
                request = directory / "request.json"
                events = directory / "events.jsonl"
                stop = directory / "stop.flag"
                request.write_text(json.dumps(asdict(options), ensure_ascii=False), encoding="utf-8")
                events.touch()
                self._control.set_enabled(options.confirm_each)
                self._control.attach(directory / CONTROL_NAME)
                if self._stop.is_set():
                    stop.touch()
                kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
                process = subprocess.Popen(worker_command(request, events, stop), stdin=subprocess.DEVNULL,
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs)
                resources.callback(self._finish_process, process)
                with self._lock:
                    self._process = process
                self._emit(job_id, {"type": "started"})
                with events.open("rb") as stream:
                    partial = b""
                    stop_at = None
                    terminated_at = None
                    heartbeat_at = time.monotonic()
                    while True:
                        partial = self._read_lines(job_id, partial + stream.read(_READ_BYTES))
                        if process.poll() is not None:
                            # Drain the worker's final flush in bounded chunks;
                            # retain bytes until a complete UTF-8 JSON line.
                            while chunk := stream.read(_READ_BYTES):
                                partial = self._read_lines(job_id, partial + chunk)
                            if partial:
                                self._read_lines(job_id, partial + b"\n")
                            return_code = process.returncode
                            break
                        self._update_control()
                        now = time.monotonic()
                        if now - heartbeat_at >= 1:
                            heartbeat_at = now
                            try:
                                self._control.heartbeat()
                            except OSError:
                                pass
                        if self._stop.is_set():
                            if stop_at is None:
                                stop.touch(exist_ok=True)
                                stop_at = now
                            elif now - stop_at > 35 and terminated_at is None:
                                process.terminate()
                                terminated_at = now
                            elif terminated_at is not None and now - terminated_at > 5:
                                process.kill()
                        time.sleep(.1)
                process.wait()
        except ValueError as error:
            self._emit(job_id, {"type": "finished", "status": "error", "message": str(error)})
        except Exception:
            self._emit(job_id, {"type": "finished", "status": "error",
                                "message": "저장 작업을 실행하지 못했습니다. 설정과 저장 위치를 확인해 주세요."})
        finally:
            if process is not None:
                self._finish_process(process)
            self._control.detach()
            with self._lock:
                self._process = None
                self._running = False
                self._review_id = None
            self._emit(job_id, {"type": "process_exit", "code": return_code})

    @staticmethod
    def _finish_process(process):
        if process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
                process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass

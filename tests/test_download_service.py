"""Download IPC ordering and bounded buffers, using disposable fake workers."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests import app_data_isolation  # noqa: F401
from luvbox import download_service
from luvbox.core import JobOptions


class _Process:
    def __init__(self, events, initial=b"", *, tail=None, exit_after=1):
        self.events = events
        events.write_bytes(initial)
        self.tail = tail
        self.exit_after = exit_after
        self.returncode = None
        self.polls = 0
        self.terminated = 0
        self.killed = 0
        self.waited = 0

    def poll(self):
        if self.returncode is None:
            self.polls += 1
            if self.polls >= self.exit_after:
                if self.tail is not None:
                    with self.events.open("ab") as stream:
                        stream.write(self.tail)
                    self.tail = None
                self.returncode = 0
        return self.returncode

    def wait(self, timeout=None):
        self.waited += 1
        return self.returncode

    def terminate(self):
        self.terminated += 1
        self.returncode = -15

    def kill(self):
        self.killed += 1
        self.returncode = -9


class DownloadServiceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="luvbox-download-ipc-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.options = JobOptions("offline", str(self.root / "media"))
        self.service = download_service.DownloadService()

    def run_worker(self, factory):
        self.service._job_id = 1
        self.service._running = True
        with patch.object(download_service.subprocess, "Popen", side_effect=factory), \
                patch.object(download_service.time, "sleep"):
            self.service._run(1, self.options)
        self.assertFalse(self.service.running)
        return self.service.drain(1000)

    def test_stalled_ui_keeps_latest_progress_without_losing_control_boundaries(self):
        service = self.service
        service._job_id = 1
        service._emit(1, {"type": "started"})
        for value in range(50_000):
            service._emit(1, {"type": "progress", "downloaded": value})
        service._emit(1, {"type": "saved", "filename": "offline.jpg"})
        for value in range(50_000, 100_000):
            service._emit(1, {"type": "progress", "downloaded": value})
        service._emit(1, {"type": "review-request", "id": "review-1"})
        service._emit(1, {"type": "finished", "status": "finished"})
        service._emit(1, {"type": "process_exit", "code": 0})
        events = service.drain(1000)
        self.assertEqual([event["type"] for event in events],
                         ["started", "progress", "saved", "progress", "review-request", "finished", "process_exit"])
        self.assertEqual([event["downloaded"] for event in events if event["type"] == "progress"],
                         [49_999, 99_999])
        self.assertEqual(service.drain(), [])

    def test_progress_for_different_jobs_is_not_merged(self):
        self.service._job_id = 1
        self.service._emit(1, {"type": "progress", "downloaded": 10})
        self.service._job_id = 2
        self.service._emit(1, {"type": "saved", "filename": "late.jpg"})
        self.service._emit(2, {"type": "progress", "downloaded": 20})
        events = self.service.drain()
        self.assertEqual([(event["job_id"], event["downloaded"]) for event in events], [(1, 10), (2, 20)])

    def test_close_retires_a_worker_stuck_after_its_terminal_event(self):
        self.service._job_id = 1
        self.service._running = True
        finished = json.dumps({"type": "finished", "status": "finished"}).encode() + b"\n"
        workers = []
        clock = [0.0]
        close_results = []

        def spawn(command, **kwargs):
            worker = _Process(Path(command[-2]), finished, exit_after=10_000)
            workers.append(worker)
            return worker

        def tick(_seconds):
            if not close_results:
                self.assertTrue(self.service._final)
                close_results.append(self.service.close())
            clock[0] += 10
            if clock[0] > 100:
                self.fail("closing must retire even a worker that already reported its result")

        with patch.object(download_service.subprocess, "Popen", side_effect=spawn), \
                patch.object(download_service.time, "monotonic", side_effect=lambda: clock[0]), \
                patch.object(download_service.time, "sleep", side_effect=tick):
            self.service._run(1, self.options)

        self.assertEqual(close_results, [False])
        self.assertFalse(self.service.running)
        self.assertEqual(workers[0].terminated, 1)
        self.assertEqual(workers[0].killed, 0)
        self.assertLessEqual(clock[0], 60)
        events = self.service.drain()
        self.assertEqual([event["type"] for event in events], ["started", "finished", "process_exit"])
        self.assertEqual(events[1]["status"], "finished")
        self.assertEqual(events[-1]["code"], -15)
        self.assertTrue(self.service.close())

    def test_thread_start_failure_rolls_back_and_allows_a_later_worker(self):
        secret = "arbitrary thread failure details"
        with patch.object(download_service.Thread, "start", side_effect=RuntimeError(secret)):
            with self.assertRaises(ValueError) as failure:
                self.service.start(self.options)
        self.assertNotIn(secret, str(failure.exception))
        self.assertFalse(self.service.running)
        self.assertIsNone(self.service._thread)
        finished = json.dumps({"type": "finished", "status": "finished"}).encode()
        workers = []

        def spawn(command, **kwargs):
            worker = _Process(Path(command[-2]), finished)
            workers.append(worker)
            return worker

        with patch.object(download_service.subprocess, "Popen", side_effect=spawn):
            self.assertTrue(self.service.start(self.options))
            self.service._thread.join(timeout=5)
            self.assertFalse(self.service._thread.is_alive())
        self.assertFalse(self.service.running)
        self.assertEqual(workers[0].terminated, 0)
        self.assertEqual([event["type"] for event in self.service.drain()],
                         ["started", "finished", "process_exit"])

    def test_chunked_utf8_and_final_eof_flush_preserve_every_event(self):
        first = json.dumps({"type": "log", "message": "진행 정보"}, ensure_ascii=False).encode()
        split = first.index("진".encode()) + 1
        tail = (first[split:] + b"\n" + json.dumps({"type": "log", "message": "x" * 150_000}).encode() + b"\n" +
                json.dumps({"type": "saved", "filename": "offline.jpg"}).encode() + b"\n" +
                json.dumps({"type": "finished", "status": "finished", "saved": 1}).encode())
        workers = []
        read_sizes = []
        original_open = Path.open

        class BoundedReader:
            def __init__(self, stream):
                self.stream = stream

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.stream.close()

            def read(self, size=-1):
                read_sizes.append(size)
                if not 0 < size <= 64 * 1024:
                    raise AssertionError("IPC read must be bounded")
                return self.stream.read(size)

        def open_path(path, mode="r", *args, **kwargs):
            stream = original_open(path, mode, *args, **kwargs)
            return BoundedReader(stream) if path.name == "events.jsonl" and mode == "rb" else stream

        def spawn(command, **kwargs):
            worker = _Process(Path(command[-2]), first[:split], tail=tail)
            workers.append(worker)
            return worker

        with patch.object(Path, "open", open_path):
            events = self.run_worker(spawn)
        self.assertEqual([event["type"] for event in events],
                         ["started", "log", "log", "saved", "finished", "process_exit"])
        self.assertEqual(events[1]["message"], "진행 정보")
        self.assertEqual(len(events[2]["message"]), 150_000)
        self.assertEqual(events[-1]["code"], 0)
        self.assertGreater(len(read_sizes), 3)
        self.assertEqual(workers[0].terminated, 0)
        self.assertGreaterEqual(workers[0].waited, 1)

    def test_oversized_ipc_line_stops_only_its_owned_worker_with_safe_error(self):
        for suffix in (b"", b"\n"):
            with self.subTest(terminated_line=bool(suffix)):
                workers = []

                def spawn(command, **kwargs):
                    worker = _Process(Path(command[-2]), b"x" * (256 * 1024 + 1) + suffix, exit_after=10)
                    workers.append(worker)
                    return worker

                events = self.run_worker(spawn)
                self.assertEqual(workers[0].terminated, 1)
                self.assertEqual(workers[0].killed, 0)
                failures = [event for event in events if event["type"] == "finished"]
                self.assertEqual(len(failures), 1)
                self.assertEqual(failures[0]["status"], "error")
                self.assertNotIn("xxxx", failures[0]["message"])
                self.assertEqual(events[-1]["code"], 1)


if __name__ == "__main__":
    unittest.main()

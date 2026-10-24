"""Bounded writer shutdown and Unicode installs with the bundled Perl runtime."""

import io
import os
import queue
import shutil
import sys
import tempfile
import threading
import time
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import Mock, patch

from tests.test_metadata import PNG, image_payload
from luvbox import metadata


class ResidentShutdownTests(unittest.TestCase):
    def start_writer(self, session, process):
        closed_by = []
        process.stdin.close.side_effect = lambda: closed_by.append(threading.get_ident())
        session._writes = queue.Queue()
        session._writer = threading.Thread(target=session._send,
                                           args=(process.stdin, session._writes), daemon=True)
        session._writer.start()
        self.addCleanup(session.close, force=True)
        return closed_by

    def test_shutdown_never_closes_a_pipe_held_by_another_reader(self):
        class ReaderPipe:
            def close(self):
                raise AssertionError("shutdown thread must not close a reader-owned pipe")

        session = metadata._ExifToolSession.__new__(metadata._ExifToolSession)
        session._lock, session._readers = threading.RLock(), []
        process = Mock()
        process.poll.return_value = 0
        process.stdout, process.stderr = ReaderPipe(), ReaderPipe()
        session._process = process
        closed_by = self.start_writer(session, process)
        session.close(force=True)
        process.stdin.close.assert_called_once()
        self.assertEqual(closed_by, [session._writer.ident])
        self.assertNotEqual(session._writer.ident, threading.get_ident())
        self.assertFalse(session._writer.is_alive())

    def test_reader_closes_its_own_stream_after_eof(self):
        stream, lines = io.BytesIO(b"one line\n"), queue.Queue()
        metadata._ExifToolSession._pump(stream, lines)
        self.assertTrue(stream.closed)
        self.assertEqual((lines.get_nowait(), lines.get_nowait()), ("one line\n", None))

    def test_reader_bounds_one_line_before_decoding_or_queueing_it(self):
        stream, lines = io.BytesIO(b"x" * 1024), queue.Queue()
        with patch.object(metadata, "_OUTPUT_LIMIT_BYTES", 64):
            metadata._ExifToolSession._pump(stream, lines)
        self.assertTrue(stream.closed)
        self.assertIsInstance(lines.get_nowait(), metadata._OutputLimitExceeded)
        self.assertIsNone(lines.get_nowait())
        self.assertTrue(lines.empty())

    def test_reader_bounds_total_output_and_short_line_count(self):
        for data, byte_limit, line_limit in ((b"x" * 40 + b"\n" + b"y" * 40, 64, 100),
                                              (b"\n" * 100, 4096, 4)):
            with self.subTest(data=data[:3]):
                stream, lines = io.BytesIO(data), queue.Queue()
                with patch.object(metadata, "_OUTPUT_LIMIT_BYTES", byte_limit), \
                        patch.object(metadata, "_OUTPUT_LIMIT_LINES", line_limit):
                    metadata._ExifToolSession._pump(stream, lines)
                output = []
                while not lines.empty():
                    output.append(lines.get_nowait())
                self.assertIsInstance(output[-2], metadata._OutputLimitExceeded)
                self.assertIsNone(output[-1])
                self.assertLessEqual(sum(len(line.encode()) for line in output if isinstance(line, str)), byte_limit)

    def test_broken_resident_pipe_stops_the_writer_before_fallback(self):
        session = metadata._ExifToolSession.__new__(metadata._ExifToolSession)
        session._lock, session._counter = threading.RLock(), 0
        session._readers = []
        session._process = Mock()
        session._process.poll.return_value = None
        session._process.kill.side_effect = lambda: setattr(session._process.poll, "return_value", -9)
        session._process.wait.return_value = -9
        closed_by = self.start_writer(session, session._process)
        with patch.object(session, "_collect", side_effect=metadata._SessionUnavailable("offline EOF")), \
                patch.object(session, "close", wraps=session.close) as close:
            with self.assertRaises(metadata._SessionUnavailable):
                session.execute(["-ver"], time.monotonic() + 2)
            close.assert_called_once_with(force=True)
        session._process.kill.assert_called_once()
        self.assertEqual(closed_by, [session._writer.ident])
        self.assertFalse(session._writer.is_alive())

    def test_stderr_overflow_stops_a_tool_without_waiting_for_stdout(self):
        command = [sys.executable, "-c", "import sys,time; sys.stderr.buffer.write(b'x'*65536); "
                   "sys.stderr.buffer.flush(); time.sleep(30)"]
        with patch.object(metadata, "_tool_command", return_value=(command, {})), \
                patch.object(metadata, "_OUTPUT_LIMIT_BYTES", 1024):
            session = metadata._ExifToolSession()
            self.addCleanup(session.close, force=True)
            started = time.monotonic()
            with self.assertRaises(metadata._OutputLimitExceeded):
                session.execute(["-ver"], time.monotonic() + 5)
            self.assertLess(time.monotonic() - started, 2)
            self.assertIsNotNone(session._process.poll())

    def test_older_or_nonwindows_runtime_retains_executable_compatibility(self):
        with tempfile.TemporaryDirectory() as folder:
            executable = Path(folder) / "exiftool.exe"
            with patch.object(metadata, "exiftool_path", return_value=executable):
                self.assertEqual(metadata._tool_command(), ([str(executable)], {}))


@unittest.skipUnless(os.name == "nt" and metadata.available(), "bundled Windows Perl required")
class BundledWriterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_real_forced_timeout_stops_the_writer_and_all_pipe_readers(self):
        session = metadata._ExifToolSession()
        self.addCleanup(session.close)
        self.assertEqual(Path(session._process.args[0]).name.lower(), "perl.exe")
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            session.execute(["-ver"], time.monotonic())
        self.assertLess(time.monotonic() - started, 6)
        self.assertIsNotNone(session._process.poll())
        self.assertFalse(session._writer.is_alive())
        self.assertTrue(session._process.stdin.closed)
        self.assertTrue(all(not reader.is_alive() for reader in session._readers))
        self.assertTrue(session._process.stdout.closed)
        self.assertTrue(session._process.stderr.closed)

    def test_compressed_metadata_is_bounded_in_resident_and_standalone_calls(self):
        from PIL import Image, PngImagePlugin
        payload = "A" * (metadata._OUTPUT_LIMIT_BYTES + 1024)
        xmp = ('<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF '
               'xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"><rdf:Description '
               'xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:description><rdf:Alt>'
               '<rdf:li xml:lang="x-default">' + payload +
               '</rdf:li></rdf:Alt></dc:description></rdf:Description></rdf:RDF></x:xmpmeta>')
        path = self.root / "compressed.png"
        info = PngImagePlugin.PngInfo()
        info.add_itxt("XML:com.adobe.xmp", xmp, zip=True)
        with Image.new("RGB", (1, 1), "red") as image:
            image.save(path, pnginfo=info)
        original = path.read_bytes()
        self.assertLess(len(original), 8192)
        for resident in (False, True):
            with self.subTest(resident=resident), (metadata.persistent() if resident else nullcontext()):
                with self.assertRaises(metadata._OutputLimitExceeded):
                    metadata._read_tags(path, time.monotonic() + 10)
                self.assertEqual(path.read_bytes(), original)
                # Retiring the bad command must not disable later clean files.
                clean = self.root / "clean.png"
                clean.write_bytes(PNG)
                self.assertEqual(metadata.embed_source(clean, "100")["status"], "embedded")

    def test_unicode_install_reads_writes_and_times_out_without_a_launcher_child(self):
        source = metadata.exiftool_path()
        installed = self.root / "한글😀 설치" / "runtime"
        installed.mkdir(parents=True)
        executable = installed / "exiftool.exe"
        shutil.copyfile(source, executable)
        old_bundle, new_bundle = source.parent / "exiftool_files", installed / "exiftool_files"
        new_bundle.mkdir()
        for item in old_bundle.iterdir():
            if item.name in {"perl.exe", "exiftool.pl"} or item.suffix.lower() == ".dll":
                shutil.copyfile(item, new_bundle / item.name)
        shutil.copytree(old_bundle / "lib", new_bundle / "lib")
        media = self.root / "미디어😀" / "100_1.png"
        media.parent.mkdir()
        media.write_bytes(PNG)
        with patch.object(metadata, "exiftool_path", return_value=executable):
            with metadata.persistent():
                session = metadata._session
                self.assertEqual(session._process.args[:2], [str(new_bundle / "perl.exe"), "exiftool.pl"])
                self.assertEqual(metadata.embed_source(media, "100", "writer", "123")["status"], "embedded")
                self.assertEqual(metadata.read_source(media), "https://x.com/i/web/status/100")
                self.assertEqual(metadata.read_author(media)["author"], "writer")
                with self.assertRaises(TimeoutError):
                    session.execute(["-ver"], time.monotonic())
                self.assertIsNotNone(session._process.poll())
                self.assertFalse(session._writer.is_alive())
                self.assertTrue(session._process.stdin.closed)
                self.assertTrue(all(not reader.is_alive() for reader in session._readers))
                # The later fallback also starts Perl directly, not a launcher.
                result = metadata._run(["-j", str(media)], time.monotonic() + 20)
                self.assertEqual(result.returncode, 0)
            self.assertEqual(metadata.read_source(media), "https://x.com/i/web/status/100")
        self.assertEqual(image_payload(media.read_bytes(), ".png"), image_payload(PNG, ".png"))
        self.assertFalse(list(media.parent.glob(".luvbox-source-*")))


if __name__ == "__main__":
    unittest.main()

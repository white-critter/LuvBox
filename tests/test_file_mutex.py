"""Store transactions across Windows processes, using synthetic DPAPI sessions."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from luvbox import login
from luvbox.file_mutex import file_mutex, replace_file


_WRITER = r'''
import contextlib, sys, time
from pathlib import Path
from luvbox import login
folder, user_id, mode, locked = sys.argv[1:]
folder = Path(folder)
login.session_path = lambda: folder / "session.bin"
if locked == "no":
    login.file_mutex = lambda *_args, **_kwargs: contextlib.nullcontext()
original = login._write_payload
def write(payload, **kwargs):
    if mode == "hold":
        (folder / "first-ready").touch()
        deadline = time.monotonic() + 10
        while not (folder / "continue").exists():
            if time.monotonic() >= deadline:
                raise RuntimeError("Synthetic writer gate timed out")
            time.sleep(.01)
    return original(payload, **kwargs)
login._write_payload = write
if mode == "second":
    (folder / "second-ready").touch()
login.save_authenticated_session("synthetic-token-" + user_id,
    {"user_id": user_id, "handle": "offline" + user_id})
'''

_SETTINGS_WRITER = r'''
import contextlib, sys, time
from pathlib import Path
from luvbox import app_settings, settings
folder, user_id, mode, locked = sys.argv[1:]
folder = Path(folder)
settings.settings_path = app_settings.settings_path = lambda: folder / "settings.json"
if locked == "no":
    app_settings.file_mutex = lambda *_args, **_kwargs: contextlib.nullcontext()
original = app_settings.os.replace
def replace(source, target):
    if mode == "hold":
        (folder / "first-ready").touch()
        deadline = time.monotonic() + 10
        while not (folder / "continue").exists():
            if time.monotonic() >= deadline:
                raise RuntimeError("Synthetic writer gate timed out")
            time.sleep(.01)
    return original(source, target)
app_settings.os.replace = replace
if mode == "second":
    (folder / "second-ready").touch()
app_settings.SettingsRepository().update(
    {"include_videos" if mode == "hold" else "include_gifs": False})
'''


def run_writers(test, directory, script, locked):
    children = []
    try:
        def launch(user_id, mode):
            process = subprocess.Popen([sys.executable, "-c", script, str(directory),
                                        user_id, mode, "yes" if locked else "no"],
                                       stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE)
            children.append(process)
            return process

        def wait_for(name):
            deadline = time.monotonic() + 8
            while not (directory / name).exists():
                test.assertGreater(deadline, time.monotonic(), name)
                test.assertTrue(all(child.poll() is None for child in children), name)
                time.sleep(.01)

        launch("2", "hold")
        wait_for("first-ready")
        second = launch("3", "second")
        wait_for("second-ready")
        if locked:
            with test.assertRaises(subprocess.TimeoutExpired):
                second.wait(timeout=.2)
        else:
            test.assertEqual(second.wait(timeout=3), 0)
        (directory / "continue").touch()
        for child in children:
            _, stderr = child.communicate(timeout=8)
            test.assertEqual(child.returncode, 0, stderr.decode(errors="replace"))
    finally:
        (directory / "continue").touch()
        for child in children:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=5)


@unittest.skipUnless(os.name == "nt", "Windows named mutex store protection")
class FileMutexTests(unittest.TestCase):
    def kernel(self, *, outcome=0, handle=123):
        return SimpleNamespace(CreateMutexW=Mock(return_value=handle),
                               WaitForSingleObject=Mock(return_value=outcome),
                               ReleaseMutex=Mock(return_value=True),
                               CloseHandle=Mock(return_value=True))

    def test_success_and_abandoned_owner_release_and_close_the_handle(self):
        for outcome in (0, 0x80):
            with self.subTest(outcome=outcome):
                kernel = self.kernel(outcome=outcome)
                with patch("ctypes.WinDLL", return_value=kernel):
                    with file_mutex(Path(tempfile.gettempdir()) / "synthetic-store.json", timeout=.125):
                        kernel.ReleaseMutex.assert_not_called()
                    kernel.WaitForSingleObject.assert_called_once_with(123, 125)
                    kernel.ReleaseMutex.assert_called_once_with(123)
                    kernel.CloseHandle.assert_called_once_with(123)

    def test_timeout_or_wait_failure_closes_unowned_handle_without_releasing_it(self):
        for outcome in (0x102, 0xFFFFFFFF):
            with self.subTest(outcome=outcome):
                kernel = self.kernel(outcome=outcome)
                with patch("ctypes.WinDLL", return_value=kernel):
                    with self.assertRaises(OSError) as error:
                        with file_mutex("synthetic-private-store.json", timeout=0):
                            self.fail("A lock timeout must not enter the transaction")
                self.assertNotIn("synthetic-private-store", str(error.exception))
                kernel.ReleaseMutex.assert_not_called()
                kernel.CloseHandle.assert_called_once_with(123)

    def test_creation_failure_and_body_exception_do_not_leak_handles(self):
        kernel = self.kernel(handle=0)
        with patch("ctypes.WinDLL", return_value=kernel):
            with self.assertRaises(OSError):
                with file_mutex("synthetic-store.json"):
                    self.fail("Mutex creation failed")
        kernel.WaitForSingleObject.assert_not_called()
        kernel.CloseHandle.assert_not_called()
        kernel = self.kernel()
        with patch("ctypes.WinDLL", return_value=kernel):
            with self.assertRaisesRegex(RuntimeError, "synthetic transaction failure"):
                with file_mutex("synthetic-store.json"):
                    raise RuntimeError("synthetic transaction failure")
        kernel.ReleaseMutex.assert_called_once_with(123)
        kernel.CloseHandle.assert_called_once_with(123)

    def test_nested_access_to_the_same_path_is_reentrant(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "session.bin"
            with file_mutex(path, timeout=0):
                with file_mutex(path, timeout=0):
                    pass

    def test_concurrent_session_connections_preserve_both_new_accounts(self):
        # The control reproduces the original lost update. In the fixed run,
        # the second process must read after the first process replaces its
        # ciphertext; each process has an independent Python thread RLock.
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for locked in (False, True):
                directory = root / str(locked)
                directory.mkdir()
                session = directory / "session.bin"
                with patch("luvbox.login.session_path", return_value=session):
                    login.save_authenticated_session("synthetic-initial-token",
                                                     {"user_id": "1", "handle": "offline1"})
                run_writers(self, directory, _WRITER, locked)
                with patch("luvbox.login.session_path", return_value=session):
                    ids = {row["user_id"] for row in login.list_accounts()}
                self.assertEqual(ids, {"1", "2", "3"} if locked else {"1", "2"})
                for token in (b"synthetic-initial-token", b"synthetic-token-"):
                    self.assertNotIn(token, session.read_bytes())

    def test_concurrent_preference_changes_merge_with_latest_unknown_keys(self):
        with tempfile.TemporaryDirectory() as folder:
            for locked in (False, True):
                directory = Path(folder) / str(locked)
                directory.mkdir()
                path = directory / "settings.json"
                path.write_text(json.dumps({"include_videos": True, "include_gifs": True,
                                            "unknown": {"preserved": True}}), encoding="utf-8")
                run_writers(self, directory, _SETTINGS_WRITER, locked)
                result = json.loads(path.read_text(encoding="utf-8"))
                self.assertFalse(result["include_videos"])
                self.assertEqual(result["include_gifs"], not locked)
                self.assertEqual(result["unknown"], {"preserved": True})


class ReplaceFileTests(unittest.TestCase):
    def test_transient_sharing_lock_retries_then_publishes(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            source, target = directory / "next.txt", directory / "current.txt"
            target.write_text("previous", encoding="utf-8")
            source.write_text("next", encoding="utf-8")
            original = os.replace
            calls = []

            def replace(src, dest):
                calls.append(Path(dest).name)
                if len(calls) == 1:
                    raise PermissionError("scanner")
                return original(src, dest)

            with patch("luvbox.file_mutex.os.replace", side_effect=replace), \
                    patch("luvbox.file_mutex.time.sleep") as sleep:
                replace_file(source, target)
            self.assertEqual(calls, ["current.txt", "current.txt"])
            self.assertEqual(target.read_text(encoding="utf-8"), "next")
            sleep.assert_called_once()

    def test_lasting_permission_error_keeps_the_previous_file(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            source, target = directory / "next.txt", directory / "current.txt"
            target.write_text("previous", encoding="utf-8")
            source.write_text("next", encoding="utf-8")
            with patch("luvbox.file_mutex.os.replace", side_effect=PermissionError("denied")), \
                    patch("luvbox.file_mutex.time.sleep") as sleep:
                with self.assertRaises(PermissionError):
                    replace_file(source, target, attempts=3, pause=0)
            self.assertEqual(target.read_text(encoding="utf-8"), "previous")
            self.assertEqual(source.read_text(encoding="utf-8"), "next")
            self.assertEqual(sleep.call_count, 2)


class FileMutexValidationTests(unittest.TestCase):
    def test_wait_is_always_finite_and_bounded(self):
        for timeout in (-1, 61, float("nan"), float("inf"), True, "5"):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                with file_mutex("synthetic-store.json", timeout=timeout):
                    self.fail("Invalid timeout accepted")


if __name__ == "__main__":
    unittest.main()

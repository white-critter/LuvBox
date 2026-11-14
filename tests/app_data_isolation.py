"""Point this test process at a private, temporary LocalAppData folder.

LuvBox moves an existing XlikeSaver data folder to LuvBox the first time any
code asks for its app-data folder. Without this, a test that builds a records
store on a developer's PC would move or read that person's real data. One
import is enough: the value is process-wide and inherited by worker processes.
Tests that patch LOCALAPPDATA themselves get this value back afterwards.

Every test module imports this before anything from ``luvbox`` (checked by
``test_app_data_isolation``): a module run on its own, as in
``python -m unittest tests.test_core``, once wrote test folders and a test row
into the real application data because only a full run happened to be isolated.

As a second line of defence the application's own folder lookup refuses the
real folder for the rest of the process. A test that points LOCALAPPDATA back
at it, or clears the variable, fails instead of writing there, and the process
ends with an error even when the code under test swallowed the exception.

Qt does not read LOCALAPPDATA: it asks Windows for the cache folder where it
keeps compiled QML (.qmlc), so test runs used to leave those files under the
real LocalAppData (``Python\\cache\\qmlcache``, and
``LuvBox\\LuvBox\\cache\\qmlcache`` for code that names the application).
QML_DISK_CACHE_PATH moves that cache into the private folder. Qt reads it once,
at the first QML a process loads, which is why it is set here, at import.
"""

import atexit
import gc
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest


def _spelled(path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


# The same module under both of its names (``tests.app_data_isolation`` in a
# test run, ``app_data_isolation`` when a test file runs as a script) must
# make one folder, not two.
_SELF = sys.modules[__name__]
sys.modules.setdefault("app_data_isolation", _SELF)
sys.modules.setdefault("tests.app_data_isolation", _SELF)

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))        # a test file run as a script still finds luvbox

# A worker process inherits the private folder; the first process says which
# folder is the real one.
_MARK = "LUVBOX_TEST_REAL_APPDATA"
_inherited = os.environ.get(_MARK)
_original = os.environ.get("LOCALAPPDATA")
REAL_FOLDERS = {_spelled(Path.home() / "AppData" / "Local")}
if _inherited:
    REAL_FOLDERS.add(_spelled(_inherited))
elif _original:
    REAL_FOLDERS.add(_spelled(_original))
    os.environ[_MARK] = _original

if _inherited and _original and _spelled(_original) not in REAL_FOLDERS:
    # A worker process started by a test. Its parent already chose a private
    # folder, often one the test reads afterwards, and removes it itself.
    FOLDER = _original
    # The parent's cache folder, shared by every process of the run.
    os.environ.setdefault("QML_DISK_CACHE_PATH", os.path.join(FOLDER, "qml-cache"))
else:
    FOLDER = tempfile.mkdtemp(prefix="luvbox-test-appdata-")
    os.environ["LOCALAPPDATA"] = FOLDER
    # Beside the application's own folder (FOLDER\LuvBox), not inside it.
    os.environ["QML_DISK_CACHE_PATH"] = os.path.join(FOLDER, "qml-cache")
    atexit.register(shutil.rmtree, FOLDER, True)

QML_CACHE = os.environ["QML_DISK_CACHE_PATH"]

# Qt objects have to be destroyed on the thread that owns them. Python's cyclic
# collector runs on whichever thread happens to allocate, and a test process
# leaves many finished controllers behind in reference cycles. Collected on a
# worker thread, their timers were destroyed there, and a later test crashed
# with an access violation while it processed events (the whole run ended with
# a segmentation fault; which test it hit depended on timing). So while tests
# run the collector is off, and it runs after each test, on the main thread.
# The application keeps one set of controllers for its whole run and deletes
# them itself at exit. Scene scripts started as child processes never reach
# this wrapper and keep the collector as it is.
_run_test = unittest.TestCase.run
_tests_run = 0


def _run_then_collect(self, result=None):
    global _tests_run
    gc.disable()
    try:
        return _run_test(self, result)
    finally:
        # What a test leaves is young: nothing was collected while it ran. A pass over
        # everything costs about a tenth of a second in this process (three minutes over
        # a full run), so it is made only now and then; it too runs here, never on a worker.
        _tests_run += 1
        if _tests_run % 40:
            gc.collect(1)
        else:
            gc.collect()


if unittest.TestCase.run is not _run_then_collect:
    unittest.TestCase.run = _run_then_collect

REACHED = []                              # attempts to use the real folder, for the exit check


class RealAppDataReached(AssertionError):
    """A test asked for this PC's real application data."""


def _guard():
    from luvbox import settings

    lookup = settings._folders

    def folders():
        target, previous = lookup()
        if _spelled(target.parent) in REAL_FOLDERS:
            REACHED.append(str(target))
            raise RealAppDataReached(
                "A test reached this PC's real application data. Import tests.app_data_isolation first and "
                "keep LOCALAPPDATA on a temporary folder (patch it to another temporary folder if needed).")
        return target, previous

    folders.__wrapped__ = lookup
    settings._folders = folders


def _report():
    if REACHED:
        sys.stderr.write("\nERROR: a test tried to use the real application data folder "
                         f"{len(REACHED)} time(s); the attempts were refused.\n")
        sys.stderr.flush()
        os._exit(3)


_guard()
atexit.register(_report)

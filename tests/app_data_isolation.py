"""Point this test process at a private, temporary LocalAppData folder.

LuvBox moves an existing XlikeSaver data folder to LuvBox the first time any
code asks for its app-data folder. Without this, a test that builds a records
store on a developer's PC would move or read that person's real data. One
import is enough: the value is process-wide and inherited by worker processes.
Tests that patch LOCALAPPDATA themselves get this value back afterwards.
"""

import atexit
import os
import shutil
import tempfile


FOLDER = tempfile.mkdtemp(prefix="luvbox-test-appdata-")
os.environ["LOCALAPPDATA"] = FOLDER
atexit.register(shutil.rmtree, FOLDER, True)

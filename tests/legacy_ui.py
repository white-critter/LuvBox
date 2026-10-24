"""Marks checks that open the previous Tk window.

The current app is the Qt window. Those checks stay in the tree for
``--legacy-ui`` and run only when LUVBOX_LEGACY_UI_TESTS=1.
"""

import os
import unittest


previous_window = unittest.skipUnless(
    os.environ.get("LUVBOX_LEGACY_UI_TESTS") == "1",
    "이전 Tk 화면 검사입니다. 보려면 LUVBOX_LEGACY_UI_TESTS=1",
)

"""The save-folder picker must be able to leave the drive it opened on."""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class SaveFolderDialogTests(unittest.TestCase):
    def test_windows_folder_dialog_is_not_locked_to_the_current_drive(self):
        text = (ROOT / "assets" / "qt" / "MainWindow.qml").read_text(encoding="utf-8")
        start = text.index("id: folderDialog")
        block = text[start:text.index("function chooseSaveFolder", start)]
        self.assertNotIn("DontUseNativeDialog", block)
        self.assertNotIn("currentFolder:", block)
        chooser = text[text.index("function chooseSaveFolder"):text.index("ConnectionDialog")]
        self.assertIn("folderDialog.currentFolder = current", chooser)
        self.assertIn("folderDialog.open()", chooser)
        self.assertIn("chooseSaveFolder()", text)

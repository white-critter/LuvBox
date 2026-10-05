"""Themed message dialogs keep tkinter.messagebox's calls and results."""

import tkinter as tk
import unittest
from unittest.mock import patch

from tests.test_gui import destroy_tk, release_tk
from xlikesaver import dialogs, theme


class DialogTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        theme.apply_style(self.root, mode="light")
        self.addCleanup(release_tk, self, "root")
        self.addCleanup(destroy_tk, self.root)
        self.addCleanup(theme.apply_style, self.root, "light")

    def open_dialogs(self):
        return [widget for widget in self.root.winfo_children() if isinstance(widget, dialogs.Dialog)]

    def test_buttons_return_their_value_and_closing_means_cancel(self):
        dialog = dialogs.Dialog(self.root, "확인", "계속할까요?", icon="question",
                                buttons=(("예", True), ("아니요", False)), cancel=False)
        self.assertEqual([button.cget("text") for button in dialog.buttons], ["예", "아니요"])
        self.assertEqual(str(dialog.buttons[0].cget("style")), "DialogPrimary.TButton")
        dialog.buttons[0].invoke()
        self.assertIs(dialog.result, True)
        self.assertFalse(self.open_dialogs())
        dialog = dialogs.Dialog(self.root, "확인", "계속할까요?", buttons=(("예", True), ("아니요", False)), cancel=False)
        dialog.cancel()
        self.assertIs(dialog.result, False)

    def test_run_is_modal_until_a_button_is_chosen(self):
        def answer():
            [dialog] = self.open_dialogs()
            self.assertTrue(dialog.winfo_viewable())
            dialog.buttons[1].invoke()
        self.root.after(150, answer)
        self.assertIs(dialogs.askyesno("파일 바꾸기", "계속할까요?", parent=self.root), False)
        self.root.after(150, lambda: self.open_dialogs()[0].buttons[0].invoke())
        self.assertEqual(dialogs.showerror("실패", "다시 시도해 주세요.", parent=self.root), "ok")

    def test_follows_dark_appearance_and_wraps_long_text_and_paths(self):
        theme.apply(self.root, "dark")
        long_path = "D:\\" + "\\".join(["아주긴폴더이름"] * 30) + "\\file.gif"
        dialog = dialogs.Dialog(self.root, "기록 저장 위치", long_path, icon="info")
        self.addCleanup(lambda: dialog.winfo_exists() and dialog.destroy())
        dialog.update_idletasks()
        self.assertEqual(dialog.cget("background"), theme.DARK["CARD"])
        self.assertLess(dialog.winfo_reqwidth(), 720)
        self.assertTrue(dialog.bind("<Control-c>"))
        dialog._copy()
        self.assertEqual(self.root.clipboard_get(), long_path)

    def test_messagebox_style_helpers_choose_icons_and_buttons(self):
        with patch.object(dialogs.Dialog, "run", autospec=True, side_effect=lambda dialog: dialog.result) as run:
            self.assertIsNone(dialogs.showinfo("안내", "본문", parent=self.root))
            self.assertIsNone(dialogs.showwarning("주의", "본문", parent=self.root))
            self.assertIs(dialogs.askyesno("질문", "본문", parent=self.root), False)
        labels = [[button.cget("text") for button in call.args[0].buttons] for call in run.call_args_list]
        self.assertEqual(labels, [["확인"], ["확인"], ["예", "아니요"]])
        for call in run.call_args_list:
            call.args[0].destroy()


if __name__ == "__main__":
    unittest.main()

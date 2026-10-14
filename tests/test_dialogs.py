"""Themed message dialogs keep tkinter.messagebox's calls and results."""

import tkinter as tk
from types import SimpleNamespace
import unittest
import weakref
from unittest.mock import patch

from tests.test_gui import destroy_tk, release_tk
from luvbox import dialogs, theme


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

    def test_delete_confirmation_distinguishes_cancel_and_redownload_choice(self):
        dialog = dialogs.DeleteDialog(self.root, 2, 4096, ["first.jpg", "second.mp4"])
        self.assertTrue(dialog.keep_deleted.get())
        self.assertIn("2개", dialog.message)
        self.assertIn("4.0 KB", dialog.message)
        self.assertIn("Windows 휴지통", dialog.message)
        self.assertEqual(dialog.default_button.cget("text"), "취소")
        dialog._return(SimpleNamespace(widget=dialog.message_label))
        self.assertIsNone(dialog.result)
        dialog = dialogs.DeleteDialog(self.root, 1, 1024, ["first.jpg"])
        dialog.keep_deleted.set(False)
        dialog.buttons[1].invoke()
        self.assertIs(dialog.result, False)
        dialog = dialogs.DeleteDialog(self.root, 1, 1024, ["first.jpg"])
        dialog.buttons[1].invoke()
        self.assertIs(dialog.result, True)

    def test_delete_confirmation_keeps_controls_on_short_screen(self):
        for mode in ("light", "dark"):
            with self.subTest(mode=mode), patch.object(tk.Toplevel, "winfo_screenheight", return_value=768):
                theme.apply(self.root, mode)
                dialog = dialogs.DeleteDialog(self.root, 200, 1048576, ["긴 파일 이름" * 40] * 200)
                self.addCleanup(lambda: dialog.winfo_exists() and dialog.destroy())
                dialog.attributes("-alpha", 0)
                dialog.deiconify()
                dialog.update()
                self.assertEqual(dialog.cget("background"), (theme.DARK if mode == "dark" else theme.LIGHT)["CARD"])
                self.assertIn("외 195개", dialog.message)
                for button in dialog.buttons:
                    top = button.winfo_rooty() - dialog.winfo_rooty()
                    self.assertGreaterEqual(top, 0)
                    self.assertLessEqual(top + button.winfo_height(), dialog.winfo_height())
                dialog.cancel()

    def test_delete_confirmation_releases_its_tcl_variable_on_close(self):
        dialog = dialogs.DeleteDialog(self.root, 1, 1024, ["first.jpg"])
        variable = weakref.ref(dialog.keep_deleted)
        dialog.cancel()
        self.assertIsNone(variable())
        self.assertIsNone(dialog.keep_deleted)

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

    def test_requested_no_is_the_default_answer_for_enter(self):
        observed = {}
        def answer():
            [dialog] = self.open_dialogs()
            observed["label"] = dialog.default_button.cget("text")
            observed["style"] = dialog.default_button.cget("style")
            # Return outside the action buttons follows the requested default.
            observed["return"] = dialog._return(SimpleNamespace(widget=dialog.message_label))
        self.root.after(150, answer)
        result = dialogs.askyesno("이전 폴더 없음", "새 폴더에서 시작할까요?",
                                 parent=self.root, default="no")
        self.assertIs(result, False)
        self.assertEqual(observed["label"], "아니요")
        self.assertTrue(str(observed["style"]).endswith("DialogPrimary.TButton"))
        self.assertEqual(observed["return"], "break")

    def test_return_activates_the_focused_action_only_once(self):
        dialog = dialogs.Dialog(self.root, "확인", "계속할까요?",
                                buttons=(("예", True), ("아니요", False)), cancel=False)
        invoked = []
        dialog.buttons[1].configure(command=lambda: invoked.append(False))
        self.assertEqual(dialog._return(SimpleNamespace(widget=dialog.buttons[1])), "break")
        self.assertEqual(invoked, [False])
        dialog.destroy()

    def test_long_message_scrolls_and_actions_fit_a_short_screen(self):
        message = "\n".join(f"파일 {index}: 기록 확인 결과" for index in range(100))
        with patch.object(tk.Toplevel, "winfo_screenheight", return_value=768), \
                patch.object(tk.Toplevel, "winfo_screenwidth", return_value=1024):
            dialog = dialogs.Dialog(self.root, "기록 확인 결과", message)
            self.addCleanup(lambda: dialog.winfo_exists() and dialog.destroy())
            dialog.attributes("-alpha", 0)
            dialog._place()
            dialog.deiconify()
            dialog.update()
            self.assertLessEqual(dialog.winfo_height(), 668)
            self.assertLess(dialog.message_view.canvas.yview()[1], 1)
            self.assertTrue(dialog.message_view.scrollbar.winfo_ismapped())
            self.assertGreater(dialog.message_view.scrollbar.winfo_width(), 1)
            dialog.message_view.canvas.yview_moveto(1)
            dialog.update()
            self.assertEqual(dialog.message_label.cget("text"), message)
            for button in dialog.buttons:
                top = button.winfo_rooty() - dialog.winfo_rooty()
                self.assertGreaterEqual(top, 0)
                self.assertLessEqual(top + button.winfo_height(), dialog.winfo_height())

    def test_nested_dialog_restores_parent_grab_and_focus(self):
        self.root.attributes("-alpha", 0)
        self.root.deiconify()
        observed = {}
        outer = dialogs.Dialog(self.root, "파일 처리", "파일을 처리할까요?",
                               buttons=(("예", True), ("아니요", False)), cancel=False)
        outer.attributes("-alpha", 0)
        def nested():
            outer.default_button.focus_force()
            inner = dialogs.Dialog(outer, "다시 확인", "변경 내용을 확인하세요.")
            inner.attributes("-alpha", 0)
            self.root.after(100, inner.buttons[0].invoke)
            observed["result"] = inner.run()
            observed["grab"] = self.root.grab_current() is outer
            # Windows delivers focus restoration with its activation event.
            self.root.after(200, check_focus)
        def check_focus():
            observed["focus"] = self.root.focus_get() is outer.default_button
            outer.cancel()
        self.root.after(150, nested)
        self.assertIs(outer.run(), False)
        self.assertEqual(observed, {"result": "ok", "grab": True, "focus": True})
        self.assertIsNone(self.root.grab_current())

    def test_open_dialog_recolors_its_icon_when_appearance_changes(self):
        dialog = dialogs.Dialog(self.root, "안내", "확인해 주세요.")
        self.addCleanup(lambda: dialog.winfo_exists() and dialog.destroy())
        badge = next(widget for widget in dialog.body.winfo_children()[0].winfo_children()
                     if isinstance(widget, tk.Canvas))
        theme.apply(self.root, "dark")
        self.assertEqual(badge.cget("background"), theme.DARK["CARD"])
        circle, _text = badge.find_all()
        self.assertEqual(badge.itemcget(circle, "fill"), theme.DARK["BLUE"])

    def test_large_type_short_screen_keeps_question_actions_inside(self):
        scaling = self.root.tk.call("tk", "scaling")
        self.addCleanup(self.root.tk.call, "tk", "scaling", scaling)
        self.root.tk.call("tk", "scaling", 2.0)
        with patch.object(tk.Toplevel, "winfo_screenheight", return_value=720), \
                patch.object(tk.Toplevel, "winfo_screenwidth", return_value=800):
            dialog = dialogs.Dialog(self.root, "기존 파일에 출처·작성자 기록", "상세 확인 내용\n" * 70,
                                    buttons=(("예", True), ("아니요", False)), default=1, cancel=False)
            self.addCleanup(lambda: dialog.winfo_exists() and dialog.destroy())
            dialog.attributes("-alpha", 0)
            dialog._place()
            dialog.deiconify()
            dialog.update()
            self.assertLessEqual(dialog.winfo_height(), 620)
            self.assertTrue(dialog.message_view.scrollbar.winfo_ismapped())
            for button in dialog.buttons:
                left = button.winfo_rootx() - dialog.winfo_rootx()
                top = button.winfo_rooty() - dialog.winfo_rooty()
                self.assertGreaterEqual(left, 0)
                self.assertLessEqual(left + button.winfo_width(), dialog.winfo_width())
                self.assertGreaterEqual(top, 0)
                self.assertLessEqual(top + button.winfo_height(), dialog.winfo_height())


if __name__ == "__main__":
    unittest.main()

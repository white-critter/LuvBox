"""The themed right-click menu: rows, keyboard, closing and running a command."""

import tkinter as tk
from types import SimpleNamespace
import unittest
import unittest.mock

from tests.test_gui import destroy_tk
from xlikesaver import theme
from xlikesaver.popup_menu import PopupMenu


class PopupMenuTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.geometry("400x300+80+80")
        self.root.attributes("-alpha", 0)
        theme.apply(self.root, "light")
        self.addCleanup(destroy_tk, self.root)
        self.addCleanup(theme.apply_style, self.root, "light")
        self.calls = []

    def menu(self, x=120, y=120):
        commands = [("열기", lambda: self.calls.append("open"), True),
                    ("사용 불가", lambda: self.calls.append("disabled"), False),
                    ("복사", lambda: self.calls.append("copy"), True)]
        menu = PopupMenu(self.root, commands, x, y)
        menu.attributes("-alpha", 0)
        self.addCleanup(lambda: menu.winfo_exists() and menu.close())
        self.root.update()
        return menu

    def settle(self):
        for _ in range(3):
            self.root.update()

    def test_rows_use_the_app_theme_and_disabled_rows_do_nothing(self):
        for mode in ("light", "dark"):
            with self.subTest(mode=mode):
                theme.apply(self.root, mode)
                palette = theme.DARK if mode == "dark" else theme.LIGHT
                menu = self.menu()
                self.assertTrue(menu.wm_overrideredirect())
                self.assertEqual(str(menu.body.cget("background")), palette["CARD"])
                self.assertEqual(str(menu.cget("background")), palette["LINE"])
                self.assertEqual([row.cget("text") for row, _callback, _enabled in menu.rows], ["열기", "사용 불가", "복사"])
                self.assertEqual(str(menu.rows[1][0].cget("foreground")), palette["DISABLED"])
                menu.invoke(1)
                self.assertTrue(menu.winfo_exists())
                menu._activate(0)
                self.assertEqual(str(menu.rows[0][0].cget("background")), palette["BUTTON_HOVER"])
                menu.close()
                self.assertFalse(menu.winfo_exists())
        self.settle()
        self.assertEqual(self.calls, [])

    def test_command_runs_after_the_menu_is_gone(self):
        menu = self.menu()
        seen = []
        menu.rows[2] = (menu.rows[2][0], lambda: seen.append(menu.winfo_exists()), True)
        menu.invoke(2)
        self.assertFalse(menu.winfo_exists())
        self.settle()
        self.assertEqual(seen, [False])

    def test_arrow_keys_skip_disabled_rows_and_enter_runs_the_row(self):
        menu = self.menu()
        menu._step(1)
        self.assertEqual(menu.active, 0)
        menu._step(1)
        self.assertEqual(menu.active, 2)
        menu._step(1)
        self.assertEqual(menu.active, 0)
        menu._step(-1)
        self.assertEqual(menu.active, 2)
        menu.event_generate("<Return>")
        self.settle()
        self.assertEqual(self.calls, ["copy"])

    def test_escape_or_a_click_elsewhere_closes_without_running(self):
        menu = self.menu()
        menu.event_generate("<Escape>")
        self.settle()
        self.assertFalse(menu.winfo_exists())
        menu = self.menu()
        menu._press_elsewhere(SimpleNamespace(x_root=menu.winfo_rootx() + 2, y_root=menu.winfo_rooty() + 2))
        self.assertTrue(menu.winfo_exists())
        menu._press_elsewhere(SimpleNamespace(x_root=menu.winfo_rootx() - 40, y_root=menu.winfo_rooty() - 40))
        self.assertFalse(menu.winfo_exists())
        self.settle()
        self.assertEqual(self.calls, [])

    def test_keyboard_focus_returns_to_the_window_after_the_menu(self):
        # Otherwise Delete, Ctrl+A and page keys did nothing until a click.
        entry = tk.Entry(self.root)
        entry.pack()
        self.root.focus_force()
        entry.focus_set()
        self.settle()
        for close in ("close", "invoke", "elsewhere"):
            with self.subTest(close=close):
                menu = self.menu()
                if close == "close":
                    menu.close()
                elif close == "invoke":
                    menu.invoke(0)
                else:
                    menu._press_elsewhere(SimpleNamespace(x_root=menu.winfo_rootx() - 40,
                                                          y_root=menu.winfo_rooty() - 40))
                self.settle()
                self.assertIs(self.root.focus_get(), entry)
        menu = self.menu()
        with unittest.mock.patch.object(entry, "focus_force") as focus:
            menu.close(restore_focus=False)  # another program became active
        focus.assert_not_called()

    def test_menu_stays_on_screen_near_the_bottom_right_corner(self):
        right, bottom = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        menu = self.menu(right - 2, bottom - 2)
        self.assertLessEqual(menu.winfo_rootx() + menu.winfo_width(), right)
        self.assertLessEqual(menu.winfo_rooty() + menu.winfo_height(), bottom)
        self.assertGreaterEqual(menu.winfo_rootx(), 0)
        self.assertGreaterEqual(menu.winfo_rooty(), 0)


if __name__ == "__main__":
    unittest.main()

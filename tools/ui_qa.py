r"""Capture reproducible UI fixtures without touching user data or X.

Run with the project interpreter, for example::

    .\.venv\Scripts\python.exe tools/ui_qa.py --output .test-output/ui2/captures

All settings, catalogs, media and protection files live in one temporary
directory. LOCALAPPDATA is isolated before any app module is imported. Network
calls and real scheduler/worker/browser/file-opening actions are rejected.
Screenshots contain only the tested Tk window's client rectangle.
"""

from __future__ import annotations

import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import gc
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
STAMP = "2026-10-06T22:10:00+09:00"


def forbidden(*_args, **_kwargs):
    raise RuntimeError("UI QA forbids network and real external actions")


def make_catalog(temporary, account="demo_account", count=168):
    """Create colorful, local-only photos/GIFs/video and deterministic records."""
    from PIL import Image, ImageDraw, ImageFont
    from xlikesaver.history import HistoryStore
    from xlikesaver.media import ffmpeg_path
    from xlikesaver.storage import resolve_storage

    output = temporary / "미디어"
    output.mkdir()
    paths = resolve_storage(output, account)
    palette = [("#6679e8", "#d9e9ff"), ("#dc8b7c", "#fff2da"),
               ("#2b997b", "#d6eee7"), ("#9a70bd", "#f2dded"),
               ("#447ca8", "#dbf2f4"), ("#c89841", "#fbebc9")]
    authors = ["moon_studio", "tiny_atelier", "blue_sky", "daily_sketch", "soft_color", "pixel_notes"]
    font_path = ROOT / "assets/fonts/Pretendard/Pretendard-Bold.ttf"
    font = ImageFont.truetype(str(font_path), 28)
    small = ImageFont.truetype(str(font_path), 16)
    source_video = temporary / "fixture-video.mp4"
    executable = ffmpeg_path()
    if executable is not None:
        generated = subprocess.run(
            [str(executable), "-nostdin", "-v", "error", "-f", "lavfi", "-i",
             "testsrc2=size=320x180:rate=10", "-t", "0.2", "-an", "-c:v", "mpeg4", str(source_video)],
            capture_output=True, timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if generated.returncode:
            raise RuntimeError("UI QA local video fixture could not be generated")
    store = HistoryStore.for_storage(paths)
    try:
        texts = []
        for index in range(count):
            tweet = str(2030000000000000000 + index)
            foreground, background = palette[index % len(palette)]
            image = Image.new("RGB", (360, 240), background)
            draw = ImageDraw.Draw(image)
            offset = index % 30
            draw.ellipse((210 - offset, -70 + offset, 410 - offset, 140 + offset), fill=foreground)
            draw.rounded_rectangle((24, 82, 240, 205), radius=26, fill=foreground)
            draw.text((42, 106), "오늘의 기록", fill="white", font=font)
            draw.text((43, 151), f"STUDIO / {index + 1:03d}", fill="white", font=small)
            kind = "animated_gif" if index % 9 == 4 else "video" if index % 13 == 5 and source_video.exists() else "photo"
            if kind == "animated_gif":
                filename = f"{tweet}_gif_1.gif"
                frame = image.copy()
                ImageDraw.Draw(frame).ellipse((260, 155, 320, 215), fill="#ffffff")
                image.save(output / filename, save_all=True, append_images=[frame], duration=300, loop=0)
            elif kind == "video":
                filename = f"{tweet}_video_1.mp4"
                shutil.copyfile(source_video, output / filename)
            else:
                filename = f"{tweet}_1.jpg"
                image.save(output / filename, quality=88)
            month = 6 + index % 4
            posted = datetime(2026, month, index % 27 + 1, 12, 30, tzinfo=timezone.utc)
            store.record_media("flat", {"tweet_id": tweet, "num": 1, "type": kind,
                                        "author": {"name": authors[index % len(authors)], "id": str(1000 + index % len(authors))},
                                        "date": posted, "_xlikesaver_metadata_status": "embedded",
                                        "_xlikesaver_author_metadata_status": "embedded"}, output / filename)
            texts.append((tweet, f"오늘의 색과 작은 장면 {index + 1}. 함께 남기는 작업 기록입니다.", ["illustration", "daily"]))
            saved = f"2026-{month:02d}-{index % 27 + 1:02d}T22:10:00+09:00"
            with store._db:
                store._db.execute("UPDATE media SET saved_at=? WHERE tweet_id=?", (saved, tweet))
                store._db.execute("INSERT INTO likes(tweet_id,like_key,liked_after,liked_before) VALUES (?,?,?,?)",
                                  (tweet, float(count - index), saved.replace("22:10", "21:10"), saved))
        store.record_tweets(texts)
        # A few saved photos deleted afterwards; one was hidden on purpose.
        gone = [index for index in range(count) if index % 9 != 4 and index % 13 != 5][3:10]
        for index in gone:
            (output / f"{2030000000000000000 + index}_1.jpg").unlink()
        store.set_missing_ignored([(str(2030000000000000000 + gone[-1]), 1, "photo")], True)
        for index in range(112):
            run = store.start_run("flat", scan_mode="quick")
            status = "error" if index % 19 == 0 else "stopped" if index % 11 == 0 else "finished"
            message = "X 요청 한도에 도달했습니다. 안내된 시각 이후 다시 시작해 주세요." if status == "error" else "직접 중단했습니다. 저장한 파일은 유지됩니다." if status == "stopped" else "새 Like 확인을 마쳤습니다."
            store.finish_run(run, status, saved=index % 25, skipped=100, size_skipped=index % 3,
                             visited=110 + index % 40, message=message,
                             last_tweet_id=str(2030000000000000000 + index),
                             last_posted_at=f"2026-09-{index % 27 + 1:02d}T21:30:00+09:00")
            month = 9 + index // 60
            started = f"2026-{month:02d}-{index % 27 + 1:02d}T22:00:00+09:00"
            finished = started.replace("22:00", "22:10")
            with store._db:
                store._db.execute("UPDATE runs SET started_at=?,finished_at=? WHERE id=?", (started, finished, run))
        return paths, store.summary()
    finally:
        store.close()


def make_contact_sheets(output, screenshots):
    """Pair light/dark captures for a quick layout review; keep full PNGs too."""
    from PIL import Image, ImageDraw, ImageFont, ImageOps
    groups = {
        "dashboard-default": ["dashboard-default-ready", "dashboard-default-running", "dashboard-default-error"],
        "dashboard": ["dashboard-ready", "dashboard-running", "dashboard-compact", "dashboard-compact-log", "dashboard-error"],
        "connection-help": ["connect-token", "connect-file", "schedule", "schedule-off", "help"],
        "settings": ["options-1", "options-2", "options-3"],
        "dialogs": ["dialog-confirm", "dialog-long"],
        "library-default": ["library-default-files", "library-maximized-files"],
        "library-browse": ["library-files", "library-filters", "library-compact", "library-compact-detail", "library-gallery"],
        "library-history-stats": ["library-runs", "library-stats", "library-stats-compact", "library-tools"],
    }
    available = {row["name"] for row in screenshots}
    font_path = ROOT / "assets/fonts/Pretendard/Pretendard-Regular.ttf"
    font = ImageFont.truetype(str(font_path), 17)
    heading = ImageFont.truetype(str(font_path), 24)
    files = []
    for group, prefixes in groups.items():
        pairs = []
        for prefix in prefixes:
            images = []
            for selected in ("light", "dark"):
                name = f"{prefix}-{selected}"
                if name in available:
                    with Image.open(output / f"{name}.png") as source:
                        scaled = ImageOps.contain(source.convert("RGB"), (540, 650))
                    images.append((name, scaled))
                else:
                    images.append(None)
            if any(images):
                pairs.append(images)
        if not pairs:
            continue
        row_heights = [max(image[1].height for image in row if image) + 48 for row in pairs]
        sheet = Image.new("RGB", (1136, 82 + sum(row_heights)), "#e9edf4")
        draw = ImageDraw.Draw(sheet)
        draw.text((20, 20), group + " / UI QA", font=heading, fill="#1e293b")
        y = 70
        for pair, row_height in zip(pairs, row_heights):
            for column, item in enumerate(pair):
                if item is None:
                    continue
                name, image = item
                x = 20 + column * 558
                draw.text((x, y), name, font=font, fill="#1e293b")
                sheet.paste(image, (x, y + 26))
            y += row_height
        filename = f"contact-{group}.png"
        sheet.save(output / filename)
        files.append(filename)
    return files


class CaptureSession:
    def __init__(self, root, app, output, *, include, scale, simulated_screen=False):
        self.root, self.app, self.output = root, app, output
        self.include = include
        self.scale = scale
        self.simulated_screen = simulated_screen
        self.manifest = []
        self.errors = []
        root.report_callback_exception = lambda kind, value, _trace: self.errors.append(f"{kind.__name__}: {value}")
        root.deiconify()
        root.update_idletasks()
        self.default_main_geometry = f"{root.winfo_width()}x{root.winfo_height()}"

    def settle(self, condition=None, timeout=8, minimum=.18):
        deadline = time.monotonic() + timeout
        start = time.monotonic()
        while time.monotonic() < deadline:
            self.root.update()
            if time.monotonic() - start >= minimum and (condition is None or condition()):
                if self.errors:
                    raise RuntimeError("UI QA callback failed: " + self.errors[0])
                return
            time.sleep(.01)
        raise TimeoutError("UI QA timed out waiting for an isolated screen")

    def capture(self, window, name, geometry=None, scroll="top", *, maximized=False):
        from PIL import ImageGrab
        if self.include and not any(name.startswith(prefix) for prefix in self.include):
            return
        previous_state = window.state()
        placement = None
        if maximized:
            # Maximize our fixture only. A simulated short screen cannot change
            # Windows' native work area, so it omits this dedicated case.
            window.state("zoomed")
            self.settle(minimum=.25)
        elif geometry:
            if window.state() != "normal":
                window.state("normal")
                self.settle(minimum=.25)
            width, height = map(int, geometry.split("x"))
            width = min(width, window.winfo_screenwidth() - 48)
            height = min(height, window.winfo_screenheight() - 100)
            x = max(0, min(90, window.winfo_screenwidth() - width - 32))
            y = max(0, min(40, window.winfo_screenheight() - height - 100))
            placement = f"{width}x{height}+{x}+{y}"
            window.geometry(placement)
        else:
            window.update_idletasks()
            width, height = window.winfo_width(), window.winfo_height()
            x = max(0, min(90, window.winfo_screenwidth() - width - 32))
            y = max(0, min(40, window.winfo_screenheight() - height - 100))
            placement = f"+{x}+{y}"
            window.geometry(placement)
        previous_topmost = window.attributes("-topmost")
        try:
            # Other applications/tests cannot cover this fixture during its
            # capture. Only our own window receives this temporary attribute.
            window.attributes("-topmost", True)
            window.deiconify()
            window.lift()
            window.focus_force()
            self.settle(minimum=.25)
            if not window.winfo_viewable():
                raise RuntimeError(f"UI QA {name} fixture is not viewable")
            if placement:
                # Native activation/restoration can finish after the first
                # placement. Apply it again to the now-visible fixture so its
                # client bounds also fit the simulated short-screen region.
                window.geometry(placement)
                self.settle(minimum=.15)
            # The main view belongs to App, whereas popups/HistoryWindow keep
            # it on their Toplevel. Settle resize before choosing an endpoint.
            view = self.app.content_view if window is self.root else getattr(window, "content_view", None)
            if view is not None:
                view.canvas.yview_moveto(1 if scroll == "bottom" else 0)
            self.settle(minimum=.25)
            x, y = window.winfo_rootx(), window.winfo_rooty()
            width, height = window.winfo_width(), window.winfo_height()
            bottom_limit = window.winfo_screenheight() if maximized else window.winfo_screenheight() - 40
            if x < 0 or y < 0 or x + width > window.winfo_screenwidth() or y + height > window.winfo_screenheight():
                raise RuntimeError(f"UI QA {name} would capture outside the screen: "
                                   f"client={width}x{height}+{x}+{y}, "
                                   f"screen={window.winfo_screenwidth()}x{window.winfo_screenheight()}, geometry={window.geometry()}")
            if y + height > bottom_limit:
                raise RuntimeError(f"UI QA {name} overlaps the reserved taskbar region: "
                                   f"client bottom={y + height}, limit={bottom_limit}")
            filename = self.output / f"{name}.png"
            ImageGrab.grab(bbox=(x, y, x + width, y + height)).save(filename)
            self.manifest.append({"name": name, "file": filename.name, "width": width, "height": height,
                                  "scaling": self.scale, "scroll": scroll, "window_state": window.state(), "viewable": True,
                                  "client_origin": {"x": x, "y": y},
                                  "controls": self._geometry(window)})
            print(f"Captured {name}: {width}x{height}", flush=True)
        finally:
            if window.winfo_exists():
                window.attributes("-topmost", previous_topmost)
                if maximized:
                    window.state(previous_state)
                    self.settle(minimum=.25)

    def _geometry(self, window):
        """Record visibility of key controls, including ancestor scroll clips."""
        import tkinter as tk
        from tkinter import ttk
        widgets = {}
        if window is self.root:
            for name in ("account_entry", "folder_entry", "login_button", "options_button", "start_button",
                         "stop_button", "progress", "log"):
                widget = getattr(self.app, name, None)
                if widget is not None:
                    widgets[name] = widget
        for name in ("tree", "preview_box", "open_file_button", "open_tweet_button", "run_tree", "author_tree", "month_chart"):
            widget = getattr(window, name, None)
            if widget is not None:
                widgets[name] = widget
        pending = list(window.winfo_children())
        while pending:
            widget = pending.pop()
            if isinstance(widget, tk.Toplevel):
                continue
            if isinstance(widget, ttk.Button) and widget.winfo_ismapped():
                widgets.setdefault(str(widget.cget("text") or widget), widget)
            if isinstance(widget, ttk.Notebook) and widget.winfo_ismapped():
                widgets.setdefault("notebook", widget)
            pending.extend(widget.winfo_children())
        result = {}
        left, top = window.winfo_rootx(), window.winfo_rooty()
        for name, widget in widgets.items():
            if not widget.winfo_ismapped():
                continue
            x, y = widget.winfo_rootx() - left, widget.winfo_rooty() - top
            width, height = widget.winfo_width(), widget.winfo_height()
            clip = [max(0, x), max(0, y), min(window.winfo_width(), x + width), min(window.winfo_height(), y + height)]
            scrollable = False
            parent = widget.master
            while parent is not None and parent is not window:
                if isinstance(parent, tk.Canvas):
                    scrollable = True
                    cx, cy = parent.winfo_rootx() - left, parent.winfo_rooty() - top
                    clip = [max(clip[0], cx), max(clip[1], cy), min(clip[2], cx + parent.winfo_width()), min(clip[3], cy + parent.winfo_height())]
                parent = parent.master
            visible_area = max(0, clip[2] - clip[0]) * max(0, clip[3] - clip[1])
            result[name] = {"x": x, "y": y, "width": width, "height": height,
                            "visible_fraction": round(visible_area / max(1, width * height), 4),
                            "in_scrollable_content": scrollable}
            if isinstance(widget, ttk.Notebook) and widget.select():
                active = widget.nametowidget(widget.select())
                tab_x = active.winfo_rootx() - widget.winfo_rootx()
                tab_y = active.winfo_rooty() - widget.winfo_rooty()
                result[name]["active_tab_insets"] = {
                    "left": tab_x, "top": tab_y,
                    "right": width - tab_x - active.winfo_width(),
                    "bottom": height - tab_y - active.winfo_height()}
        return result

    def appearance(self, selected):
        from xlikesaver import theme
        self.app.appearance.set(selected)
        theme.apply(self.root, selected)
        self.app._update_theme_caption()
        # Selective popup/library reruns must also expose their Tk owner;
        # Windows keeps a transient window hidden while its owner is withdrawn.
        self.root.deiconify()
        self.settle(minimum=.1)

    def dashboard(self, selected):
        app = self.app
        self.appearance(selected)
        app._clear_log()
        app.running = False
        app._set_running(False)
        app.saved, app.skipped, app.size_skipped = 0, 0, 0
        app._update_counts()
        app.progress.stop()
        app.progress.configure(mode="determinate", value=0)
        app.progress_summary.set("")
        app.progress_details.set("")
        app.detailed_progress.set(False)
        app._toggle_progress()
        app.status.set("준비되었습니다")
        app.status_detail.set("아이디와 폴더를 확인하고 ‘저장 시작’을 누르세요.")
        self.capture(self.root, f"dashboard-default-ready-{selected}", self.default_main_geometry)
        self.capture(self.root, f"dashboard-ready-{selected}", "1180x880")
        app.running = True
        app._set_running(True)
        app.saved, app.skipped, app.size_skipped = 24, 136, 2
        app._update_counts()
        app.detailed_progress.set(True)
        app._toggle_progress()
        app._update_progress({"downloaded": 6291456, "total": 10485760, "speed": 2097152,
                              "filename": "2030000000000000032_video_1.mp4", "media_type": "video",
                              "author": "moon_studio", "tweet_id": "2030000000000000032", "posted_at": STAMP})
        app.status_detail.set("이미 저장한 파일은 건너뛰고 새 파일만 받습니다.")
        for index in range(4):
            app._log("저장 · " + f"20300000000000000{index + 20:02d}_1.jpg",
                     {"tweet_id": f"20300000000000000{index + 20:02d}", "author": "moon_studio", "posted_at": STAMP})
        self.capture(self.root, f"dashboard-default-running-{selected}", self.default_main_geometry)
        self.capture(self.root, f"dashboard-running-{selected}", "1180x880")
        self.capture(self.root, f"dashboard-compact-{selected}", "820x700")
        self.capture(self.root, f"dashboard-compact-log-{selected}", "820x700", scroll="bottom")
        app.running = False
        app._set_running(False)
        app.progress.configure(mode="determinate", value=0)
        app.status.set("문제가 생겨 멈췄습니다")
        app.status_detail.set("X 요청 한도에 도달했습니다. 22:40 이후 다시 시작해 주세요. 이미 저장한 파일은 유지됩니다.")
        self.capture(self.root, f"dashboard-default-error-{selected}", self.default_main_geometry)
        self.capture(self.root, f"dashboard-error-{selected}", "1180x880")

    def popups(self, selected, session_mock):
        app = self.app
        self.appearance(selected)
        self.root.geometry("1180x880+60+60")
        session_mock.return_value = False
        app._auth_changed()
        app.show_connect()
        self.capture(app.connect_window, f"connect-token-{selected}")
        app.auth.set("cookies.txt 파일")
        self.capture(app.connect_window, f"connect-file-{selected}")
        app._close_connect()
        app.auth.set("인증 토큰 (권장)")
        session_mock.return_value = True
        app._auth_changed()
        app.show_options()
        for index, tab in enumerate(app.option_tabs.tabs()):
            app.option_tabs.select(tab)
            self.capture(app.options_window, f"options-{index + 1}-{selected}", "740x760")
        app._close_options(save=False, restore=True)
        app.show_schedule()
        self.settle(lambda: not app.schedule_busy)
        self.capture(app.schedule_window, f"schedule-{selected}")
        original_schedule = app.schedule_info
        try:
            app.schedule_info = {"registered": False}
            app._show_schedule_info()
            self.capture(app.schedule_window, f"schedule-off-{selected}")
        finally:
            app.schedule_info = original_schedule
            app._show_schedule_info()
        app._close_schedule()
        app.help()
        self.capture(app.help_window, f"help-{selected}", "800x760")
        app.help_window.destroy()
        app.help_window = None

    def library(self, selected):
        app = self.app
        self.appearance(selected)
        app.show_history()
        history = app.history_window
        self.settle(lambda: not history.busy and bool(history.rows), timeout=15)
        history.tabs.select(0)
        first = history.tree.get_children()[0]
        history.tree.selection_set(first)
        history.tree.focus(first)
        history.select_record()
        self.settle(lambda: history._preview_image is not None, timeout=8)
        default_geometry = f"{history.winfo_width()}x{history.winfo_height()}"
        self.capture(history, f"library-default-files-{selected}", default_geometry)
        if not self.simulated_screen:
            self.capture(history, f"library-maximized-files-{selected}", maximized=True)
        self.capture(history, f"library-files-{selected}", "1200x860")
        history.toggle_filters()
        self.capture(history, f"library-filters-{selected}", "1200x860")
        history.toggle_filters()
        self.capture(history, f"library-compact-{selected}", "820x700")
        self.capture(history, f"library-compact-detail-{selected}", "820x700", scroll="bottom")
        history.geometry("1200x860+90+80")
        history.set_view("grid")
        self.settle(lambda: len(history.grid_view.images) >= min(25, len(history.rows)), timeout=12)
        self.capture(history, f"library-gallery-{selected}", "1200x860")
        history.tabs.select(1)
        first_run = history.run_tree.get_children()[0]
        history.run_tree.selection_set(first_run)
        history.select_run()
        self.capture(history, f"library-runs-{selected}", "1200x860")
        history.tabs.select(2)
        self.capture(history, f"library-stats-{selected}", "1200x860")
        self.capture(history, f"library-stats-compact-{selected}", "820x700")
        history.tabs.select(3)
        history.missing_tree.selection_set(history.missing_tree.get_children()[:2])
        history._update_missing_actions()
        self.capture(history, f"library-missing-{selected}", "1200x860")
        self.capture(history, f"library-missing-compact-{selected}", "820x700")
        history.show_ignored.set(True)
        history._show_missing()
        self.capture(history, f"library-missing-hidden-{selected}", "1200x860")
        history.show_ignored.set(False)
        history._show_missing()
        history.tabs.select(0)
        history.show_tools()
        self.capture(history._tools_window, f"library-tools-{selected}")
        history._tools_window.withdraw()
        history.close()
        self.app.history_window = None

    def dialogs(self):
        from xlikesaver import dialogs
        cases = [
            ("dialog-confirm-light", "light", "이전 저장 폴더 연결 확인",
             "이전 폴더를 찾을 수 없습니다.\n새 폴더에서 저장을 시작할까요?\n\n"
             "이전 기록을 가져오지 못해 중복 저장될 수 있습니다.\n"
             "기존 파일과 기록은 보존됩니다. 이전 폴더를 다시 선택하면 기록을 합칠 수 있습니다.", True),
            ("dialog-confirm-dark", "dark", "기존 파일에 출처·작성자 기록",
             "파일 안에 트윗 주소와 작성자 아이디를 기록합니다.\n\n"
             "파일을 다시 쓰므로 Google Drive·OneDrive 폴더라면 다시 업로드될 수 있습니다.\n"
             "미디어는 그대로 보존하며 X에 요청하지 않습니다.\n\n계속할까요?", True),
            ("dialog-long-light", "light", "기록 확인 결과",
             "\n".join(f"파일 {index}: 기록을 확인했습니다." for index in range(1, 80)), False),
        ]
        for name, selected, title, message, question in cases:
            self.appearance(selected)
            options = {"icon": "question", "buttons": (("예", True), ("아니요", False)), "default": 1, "cancel": False} if question else {}
            dialog = dialogs.Dialog(self.root, title, message, **options)
            try:
                dialog._place()
                self.capture(dialog, name)
                if question and dialog.default_button.cget("text") != "아니요":
                    raise RuntimeError("UI QA question did not keep its conservative default")
            finally:
                dialog.destroy()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / ".test-output/ui2/captures")
    parser.add_argument("--themes", nargs="+", choices=("light", "dark"), default=("light", "dark"))
    parser.add_argument("--only", help="Comma-separated screenshot name prefixes")
    parser.add_argument("--scaling", type=float, default=1.3333333333333333)
    parser.add_argument("--screen-size", help="Simulated fixture screen dimensions, e.g. 1366x768; never changes the display")
    args = parser.parse_args()
    simulated_size = None
    if args.screen_size:
        try:
            simulated_size = tuple(map(int, args.screen_size.lower().split("x")))
            if len(simulated_size) != 2 or min(simulated_size) < 600:
                raise ValueError
        except ValueError:
            parser.error("--screen-size needs WIDTHxHEIGHT, both at least 600")
    output = args.output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(ROOT))
    previous_local = os.environ.get("LOCALAPPDATA")
    before_threads = set(threading.enumerate())
    root = app = capture = None
    network_attempts = []
    try:
        with tempfile.TemporaryDirectory(prefix="XlikeSaver-UIQA-") as directory, ExitStack() as stack:
            temporary = Path(directory)
            # Do this before fonts/settings/storage/protection modules load.
            os.environ["LOCALAPPDATA"] = str(temporary / "LocalAppData")
            import tkinter as tk
            from xlikesaver import __version__, dialogs
            from xlikesaver.gui import App
            from xlikesaver.settings import settings_path
            from xlikesaver import scheduled
            def no_network(*_args, **_kwargs):
                network_attempts.append("blocked")
                forbidden()
            for name in ("socket.socket.connect", "socket.socket.connect_ex", "socket.create_connection",
                         "urllib.request.urlopen", "requests.sessions.Session.request"):
                stack.enter_context(patch(name, side_effect=no_network))
            for name in ("webbrowser.open", "os.startfile", "xlikesaver.gui.save_auth_token",
                         "xlikesaver.gui.clear_session", "xlikesaver.gui.App._run_process",
                         "xlikesaver.scheduled.register_task", "xlikesaver.scheduled.unregister_task",
                         "xlikesaver.core.run_worker"):
                stack.enter_context(patch(name, side_effect=forbidden))
            session_mock = stack.enter_context(patch("xlikesaver.gui.has_session", return_value=True))
            stack.enter_context(patch("xlikesaver.history._now", return_value=STAMP))
            scheduled_info = {"registered": True, "current": True, "time": "22:00", "enabled": True,
                              "catch_up": True, "on_battery": True, "next_run": "2026-10-07T22:00:00",
                              "program": f"XlikeSaver-{__version__}.exe"}
            stack.enter_context(patch.object(scheduled, "task_status", side_effect=lambda: dict(scheduled_info)))
            stack.enter_context(patch.object(scheduled, "last_run", return_value={
                "finished_at": STAMP, "status": "finished", "saved": 24, "failed": False}))
            paths, fixture_summary = make_catalog(temporary)
            settings = {"account": "demo_account", "output_dir": str(paths.output_dir), "auth_mode": "managed",
                        "folder_mode": "flat", "include_videos": True, "include_gifs": True,
                        "max_video_mb": 200, "gif_format": "gif", "gif_size": "720", "scan_mode": "quick",
                        "schedule_time": "22:00", "record_locations": {"demo_account": str(paths.output_dir)}}
            settings_file = settings_path()
            settings_file.parent.mkdir(parents=True, exist_ok=True)
            settings_file.write_text(json.dumps(settings, ensure_ascii=False), encoding="utf-8")
            root = tk.Tk()
            root.withdraw()
            if simulated_size:
                screen_width = min(simulated_size[0], root.winfo_screenwidth())
                screen_height = min(simulated_size[1], root.winfo_screenheight())
                stack.enter_context(patch.object(tk.Misc, "winfo_screenwidth", return_value=screen_width))
                stack.enter_context(patch.object(tk.Misc, "winfo_screenheight", return_value=screen_height))
            root.tk.call("tk", "scaling", args.scaling)
            app = App(root)
            app.schedule_info = dict(scheduled_info)
            app._show_schedule_info()
            include = [item.strip() for item in args.only.split(",")] if args.only else []
            capture = CaptureSession(root, app, output, include=include, scale=args.scaling, simulated_screen=bool(simulated_size))
            try:
                def wants(prefixes):
                    return not include or any(name.startswith(prefix) or prefix.startswith(name)
                                              for name in prefixes for prefix in include)
                for selected in args.themes:
                    if wants(("dashboard",)):
                        capture.dashboard(selected)
                    if wants(("connect", "options", "schedule", "help")):
                        capture.popups(selected, session_mock)
                    if wants(("library",)):
                        capture.library(selected)
                if wants(("dialog",)):
                    capture.dialogs()
                if network_attempts:
                    raise RuntimeError("UI QA detected an attempted network call")
                screenshots = capture.manifest
                # A focused rerun updates that subset while keeping the full
                # same-version review manifest and overview sheets intact.
                previous_file = output / "manifest.json"
                if include and previous_file.is_file():
                    previous = json.loads(previous_file.read_text(encoding="utf-8"))
                    if previous.get("version") == __version__:
                        refreshed = {row["name"]: row for row in screenshots}
                        screenshots = [refreshed.pop(row["name"], row) for row in previous.get("screenshots", [])]
                        screenshots.extend(refreshed.values())
                contacts = make_contact_sheets(output, screenshots)
                manifest = {"version": __version__, "fixtures": {"account": "demo_account", "files": fixture_summary["files"],
                            "runs": 112, "temporary_data": True}, "network_attempts": len(network_attempts),
                            "screen": {"width": root.winfo_screenwidth(), "height": root.winfo_screenheight(),
                                       "simulated": bool(simulated_size)}, "default_main_geometry": capture.default_main_geometry,
                            "screenshots": screenshots, "updated_this_run": [row["name"] for row in capture.manifest],
                            "contact_sheets": contacts}
                (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            finally:
                # Stop UI-free decoders before destroying their Tk owner. The
                # capture tool never starts a download or a metadata operation.
                history = app.history_window
                if history is not None and history.winfo_exists():
                    history._preview_loader.close()
                    history.grid_view.release()
                    history.destroy()
                    app.history_window = None
                for thread in threading.enumerate():
                    if thread not in before_threads and thread is not threading.current_thread():
                        thread.join(timeout=5)
                for timer in root.tk.call("after", "info"):
                    root.tk.call("after", "cancel", timer)
                root.destroy()
                app = root = capture = history = None
                gc.collect()
        print(f"UI QA complete: {output / 'manifest.json'}", flush=True)
    finally:
        # Also cover failures during App construction, before the normal
        # capture lifecycle begins. No Tcl interpreter is left behind.
        if root is not None:
            try:
                for timer in root.tk.call("after", "info"):
                    root.tk.call("after", "cancel", timer)
                root.destroy()
            except Exception:
                pass
            app = root = capture = None
            gc.collect()
        if previous_local is None:
            os.environ.pop("LOCALAPPDATA", None)
        else:
            os.environ["LOCALAPPDATA"] = previous_local


if __name__ == "__main__":
    main()

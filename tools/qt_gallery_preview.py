"""Offline Qt gallery experiment using only disposable generated records/media."""

from __future__ import annotations

import argparse
from contextlib import closing
import json
import math
import os
from pathlib import Path
import platform
import sqlite3
import sys
import tempfile
import time

from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def make_fixture(directory, count):
    paths = {}
    database = directory / "fixture.sqlite3"
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("CREATE TABLE photos (ordinal INTEGER PRIMARY KEY, key TEXT UNIQUE, path TEXT, caption TEXT)")
        for index in range(count):
            key = f"{index:016x}"
            path = directory / f"{key}.png"
            size = ((240, 120), (120, 240), (150, 112))[index % 3]
            color = ((index * 37) % 220 + 25, (index * 59) % 220 + 25, (index * 83) % 220 + 25)
            with Image.new("RGBA", size, (240, 234, 255, 0)) as image:
                draw = ImageDraw.Draw(image)
                draw.ellipse((0, 0, size[0] - 1, size[1] - 1), fill=(*color, 210))
                draw.text((8, 8), str(index + 1), fill=(25, 30, 40, 255))
                image.save(path)
            caption = f"@demo_{index:05d} · {index + 1:,}"
            connection.execute("INSERT INTO photos VALUES (?,?,?,?)", (index, key, str(path), caption))
            paths[key] = path
        connection.commit()
    return database, paths


def fixture_reader(database):
    def read(query, offset, limit):
        pattern = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
            total = connection.execute("SELECT COUNT(*) FROM photos WHERE caption LIKE ? ESCAPE '\\'", (pattern,)).fetchone()[0]
            rows = connection.execute("SELECT key,caption FROM photos WHERE caption LIKE ? ESCAPE '\\' ORDER BY ordinal LIMIT ? OFFSET ?",
                                      (pattern, limit, offset)).fetchall()
        return total, [{"key": key, "caption": caption} for key, caption in rows]
    return read


def working_set_bytes():
    """Windows process working set, or None where that counter is unavailable."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    class Counters(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    counters = Counters()
    counters.cb = ctypes.sizeof(Counters)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    measure = kernel.K32GetProcessMemoryInfo
    measure.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
    measure.restype = wintypes.BOOL
    if not measure(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
        return None
    return int(counters.WorkingSetSize)


def _input_union():
    import ctypes
    from ctypes import wintypes

    pointer = ctypes.c_size_t

    class Mouse(ctypes.Structure):
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                    ("flags", wintypes.DWORD), ("time", wintypes.DWORD), ("extra", pointer)]

    class Key(ctypes.Structure):
        _fields_ = [("vk", wintypes.WORD), ("scan", wintypes.WORD), ("flags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("extra", pointer)]

    class Union(ctypes.Union):
        _fields_ = [("mouse", Mouse), ("key", Key)]

    class Event(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("value", Union)]

    return ctypes, Event


def send_wheel_notch(notches):
    """Deliver one Windows wheel movement to the foreground window."""
    ctypes, event_type = _input_union()
    event = event_type()
    event.type = 0
    event.value.mouse.mouseData = ctypes.c_uint32(notches * 120).value
    event.value.mouse.flags = 0x0800
    sent = ctypes.windll.user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(event))
    return sent == 1


def send_key(virtual_key):
    ctypes, event_type = _input_union()
    user32 = ctypes.windll.user32
    down, up = event_type(), event_type()
    down.type = up.type = 1
    down.value.key.vk = up.value.key.vk = virtual_key
    up.value.key.flags = 0x0002
    return user32.SendInput(2, (event_type * 2)(down, up), ctypes.sizeof(event_type)) == 2


def place_cursor_over(window, item):
    import ctypes
    from PySide6.QtCore import QPointF

    from ctypes import wintypes

    origin = window.mapToGlobal(QPointF(0, 0))
    center = item.mapToGlobal(QPointF(item.width() / 2, item.height() / 2))
    fraction_x = min(1.0, max(0.0, (center.x() - origin.x()) / max(1.0, float(window.width()))))
    fraction_y = min(1.0, max(0.0, (center.y() - origin.y()) / max(1.0, float(window.height()))))
    user32 = ctypes.windll.user32
    user32.GetForegroundWindow.restype = ctypes.c_void_p
    user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
    user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
    user32.GetClientRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.RECT)]
    user32.GetClientRect.restype = wintypes.BOOL
    user32.ClientToScreen.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.POINT)]
    user32.ClientToScreen.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    user32.AttachThreadInput.restype = wintypes.BOOL
    user32.BringWindowToTop.argtypes = [ctypes.c_void_p]
    user32.BringWindowToTop.restype = wintypes.BOOL
    kernel32 = ctypes.windll.kernel32
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD
    previous = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(previous))
    hwnd = int(window.winId())
    foreground_hwnd = user32.GetForegroundWindow()
    foreground_thread = user32.GetWindowThreadProcessId(foreground_hwnd, None)
    this_thread = kernel32.GetCurrentThreadId()
    attached = bool(foreground_thread and foreground_thread != this_thread and user32.AttachThreadInput(this_thread, foreground_thread, True))
    window.show()
    window.raise_()
    window.requestActivate()
    user32.BringWindowToTop(hwnd)
    user32.SetForegroundWindow(hwnd)
    if attached:
        user32.AttachThreadInput(this_thread, foreground_thread, False)
    foreground = int(user32.GetForegroundWindow() or 0) == hwnd
    client_origin = wintypes.POINT(0, 0)
    client_rect = wintypes.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(client_rect))
    user32.ClientToScreen(hwnd, ctypes.byref(client_origin))
    native = (client_origin.x + int(client_rect.right * fraction_x), client_origin.y + int(client_rect.bottom * fraction_y))
    user32.WindowFromPhysicalPoint.argtypes = [wintypes.POINT]
    user32.WindowFromPhysicalPoint.restype = ctypes.c_void_p
    user32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    user32.GetAncestor.restype = ctypes.c_void_p

    def covers(x, y):
        found = user32.WindowFromPhysicalPoint(wintypes.POINT(int(x), int(y)))
        root = user32.GetAncestor(found, 2) if found else None
        return int(root or found or 0) == hwnd

    point = native
    if foreground:
        from PySide6.QtGui import QCursor
        QCursor.setPos(center.toPoint())
        placed = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(placed))
        if covers(placed.x, placed.y):
            point = (placed.x, placed.y)
        elif covers(*native):
            user32.SetCursorPos(*native)
            point = native
    return foreground, (previous.x, previous.y), point


def restore_cursor(position):
    import ctypes
    ctypes.windll.user32.SetCursorPos(int(position[0]), int(position[1]))


def wheel_travel(positions):
    deltas = [b - a for a, b in zip(positions, positions[1:])]
    moved = [value for value in deltas if abs(value) >= 0.5]
    median = sorted(moved)[len(moved) // 2] if moved else 0
    return {"events": len(deltas), "moved": len(moved), "median_px": round(median, 1), "total_px": round(sum(deltas), 1)}


def timing_summary(values):
    ordered = sorted(values)
    if not ordered:
        return {"samples": 0, "p95_ms": None, "max_ms": None}
    return {"samples": len(ordered), "p95_ms": round(ordered[min(len(ordered) - 1, math.ceil(len(ordered) * .95) - 1)], 3),
            "max_ms": round(ordered[-1], 3)}


def run(options):
    try:
        from PySide6.QtCore import QTimer, Qt, qInstallMessageHandler, qVersion
        from PySide6.QtGui import QGuiApplication
        from PySide6.QtQml import QQmlApplicationEngine
        from PySide6.QtQuick import QQuickItem
        from PySide6.QtQuickControls2 import QQuickStyle
        from shiboken6 import delete
        from luvbox.qt_gallery import GalleryModel, SafeThumbnailProvider
        from luvbox.qt_wheel import WheelInputFilter
    except ImportError:
        raise SystemExit("Qt 실험 의존성이 없습니다. .venv/Scripts/python.exe -m pip install -r requirements-qt.txt")

    if options.qt_scale != 1:
        os.environ["QT_SCALE_FACTOR"] = f"{options.qt_scale:g}"
    messages = []
    qInstallMessageHandler(lambda kind, context, message: messages.append({"type": int(kind), "message": message}))
    QQuickStyle.setStyle("Fusion")
    app = QGuiApplication([sys.argv[0]])
    app.setApplicationName("LuvBox Qt gallery prototype")
    report = {"ok": False, "fixture_only": True, "real_accounts_accessed": False, "rows": options.rows,
              "python": platform.python_version(), "qt": qVersion(), "physical_display_fps_measured": False,
              "input_method": ("injected SendInput wheel" if options.wheel_check
                               else "programmatic contentY sweep" if options.benchmark else "interactive preview"),
              "qt_scale_factor": options.qt_scale, "os_display_scale_changed": False,
              "first_visible_definition": "engine-loaded to first all-ready visible sample; sampled every 100ms",
              "ready_fraction_sample_ms": 100,
              "thumbnail_size": options.size,
              "delegate_cache": "one cell, unchanged",
              "production_ui_replaced": False,
              "measurement_definitions": {
                  "queue_wait_ms": "Enqueue until a decode worker starts the job.",
                  "decode_ms": "Decoder call only, excluding the queue and the QImage copy.",
                  "delivery_ms": "Copy of a cached QImage inside the image provider response.",
                  "upload_request_ms": "Ready notification until requestImage. This is not GPU texture upload or a physical display interval.",
                  "cancelled": "Queued thumbnail jobs dropped before decode because they left the focus window.",
                  "rss_working_set_bytes": "Windows process working set during the scroll. Includes Python and Qt, not thumbnail bytes alone.",
                  "moving_ready_fraction": "ready/visible at each programmatic scroll step. The visible counters themselves refresh every 100ms.",
              }}
    with tempfile.TemporaryDirectory(prefix="luvbox-qt-gallery-") as folder:
        directory = Path(folder)
        database, paths = make_fixture(directory, options.rows)
        provider = SafeThumbnailProvider()
        for key, path in paths.items():
            provider.register(key, path)
        model = GalleryModel(fixture_reader(database), provider=provider, batch_size=200)
        wheel_input = WheelInputFilter()
        engine = QQmlApplicationEngine()
        engine.addImageProvider("thumbs", provider)
        engine.setInitialProperties({"galleryModel": model, "wheelInputFilter": wheel_input})
        engine.load(ROOT / "assets/qt/GalleryPreview.qml")
        if not engine.rootObjects():
            wheel_input.close()
            model.shutdown()
            provider.close()
            return {**report, "error": "QML scene failed to load", "qt_messages": messages}
        window = engine.rootObjects()[0]
        report["default_screen_is_save"] = window.property("currentScreen") == 0
        available = window.screen().availableGeometry()
        if int(window.property("width")) > available.width() - 16 or int(window.property("height")) > available.height() - 16:
            window.setProperty("x", available.x() + 8)
            window.setProperty("y", available.y() + 8)
            window.setProperty("width", min(int(window.property("width")), max(int(window.property("minimumWidth")), available.width() - 16)))
            window.setProperty("height", min(int(window.property("height")), max(int(window.property("minimumHeight")), available.height() - 16)))
        window.setProperty("densityChoice", ("small", "normal", "large").index(options.size))
        grid = window.findChild(QQuickItem, "galleryGrid")
        started = time.perf_counter()
        model.search("")
        heartbeats, frame_intervals, ready_fractions, working_sets = [], [], [], []
        state = {"phase": "loading", "last_heartbeat": started, "last_frame": None, "step": 0,
                 "first_visible_ms": None, "scroll_started": None, "scroll_finished": None}

        def heartbeat():
            now = time.perf_counter()
            elapsed = (now - state["last_heartbeat"]) * 1000
            state["last_heartbeat"] = now
            if state["phase"] in ("scroll", "wheel-down", "wheel-up"):
                heartbeats.append(elapsed)

        def frame():
            now = time.perf_counter()
            if state["phase"] in ("scroll", "wheel-down", "wheel-up") and state["last_frame"] is not None:
                frame_intervals.append((now - state["last_frame"]) * 1000)
            state["last_frame"] = now

        def capture():
            if options.capture:
                options.capture.parent.mkdir(parents=True, exist_ok=True)
                if not window.grabWindow().save(str(options.capture)):
                    raise RuntimeError("Qt capture failed")

        def grab_during(fraction, content_y):
            if options.capture is None or fraction >= 1:
                return
            grabs = state.setdefault("grabs", [])
            if len(grabs) >= 2 or (grabs and fraction >= grabs[-1]["ready_fraction"]):
                return
            path = options.capture.with_name(f"{options.capture.stem}-during-{len(grabs) + 1}.png")
            path.parent.mkdir(parents=True, exist_ok=True)
            if window.grabWindow().save(str(path)):
                grabs.append({"path": path.name, "ready_fraction": round(fraction, 3), "content_y": round(content_y, 1)})

        def exercise(now):
            if now - started > options.timeout_seconds:
                report["error"] = "prototype timeout"
                if state.get("cursor") and not state.get("cursor_restored"):
                    restore_cursor(state["cursor"])
                    state["cursor_restored"] = True
                app.quit()
                return
            if model.error:
                report["error"] = model.error
                app.quit()
                return
            visible, ready = window.property("visibleTiles"), window.property("readyTiles")
            if state["first_visible_ms"] is None and visible and visible == ready:
                state["first_visible_ms"] = round((now - started) * 1000, 3)
            phase = state["phase"]
            if phase == "loading":
                if model.count < model.total and not model.busy:
                    model.loadMore()
                if model.count == options.rows and state["first_visible_ms"] is not None:
                    report["loaded_metadata_rows"] = model.count
                    report["initial_provider"] = provider.snapshot()
                    grid.forceActiveFocus()
                    foreground, previous_cursor, point = place_cursor_over(window, grid)
                    state.update(foreground=foreground, cursor=previous_cursor, cursor_target=point)
                    if not foreground:
                        report["error"] = "gallery window was not the foreground window; wheel input was not sent"
                        restore_cursor(previous_cursor)
                        state["cursor_restored"] = True
                        sweep.stop()
                        app.quit()
                        return
                    state.update(phase="wheel-down", step=0, scroll_started=now, positions=[], ready=[])
                return
            if phase in ("wheel-down", "wheel-up"):
                content_y = grid.property("contentY")
                state["positions"].append(content_y)
                if visible:
                    fraction = ready / visible
                    state["ready"].append(fraction)
                    ready_fractions.append(fraction)
                    if fraction < 1:
                        state["blank_samples"] = state.get("blank_samples", 0) + 1
                    grab_during(fraction, content_y)
                resident = working_set_bytes()
                if resident is not None:
                    working_sets.append(resident)
                if state["step"] >= options.wheel_notches:
                    if phase == "wheel-down" and len(set(round(value) for value in state["positions"])) < 2:
                        report["error"] = "injected wheel did not move the gallery"
                        restore_cursor(state["cursor"])
                        state["cursor_restored"] = True
                        app.quit()
                        return
                    state["step"] = 0
                    report[phase.replace("-", "_")] = wheel_travel(state["positions"])
                    state["positions"].clear()
                    if phase == "wheel-down":
                        state["phase"] = "wheel-up"
                        return
                    grid.forceActiveFocus()
                    grid.setProperty("currentIndex", 0)
                    sweep.setInterval(40)
                    state.update(phase="keys", scroll_finished=now, key_index=0)
                    return
                if not send_wheel_notch((-options.wheel_step if phase == "wheel-down" else options.wheel_step)):
                    report["error"] = "Windows wheel input was not accepted"
                    restore_cursor(state["cursor"])
                    state["cursor_restored"] = True
                    app.quit()
                    return
                state["step"] += 1
                return
            if phase == "keys":
                if state["step"] < 4:
                    if not send_key(0x28):
                        report["error"] = "keyboard input was not accepted"
                        app.quit()
                        return
                    state["step"] += 1
                    return
                if state["step"] == 4:
                    state["step"] += 1
                    return
                report["keyboard"] = {"before_index": state["key_index"], "after_index": grid.property("currentIndex"),
                                      "downs": 4}
                state.update(phase="density", step=0)
                return
            if phase == "density":
                if state["step"] >= 36:
                    restore_cursor(state["cursor"])
                    state["cursor_restored"] = True
                    state["phase"] = "settling"
                    return
                choice = (0, 2, 1)[state["step"] // 12]
                slot = state["step"] % 12
                if slot == 0:
                    window.setProperty("densityRequest", choice)
                elif slot == 11:
                    anchor, top = window.property("heldAnchor"), window.property("topIndex")
                    cell = grid.property("cellWidth") or 0
                    columns = max(1, int(grid.width() // cell)) if cell else 0
                    report.setdefault("density", []).append({
                        "choice": choice, "anchor": anchor, "top_index": top, "columns": columns,
                        "content_y": round(grid.property("contentY") or 0, 1)})
                state["step"] += 1
                return
            if phase == "settling":
                keyboard = report.get("keyboard") or {}
                density = report.get("density") or []
                moved = keyboard.get("after_index", -1) > keyboard.get("before_index", -1)

                def held(item):
                    columns, anchor, top = item.get("columns") or 0, item.get("anchor"), item.get("top_index")
                    return bool(columns) and anchor is not None and top is not None and anchor >= 0 and top == anchor - (anchor % columns)

                anchored = bool(density) and all(held(item) for item in density)
                report["controls_observed"] = {"keyboard_index_increased": moved, "density_kept_top_index": anchored}
                report["ready_fraction_min"] = min(ready_fractions) if ready_fractions else None
                report["blank_samples"] = state.get("blank_samples", 0)
                report["framebuffer_grabs"] = state.get("grabs", [])
                report["framebuffer_grab_meaning"] = "Qt window framebuffer during the injected wheel. Not a photograph of the monitor."
                capture()
                report["ok"] = bool(moved and anchored and ready_fractions)
                app.quit()

        def advance():
            if options.wheel_check:
                try:
                    exercise(time.perf_counter())
                except Exception as error:
                    report["error"] = f"{type(error).__name__}: {error}"
                    sweep.stop()
                    app.quit()
                return
            now = time.perf_counter()
            if now - started > options.timeout_seconds:
                report["error"] = "prototype timeout"
                app.quit()
                return
            if model.error:
                report["error"] = model.error
                app.quit()
                return
            visible, ready = window.property("visibleTiles"), window.property("readyTiles")
            if state["first_visible_ms"] is None and visible and visible == ready:
                state["first_visible_ms"] = round((now - started) * 1000, 3)
            if state["phase"] == "loading":
                if model.count < model.total and not model.busy:
                    model.loadMore()
                if model.count == options.rows and state["first_visible_ms"] is not None:
                    report["loaded_metadata_rows"] = model.count
                    report["initial_provider"] = provider.snapshot()
                    state.update(phase="scroll", scroll_started=now, last_frame=None)
                    sweep.setInterval(options.scroll_ms)
                return
            if state["phase"] == "scroll":
                step = state["step"]
                distance = max(0, grid.property("contentHeight") - grid.property("height"))
                fraction = step / options.scroll_steps if step <= options.scroll_steps else (2 * options.scroll_steps - step) / options.scroll_steps
                grid.setProperty("contentY", distance * max(0, fraction))
                if visible:
                    ready_fractions.append(ready / visible)
                resident = working_set_bytes()
                if resident is not None:
                    working_sets.append(resident)
                state["step"] += 1
                if state["step"] > 2 * options.scroll_steps:
                    state.update(phase="settling", scroll_finished=now)
                    sweep.setInterval(50)
                return
            if state["phase"] == "settling" and now - state["scroll_finished"] >= .2 and visible and ready == visible:
                capture()
                report["ok"] = True
                app.quit()

        pulse = QTimer()
        pulse.setTimerType(Qt.TimerType.PreciseTimer)
        pulse.setInterval(10)
        pulse.timeout.connect(heartbeat)
        window.frameSwapped.connect(frame)
        sweep = QTimer()
        sweep.setInterval(30)
        sweep.timeout.connect(advance)
        if options.benchmark or options.wheel_check:
            window.setProperty("currentScreen", 1)
            if options.wheel_check:
                window.setProperty("sampleOnScroll", True)
                report["ready_fraction_sample_ms"] = options.wheel_ms
                report["ready_fraction_sampled_on"] = "each contentY change during the injected wheel"
                sweep.setInterval(options.wheel_ms)
            pulse.start()
            sweep.start()
        app.exec()
        pulse.stop()
        sweep.stop()
        report.update(first_visible_ms=state["first_visible_ms"], heartbeat=timing_summary(heartbeats),
                      renderer=window.rendererInterface().graphicsApi().name,
                      viewport={"width": grid.width(), "height": grid.height(), "device_pixel_ratio": window.devicePixelRatio()},
                      frame_callback_intervals=timing_summary(frame_intervals),
                      scroll_steps_per_direction=options.scroll_steps, nominal_scroll_ms=2 * options.scroll_steps * options.scroll_ms,
                      actual_scroll_ms=round((state["scroll_finished"] - state["scroll_started"]) * 1000, 3) if state["scroll_finished"] else None,
                      moving_ready_fraction=sum(ready_fractions) / len(ready_fractions) if ready_fractions else None,
                      rss_working_set={"samples": len(working_sets),
                                       "max_bytes": max(working_sets) if working_sets else None,
                                       "end_bytes": working_sets[-1] if working_sets else None},
                      grid={"cell_width": grid.property("cellWidth"), "cell_height": grid.property("cellHeight"),
                            "content_height": grid.property("contentHeight"), "content_width": grid.property("contentWidth")},
                      peak_delegates=window.property("peakDelegates"), provider=provider.snapshot(), qt_messages=messages)
        screen = window.screen()
        report["screen"] = {"name": screen.name(), "device_pixel_ratio": screen.devicePixelRatio(),
                            "logical_dpi": screen.logicalDotsPerInch(), "physical_dpi": screen.physicalDotsPerInch(),
                            "width": screen.size().width(), "height": screen.size().height()}
        if not options.benchmark and not options.wheel_check:
            report["ok"] = True
        window.close()
        wheel_input.close()
        report["catalog_worker_stopped"] = model.shutdown()
        provider.close()
        delete(engine)
        app.processEvents()
        report["thumbnail_worker_idle"] = provider.wait_for_idle(10)
        report["provider_after_close"] = provider.snapshot()
        report["ok"] = report["ok"] and report["catalog_worker_stopped"] and report["thumbnail_worker_idle"] and not any(message["type"] in (1, 2, 3) for message in messages)
        probe = next(iter(paths.values()))
        renamed = probe.with_suffix(".renamed")
        probe.rename(renamed)
        renamed.rename(probe)
        report["fixture_file_lock_released"] = True
    report["fixture_directory_removed"] = not directory.exists()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=1000)
    parser.add_argument("--benchmark", action="store_true")
    parser.add_argument("--wheel-check", action="store_true")
    parser.add_argument("--wheel-notches", type=int, default=24)
    parser.add_argument("--wheel-ms", type=int, default=40)
    parser.add_argument("--wheel-step", type=int, default=1, help="notches delivered in each injected wheel event")
    parser.add_argument("--qt-scale", type=float, default=1.0, help="QT_SCALE_FACTOR for this process only")
    parser.add_argument("--size", choices=("small", "normal", "large"), default="normal")
    parser.add_argument("--scroll-steps", type=int, default=120)
    parser.add_argument("--scroll-ms", type=int, default=24)
    parser.add_argument("--timeout-seconds", type=int, default=180)
    parser.add_argument("--capture", type=Path)
    parser.add_argument("--output", type=Path)
    options = parser.parse_args()
    if not 1 <= options.rows <= 10000 or min(options.scroll_steps, options.scroll_ms, options.timeout_seconds, options.wheel_notches, options.wheel_ms) < 1:
        parser.error("rows must be 1..10000; timing parameters must be positive")
    if not 1 <= options.wheel_step <= 40 or not any(math.isclose(options.qt_scale, allowed) for allowed in (1, 1.25, 1.5, 2)):
        parser.error("wheel-step must be 1..40 and qt-scale must be 1, 1.25, 1.5, or 2")
    if options.benchmark and options.wheel_check:
        parser.error("benchmark and wheel-check are separate runs")
    report = run(options)
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if options.output:
        options.output.parent.mkdir(parents=True, exist_ok=True)
        options.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

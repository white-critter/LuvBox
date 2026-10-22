"""Measure gallery scheduling and preview retention using generated local media.

Run: python tools/benchmark_gallery.py --rows 10000 --unique --scroll
Requires a desktop/display. Heartbeats measure the event loop, not display FPS.
Fixture generation and theme setup are outside the measured phases. --unique
uses different files, aspect ratios and alpha; no accounts or real media are read.
--unique-captions also exercises new author/date text instead of cached captions.
--trace-scheduler adds optional Linux scheduler evidence at heartbeat boundaries.
"""

from __future__ import annotations

import argparse
from collections import deque
import gc
import json
import platform
import sys
import tempfile
import threading
import time
import tkinter as tk
from pathlib import Path

from PIL import Image, ImageDraw, ImageTk

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from luvbox import theme
from luvbox.gallery import TILE, ThumbnailGrid


def _cgroup_cpu():
    """Optional cgroup-v2 counters; the scope can include other task processes."""
    if sys.platform != "linux":
        return None
    root = Path("/sys/fs/cgroup")
    try:
        lines = (Path("/proc/self/cgroup").read_text()).splitlines()
        relative = next(line[3:] for line in lines if line.startswith("0::"))
        location = root / relative.lstrip("/")
        if (location / "cpu.stat").is_file():
            root = location
    except (OSError, StopIteration):
        pass
    try:
        counters = {}
        for line in (root / "cpu.stat").read_text().splitlines():
            key, value = line.split()
            if value.isdecimal():
                counters[key] = int(value)
        quota, period = (root / "cpu.max").read_text().split()
        return {"scope": "shared_execution_cgroup", "quota_us": None if quota == "max" else int(quota),
                "period_us": int(period), "stat": counters}
    except (OSError, ValueError):
        return None


class _ThreadScheduler:
    """Sample only the current UI thread; no per-image diagnostic I/O."""

    def __init__(self):
        self.enabled = sys.platform == "linux"
        self.getrusage = self.rusage_thread = None
        self.schedstat_readable = self.positive_wait_observed = False
        self.schedstats_enabled = None
        self.samples = 0
        self.read_ms = self.max_read_ms = 0.
        self.totals, self.maxima = {}, {}
        if self.enabled:
            try:
                self.schedstats_enabled = bool(int(Path("/proc/sys/kernel/sched_schedstats").read_text()))
            except (OSError, ValueError):
                pass
            try:
                import resource
                self.getrusage, self.rusage_thread = resource.getrusage, resource.RUSAGE_THREAD
            except (ImportError, AttributeError):
                pass
        self.previous = self._read() if self.enabled else None
        self.previous_time = time.perf_counter()

    def _read(self):
        before = time.perf_counter()
        values = {}
        try:
            run, wait, slices = map(int, Path("/proc/thread-self/schedstat").read_text().split())
            if min(run, wait, slices) >= 0:
                values.update(runtime_ms=run / 1_000_000, runnable_wait_ms=wait / 1_000_000,
                              timeslices=slices)
                self.schedstat_readable = True
                self.positive_wait_observed |= wait > 0
        except (OSError, ValueError):
            pass
        if self.getrusage is not None:
            try:
                usage = self.getrusage(self.rusage_thread)
                values.update(voluntary_switches=usage.ru_nvcsw, involuntary_switches=usage.ru_nivcsw)
            except (OSError, ValueError):
                pass
        elapsed = (time.perf_counter() - before) * 1000
        self.read_ms += elapsed
        self.max_read_ms = max(self.max_read_ms, elapsed)
        return values

    def delta(self):
        if not self.enabled:
            return None
        current = self._read()
        now = time.perf_counter()
        result = {"sample_interval_ms": round((now - self.previous_time) * 1000, 3)}
        for name in ("runtime_ms", "runnable_wait_ms", "timeslices", "voluntary_switches", "involuntary_switches"):
            old, new = self.previous.get(name), current.get(name)
            difference = new - old if old is not None and new is not None else None
            # Unsupported/reset counters are unknown, not evidence of no wait.
            if difference is not None and difference >= 0:
                result[name] = round(difference, 3) if name.endswith("_ms") else difference
                self.totals[name] = self.totals.get(name, 0) + difference
                self.maxima[name] = max(self.maxima.get(name, 0), difference)
            else:
                result[name] = None
        self.previous, self.previous_time = current, now
        self.samples += 1
        return result

    def summary(self):
        return {"samples": self.samples, "schedstat_readable": self.schedstat_readable,
                "kernel_schedstats_enabled": self.schedstats_enabled,
                "positive_runnable_wait_observed": self.positive_wait_observed,
                "rusage_thread_available": self.getrusage is not None,
                "counter_totals": {key: round(value, 3) for key, value in self.totals.items()},
                "counter_maxima": {key: round(value, 3) for key, value in self.maxima.items()},
                "counter_read_total_ms": round(self.read_ms, 3), "counter_read_max_ms": round(self.max_read_ms, 3),
                "notes": ["Counters are sampled near UI heartbeat boundaries, not inside every traced call.",
                          "Runnable wait counts time ready to run but not scheduled; blocked/sleeping time is different.",
                          "A zero wait counter alone does not prove no contention if kernel accounting cannot be confirmed.",
                          "Context switches do not establish why a thread blocked, or prove GIL contention."]}


class _SlowTrace:
    """Optional bounded diagnostics. It never records file paths or Tk objects."""

    def __init__(self, started, phase, *, threshold_ms=16.7, capacity=1000, scheduler=False):
        self.started, self.phase = started, phase
        self.threshold_ms = threshold_ms
        self.main_ident = threading.get_ident()
        self.spans = deque(maxlen=capacity)
        self.gc_events = deque(maxlen=256)
        self.dropped = self.gc_dropped = 0
        self.summaries = {}
        self.gc_summaries = {}
        self.gc_starts = {}
        self.worker_decode_cpu = 0.
        self.worker_active = None
        self.enabled = True
        self.scheduler = _ThreadScheduler() if scheduler and sys.platform == "linux" else None
        self.cgroup_before = _cgroup_cpu()
        gc.callbacks.append(self._gc_callback)

    def sample(self):
        return (time.perf_counter(), time.thread_time(), time.process_time(),
                self.phase(), threading.get_ident(), self.worker_decode_cpu)

    def record(self, kind, start, end=None, *, details=None):
        if not self.enabled:
            return None
        end = self.sample() if end is None else end
        wall, cpu, process = ((end[index] - start[index]) * 1000 for index in range(3))
        summary = self.summaries.get(kind)
        if summary is None:
            summary = self.summaries[kind] = {"count": 0, "wall_ms": 0., "thread_cpu_ms": 0.,
                                            "process_cpu_ms": 0., "max_wall_ms": 0.}
        summary["count"] += 1
        summary["wall_ms"] += wall
        summary["thread_cpu_ms"] += cpu
        summary["process_cpu_ms"] += process
        summary["max_wall_ms"] = max(summary["max_wall_ms"], wall)
        if kind == "worker.decode":
            self.worker_decode_cpu += cpu
        if wall <= self.threshold_ms and kind != "gc.collection":
            return None
        main = start[4] == self.main_ident
        event = {"kind": kind, "phase": start[3], "end_phase": end[3],
                 "thread": "main" if main else "worker",
                 "start_ms": round((start[0] - self.started) * 1000, 3),
                 "end_ms": round((end[0] - self.started) * 1000, 3),
                 "perf_start_s": round(start[0], 9), "perf_end_s": round(end[0], 9),
                 "wall_ms": round(wall, 3), "thread_cpu_ms": round(cpu, 3),
                 "main_thread_cpu_ms": round(cpu, 3) if main else None,
                 "worker_thread_cpu_ms": None if main else round(cpu, 3),
                 "process_cpu_ms": round(process, 3),
                 "completed_worker_decode_cpu_ms": round(end[5] - start[5], 3)}
        if details:
            event.update(details)
        if kind == "heartbeat" and self.worker_active is not None:
            event["worker_decode_active_since_ms"] = round((self.worker_active - self.started) * 1000, 3)
        if wall > self.threshold_ms:
            if len(self.spans) == self.spans.maxlen:
                self.dropped += 1
            self.spans.append(event)
        return event

    def call(self, kind, function, *args, details=None, **kwargs):
        if not self.enabled:
            return function(*args, **kwargs)
        start = self.sample()
        if kind == "worker.decode":
            self.worker_active = start[0]
        try:
            return function(*args, **kwargs)
        finally:
            self.record(kind, start, details=details)
            if kind == "worker.decode":
                self.worker_active = None

    def _gc_callback(self, stage, info):
        # No locks: a collection can interrupt trace bookkeeping on either
        # thread. These append-only diagnostics do not acquire application locks.
        if not self.enabled:
            return
        identity = threading.get_ident()
        if stage == "start":
            self.gc_starts[identity] = self.sample()
            return
        start = self.gc_starts.pop(identity, None)
        if start is None:
            return
        end = self.sample()
        generation = info.get("generation", -1)
        event = self.record("gc.collection", start, end,
                            details={"generation": generation, "collected": info.get("collected", 0),
                                     "uncollectable": info.get("uncollectable", 0)})
        if event is None:
            return
        totals = self.gc_summaries.setdefault(str(generation), {"count": 0, "wall_ms": 0.,
                                                                "thread_cpu_ms": 0., "max_wall_ms": 0.})
        totals["count"] += 1
        totals["wall_ms"] += event["wall_ms"]
        totals["thread_cpu_ms"] += event["thread_cpu_ms"]
        totals["max_wall_ms"] = max(totals["max_wall_ms"], event["wall_ms"])
        if len(self.gc_events) == self.gc_events.maxlen:
            self.gc_dropped += 1
        self.gc_events.append(event)

    def finish(self):
        self.close()
        after = _cgroup_cpu()
        delta = None
        if self.cgroup_before is not None and after is not None:
            delta = {key: value - self.cgroup_before["stat"].get(key, 0)
                     for key, value in after["stat"].items()}
        return {"threshold_ms": self.threshold_ms, "capacity": self.spans.maxlen,
                "slow_spans": sorted(self.spans, key=lambda event: event["start_ms"]),
                "dropped_slow_spans": self.dropped,
                "gc_collections": list(self.gc_events), "dropped_gc_collections": self.gc_dropped,
                "gc_by_generation": self.gc_summaries,
                "span_totals": {kind: {key: round(value, 3) if isinstance(value, float) else value
                                        for key, value in summary.items()}
                                for kind, summary in self.summaries.items()},
                "cgroup_cpu_before": self.cgroup_before, "cgroup_cpu_after": after,
                "cgroup_cpu_stat_delta": delta,
                "main_thread_scheduler": self.scheduler.summary() if self.scheduler is not None else None,
                "notes": ["Spans are inclusive and may overlap; do not add their wall times.",
                          "Heartbeat CPU deltas cover the time since the prior heartbeat callback.",
                          "Completed worker CPU accounts decoder calls only and can cross heartbeat boundaries.",
                          "GC retains the last 256 collections; slow collections also appear in slow_spans.",
                          "cgroup counters can include other processes sharing the execution environment."]}

    def close(self):
        self.enabled = False
        if self._gc_callback in gc.callbacks:
            gc.callbacks.remove(self._gc_callback)


def percentile(values, fraction):
    if not values:
        return None
    return round(sorted(values)[min(len(values) - 1, int(len(values) * fraction))], 3)


def timing_summary(values):
    return {"samples": len(values), "p50": percentile(values, .5), "p95": percentile(values, .95),
            "max": round(max(values), 3) if values else None,
            "over_16_7": sum(value > 16.7 for value in values)}


def _records(directory, count, decoded, unique, unique_captions=False):
    """Create fixtures before starting the UI timers, never scan user media."""
    paths = []
    if decoded:
        for index in range(count if unique else 1):
            size = ((240, 120), (120, 240), TILE)[index % 3] if unique else TILE
            color = ((index * 37 + 70) % 256, (index * 19 + 90) % 256, (index * 53 + 110) % 256)
            path = directory / f"synthetic-{index}.png"
            with Image.new("RGBA", size, (240, 234, 255, 0 if unique else 255)) as image:
                draw = ImageDraw.Draw(image)
                draw.ellipse((0, 0, size[0] - 1, size[1] - 1), fill=(*color, 210 if unique else 255))
                image.save(path)
            paths.append(str(path))
    rows = []
    for index in range(count):
        record = {"author": f"demo_{index % 24:02}", "posted_at": f"2026-10-{index % 27 + 1:02}"}
        if unique_captions:
            record.update(author=f"creator{index:08}",
                          posted_at=f"{2000 + index // 336:04}-{index // 28 % 12 + 1:02}-{index % 28 + 1:02}")
        if decoded:
            record["path"] = paths[index] if unique else paths[0]
        rows.append((str(index), record))
    return rows


def _visible_keys(grid):
    if not grid.columns or grid.canvas.winfo_height() <= 1:
        return ()
    top = max(0, grid.canvas.canvasy(0))
    first = int(top // grid._cell_height) * grid.columns
    last = (int((top + grid.canvas.winfo_height()) // grid._cell_height) + 1) * grid.columns
    return grid.order[first:last]


def _visible_settled(grid):
    keys = _visible_keys(grid)
    valid = getattr(grid, "_valid_sources", None)
    if valid is not None:
        return bool(keys) and all(not grid._paths.get(key) or key in valid for key in keys)
    return bool(keys) and all(not grid._paths.get(key) or key in grid._source_images
                              or key in grid._unavailable or key in grid.images for key in keys)


def _preview_snapshot(grid):
    """Observed image references; excludes Tk storage and decoder local work."""
    sources = [image for image in grid._source_images.values() if image is not None]
    with grid._decoder.condition:
        worker = list(grid._decoder.cache.values())
    with grid._results.mutex:
        queued = [result[2] for result in grid._results.queue if result[2] is not None]
        queue_size = len(grid._results.queue)
    images = {id(image): image for image in (*sources, *worker, *queued)}
    tiles = (*grid.tiles.values(), *grid._tile_pool)
    pooled_previews = {id(tile._preview_image) for tile in tiles
                       if getattr(tile, "_preview_image", None) is not None}
    tk_images = len(pooled_previews) if any(hasattr(tile, "_preview_image") for tile in tiles) else len(grid.images)
    visible = [key for key in _visible_keys(grid) if grid._paths.get(key)]
    valid = getattr(grid, "_valid_sources", grid._source_images)
    ready = sum(key in valid and key in grid.images for key in visible)
    failed = sum(key in valid and grid._source_images.get(key) is None for key in visible)
    failed_sources = sum(key in valid and bool(grid._paths.get(key)) and image is None
                         for key, image in grid._source_images.items())
    # PIL byte estimates cover pixel buffers only, not Python/decoder/Tk overhead.
    pixels = sum(image.width * image.height * len(image.getbands()) for image in images.values())
    return {"source_entries": len(grid._source_images), "source_images": len(sources),
            "worker_cache_images": len(worker), "result_queue": queue_size,
            "observed_unique_pil_images": len(images), "observed_pil_pixel_bytes": pixels,
            "native_tiles": len(grid.tiles) + len(grid._tile_pool), "tk_images": tk_images,
            "pending_keys": len(getattr(grid, "_pending_keys", ())),
            "valid_sources": len(getattr(grid, "_valid_sources", ())),
            "visible_previews": len(visible), "visible_previews_ready": ready,
            "visible_previews_failed": failed, "visible_previews_pending": len(visible) - ready - failed,
            "current_generation_failed_sources": failed_sources}


def measure(count, decoded, *, unique=False, unique_captions=False, scroll=False, poll_ms=120, scroll_steps=120, scroll_ms=24, trace_slow=False, trace_scheduler=False):
    with tempfile.TemporaryDirectory(prefix="luvbox-gallery-benchmark-") as temporary:
        rows = _records(Path(temporary), count, decoded, unique, unique_captions)
        root = tk.Tk()
        root.withdraw()
        grid = None
        thread = None
        trace = None
        original_paste = None
        try:
            theme.apply(root)
            construct_started = time.perf_counter()
            grid = ThumbnailGrid(root, on_select=lambda _: None, on_open=lambda _: None)
            construct_ms = (time.perf_counter() - construct_started) * 1000
            grid.pack(fill="both", expand=True)
            callback_errors = []

            def callback_error(kind, value, traceback):
                callback_errors.append(f"{kind.__name__}: {value}")
                root.quit()

            root.report_callback_exception = callback_error
            decode_count = 0
            decode_paths = set()
            decode_lock = threading.Lock()
            decode = grid._decoder.decoder

            def tracked_decode(path, size):
                nonlocal decode_count
                with decode_lock:
                    decode_count += 1
                    decode_paths.add(str(path))
                if trace is not None:
                    index = int(path.stem.removeprefix("synthetic-"))
                    return trace.call("worker.decode", decode, path, size, details={"fixture_index": index})
                return decode(path, size)

            grid._decoder.decoder = tracked_decode
            self_polling = hasattr(grid, "_valid_sources")
            heartbeats = {name: [] for name in ("initial", "resize", "scroll", "settle")}
            layouts, polls, observations = [], [], []
            maxima = {}
            snapshots = {}
            readiness = {name: [] for name in heartbeats}
            phase = "initial"
            active = True
            initial_settled_ms = None
            initial_decode_count = None
            started = last = time.perf_counter()
            phase_started = started
            durations = {}
            deadline = started + 60 + (2 * scroll_steps * scroll_ms / 1000 if scroll else 0)
            last_snapshot = 0
            if trace_slow or trace_scheduler:
                trace = _SlowTrace(started, lambda: phase, scheduler=trace_scheduler)
                attach = grid._attach_image
                grid._attach_image = lambda *args, **kwargs: trace.call("attach_image", attach, *args, **kwargs)
                original_paste = ImageTk.PhotoImage.paste
                ImageTk.PhotoImage.paste = lambda *args, **kwargs: trace.call("photo.paste", original_paste, *args, **kwargs)
            last_heartbeat_trace = trace.sample() if trace is not None else None
            layout = grid._layout

            def set_phase(name):
                nonlocal phase, phase_started
                if name != phase:
                    now = time.perf_counter()
                    durations[phase] = round((now - phase_started) * 1000, 3)
                    phase, phase_started = name, now

            def timed_layout():
                before = time.perf_counter()
                layout()
                layouts.append((time.perf_counter() - before) * 1000)

            grid._layout = timed_layout
            poll_method = grid.poll

            def timed_poll(*args, **kwargs):
                before = time.perf_counter()
                span = trace.sample() if trace is not None else None
                try:
                    return poll_method(*args, **kwargs)
                finally:
                    polls.append((time.perf_counter() - before) * 1000)
                    if trace is not None:
                        trace.record("poll", span)

            grid.poll = timed_poll

            def snapshot():
                before = time.perf_counter()
                span = trace.sample() if trace is not None else None
                sample = _preview_snapshot(grid)
                for key, value in sample.items():
                    maxima[key] = max(maxima.get(key, 0), value)
                snapshots[phase] = sample
                readiness[phase].append(sample["visible_previews_ready"] / sample["visible_previews"]
                                        if sample["visible_previews"] else 1.)
                observations.append((time.perf_counter() - before) * 1000)
                if trace is not None:
                    trace.record("observer", span)

            def heartbeat():
                nonlocal last, last_snapshot, last_heartbeat_trace
                now = time.perf_counter()
                if trace is not None:
                    end = trace.sample()
                    details = ({"main_thread_scheduler": trace.scheduler.delta()}
                               if trace.scheduler is not None else None)
                    trace.record("heartbeat", last_heartbeat_trace, end, details=details)
                    last_heartbeat_trace = end
                heartbeats[phase].append((now - last) * 1000)
                last = now
                if now - last_snapshot >= .05:
                    snapshot()
                    last_snapshot = now
                if now > deadline:
                    raise RuntimeError("Gallery benchmark exceeded its configured sweep duration plus 60 seconds")
                if active:
                    root.after(5, heartbeat)

            def poll():
                if not active:
                    return
                grid.poll(limit=24)
                root.after(poll_ms, poll)

            widths = list(range(1440, 779, -8)) + list(range(780, 1441, 8))
            fractions = ([index / scroll_steps for index in range(1, scroll_steps + 1)]
                         + [index / scroll_steps for index in range(scroll_steps - 1, -1, -1)])

            def finish_when_settled():
                nonlocal active
                set_phase("settle")
                if not decoded or _visible_settled(grid):
                    snapshot()
                    active = False
                    root.after(30, root.quit)
                else:
                    root.after(5, finish_when_settled)

            def scroll_view(index=0):
                set_phase("scroll")
                if index == len(fractions):
                    finish_when_settled()
                    return
                grid.canvas.yview_moveto(fractions[index])
                root.after(scroll_ms, scroll_view, index + 1)

            def resize(index=0):
                set_phase("resize")
                if index == len(widths):
                    snapshot()
                    if scroll:
                        scroll_view()
                    else:
                        finish_when_settled()
                    return
                root.geometry(f"{widths[index]}x900")
                root.after(8, resize, index + 1)

            def wait_for_initial():
                nonlocal initial_settled_ms, initial_decode_count
                if _visible_settled(grid):
                    initial_settled_ms = round((time.perf_counter() - started) * 1000, 3)
                    with decode_lock:
                        initial_decode_count = decode_count
                    snapshot()
                    resize()
                else:
                    root.after(5, wait_for_initial)

            root.after(0, heartbeat)
            if not self_polling:
                root.after(0, poll)
            root.after(0, wait_for_initial)
            grid.show(rows, set)
            root.geometry("1440x900")
            root.deiconify()
            root.mainloop()
            set_phase("done")
            if callback_errors:
                raise RuntimeError("; ".join(callback_errors))
            thread = grid._decoder.thread
            with decode_lock:
                completed_decodes, unique_decoded = decode_count, len(decode_paths)
            all_heartbeats = [value for samples in heartbeats.values() for value in samples]
            return {"rows": count, "decoded": decoded, "unique_files": unique, "scroll": scroll,
                    "unique_captions": unique_captions, "grid_construct_ms": round(construct_ms, 3),
                    "readiness_definition": "A visible current-generation source with an assigned Tk photo; unavailable results are failures, not photo-ready.",
                    "memory_definition": "Observed PIL source/worker-cache/queue pixel buffers only; excludes decoder local/in-flight references, Tk storage, metadata and process RSS.",
                    "poll_interval_ms": None if self_polling else poll_ms,
                    "self_polling": self_polling, "poll_limit": None if self_polling else 24,
                    "geometry_requests": len(widths), "scroll_requests": len(fractions) if scroll else 0,
                    "scroll_interval_ms": scroll_ms if scroll else None,
                    "scroll_fraction_per_request": round(1 / scroll_steps, 6) if scroll else None,
                    "result_queue_capacity": grid._results.maxsize,
                    "first_visible_settled_ms": initial_settled_ms,
                    "decode_calls_at_first_visible": initial_decode_count,
                    "decode_calls": completed_decodes, "unique_files_decoded": unique_decoded,
                    "max_retained": maxima, "phase_final_retained": snapshots, "phase_duration_ms": durations,
                    "layout_ms": timing_summary(layouts), "poll_ms": timing_summary(polls),
                    "observer_ms": timing_summary(observations),
                    "heartbeat_ms": timing_summary(all_heartbeats),
                    "phase_heartbeat_ms": {name: timing_summary(samples)
                                           for name, samples in heartbeats.items() if samples},
                    "slow_trace": trace.finish() if trace is not None else None,
                    "phase_visible_ready_fraction": {
                        name: {"samples": len(samples), "min": round(min(samples), 3),
                               "p50": percentile(samples, .5),
                               "mean": round(sum(samples) / len(samples), 3)}
                        for name, samples in readiness.items() if samples}}
        finally:
            if trace is not None:
                trace.close()
            if original_paste is not None:
                ImageTk.PhotoImage.paste = original_paste
            if grid is not None:
                thread = grid._decoder.thread
            root.destroy()
            if thread is not None:
                thread.join(timeout=5)
                if thread.is_alive():
                    raise RuntimeError("Gallery decoder did not close")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=1000)
    parser.add_argument("--decoded", action="store_true", help="Use one generated opaque 150x112 photo")
    parser.add_argument("--unique", action="store_true", help="Use a different generated photo per row, with varied aspect/alpha")
    parser.add_argument("--unique-captions", action="store_true", help="Use distinct author/date captions to include uncached text fitting")
    parser.add_argument("--scroll", action="store_true", help="Sweep from top to bottom and back while decoding continues")
    parser.add_argument("--poll-ms", type=int, default=120, help="Fallback polling interval for an eager grid; ignored by the self-polling viewport grid")
    parser.add_argument("--scroll-steps", type=int, default=120, help="Scroll requests per direction")
    parser.add_argument("--scroll-ms", type=int, default=24, help="Delay between scroll requests")
    parser.add_argument("--trace-slow", action="store_true", help="Record bounded slow spans, GC and CPU quota diagnostics")
    parser.add_argument("--trace-scheduler", action="store_true", help="Include slow tracing and optional Linux UI-thread scheduler counters per heartbeat")
    options = parser.parse_args()
    if not 1 <= options.rows <= 100000:
        parser.error("rows must be between 1 and 100000")
    if min(options.poll_ms, options.scroll_steps, options.scroll_ms) < 1:
        parser.error("poll-ms, scroll-steps and scroll-ms must be positive")
    results = measure(options.rows, options.decoded or options.unique, unique=options.unique,
                      unique_captions=options.unique_captions,
                      scroll=options.scroll, poll_ms=options.poll_ms,
                      scroll_steps=options.scroll_steps, scroll_ms=options.scroll_ms,
                      trace_slow=options.trace_slow, trace_scheduler=options.trace_scheduler)
    print(json.dumps({"python": platform.python_version(), "platform": platform.system(),
                      "tk": tk.TkVersion, "results": results}, indent=2))


if __name__ == "__main__":
    main()

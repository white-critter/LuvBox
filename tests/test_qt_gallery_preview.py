"""Disposable fixture boundaries for the opt-in Qt preview tool."""

from pathlib import Path
import sqlite3
import tempfile
import unittest

from tools.qt_gallery_preview import fixture_reader, make_fixture, timing_summary, wheel_travel


class GalleryPreviewFixtureTests(unittest.TestCase):
    def test_delegate_cache_stays_one_cell_and_focus_schedules_decode(self):
        source = (Path(__file__).resolve().parents[1] / "assets/qt/MainWindow.qml").read_text(encoding="utf-8")
        self.assertIn("cacheBuffer: cellHeight", source)
        self.assertNotIn("cacheBuffer: cellHeight *", source)
        self.assertIn("sampleOnScroll", source)
        self.assertIn("focusViewport", source)
        self.assertIn("thumbReady", source)
        self.assertIn("cancelFlick", source)
        self.assertIn("anchorTimer", source)
        self.assertIn("devicePixelRatio", source)
    def test_reader_returns_stable_bounded_batches_and_closes_connections(self):
        with tempfile.TemporaryDirectory() as folder:
            database, paths = make_fixture(Path(folder), 5)
            read = fixture_reader(database)
            total, first = read("", 0, 2)
            self.assertEqual(total, 5)
            self.assertEqual([row["key"] for row in first], list(paths)[:2])
            total, following = read("", 2, 2)
            self.assertEqual(total, 5)
            self.assertEqual([row["key"] for row in following], list(paths)[2:4])
            self.assertEqual(set(first[0]), {"key", "caption"})
            temporary = database.with_suffix(".renamed")
            database.rename(temporary)
            temporary.rename(database)

    def test_search_is_bound_as_literal_text(self):
        with tempfile.TemporaryDirectory() as folder:
            database, _ = make_fixture(Path(folder), 3)
            read = fixture_reader(database)
            self.assertEqual(read("demo_00001", 0, 200)[0], 1)
            self.assertEqual(read("%", 0, 200), (0, []))
            self.assertEqual(read("' OR 1=1 --", 0, 200), (0, []))

    def test_reader_does_not_create_missing_database(self):
        with tempfile.TemporaryDirectory() as folder:
            database = Path(folder) / "absent.sqlite3"
            with self.assertRaises(sqlite3.OperationalError):
                fixture_reader(database)("", 0, 200)
            self.assertFalse(database.exists())

    def test_empty_timing_is_not_reported_as_zero_latency(self):
        self.assertEqual(timing_summary([]), {"samples": 0, "p95_ms": None, "max_ms": None})
        self.assertEqual(timing_summary([3, 1, 2]), {"samples": 3, "p95_ms": 3, "max_ms": 3})

    def test_wheel_travel_ignores_subpixel_jitter(self):
        self.assertEqual(wheel_travel([0, 0.2, 120.2, 240]),
                         {"events": 3, "moved": 2, "median_px": 120.0, "total_px": 240.0})

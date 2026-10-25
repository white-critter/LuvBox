"""Original-size bounds, real wheel/drag input, and safe image-header metadata."""

from importlib.util import find_spec
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import zlib

from PIL import Image, ImageFile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from luvbox.preview import photo_dimensions, photo_info


class PhotoDimensionsTests(unittest.TestCase):
    def test_header_metadata_preserves_raw_and_oriented_size_without_decoding(self):
        with tempfile.TemporaryDirectory() as folder:
            for suffix in (".jpg", ".png", ".webp"):
                path = Path(folder) / ("photo" + suffix)
                exif = Image.Exif()
                exif[274] = 6
                with Image.new("RGB", (120, 80), "green") as image:
                    image.save(path, exif=exif)
                original, timestamp = path.read_bytes(), path.stat().st_mtime_ns
                with patch.object(ImageFile.ImageFile, "load", side_effect=AssertionError("raster decode")):
                    self.assertEqual(photo_info(path), {"pixel_width": 80, "pixel_height": 120,
                                                        "source_width": 120, "source_height": 80})
                    self.assertEqual(photo_dimensions(path), (80, 120))
                self.assertEqual(path.read_bytes(), original)
                self.assertEqual(path.stat().st_mtime_ns, timestamp)

    def test_png_exif_after_pixel_data_is_read_without_inflating_pixels(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "late.png"
            with Image.new("RGB", (90, 60), "red") as image:
                image.save(path)
            exif = Image.Exif()
            exif[274] = 8
            payload = exif.tobytes()[6:]
            chunk = struct.pack(">I", len(payload)) + b"eXIf" + payload
            chunk += struct.pack(">I", zlib.crc32(b"eXIf" + payload))
            data = path.read_bytes()
            path.write_bytes(data[:-12] + chunk + data[-12:])
            with patch.object(ImageFile.ImageFile, "load", side_effect=AssertionError("raster decode")):
                self.assertEqual(photo_dimensions(path), (60, 90))
                damaged = bytearray(path.read_bytes())
                damaged[-13] ^= 1  # Late eXIf CRC, immediately before IEND.
                path.write_bytes(damaged)
                self.assertEqual(photo_dimensions(path), (0, 0))

    def test_large_axes_pixel_budgets_and_non_still_sources_are_excluded(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "wide.png"
            with Image.new("RGB", (17000, 2), "red") as image:
                image.save(path)
            self.assertEqual(photo_dimensions(path), (0, 0))
            with Image.new("RGB", (120, 80), "red") as image:
                image.save(path)
                gif = path.with_suffix(".gif")
                image.save(gif)
            self.assertEqual(photo_dimensions(path, max_pixels=9000), (0, 0))
            for invalid in (0, -1, True, 60_000_001):
                self.assertEqual(photo_dimensions(path, invalid), (0, 0))
            self.assertEqual(photo_dimensions(gif), (0, 0))
            path.write_bytes(gif.read_bytes())
            self.assertEqual(photo_dimensions(path), (0, 0))

    def test_oversized_container_is_not_given_to_the_decoder(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "oversized.webp"
            with path.open("wb") as stream:
                stream.seek(64 * 1024 * 1024)
                stream.write(b"\0")
            with patch("luvbox.preview.Image.open") as decoder:
                self.assertEqual(photo_info(path), {})
                decoder.assert_not_called()


def interaction_scene():
    sys.path.insert(0, str(ROOT))
    from PySide6.QtCore import QCoreApplication, QEvent, QMetaObject, QObject, QPoint, QPointF, Qt, QUrl, qInstallMessageHandler
    from PySide6.QtGui import QGuiApplication, QMouseEvent, QWheelEvent
    from PySide6.QtQml import QQmlApplicationEngine
    from PySide6.QtQuickControls2 import QQuickStyle
    from shiboken6 import delete

    QQuickStyle.setStyle("Basic")
    application = QGuiApplication([])
    messages = []
    old_handler = qInstallMessageHandler(lambda _kind, _context, message: messages.append(message))
    engine = window = None
    checks = []

    def settle(duration=.05):
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            application.processEvents()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            time.sleep(.002)

    def wait(condition, label):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            settle(.005)
            if preview.property("failed"):
                raise AssertionError(label + ": image load failed")
            if condition():
                return
        raise AssertionError(label + ": timed out")

    def invoke(method):
        if not QMetaObject.invokeMethod(preview, method):
            raise AssertionError("Could not invoke " + method)

    def show(path, *, dimensions=None):
        preview.setProperty("playing", False)
        details = photo_info(path) if dimensions is None else dimensions
        for prop, name in (("sourcePixelWidth", "pixel_width"), ("sourcePixelHeight", "pixel_height"),
                           ("sourceRawWidth", "source_width"), ("sourceRawHeight", "source_height")):
            preview.setProperty(prop, details.get(name, 0))
        preview.setProperty("sourceUrl", QUrl.fromLocalFile(str(path)))
        preview.setProperty("playing", True)
        wait(lambda: preview.property("ready"), "fit image")

    try:
        with tempfile.TemporaryDirectory(prefix="luvbox-zoom-") as folder:
            photo_path = Path(folder) / "large.png"
            rotated_path = Path(folder) / "rotated.jpg"
            changed_path = Path(folder) / "changed.png"
            small_path = Path(folder) / "small.png"
            with Image.new("RGB", (3200, 2000), "green") as image:
                image.save(photo_path)
            with Image.new("RGB", (64, 32), "green") as image:
                image.save(small_path)
            with Image.new("RGB", (1000, 600), "blue") as image:
                exif = Image.Exif()
                exif[274] = 6
                image.save(rotated_path, exif=exif)
            engine = QQmlApplicationEngine()
            engine.loadData(b'''import QtQuick
import QtQuick.Controls
import "."
ApplicationWindow {
    width: 640; height: 480; visible: true
    MediaPreview { objectName: "interactivePreview"; anchors.fill: parent; anchors.margins: 20; playing: false }
}''', QUrl.fromLocalFile(str(ROOT / "assets/qt/interaction-test.qml")))
            if not engine.rootObjects():
                raise AssertionError("Product preview failed to load")
            window = engine.rootObjects()[0]
            preview = window.findChild(QObject, "interactivePreview")
            photo = preview.findChild(QObject, "previewPhoto")
            show(photo_path)
            if preview.property("zoomFactor") != 1 or not preview.property("canActualSize"):
                raise AssertionError("Initial preview is not fitted/eligible for original size")
            invoke("actualSize")
            wait(lambda: preview.property("ready") and abs(preview.property("zoomPercent") - 100) < .01,
                 "physical original scale")
            if (photo.property("implicitWidth"), photo.property("implicitHeight")) != (3200, 2000):
                raise AssertionError("Original-size action retained a downscaled image")
            checks.append("physical-original-size")

            center = preview.mapToScene(QPointF(preview.width() / 2, preview.height() / 2))
            global_center = QPointF(window.mapToGlobal(center.toPoint()))
            before = preview.property("zoomFactor")
            wheel = QWheelEvent(center, global_center, QPoint(), QPoint(0, 120), Qt.MouseButton.NoButton,
                                Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
            QCoreApplication.sendEvent(window, wheel)
            settle()
            if preview.property("zoomFactor") <= before or not wheel.isAccepted():
                raise AssertionError("Mouse wheel did not zoom/consume its event")
            if not preview.property("canPan"):
                raise AssertionError("Zoomed image cannot be dragged")
            for kind, offset, button, buttons in (
                (QEvent.Type.MouseButtonPress, QPointF(), Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton),
                (QEvent.Type.MouseMove, QPointF(90, 40), Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton),
                (QEvent.Type.MouseButtonRelease, QPointF(90, 40), Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton)):
                position = center + offset
                event = QMouseEvent(kind, position, QPointF(window.mapToGlobal(position.toPoint())),
                                    button, buttons, Qt.KeyboardModifier.NoModifier)
                QCoreApplication.sendEvent(window, event)
                settle(.01)
            if preview.property("panX") < 80 or preview.property("panY") < 30:
                raise AssertionError("Left-drag did not move the zoomed image")
            checks.append("wheel-and-left-drag")
            for _ in range(40):
                invoke("zoomIn")
            if preview.property("zoomFactor") > 16 or preview.property("canZoomIn"):
                raise AssertionError("Zoom-in limit is not enforced")
            for _ in range(80):
                invoke("zoomOut")
            if preview.property("zoomFactor") < .05 or preview.property("canPan"):
                raise AssertionError("Zoom-out/pan limits are not enforced")
            checks.append("zoom-and-pan-bounds")

            show(rotated_path)
            if preview.property("zoomFactor") != 1 or preview.property("panX") or preview.property("panY"):
                raise AssertionError("A new source inherited the previous view transform")
            invoke("actualSize")
            wait(lambda: preview.property("ready") and photo.property("implicitWidth") == 600,
                 "oriented original size")
            if photo.property("implicitHeight") != 1000 or abs(preview.property("zoomPercent") - 100) > .01:
                raise AssertionError("EXIF orientation broke original-size viewing")
            checks.append("oriented-original-and-source-reset")

            # The file can be replaced after its header was read. A raw-size
            # request alone can upscale the replacement's other axis wildly.
            with Image.new("RGB", (1000, 600), "red") as image:
                image.save(changed_path)
            recorded = photo_info(changed_path)
            show(changed_path, dimensions=recorded)
            with Image.new("RGB", (2000, 100), "blue") as image:
                image.save(changed_path)
            invoke("actualSize")
            wait(lambda: preview.property("ready") and photo.property("sourceClipRect").width() > 0,
                 "bounded replaced source")
            if (photo.property("implicitWidth") > 1000 or photo.property("implicitHeight") > 600 or
                    photo.property("sourceSize").width() <= 0 or photo.property("sourceSize").height() <= 0):
                raise AssertionError("Replaced aspect ratio escaped the original-size output bounds")
            checks.append("replaced-aspect-ratio-bounded")

            show(small_path)
            if (photo.property("implicitWidth"), photo.property("implicitHeight")) != (64, 32):
                raise AssertionError("A small fitted photo was needlessly decoded at a larger size")
            invoke("actualSize")
            wait(lambda: preview.property("ready") and abs(preview.property("zoomPercent") - 100) < .01,
                 "small photo physical original size")
            checks.append("small-photo-no-decode-upscale")

            show(photo_path, dimensions={})
            invoke("actualSize")
            if preview.property("canActualSize") or preview.property("_originalResolution"):
                raise AssertionError("Unknown dimensions enabled unrestricted original decoding")
            preview.setProperty("playing", False)
            wait(lambda: not preview.property("activeSource").toString(), "release")
            if preview.property("zoomFactor") != 1 or preview.property("panX") or preview.property("panY"):
                raise AssertionError("Closing a preview retained its transform")
            settle()
            parked = photo_path.with_suffix(".parked")
            photo_path.rename(parked)
            parked.rename(photo_path)
            checks.append("unknown-source-and-release")
            errors = [m for m in messages if any(term in m for term in (
                "ReferenceError", "TypeError", "Cannot assign", "Cannot read", "Binding loop", "is not a type"))]
            if errors:
                raise AssertionError("QML errors: " + repr(errors))
            return {"checks": checks, "pixel_ratio": preview.property("_pixelRatio"), "qml_errors": errors}
    finally:
        if window is not None:
            window.close()
        if engine is not None:
            delete(engine)
            application.processEvents()
        qInstallMessageHandler(old_handler)


@unittest.skipUnless(find_spec("PySide6") is not None, "Qt dependencies are not installed")
class PreviewInteractionTests(unittest.TestCase):
    def test_original_zoom_wheel_pan_reset_and_replacement_bounds(self):
        for scale in ("1", "1.5"):
            with self.subTest(scale=scale):
                env = dict(os.environ, QT_QPA_PLATFORM="offscreen", QT_QUICK_BACKEND="software",
                           QT_SCALE_FACTOR=scale, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
                result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scene"],
                                        cwd=ROOT, env=env, capture_output=True, text=True,
                                        encoding="utf-8", errors="replace", timeout=45,
                                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                report = json.loads(result.stdout)
                self.assertEqual(len(report["checks"]), 7)
                self.assertEqual(report["qml_errors"], [])


if __name__ == "__main__":
    print(json.dumps(interaction_scene()))

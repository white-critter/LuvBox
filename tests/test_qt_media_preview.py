"""Play generated local media through the product QML component in a child."""

from importlib.util import find_spec
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]


def check_preview_scene():
    sys.path.insert(0, str(ROOT))
    from PIL import Image
    from PySide6.QtCore import QCoreApplication, QEvent, QObject, QUrl, qInstallMessageHandler
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtMultimedia import QMediaPlayer
    from PySide6.QtQml import QQmlApplicationEngine
    from PySide6.QtQuickControls2 import QQuickStyle
    from shiboken6 import delete
    from luvbox.media import ffmpeg_path

    QQuickStyle.setStyle("Basic")
    application = QGuiApplication([])
    messages = []
    previous_handler = qInstallMessageHandler(lambda _kind, _context, message: messages.append(message))
    engine = None
    window = None
    temporary = tempfile.TemporaryDirectory(prefix="luvbox-qt-media-", ignore_cleanup_errors=True)

    def settle(duration=.05):
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            application.processEvents()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            time.sleep(.002)

    def wait_for(condition, label, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            application.processEvents()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            if preview.property("failed"):
                raise AssertionError(label + ": media failed to load")
            if condition():
                return
            time.sleep(.003)
        raise AssertionError(label + ": preview did not settle")

    try:
        with temporary as folder:
            directory = Path(folder)
            photo = directory / "photo.png"
            gif = directory / "animation.gif"
            video = directory / "clip.mp4"
            with Image.new("RGB", (48, 32), (20, 210, 40)) as picture:
                picture.save(photo)
            with Image.new("RGB", (48, 32), "red") as first, Image.new("RGB", (48, 32), "blue") as second:
                first.save(gif, save_all=True, append_images=[second], duration=[120, 120], loop=0)
            subprocess.run([str(ffmpeg_path()), "-nostdin", "-v", "error", "-f", "lavfi", "-i",
                            "testsrc2=size=64x48:rate=8", "-t", "0.5", "-an", "-c:v", "mpeg4", str(video)],
                           check=True, capture_output=True, timeout=20,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            engine = QQmlApplicationEngine()
            engine.loadData(b'''import QtQuick
import QtQuick.Controls
import "."
ApplicationWindow {
    width: 320; height: 240; visible: true
    MediaPreview { objectName: "fixturePreview"; anchors.fill: parent; playing: false }
}
''', QUrl.fromLocalFile(str(ROOT / "assets/qt/media-test.qml")))
            if not engine.rootObjects():
                raise AssertionError("Product MediaPreview component did not load")
            window = engine.rootObjects()[0]
            preview = window.findChild(QObject, "fixturePreview")
            player = preview.findChild(QMediaPlayer)
            if player is None or player.audioOutput() is None:
                raise AssertionError("Product media player or muted audio output is missing")
            valid_frames = []
            sink = player.videoSink()
            if sink is None:
                raise AssertionError("Product video output is not connected")
            sink.videoFrameChanged.connect(lambda frame: valid_frames.append(True) if frame.isValid() else None)
            checks = []

            def show(path, media_type):
                preview.setProperty("playing", False)
                preview.setProperty("mediaType", media_type)
                preview.setProperty("sourceUrl", QUrl.fromLocalFile(str(path)))
                preview.setProperty("playing", True)
                wait_for(lambda: preview.property("ready"), path.suffix + " ready")

            show(photo, "photo")
            settle()
            frame = window.grabWindow()
            if frame.isNull() or frame.pixelColor(frame.width() // 2, frame.height() // 2).green() < 180:
                raise AssertionError("The photo did not render in the QML window")
            checks.append("photo-rendered")

            show(gif, "animated_gif")
            animation = next(item for item in preview.findChildren(QObject)
                             if "AnimatedImage" in item.metaObject().className())
            if animation.property("frameCount") != 2:
                raise AssertionError("The GIF was not loaded as a two-frame animation")
            wait_for(lambda: animation.property("currentFrame") == 1, "GIF advances")
            checks.append("gif-advances")

            for media_type in ("video", "animated_gif"):
                baseline = len(valid_frames)
                show(video, media_type)
                if not preview.property("videoSource") or not player.audioOutput().isMuted():
                    raise AssertionError("The MP4 preview is not using muted video playback")
                if player.loops() != QMediaPlayer.Infinite:
                    raise AssertionError("The short preview is not configured to loop")
                wait_for(lambda: len(valid_frames) >= baseline + 6, media_type + " decoded looping frames")
                checks.append(media_type + "-decoded-muted-loop")

            preview.setProperty("playing", False)
            wait_for(lambda: player.source().isEmpty()
                     and player.playbackState() == QMediaPlayer.PlaybackState.StoppedState,
                     "release player")
            if not preview.property("activeSource").isEmpty():
                raise AssertionError("A hidden preview retained its active media source")
            settle()
            for path in (photo, gif, video):
                parked = path.with_name("released-" + path.name)
                path.rename(parked)
                parked.rename(path)
            checks.append("released-files-renamable")
            errors = [message for message in messages if re.search(
                r"ReferenceError|TypeError|Cannot assign|Cannot read|Binding loop|is not a type", message)]
            if errors:
                raise AssertionError("QML errors: " + repr(errors))
            return {"checks": checks, "valid_video_frames": len(valid_frames)}
    finally:
        if window is not None:
            window.close()
        if engine is not None:
            delete(engine)
            application.processEvents()
        qInstallMessageHandler(previous_handler)
        temporary.cleanup()


def check_review_release_scene(replace_candidate=False):
    sys.path.insert(0, str(ROOT))
    from PIL import Image
    from PySide6.QtCore import QCoreApplication, QEvent, QEventLoop, QMetaObject, QObject, Property, QTimer, QUrl, Signal, Slot
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtQml import QQmlApplicationEngine
    from PySide6.QtQuickControls2 import QQuickStyle
    from shiboken6 import delete

    QQuickStyle.setStyle("Basic")
    application = QGuiApplication([])
    temporary = tempfile.TemporaryDirectory(prefix="luvbox-qt-review-release-")
    engine = None
    try:
        path = Path(temporary.name) / "review.gif"
        with Image.new("RGB", (48, 32), "red") as first, Image.new("RGB", (48, 32), "blue") as second:
            first.save(path, save_all=True, append_images=[second], duration=[120, 120], loop=0)
        decisions = []
        decision_loop = QEventLoop()
        marker_reached = False

        class Fixture(QObject):
            changed = Signal()
            _id = "fixture"

            @Property("QVariantMap", notify=changed)
            def reviewItem(self):
                return {"id": self._id, "url": QUrl.fromLocalFile(str(path)).toString(),
                        "media_type": "animated_gif", "filename": path.name}

            @Slot()
            def replaceCandidate(self):
                self._id = "replacement"
                self.changed.emit()

            @Slot()
            def complete(self):
                nonlocal marker_reached
                marker_reached = True
                decision_loop.quit()

            @Slot(str)
            def respondReview(self, action):
                # Model a worker that consumes the decision immediately. It
                # must be safe to finalize the very file the preview opened.
                try:
                    parked = path.with_name("approved.gif")
                    path.rename(parked)
                    parked.rename(path)
                    decisions.append((action, True))
                except PermissionError:
                    decisions.append((action, False))
                decision_loop.quit()

        fixture = Fixture()
        engine = QQmlApplicationEngine()
        engine.setInitialProperties({"fixture": fixture})
        engine.loadData(b'''import QtQuick
import QtQuick.Controls
import "."
ApplicationWindow {
    id: window
    required property var fixture
    width: 800; height: 600; visible: true
    readonly property var preview: chooser.previewMedia
    function approve() { chooser.submit("save") }
    function approveThenReplace() {
        chooser.submit("save")
        fixture.replaceCandidate()
        Qt.callLater(function() { fixture.complete() })
    }
    SaveReviewDialog { id: chooser; parent: Overlay.overlay; app: window.fixture }
    Component.onCompleted: chooser.open()
}
''', QUrl.fromLocalFile(str(ROOT / "assets/qt/review-release-test.qml")))
        if not engine.rootObjects():
            raise AssertionError("Product save review dialog did not load")
        window = engine.rootObjects()[0]

        def wait_for(condition):
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                application.processEvents()
                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                if condition():
                    return
                time.sleep(.003)
            raise AssertionError("Save review did not settle")

        wait_for(lambda: window.property("preview").property("ready"))
        # A real click runs inside Qt's event loop, which processes deferred
        # deletion differently from a test's manual processEvents loop.
        method = "approveThenReplace" if replace_candidate else "approve"
        QTimer.singleShot(0, lambda: QMetaObject.invokeMethod(window, method))
        watchdog = QTimer()
        watchdog.setSingleShot(True)
        watchdog.timeout.connect(decision_loop.quit)
        watchdog.start(8000)
        decision_loop.exec()
        watchdog.stop()
        if not decisions and not replace_candidate:
            raise AssertionError("Save review approval was not delivered")
        return {"decision": decisions[0][0] if decisions else "", "decisions": len(decisions),
                "file_released": decisions[0][1] if decisions else False, "marker_reached": marker_reached}
    finally:
        if engine is not None:
            delete(engine)
            application.processEvents()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        temporary.cleanup()


@unittest.skipUnless(find_spec("PySide6") is not None, "Qt dependencies are not installed")
class MediaPreviewQmlTests(unittest.TestCase):
    def run_scene(self, scene):
        with tempfile.TemporaryDirectory(prefix="luvbox-qt-media-appdata-") as folder:
            environment = dict(os.environ, LOCALAPPDATA=folder, APPDATA=str(Path(folder) / "roaming"),
                               QT_QPA_PLATFORM="offscreen", QT_QUICK_BACKEND="software",
                               QT_QPA_FONTDIR=str(ROOT / "assets/fonts/Pretendard"))
            result = subprocess.run([sys.executable, str(Path(__file__).resolve()), scene],
                                    cwd=ROOT, env=environment, capture_output=True, text=True,
                                    encoding="utf-8", errors="replace", timeout=45,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    @unittest.skipUnless(os.name == "nt", "Windows bundled FFmpeg execution")
    def test_photos_animated_gifs_and_mp4_previews_decode_and_release(self):
        report = self.run_scene("--scene")
        self.assertEqual(len(report["checks"]), 5)
        self.assertGreaterEqual(report["valid_video_frames"], 12)

    def test_save_decision_reaches_the_worker_after_the_preview_releases_its_file(self):
        report = self.run_scene("--review-scene")
        self.assertEqual(report["decision"], "save")
        self.assertTrue(report["file_released"], "The worker received Save while the GIF was still open")

    def test_deferred_save_cannot_approve_a_replacement_candidate(self):
        report = self.run_scene("--stale-review-scene")
        self.assertTrue(report["marker_reached"], "Deferred review callbacks did not finish")
        self.assertEqual(report["decisions"], 0)


if __name__ == "__main__":
    print(json.dumps(check_preview_scene() if sys.argv[1] == "--scene"
                     else check_review_release_scene(sys.argv[1] == "--stale-review-scene")))

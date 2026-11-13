"""Switching media inside an open gallery preview, through the real window.

The preview keeps playing while the controller replaces the selection, so the
file and its media type change together. A direct open does not go through that
path, which is why both must be driven here with real video and GIF files.
"""

from importlib.util import find_spec
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
try:
    from tests import app_data_isolation  # noqa: F401  (never this PC's real app data)
except ModuleNotFoundError:      # run as a script: the tests folder is the path
    import app_data_isolation  # noqa: F401


ROOT = Path(__file__).resolve().parents[1]

# name, media type, native size. Neighbours cover every kind of transition:
# photo/GIF/MP4 into and out of video, video to video, and GIF to GIF.
FIXTURES = (
    ("photo-landscape.png", "photo", (1600, 1000)),
    ("clip-landscape.mp4", "video", (64, 48)),
    ("clip-portrait.mp4", "video", (48, 64)),
    ("anim-wide.gif", "animated_gif", (160, 40)),
    ("clip-second.mp4", "video", (64, 48)),
    ("anim-x.mp4", "animated_gif", (64, 48)),
    ("anim-tall.gif", "animated_gif", (40, 160)),
    ("photo-portrait.png", "photo", (600, 900)),
    ("anim-square.gif", "animated_gif", (60, 60)),
    ("anim-tiny.gif", "animated_gif", (16, 16)),
    ("anim-large.gif", "animated_gif", (1200, 600)),
    ("clip-last.mp4", "video", (64, 48)),
)


def check_scene(screenshots=None):
    sys.path.insert(0, str(ROOT))
    from PIL import Image
    from PySide6.QtCore import QCoreApplication, QEvent, QMetaObject, QObject, Qt, QUrl, qInstallMessageHandler
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtMultimedia import QMediaPlayer
    from PySide6.QtQml import QQmlApplicationEngine
    from PySide6.QtQuick import QQuickItem
    from PySide6.QtQuickControls2 import QQuickStyle
    from PySide6.QtTest import QTest
    from shiboken6 import delete, isValid
    from luvbox.app_settings import default_settings
    from luvbox.media import ffmpeg_path
    from luvbox.qt_app import ApplicationController
    from luvbox.qt_wheel import WheelInputFilter

    QQuickStyle.setStyle("Basic")
    application = QGuiApplication([])
    messages = []
    old_handler = qInstallMessageHandler(lambda _kind, _context, text: messages.append(text))
    controller = wheel = engine = window = None
    report = {"checks": []}

    def settle(duration=.05):
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            application.processEvents()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            time.sleep(.002)

    def wait(condition, label, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            settle(.01)
            if condition():
                return
        raise AssertionError(label + " did not settle")

    def find(name):
        found = window.findChild(QObject, name)
        if found is None:
            raise AssertionError("Product Qt object missing: " + name)
        return found

    def click(item):
        if not QMetaObject.invokeMethod(item, "clicked"):
            raise AssertionError("Could not click the product button")

    def make(path, kind, size):
        width, height = size
        if kind == "photo":
            with Image.new("RGB", size, "#3f669b") as picture:
                picture.save(path)
        elif path.suffix == ".gif":
            with Image.new("RGB", size, "red") as first, Image.new("RGB", size, "blue") as second:
                first.save(path, save_all=True, append_images=[second], duration=[120, 120], loop=0)
        else:
            subprocess.run([str(ffmpeg_path()), "-nostdin", "-v", "error", "-f", "lavfi", "-i",
                            f"testsrc2=size={width}x{height}:rate=8", "-t", "1", "-an", "-c:v", "mpeg4", str(path)],
                           check=True, capture_output=True, timeout=30,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    with tempfile.TemporaryDirectory(prefix="luvbox-media-switch-") as temporary:
        directory = Path(temporary)
        files = []
        for name, kind, size in FIXTURES:
            path = directory / name
            make(path, kind, size)
            files.append((path, kind, size))
        values = {**default_settings(), "account": "example", "output_dir": str(directory / "media")}

        class MemorySettings:
            def load(self): return dict(values)
            def update(self, changes):
                values.update(changes)
                return dict(values)

        class Service:
            job_id = 7
            def start(self, _options): return True
            def drain(self, _limit): return []
            def close(self): pass
            def stop(self): pass
            def set_review_enabled(self, _enabled): pass
            def resolve_review(self, _ident, _action): return False

        patches = [patch.dict(os.environ, {"LOCALAPPDATA": str(directory / "appdata"),
                                           "APPDATA": str(directory / "roaming")}),
                   patch("luvbox.qt_app.SettingsRepository", MemorySettings),
                   patch("luvbox.qt_accounts.AccountController._read_state", new=staticmethod(lambda: {
                       "accounts": [], "identity": {}, "connected": False})),
                   patch("luvbox.qt_schedule.task_status", return_value={"registered": False}),
                   patch("luvbox.qt_browser_setup.available_browsers", return_value=[]),
                   patch("luvbox.browser_discovery.open_browser_url", side_effect=AssertionError("No browser access")),
                   patch("luvbox.qt_download.DownloadService", return_value=Service())]
        for item in patches:
            item.start()
        try:
            controller = ApplicationController()
            wheel = WheelInputFilter()
            engine = QQmlApplicationEngine()
            engine.addImageProvider("thumbs", controller.library.provider)
            engine.setInitialProperties({"galleryModel": controller.library.model, "wheelInputFilter": wheel,
                                         "appController": controller})
            engine.load(ROOT / "assets/qt/MainWindow.qml")
            if not engine.rootObjects():
                raise AssertionError("The product window did not load: " + str(messages))
            window = engine.rootObjects()[0]
            library = controller.library
            wait(lambda: not library.model.busy, "empty fixture library")

            def read(token, offset, limit):
                request = library._repository.current()
                if request is None or token != str(request.generation):
                    return 0, []
                rows = [{"key": f"g{request.generation}_flat_{101 + index}_1_{kind}", "caption": str(index)}
                        for index, (_path, kind, _size) in enumerate(files)]
                return len(rows), rows[offset:offset + limit]

            def selection(key, _generation):
                index = int(key.split("_")[2]) - 101
                path, kind, (width, height) = files[index]
                still = kind == "photo"
                return {"key": key, "preview": QUrl.fromLocalFile(str(path)).toString(), "path": str(path),
                        "missing": False, "media_type": kind, "author": "example", "body": "",
                        "public_info": path.name,
                        "pixel_width": width if still else 0, "pixel_height": height if still else 0,
                        "source_width": width if still else 0, "source_height": height if still else 0}

            library.model._reader = read
            patches.append(patch.object(library._repository, "selected", side_effect=selection))
            patches[-1].start()
            library.refresh()
            wait(lambda: not library.model.busy and library.model.count == len(files), "mixed-media gallery")

            dialog, media = find("previewDialog"), find("selectedMedia")
            players = media.findChildren(QMediaPlayer)
            if len(players) != 1 or players[0].videoSink() is None:
                raise AssertionError("The preview must own exactly one connected media player")
            player = players[0]
            frames = []
            player.videoSink().videoFrameChanged.connect(lambda frame: frames.append(1) if frame.isValid() else None)
            animation = next(item for item in media.findChildren(QQuickItem)
                             if "AnimatedImage" in item.metaObject().className())

            def capture(index):
                # Optional evidence from a real window (--screenshots DIR); the
                # offscreen test backend does not paint video pixels.
                if screenshots:
                    destination = Path(screenshots).resolve()
                    destination.mkdir(parents=True, exist_ok=True)
                    name = f"{index:02d}-{files[index][0].stem}-{window.width()}x{window.height()}.png"
                    if not window.grabWindow().save(str(destination / name)):
                        raise AssertionError("Could not capture the preview")

            def url(index):
                return QUrl.fromLocalFile(str(files[index][0])).toString()

            def show(index, step):
                """Switch with the product control; return once its media is displayed."""
                before = len(frames)
                if step == "next":
                    click(find("previewNextButton"))
                elif step == "previous":
                    click(find("previewPreviousButton"))
                elif step == "right":
                    QTest.keyClick(window, Qt.Key.Key_Right)
                elif step == "left":
                    QTest.keyClick(window, Qt.Key.Key_Left)
                else:
                    library.selectItem(library.model._rows[index]["key"])
                label = f"{files[index][0].name} via {step}"
                try:
                    wait(lambda: not library.busy and library.selectedIndex == index and media.property("ready"),
                         label + " ready")
                except AssertionError as error:
                    raise AssertionError(
                        f"{error} (busy={library.busy}, index={library.selectedIndex}, source={player.source().toString()!r}, "
                        f"status={player.mediaStatus()}, state={player.playbackState()}, error={player.errorString()!r}, "
                        f"loading={media.property('loading')}, failed={media.property('failed')}, "
                        f"type={media.property('mediaType')!r}, url={media.property('sourceUrl').toString()[-20:]!r})") from None
                verify(index, before, label)

            def verify(index, frames_before, label):
                path, kind, (width, height) = files[index]
                is_video = path.suffix == ".mp4"
                if library.selectedPreviewUrl != url(index):
                    raise AssertionError(label + ": the controller did not select the expected file")
                if is_video:
                    if player.source() != QUrl.fromLocalFile(str(path)):
                        raise AssertionError(label + ": the player was not given the video")
                    wait(lambda: len(frames) >= frames_before + 3, label + " decoded frames")
                    if player.playbackState() != QMediaPlayer.PlaybackState.PlayingState:
                        raise AssertionError(label + ": the video is not playing")
                    if not media.property("videoSource"):
                        raise AssertionError(label + ": preview is not in video mode")
                    painted = (media.property("_paintedWidth"), media.property("_paintedHeight"))
                else:
                    if not player.source().isEmpty() or player.playbackState() != QMediaPlayer.PlaybackState.StoppedState:
                        raise AssertionError(f"{label}: a previous video stayed loaded behind a still or GIF "
                                             f"(source={player.source().toString()!r}, state={player.playbackState()})")
                    renderer = animation if path.suffix == ".gif" else find("previewPhoto")
                    if path.suffix == ".gif" and (animation.property("implicitWidth"),
                                                  animation.property("implicitHeight")) != (width, height):
                        raise AssertionError(label + ": the GIF was not decoded at its native size")
                    painted = (renderer.property("paintedWidth"), renderer.property("paintedHeight"))
                if painted[1] <= 0 or abs(painted[0] / painted[1] - width / height) > .02 * width / height:
                    raise AssertionError(f"{label}: displayed {painted[0]:.0f}x{painted[1]:.0f} is not {width}x{height}")
                if painted[0] > media.width() + 1 or painted[1] > media.height() + 1:
                    raise AssertionError(label + ": the media overflowed the preview")
                if not (media.property("ready") and not media.property("failed") and not media.property("unavailable")):
                    raise AssertionError(label + ": the preview reports an error")
                capture(index)

            library.selectItem(library.model._rows[0]["key"])
            wait(lambda: not library.busy and library.selectedIndex == 0, "first item")
            window.resize(1080, 760)
            dialog.open()
            wait(lambda: media.property("ready"), "first photo")
            verify(0, len(frames), "direct open")

            for index in range(1, len(files)):
                show(index, "next")
            report["checks"].append("next-through-every-media-pair")
            for index in range(len(files) - 2, -1, -1):
                show(index, "previous")
            report["checks"].append("previous-through-every-media-pair")
            window.requestActivate()
            dialog.property("contentItem").forceActiveFocus()
            for index in range(1, len(files)):
                show(index, "right")
            for index in range(len(files) - 2, -1, -1):
                show(index, "left")
            report["checks"].append("arrow-keys-through-every-media-pair")

            # Back and forth across one pair, without waiting beyond readiness.
            for _ in range(6):
                for target, step in ((1, "next"), (2, "next"), (3, "next"), (2, "previous"), (1, "previous"), (0, "previous")):
                    show(target, step)
            report["checks"].append("repeated-quick-switches")

            # A video entered by switching must still release and reopen cleanly.
            show(4, "select")
            for cycle in range(8):
                dialog.close()
                wait(lambda: player.source().isEmpty()
                     and player.playbackState() == QMediaPlayer.PlaybackState.StoppedState
                     and not media.property("activeSource").toString(), f"video released {cycle}")
                renamed = files[4][0].with_name("released-" + files[4][0].name)
                files[4][0].rename(renamed)
                renamed.rename(files[4][0])
                before = len(frames)
                dialog.open()
                wait(lambda: media.property("ready"), f"video reopened {cycle}")
                verify(4, before, f"reopen {cycle}")
            report["checks"].append("open-close-cycles-release-the-file")

            # Shortcut 'm' toggles video mute
            show(1, "select")
            assert media.property("muted") is True
            QTest.keyClick(window, Qt.Key.Key_M)
            settle(.05)
            assert media.property("muted") is False, "M key un-mutes video"
            QTest.keyClick(window, Qt.Key.Key_M)
            settle(.05)
            assert media.property("muted") is True, "M key re-mutes video" 

            # Resizing keeps every kind in proportion.
            for index in (3, 6, 1, 7):
                show(index, "select")
                for size in ((680, 480), (1600, 1000), (900, 700)):
                    window.resize(*size)
                    settle(.12)
                    verify(index, len(frames) - 3, f"{files[index][0].name} at {size}")
            report["checks"].append("proportions-survive-window-resize")

            if len(media.findChildren(QMediaPlayer)) != 1:
                raise AssertionError("The preview accumulated media players")
            dialog.close()
            wait(lambda: player.source().isEmpty(), "final release")
            errors = [text for text in messages if any(word in text for word in (
                "ReferenceError", "TypeError", "Cannot assign", "Cannot read", "Binding loop", "is not a type"))]
            report["qml_errors"] = errors
            if errors:
                raise AssertionError("QML errors: " + repr(errors[:3]))
            return report
        finally:
            if controller is not None:
                controller.requestClose()
                wait(lambda: controller.shutdownReady, "controller shutdown")
            if wheel is not None:
                wheel.close()
            for item in reversed(patches):
                item.stop()
            if window is not None and isValid(window):
                window.close()
            if engine is not None and isValid(engine):
                delete(engine)
                settle()
            qInstallMessageHandler(old_handler)


@unittest.skipUnless(find_spec("PySide6") is not None, "Qt dependencies are not installed")
class PreviewMediaSwitchTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows bundled FFmpeg execution")
    def test_every_kind_of_media_displays_in_proportion_after_previous_next_and_arrow_keys(self):
        environment = dict(os.environ, QT_QPA_PLATFORM="offscreen", QT_QUICK_BACKEND="software",
                           PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
        result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scene"],
                                cwd=ROOT, env=environment, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=170,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(len(report["checks"]), 6)
        self.assertEqual(report["qml_errors"], [])


if __name__ == "__main__":
    folder = sys.argv[sys.argv.index("--screenshots") + 1] if "--screenshots" in sys.argv else None
    print(json.dumps(check_scene(folder)))

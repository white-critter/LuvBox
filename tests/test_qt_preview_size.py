"""Check actual gallery/save preview geometry with isolated synthetic media."""

from contextlib import ExitStack
from importlib.util import find_spec
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from threading import get_ident
import time
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def check_scene(*, baseline=False, screenshots=None, features=False):
    sys.path.insert(0, str(ROOT))
    from PIL import Image, ImageDraw
    from PySide6.QtCore import QCoreApplication, QEvent, QMetaObject, QObject, QPointF, Qt, QUrl, qInstallMessageHandler
    from PySide6.QtGui import QFont, QFontDatabase, QGuiApplication, QWindow
    from PySide6.QtQml import QQmlApplicationEngine
    from PySide6.QtQuick import QQuickItem
    from PySide6.QtQuickControls2 import QQuickStyle
    from PySide6.QtMultimedia import QMediaPlayer
    from PySide6.QtTest import QTest
    from shiboken6 import delete, isValid
    from luvbox.app_settings import default_settings
    from luvbox.qt_app import ApplicationController
    from luvbox.qt_wheel import WheelInputFilter
    from luvbox.qt_saved_preview import SavedPreviewLoader, decode_saved_preview

    QQuickStyle.setStyle("Basic")
    application = QGuiApplication([])
    QFontDatabase.addApplicationFont(str(ROOT / "assets/fonts/Pretendard/Pretendard-Regular.ttf"))
    font = QFont("Pretendard")
    font.setPixelSize(14)
    application.setFont(font)
    messages = []
    old_handler = qInstallMessageHandler(lambda _kind, _context, text: messages.append(text))
    controller = wheel = engine = window = None
    report = {"baseline": baseline, "checks": [], "gallery": {}, "review": {}, "screenshots": []}

    def settle(duration=.08):
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            application.processEvents()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            time.sleep(.002)

    def wait(condition, label, timeout=7):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            settle(.015)
            if condition():
                return
        raise AssertionError(label + " did not settle")

    def invoke(item, method):
        if item is None or not QMetaObject.invokeMethod(item, method):
            raise AssertionError("Qt action failed: " + method)
        settle()

    def find(name):
        found = window.findChild(QObject, name)
        if found is None:
            raise AssertionError("Product Qt object missing: " + name)
        return found

    def capture(name):
        if screenshots:
            destination = Path(screenshots).resolve()
            destination.mkdir(parents=True, exist_ok=True)
            target = destination / (("before-" if baseline else "after-") + name + ".png")
            if not window.grabWindow().save(str(target)):
                raise AssertionError("Could not capture native preview")
            report["screenshots"].append(str(target))

    def bounds(item, label):
        top_left = item.mapToScene(QPointF(0, 0))
        if (top_left.x() < -1 or top_left.y() < -1
                or top_left.x() + item.width() > window.width() + 1
                or top_left.y() + item.height() > window.height() + 1):
            raise AssertionError(label + " escaped the available window")

    def metrics(dialog, media):
        # PreserveAspectFit must show the complete landscape photo, not crop it
        # or stretch it to the enlarged dialog's aspect ratio.
        photo = next((child for child in media.findChildren(QQuickItem)
                      if child.property("visible") and child.property("paintedWidth") is not None), None)
        if photo is None:
            raise AssertionError("The actual photo renderer is missing")
        painted_width, painted_height = photo.property("paintedWidth"), photo.property("paintedHeight")
        if painted_height <= 0 or abs(painted_width / painted_height - 1.6) > .015:
            raise AssertionError("Enlarged preview cropped or distorted the generated photo")
        return {"dialog": [dialog.property("width"), dialog.property("height")],
                "canvas": [media.width(), media.height()], "painted": [painted_width, painted_height]}

    def action_buttons(dialog, expected):
        for text in expected:
            matches = [item for item in dialog.findChildren(QQuickItem)
                       if item.property("text") == text and item.property("down") is not None
                       and item.property("visible")]
            if not matches:
                raise AssertionError("Visible preview action missing: " + text)
            for item in matches:
                bounds(item, text)

    with tempfile.TemporaryDirectory(prefix="luvbox-preview-size-") as temporary, ExitStack() as mocks:
        directory = Path(temporary)
        mocks.enter_context(patch.dict(os.environ, {"LOCALAPPDATA": str(directory / "appdata"),
                                                    "APPDATA": str(directory / "roaming")}))
        photo_path = directory / "landscape.png"
        with Image.new("RGB", (1600, 1000), "#e8ac40") as photo:
            draw = ImageDraw.Draw(photo)
            draw.rectangle((800, 0, 1599, 999), fill="#3f9b75")
            photo.save(photo_path)
        values = {**default_settings(), "account": "example", "output_dir": str(directory / "media")}
        class MemorySettings:
            def load(self): return dict(values)
            def update(self, changes):
                values.update(changes)
                return dict(values)
        mocks.enter_context(patch("luvbox.qt_app.SettingsRepository", MemorySettings))
        mocks.enter_context(patch("luvbox.qt_accounts.AccountController._read_state", new=staticmethod(lambda: {
            "accounts": [], "identity": {}, "connected": False})))
        mocks.enter_context(patch("luvbox.qt_schedule.task_status", return_value={"registered": False}))
        mocks.enter_context(patch("luvbox.qt_browser_setup.available_browsers", return_value=[]))
        mocks.enter_context(patch("luvbox.browser_discovery.open_browser_url", side_effect=AssertionError("No browser access")))
        decoded = []
        class Service:
            job_id = 7
            def __init__(self): self.pending = []
            def start(self, _options): return True
            def drain(self, _limit): return [self.pending.pop(0)] if self.pending else []
            def close(self): self.pending.append({"type": "process_exit", "job_id": 7, "code": 0})
            def stop(self): pass
            def set_review_enabled(self, _enabled): pass
            def resolve_review(self, _ident, _action): return False
        service = Service()
        if features:
            def decode(path):
                decoded.append((str(path), get_ident()))
                return decode_saved_preview(path)
            mocks.enter_context(patch("luvbox.qt_download.DownloadService", return_value=service))
            mocks.enter_context(patch("luvbox.qt_download.SavedPreviewLoader",
                                      side_effect=lambda deliver: SavedPreviewLoader(deliver, decoder=decode)))
        qml_path = ROOT / "assets/qt/MainWindow.qml"
        if baseline:
            assets = directory / "assets"
            shutil.copytree(ROOT / "assets/qt", assets / "qt")
            shutil.copytree(ROOT / "assets/icons", assets / "icons")
            for name in ("MainWindow.qml", "SaveReviewDialog.qml"):
                original = subprocess.check_output(["git", "show", "HEAD:assets/qt/" + name], cwd=ROOT)
                (assets / "qt" / name).write_bytes(original)
            qml_path = assets / "qt/MainWindow.qml"
        try:
            controller = ApplicationController()
            wheel = WheelInputFilter()
            engine = QQmlApplicationEngine()
            engine.addImageProvider("thumbs", controller.library.provider)
            engine.setInitialProperties({"galleryModel": controller.library.model, "wheelInputFilter": wheel,
                                         "appController": controller})
            engine.load(qml_path)
            if not engine.rootObjects():
                raise AssertionError("The product window did not load: " + str(messages))
            window = engine.rootObjects()[0]
            wait(lambda: not controller.library.model.busy, "empty fixture library")
            controller.library._selected = {
                "key": "synthetic-photo", "author": "example", "body": "사진 설명을 확인합니다. " * 120,
                "source": "https://x.com/example/status/100", "path": str(photo_path), "missing": False,
                "missing_note": "", "preview": QUrl.fromLocalFile(str(photo_path)).toString(),
                "media_type": "photo", "public_info": "landscape.png · 2026-10-25 14:00"}
            controller.library.changed.emit()
            dialog, media = find("previewDialog"), find("selectedMedia")
            for width, height in ((680, 480), (1080, 760), (1600, 1000)):
                window.showNormal()
                window.resize(width, height)
                invoke(dialog, "open")
                wait(lambda: media.property("ready"), "gallery photo")
                settle(.1)
                size = f"{width}x{height}"
                data = metrics(dialog, media)
                parent = dialog.property("parent")
                if (dialog.property("x") < -1 or dialog.property("y") < -1
                        or dialog.property("x") + dialog.property("width") > parent.width() + 1
                        or dialog.property("y") + dialog.property("height") > parent.height() + 1):
                    raise AssertionError("Gallery dialog escaped its overlay")
                action_buttons(dialog, ("닫기", "파일 열기", "폴더", "원문 보기"))
                if not baseline:
                    action_buttons(dialog, ("이전", "다음", "−", "+", "화면 맞춤", "원본 100%", "창 최대화"))
                    if data["dialog"][0] < width - 26 or data["dialog"][1] < height - 26:
                        raise AssertionError("The preview retained its old fixed size cap")
                    body = find("previewBodyScroll")
                    if dialog.property("detailsVisible") or body.property("visible"):
                        raise AssertionError("A long description consumed preview space by default")
                    collapsed_height = media.height()
                    invoke(find("previewDetailsButton"), "clicked")
                    if not body.property("visible") or media.height() >= collapsed_height:
                        raise AssertionError("The body toggle did not reveal the stored description")
                    action_buttons(dialog, ("닫기", "파일 열기", "폴더", "원문 보기"))
                    if media.height() <= 0:
                        raise AssertionError("Opening the body removed the image viewport")
                    invoke(find("previewDetailsButton"), "clicked")
                report["gallery"][size] = data
                if size == "1080x760":
                    capture("gallery")
                invoke(dialog, "close")
                wait(lambda: not media.property("activeSource").toString(), "gallery release")
            report["checks"].append("gallery-sizes-body-actions-aspect")
            if not baseline:
                window.resize(1080, 760)
                invoke(dialog, "open")
                invoke(find("previewMaximizeButton"), "clicked")
                wait(lambda: window.visibility() == QWindow.Visibility.Maximized, "gallery window maximization")
                if "복원" not in find("previewMaximizeButton").property("text"):
                    raise AssertionError("Gallery maximize control did not reflect native state")
                invoke(find("previewMaximizeButton"), "clicked")
                wait(lambda: window.visibility() == QWindow.Visibility.Windowed, "gallery window restoration")
                invoke(dialog, "close")
                report["checks"].append("gallery-native-maximize-restore")

            if features:
                # The buttons must drive the same real controller/model used
                # by the grid, including metadata and source-dependent zoom.
                portrait = directory / "portrait.png"
                with Image.new("RGB", (600, 900), "#3f669b") as picture:
                    picture.save(portrait)
                files = [(photo_path, 1600, 1000), (portrait, 600, 900)]
                library = controller.library
                def read(token, offset, limit):
                    request = library._repository.current()
                    if request is None or token != str(request.generation): return 0, []
                    rows = []
                    for index, (path, _width, _height) in enumerate(files):
                        key = f"g{request.generation}_flat_{101 + index}_1_photo"
                        library.provider.register(key, path)
                        rows.append({"key": key, "caption": str(index)})
                    return len(rows), rows[offset:offset + limit]
                def selection(key, _generation):
                    index = int(key.split("_")[2]) - 101
                    path, width, height = files[index]
                    return {"key": key, "preview": QUrl.fromLocalFile(str(path)).toString(),
                            "path": str(path), "missing": False, "media_type": "photo",
                            "author": "example", "body": "body " + str(index), "public_info": path.name,
                            "pixel_width": width, "pixel_height": height,
                            "source_width": width, "source_height": height}
                library.model._reader = read
                mocks.enter_context(patch.object(library._repository, "selected", side_effect=selection))
                library.refresh()
                wait(lambda: not library.model.busy and library.model.count == 2, "two-photo gallery")
                first = library.model._rows[0]["key"]
                library.selectItem(first)
                wait(lambda: not library.busy and library.selectedKey == first, "first adjacent photo")
                invoke(dialog, "open")
                wait(lambda: media.property("ready"), "first adjacent photo decode")
                zoom = next(item for item in dialog.findChildren(QQuickItem)
                            if item.property("text") == "+" and item.property("down") is not None)
                invoke(zoom, "clicked")
                if media.property("zoomFactor") <= 1:
                    raise AssertionError("The product zoom toolbar did not enlarge the preview")
                if find("previewPreviousButton").property("enabled"):
                    raise AssertionError("Previous was enabled at the first photo")
                invoke(find("previewNextButton"), "clicked")
                wait(lambda: not library.busy and library.selectedIndex == 1 and media.property("ready"),
                     "next photo through the preview button")
                if library.selectedPreviewUrl != QUrl.fromLocalFile(str(portrait)).toString():
                    raise AssertionError("Next did not refresh the preview source")
                if abs(media.property("zoomFactor") - 1) > .01:
                    raise AssertionError("A new photo inherited the previous photo's zoom")
                if find("previewNextButton").property("enabled"):
                    raise AssertionError("Next was enabled at the final photo")
                invoke(find("previewPreviousButton"), "clicked")
                wait(lambda: not library.busy and library.selectedIndex == 0 and media.property("ready"),
                     "previous photo through the preview button")
                if find("galleryGrid").property("currentIndex") != 0:
                    raise AssertionError("Preview navigation and the gallery selection diverged")
                report["checks"].append("preview-adjacent-buttons-and-zoom-reset")

                # Exercise the real key delivery path, including focus on a
                # popup child and a toolbar button; emitting a clicked signal
                # cannot detect keys falling through to the grid underneath.
                window.requestActivate()
                settle()
                dialog.property("contentItem").forceActiveFocus()
                QTest.keyClick(window, Qt.Key.Key_Right)
                wait(lambda: not library.busy and library.selectedIndex == 1 and media.property("ready"),
                     "right arrow in the preview")
                zoom.forceActiveFocus()
                QTest.keyClick(window, Qt.Key.Key_Left)
                wait(lambda: not library.busy and library.selectedIndex == 0 and media.property("ready"),
                     "left arrow with toolbar focus")
                QTest.keyClick(window, Qt.Key.Key_Left)
                settle()
                if library.selectedIndex != 0:
                    raise AssertionError("Left arrow left the first filtered photo")
                QTest.keyClick(window, Qt.Key.Key_Right)
                wait(lambda: not library.busy and library.selectedIndex == 1, "last photo by keyboard")
                QTest.keyClick(window, Qt.Key.Key_Right)
                settle()
                if library.selectedIndex != 1:
                    raise AssertionError("Right arrow left the final filtered photo")
                invoke(find("previewDetailsButton"), "clicked")
                body_text = next(item for item in find("previewBodyScroll").findChildren(QQuickItem)
                                 if item.property("readOnly") is True)
                body_text.forceActiveFocus()
                QTest.keyClick(window, Qt.Key.Key_Left)
                settle()
                if library.selectedIndex != 1 or not body_text.property("activeFocus"):
                    raise AssertionError("A body-text cursor key navigated to another photo")
                invoke(find("previewDetailsButton"), "clicked")
                report["checks"].append("preview-real-arrow-keys-focus-and-boundaries")
                invoke(dialog, "close")

                window.setProperty("currentScreen", 1)
                grid = find("galleryGrid")
                grid.setProperty("currentIndex", 0)
                for width, height in ((680, 480), (1080, 760), (1600, 1000)):
                    window.resize(width, height)
                    settle(.1)
                    density = find("thumbSizeChoice")
                    preview_button = next(item for item in window.findChildren(QQuickItem)
                                          if item.property("text") == "미리보기" and item.property("down") is not None)
                    for busy in (True, False):
                        library.model._busy = busy
                        library.model.changed.emit()
                        settle()
                        right_edge = density.mapToScene(QPointF(density.width(), 0)).x()
                        preview_right = preview_button.mapToScene(QPointF(preview_button.width(), 0)).x()
                        if abs(right_edge - preview_right) > 1:
                            raise AssertionError(f"Gallery filter buttons do not align at {width}px: {right_edge}, {preview_right}")
                        bounds(preview_button, "gallery preview button")
                    capture(f"gallery-toolbar-{width}")
                report["checks"].append("gallery-toolbar-right-edges-aligned")
                window.setProperty("currentScreen", 0)

            controller._review = {"id": "synthetic-review", "url": QUrl.fromLocalFile(str(photo_path)).toString(),
                                  "filename": "landscape.png", "author": "example", "media_type": "photo"}
            controller.changed.emit()
            review = next(item for item in window.findChildren(QObject)
                          if item.property("title") == "이 사진을 저장할까요?")
            controller.reviewReady.emit()
            review_media = next(item for item in review.property("contentItem").childItems()
                                if item.property("sourceUrl") is not None)
            wait(lambda: review_media.property("ready"), "save review photo")
            for width, height in ((680, 480), (1080, 760), (1600, 1000)):
                window.resize(width, height)
                settle(.1)
                data = metrics(review, review_media)
                if not baseline and (data["dialog"][0] < width - 26 or data["dialog"][1] < height - 26):
                    raise AssertionError("Save review retained its old fixed size cap")
                action_buttons(review, ("닫기", "더보기", "건너뛰기", "저장"))
                if not baseline:
                    action_buttons(review, ("−", "+", "화면 맞춤", "원본 100%", "창 최대화"))
                report["review"][f"{width}x{height}"] = data
                if width == 1080:
                    capture("save-review")
            report["checks"].append("save-review-sizes-actions-aspect")
            if not baseline:
                invoke(find("reviewMaximizeButton"), "clicked")
                wait(lambda: window.visibility() == QWindow.Visibility.Maximized, "review window maximization")
                invoke(find("reviewMaximizeButton"), "clicked")
                wait(lambda: window.visibility() == QWindow.Visibility.Windowed, "review window restoration")
                report["checks"].append("review-native-maximize-restore")
            invoke(review, "close")
            wait(lambda: not review_media.property("activeSource").toString(), "review release")
            # Both popups must release the actual image file when closed.
            parked = photo_path.with_suffix(".parked")
            photo_path.rename(parked)
            parked.rename(photo_path)
            report["checks"].append("closed-preview-file-released")

            if features:
                # Use the worker's bounded public metadata shape, current-job
                # queue and actual background first-frame decoder. Never start
                # a real downloader, access a login, or contact X.
                from datetime import datetime, timezone
                from luvbox.core import _media_details, _saved_post_details
                download = controller.download
                window.setProperty("currentScreen", 0)
                window.resize(1080, 1000)
                if not download.start(object()):
                    raise AssertionError("The synthetic current job did not start")
                body = "<b>literal body</b>\nsecond line & <not-html>"
                def saved(path, kind="photo"):
                    metadata = {"tweet_id": "123", "num": 1, "type": kind, "extension": path.suffix.lstrip("."),
                                "_luvbox_path": str(path), "author": {"name": "example", "nick": "<Display Name>"},
                                "content": body, "date": datetime(2026, 10, 25, 3, 30, tzinfo=timezone.utc)}
                    service.pending.append({"type": "saved", "job_id": 7, "path": str(path),
                                            **_media_details(metadata), **_saved_post_details(metadata)})
                    download._pump()
                    settle()
                panel, toggle = find("savedDetailsPanel"), find("savedDetailsToggle")
                thumb, saved_body = find("savedMediaThumbnail"), find("savedTweetBody")
                saved(photo_path)
                if panel.property("visible") or decoded or download.currentSavedItem.get("preview_url"):
                    raise AssertionError("The hidden optional card decoded a saved image")
                if download.currentSavedItem["filename"] != photo_path.name:
                    raise AssertionError("The hidden card lost the latest saved item")
                toggle.setProperty("checked", True)
                invoke(toggle, "toggled")
                wait(lambda: download.detailsEnabled and bool(download.currentSavedItem.get("preview_url"))
                     and thumb.property("paintedWidth") > 0, "live details toggle and thumbnail")
                wait(lambda: not controller._pending_settings, "details preference commit")
                if not panel.property("visible") or not toggle.property("enabled") or not download.running:
                    raise AssertionError("Details could not be enabled during the running save")
                if saved_body.property("text") != body:
                    raise AssertionError("Saved post text was not displayed as literal plain text")
                if saved_body.property("length") != len(body):
                    raise AssertionError("The saved body interpreted HTML markup")
                if not download.currentSavedItem["preview_url"].startswith("data:image/png;base64,"):
                    raise AssertionError("The saved detail preview did not use a bounded static PNG")
                last = dict(download.currentSavedItem)
                service.pending.extend([{"type": "progress", "job_id": 7, "downloaded": 1, "total": 2},
                                        {"type": "skipped", "job_id": 7, "path": str(portrait)},
                                        {"type": "saved", "job_id": 6, "path": str(portrait)}])
                download._pump()
                settle()
                if download.currentSavedItem != last or len(decoded) != 1:
                    raise AssertionError("Progress/skipped/stale-job events replaced or redecoded the saved item")
                report["checks"].append("saved-card-live-toggle-plaintext-and-stable-item")
                scroll = panel.parentItem()
                while scroll is not None and scroll.property("contentY") is None:
                    scroll = scroll.parentItem()
                if scroll is not None:
                    scroll.setProperty("contentY", max(0, scroll.property("contentHeight") - scroll.height()))
                    settle()
                capture("saved-details")
                toggle.setProperty("checked", False)
                invoke(toggle, "toggled")
                wait(lambda: not download.detailsEnabled and not controller._pending_settings, "details disable")
                if panel.property("visible") or thumb.property("source").toString():
                    raise AssertionError("Hiding the card retained an active image source")
                gif = directory / "saved-animation.gif"
                with Image.new("RGB", (64, 48), "red") as red, Image.new("RGB", (64, 48), "blue") as blue:
                    red.save(gif, save_all=True, append_images=[blue], duration=[100, 100], loop=0)
                saved(gif, "animated_gif")
                if len(decoded) != 1 or download.currentSavedItem.get("preview_url"):
                    raise AssertionError("A hidden GIF detail triggered decoding")
                toggle.setProperty("checked", True)
                invoke(toggle, "toggled")
                wait(lambda: len(decoded) == 2 and not download.currentSavedItem["preview_loading"]
                     and thumb.property("paintedWidth") > 0, "static GIF first frame")
                gif_url = thumb.property("source").toString()
                settle(.25)
                if (not gif_url.startswith("data:image/png;base64,") or thumb.property("source").toString() != gif_url
                        or panel.findChildren(QMediaPlayer)
                        or any("AnimatedImage" in item.metaObject().className() for item in panel.findChildren(QObject))):
                    raise AssertionError("The saved GIF card animated instead of showing one static first frame")
                from luvbox.media import ffmpeg_path
                video = directory / "saved-video.mp4"
                subprocess.run([str(ffmpeg_path()), "-nostdin", "-v", "error", "-f", "lavfi", "-i",
                                "testsrc2=size=64x48:rate=8", "-t", "0.5", "-an", "-c:v", "mpeg4", str(video)],
                               check=True, capture_output=True, timeout=20,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                saved(video, "video")
                wait(lambda: len(decoded) == 3 and not download.currentSavedItem["preview_loading"]
                     and thumb.property("paintedWidth") > 0, "static video first frame")
                if not thumb.property("source").toString().startswith("data:image/png;base64,"):
                    raise AssertionError("The saved video card started media playback")
                if any(thread == get_ident() for _path, thread in decoded):
                    raise AssertionError("A saved first frame decoded on the Qt UI thread")
                report["checks"].append("saved-card-static-gif-video-first-frames-off-thread")
                service.pending.extend([{"type": "finished", "job_id": 7, "status": "finished", "saved": 3},
                                        {"type": "process_exit", "job_id": 7, "code": 0}])
                download._pump()
                wait(lambda: not download.running, "synthetic job exit")
            qml_errors = [text for text in messages if re.search(
                r"ReferenceError|TypeError|Cannot assign|Cannot read|Binding loop|is not a type|failed to load", text)]
            if qml_errors:
                raise AssertionError("QML errors: " + str(qml_errors))
            report["qml_errors"] = qml_errors
            return report
        finally:
            if controller is not None:
                controller.requestClose()
                wait(lambda: controller.shutdownReady, "controller shutdown")
            if wheel is not None:
                wheel.close()
            if window is not None and isValid(window):
                window.close()
            if engine is not None and isValid(engine):
                delete(engine)
                settle()
            qInstallMessageHandler(old_handler)


@unittest.skipUnless(find_spec("PySide6") is not None, "Qt dependencies are not installed")
class PreviewSizeTests(unittest.TestCase):
    def test_product_preview_uses_available_space_without_hiding_actions(self):
        environment = dict(os.environ, QT_QPA_PLATFORM="offscreen", QT_QUICK_BACKEND="software",
                           PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
        result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scene"],
                                cwd=ROOT, env=environment, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=45,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(len(report["checks"]), 5)
        self.assertEqual(report["qml_errors"], [])
        # Navigation and zoom controls occupy one row; the long description
        # must still leave substantially more image space than the old326px.
        self.assertGreater(report["gallery"]["1080x760"]["canvas"][1], 420)

    def test_saved_details_and_preview_controls_work_in_the_real_window(self):
        for scale in ("1", "1.5"):
            with self.subTest(scale=scale):
                environment = dict(os.environ, QT_QPA_PLATFORM="offscreen", QT_QUICK_BACKEND="software",
                                   QT_SCALE_FACTOR=scale, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
                result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scene", "--features"],
                                        cwd=ROOT, env=environment, capture_output=True, text=True,
                                        encoding="utf-8", errors="replace", timeout=55,
                                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                report = json.loads(result.stdout)
                self.assertEqual(len(report["checks"]), 10)
                self.assertEqual(report["qml_errors"], [])


if __name__ == "__main__":
    screenshots = sys.argv[sys.argv.index("--screenshots") + 1] if "--screenshots" in sys.argv else None
    print(json.dumps(check_scene(baseline="--baseline" in sys.argv, screenshots=screenshots,
                                 features="--features" in sys.argv)))

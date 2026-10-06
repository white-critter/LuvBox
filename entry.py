"""Entry point shared by Python and the packaged Windows application."""

import json
import os
import sys
from pathlib import Path


def main() -> int:
    # PyInstaller's windowed interpreter has no standard streams. Libraries
    # may still expect these attributes while running GUI or diagnostic code.
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    if len(sys.argv) > 1 and sys.argv[1] == "--self-test":
        if len(sys.argv) != 3:
            return 2
        report = {"ok": False}
        root = None
        # Keep the check's settings, locks and records out of the user's app
        # data (the worker child process inherits this too).
        import tempfile as _tempfile
        app_data = _tempfile.TemporaryDirectory(prefix="xlikesaver-selftest-", ignore_cleanup_errors=True)
        os.environ["LOCALAPPDATA"] = app_data.name
        try:
            import importlib
            import base64
            import subprocess
            import tempfile
            import tkinter as tk
            from tkinter import font as tkfont
            import certifi
            from gallery_dl import extractor, version
            from xlikesaver.gui import App, worker_command
            from xlikesaver import __version__, metadata
            from xlikesaver.core import check_gallery_hooks
            from xlikesaver.history import HistoryStore
            from xlikesaver.storage import migrate_records, resolve_storage
            from xlikesaver.media import convert_mp4_to_gif, ffmpeg_path
            from xlikesaver.preview import thumbnail
            from xlikesaver import charts, gallery  # bundled UI modules
            from PIL import Image

            importlib.import_module("gallery_dl.extractor.utils.twitter_transaction_id")
            importlib.import_module("gallery_dl.downloader.http")
            # Every private gallery-dl member the worker replaces still exists.
            check_gallery_hooks()
            match = extractor.find("https://x.com/example/likes")
            assert match and match.subcategory == "likes"
            assert Path(certifi.where()).is_file()
            root = tk.Tk()
            root.withdraw()
            app = App(root)
            root.update_idletasks()
            from xlikesaver import fonts, theme
            actual_font = tkfont.Font(root=root, font=theme.BODY).actual("family")
            assert actual_font == fonts.FAMILY
            report["font"] = actual_font
            for name in fonts.FILES:
                assert (fonts.asset_directory() / name).is_file()
            original_mode = theme.mode(root)
            theme.apply(root, "dark")
            assert app.log.cget("background") == theme.DARK["CARD"]
            theme.apply(root, "light")
            assert app.log.cget("background") == theme.LIGHT["CARD"]
            theme.apply(root, original_mode)
            theme_images = "drawn"
            if getattr(sys, "frozen", False):
                # Both appearances were drawn once and kept for the next start.
                cached = list((Path(app_data.name) / "XlikeSaver" / "cache").glob(f"theme-{__version__}-*.zip"))
                assert len(cached) == 2, cached
                theme_images = "cached-light-and-dark"
            assert int(app.progress.cget("highlightthickness")) == 0
            assert app.auth.get()
            assert app.content_view.winfo_manager() == "grid"
            app.show_options()
            assert len(app.option_tabs.tabs()) == 3
            assert app.options_window.footer.winfo_manager() == "pack"
            app._close_options(save=False, restore=True)
            assert app.options_window is None
            assert metadata.available()
            # This also verifies the extracted Perl runtime and Unicode paths.
            with tempfile.TemporaryDirectory(prefix="xlikesaver-check-") as folder:
                target = Path(folder) / "한글 출처 확인.png"
                target.write_bytes(base64.b64decode(
                    "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAEklEQVR4nGNUjl/AwMDAxAAGAA1FASYPxkisAAAAAElFTkSuQmCC"))
                author = {"author": "Example_Author", "author_id": "1234567890",
                          "profile_url": "https://x.com/Example_Author"}
                result = metadata.embed_source(target, "100", author["author"], author["author_id"])
                assert result["status"] == "embedded"
                assert result["author_status"] == "embedded"
                assert metadata.read_source(target) == "https://x.com/i/web/status/100"
                assert metadata.read_author(target) == author
                local_preview = thumbnail(target)
                assert local_preview.image is not None and local_preview.image.size == (2, 2)
                executable = ffmpeg_path()
                assert executable is not None and executable.is_file()
                video = Path(folder) / "GIF 원본 확인.mp4"
                generated = subprocess.run(
                    [str(executable), "-nostdin", "-v", "error", "-f", "lavfi", "-i",
                     "testsrc2=size=64x48:rate=8", "-t", "0.5", "-an", "-c:v", "mpeg4", "-q:v", "2", str(video)],
                    capture_output=True, timeout=15,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                assert generated.returncode == 0
                animated = convert_mp4_to_gif(video)
                with Image.open(animated) as decoded:
                    assert decoded.format == "GIF" and decoded.n_frames >= 4 and decoded.size == (64, 48)
                assert metadata.embed_source(animated, "102", author["author"], author["author_id"])["status"] == "embedded"
                assert metadata.read_source(animated) == "https://x.com/i/web/status/102"
                assert metadata.read_author(animated) == author
                assert thumbnail(video).image is not None and thumbnail(animated).image is not None
                # Smaller GIF preset: fitted size and an exact capped frame count.
                wide = Path(folder) / "GIF 크기 확인.mp4"
                generated = subprocess.run(
                    [str(executable), "-nostdin", "-v", "error", "-f", "lavfi", "-i",
                     "testsrc2=size=640x360:rate=30", "-t", "1", "-an", "-c:v", "mpeg4", "-q:v", "2", str(wide)],
                    capture_output=True, timeout=15,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                assert generated.returncode == 0
                smaller = convert_mp4_to_gif(wide, size="480")
                with Image.open(smaller) as decoded:
                    assert decoded.size == (480, 270) and decoded.n_frames == 12
                from xlikesaver import dialogs
                from xlikesaver.media import can_recycle
                assert isinstance(can_recycle(folder), bool)
                dialog = dialogs.Dialog(root, "확인", "자체 점검", buttons=(("예", True), ("아니요", False)),
                                        default=1, cancel=False)
                assert dialog.default_button is dialog.buttons[1]
                dialog.buttons[0].invoke()
                assert dialog.result is True
                resident = Path(folder) / "상주 출처 확인.png"
                resident.write_bytes(target.read_bytes())
                with metadata.persistent():
                    assert metadata._session is not None
                    assert metadata.embed_source(resident, "101", author["author"], author["author_id"])["status"] == "embedded"
                    assert metadata.read_source(resident) == "https://x.com/i/web/status/101"
                    assert metadata.read_author(resident) == author
                assert metadata._session is None
                paths = resolve_storage(Path(folder), "example")
                assert paths.media_dir == Path(folder).resolve()
                store = HistoryStore.for_storage(paths)
                try:
                    assert store.summary()["files"] == 0
                    store.record_media("flat", {"tweet_id": "100", "num": 1, "type": "photo",
                                                "author": {"name": author["author"], "id": author["author_id"]},
                                                "_xlikesaver_metadata_status": "embedded",
                                                "_xlikesaver_author_metadata_status": "embedded"}, target)
                    previous = store.records()[0]
                finally:
                    store.close()
                # Live records are in app data; the media folder gets a snapshot.
                assert not paths.record_dir.is_relative_to(Path(folder))
                relocated = resolve_storage(Path(folder) / "새 저장 폴더", "example")
                migration = migrate_records(paths, relocated)
                assert migration.changed and migration.media_records == 1
                assert (relocated.snapshot_dir / ".xlikesaver-history.sqlite3").is_file()
                store = HistoryStore.for_storage(relocated)
                try:
                    store.import_legacy(refresh=True)
                    record = store.records()[0]
                    assert record["path"] == str(target) and store.is_media_path(target)
                    assert record["saved_at"] == previous["saved_at"]
                    assert record["metadata_status"] == "embedded"
                    assert record["author_metadata_status"] == "embedded"
                    assert record["author_id"] == author["author_id"]
                    page = store.record_page(page=9, page_size=50)
                    assert page["total"] == 1 and page["page"] == 0 and len(page["rows"]) == 1
                    assert store.run_page()["total"] == 0
                    # Like order is kept beside the records and sorts them.
                    run = store.start_run("flat")
                    order = store.like_order(run)
                    order.visit("100")
                    order.flush()
                    store.finish_run(run, "finished")
                    liked = store.record_page(order="liked", period=("posted", "", ""))["rows"][0]
                    assert liked["liked_before"] and liked["like_key"] is not None
                    store.record_tweets([("100", "자체 점검 글", ["점검"])])
                    assert store.record_page(query="#점검")["total"] == 1
                    assert store.record_page(author="Example_Author")["total"] == 1
                    assert store.statistics()["authors"][0]["files"] == 1
                    store.record_media("author", {"tweet_id": "100", "num": 1, "type": "photo",
                                                   "author": {"name": author["author"], "id": author["author_id"]}},
                                       target, status="existing")
                    totals = store.overview()
                    assert totals["files"] == 1 and totals["record_count"] == 2
                    assert totals["photo"] == 1 and totals["bytes"] == target.stat().st_size
                    assert totals["stats"]["authors"][0]["files"] == 1
                    assert charts.bins([("2026-01", 1), ("2026-03", 2)])[0][1] == ("2026-02", 0)
                    assert gallery.TILE == (150, 112)
                    # Damaged-database salvage reads the catalog page by page.
                    from xlikesaver.recovery import salvage
                    assert len(salvage(store.path)["media"]["rows"]) == 2
                finally:
                    store.close()
                # Exercise the actual child executable under the frozen
                # parent's environment, stopping before auth or HTTP requests.
                request, events, stop = (Path(folder) / name for name in
                                         ("request.json", "events.jsonl", "stop.flag"))
                request.write_text(json.dumps({"account": "example", "output_dir": folder}), encoding="utf-8")
                stop.touch()
                worker = subprocess.run(worker_command(request, events, stop),
                                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL, timeout=30,
                                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                assert worker.returncode == 0
                final = json.loads(events.read_text(encoding="utf-8").splitlines()[-1])
                assert final["type"] == "finished" and final["status"] == "stopped"
                assert final["saved"] == 0
            report = {"ok": True, "version": __version__, "gallery_dl": version.__version__,
                      "font": actual_font, "record_pagination": "queried-and-clamped",
                      "appearance": "light-dark-live-switch", "theme_images": theme_images,
                      "local_previews": "photo-video-gif",
                      "gif_conversion": "animated-with-source-author-metadata", "gif_size_presets": "fitted-exact-frames",
                      "dialogs": "themed", "like_order": "recorded-and-sorted", "tweet_text": "searchable",
                      "statistics": "authors-and-months", "catalog_recovery": "salvage-verified",
                      "records": "local-with-folder-snapshot", "progress": "rounded-borderless",
                      "workspace": "sidebar-scrollable-dashboard-three-settings-tabs",
                      "tk": tk.TkVersion, "python": sys.version.split()[0],
                      "extractor": type(match).__name__, "frozen": bool(getattr(sys, "frozen", False)),
                      "source_metadata": "embedded-and-read-back", "author_metadata": "handle-profile-and-user-id-read-back",
                      "flat_storage": "direct",
                      "worker": "stopped-before-authentication", "record_relocation": "preserved-old-file",
                      "gallery_hooks": "verified", "resident_exiftool": "embedded-and-read-back"}
        except Exception as exc:
            report["error"] = type(exc).__name__
        finally:
            if root is not None:
                root.destroy()
        Path(sys.argv[2]).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return 0 if report["ok"] else 1

    if len(sys.argv) > 1 and sys.argv[1] == "--worker":
        if len(sys.argv) != 5:
            return 2
        request_path, events_path, stop_path = map(Path, sys.argv[2:])
        with events_path.open("a", encoding="utf-8", buffering=1) as events:
            def emit(event: dict) -> None:
                events.write(json.dumps(event, ensure_ascii=False) + "\n")
                events.flush()

            try:
                from xlikesaver.core import JobOptions, run_worker

                options = JobOptions(**json.loads(request_path.read_text(encoding="utf-8")))
                return run_worker(options, emit, stop_path)
            except Exception as exc:
                # Do not serialize exception contents: they may contain a URL
                # with credentials supplied by an upstream library.
                emit({"type": "finished", "status": "error",
                      "message": f"작업 중 오류가 발생했습니다 ({type(exc).__name__}). 입력과 작업 로그를 확인해 주세요."})
                return 1

    if len(sys.argv) > 1 and sys.argv[1] == "--run-saved":
        # Unattended run for Windows Task Scheduler; see xlikesaver/scheduled.py.
        if len(sys.argv) > 3 or (len(sys.argv) == 3 and sys.argv[2] != "--full"):
            return 2
        from xlikesaver.scheduled import run_saved

        return run_saved(full=len(sys.argv) == 3)

    if len(sys.argv) > 1:
        return 2

    from xlikesaver.gui import launch

    launch()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

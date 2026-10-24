"""Offline packaged-runtime checks using only generated media and temporary records."""

import base64
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from . import __version__


def _backend_checks():
    import certifi
    from gallery_dl import extractor, version
    from PIL import Image
    from . import metadata
    from .core import check_gallery_hooks
    from .download_service import worker_command
    from .history import HistoryStore
    from .storage import migrate_records, resolve_storage
    from .media import convert_mp4_to_gif, ffmpeg_path
    from .preview import thumbnail

    importlib.import_module("gallery_dl.extractor.utils.twitter_transaction_id")
    importlib.import_module("gallery_dl.downloader.http")
    check_gallery_hooks()
    match = extractor.find("https://x.com/example/likes")
    assert match and match.subcategory == "likes"
    assert Path(certifi.where()).is_file()
    assert metadata.available()
    # This also verifies the extracted Perl runtime and Unicode paths.
    with tempfile.TemporaryDirectory(prefix="luvbox-check-") as folder:
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
        # Previews kept for files deleted later: WebP must be bundled.
        from luvbox import thumbcache
        previews = thumbcache.Cache("example")
        try:
            previews.put(("100", 1, "photo"), local_preview.image)
            assert previews.get(("100", 1, "photo")).startswith(b"RIFF")
        finally:
            previews.close()
        assert thumbcache.recall("example", ("100", 1, "photo")) is not None
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
        resident = Path(folder) / "상주 출처 확인.png"
        resident.write_bytes(target.read_bytes())
        with metadata.persistent():
            assert metadata._session is not None
            assert Path(metadata._session._process.args[0]).name.lower() == "perl.exe"
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
                                        "_luvbox_metadata_status": "embedded",
                                        "_luvbox_author_metadata_status": "embedded"}, target)
            previous = store.records()[0]
        finally:
            store.close()
        # Live records are in app data; the media folder gets a snapshot.
        assert not paths.record_dir.is_relative_to(Path(folder))
        relocated = resolve_storage(Path(folder) / "새 저장 폴더", "example")
        migration = migrate_records(paths, relocated)
        assert migration.changed and migration.media_records == 1
        assert (relocated.snapshot_dir / ".luvbox-history.sqlite3").is_file()
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
            # A deleted file is listed once, can stay deleted, and is
            # requested again only by its archive key.
            assert totals["missing_files"] == 0 and totals["missing"] == []
            gone = relocated.media_dir / "200_1.jpg"
            gone.write_bytes(b"deleted later")
            store.record_media("flat", {"tweet_id": "200", "num": 1, "type": "photo"}, gone)
            gone.unlink()
            store.import_legacy(refresh=True)
            [missing] = store.missing_media()
            assert missing["archive_key"] == "200_1" and store.summary()["missing_files"] == 1
            store.set_missing_ignored([("200", 1, "photo")], True)
            assert store.summary()["ignored_missing"] == 1 and store.ignored_media() == {("200", 1, "photo")}
            from luvbox.core import JobOptions
            retry = JobOptions("example", folder, targets=["200_1", "200_1"]).validate()
            assert retry.targets == ("200_1",)
            # Exercise external removal and restoration with a fixture.
            disposable = relocated.media_dir / "300_1.jpg"
            disposable.write_bytes(b"isolated external removal self-test")
            store.record_media("flat", {"tweet_id": "300", "num": 1, "type": "photo"}, disposable)
            receipt = disposable.with_name("externally-moved-selftest.jpg")
            disposable.rename(receipt)
            store.import_legacy(refresh=True)
            assert store.summary()["missing_files"] == 1
            assert ("300", 1, "photo") not in store.ignored_media()
            receipt.rename(disposable)
            store.import_legacy(refresh=True)
            assert store.summary()["missing_files"] == 0
            assert store.summary()["files"] == 2
            # Damaged-database salvage reads the catalog page by page.
            from luvbox.recovery import salvage
            assert len(salvage(store.path)["media"]["rows"]) == 4
        finally:
            store.close()
        # Exercise the protected deletion and restore bookkeeping with
        # a temporary GIF. Substitute only the native move: a self-test
        # must never touch the user's actual Windows Recycle Bin.
        import threading
        from luvbox import deletion, recycling
        disposable_gif = relocated.media_dir / "400_animated_gif_1.gif"
        disposable_gif.write_bytes(animated.read_bytes())
        store = HistoryStore.for_storage(relocated)
        try:
            store.record_media("flat", {"tweet_id": "400", "num": 1, "type": "animated_gif"}, disposable_gif)
            chosen = next(row for row in store.records() if row["tweet_id"] == "400")
        finally:
            store.close()
        chosen["_delete_signature"] = recycling.file_signature(disposable_gif)
        parked = Path(folder) / "simulated-recycled-selftest.gif"
        original_recycle = deletion.recycle_file

        def simulate_recycle(path, *, expected):
            assert Path(path) == disposable_gif
            assert recycling.file_signature(path) == expected
            Path(path).rename(parked)
            return str(parked)

        deletion.recycle_file = simulate_recycle
        try:
            outcome = deletion.run_deletion(relocated, [chosen], True, threading.Event(), lambda _: None)
        finally:
            deletion.recycle_file = original_recycle
        assert outcome["recycled"] == 1 and outcome["failed"] == 0
        assert not disposable_gif.exists() and parked.is_file()
        store = HistoryStore.for_storage(relocated)
        try:
            after = next(row for row in store.records() if row["tweet_id"] == "400")
            assert after["path"] == "" and after["last_path"] == str(disposable_gif)
            assert after["saved_at"] == chosen["saved_at"]
            assert ("400", 1, "animated_gif") in store.ignored_media()
            assert store.deletion_page()["rows"][0]["status"] == "recycled"
            parked.rename(disposable_gif)
            store.import_legacy(refresh=True)
            restored = next(row for row in store.records() if row["tweet_id"] == "400")
            assert restored["path"] == str(disposable_gif)
            assert ("400", 1, "animated_gif") not in store.ignored_media()
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
    return {"ok": True, "gallery_dl": version.__version__,
            "extractor": type(match).__name__, "gallery_hooks": "verified",
            "source_metadata": "embedded-and-read-back", "author_metadata": "handle-profile-and-user-id-read-back",
            "resident_exiftool": "embedded-and-read-back", "local_previews": "photo-video-gif",
            "gif_conversion": "animated-with-source-author-metadata", "gif_size_presets": "fitted-exact-frames",
            "record_relocation": "preserved-old-file", "record_pagination": "queried-and-clamped",
            "like_order": "recorded-and-sorted", "tweet_text": "searchable",
            "catalog_recovery": "salvage-verified", "missing_previews": "webp-kept-on-this-pc",
            "library_deletion": "simulated-recycle-and-gif-restoration", "worker": "stopped-before-authentication"}


def run(destination, download_check):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    report = {"ok": False, "version": __version__, "frozen": bool(getattr(sys, "frozen", False)),
              "python": sys.version.split()[0], "isolated": True, "authentication_or_network": False}
    environment = {name: os.environ.get(name) for name in ("LOCALAPPDATA", "APPDATA")}
    try:
        with tempfile.TemporaryDirectory(prefix="luvbox-selftest-") as app_data:
            try:
                os.environ["LOCALAPPDATA"] = app_data
                os.environ["APPDATA"] = str(Path(app_data) / "roaming")
                report["backend"] = _backend_checks()
                report["download_start"] = download_check()
                assert report["download_start"]["ok"], "offline download initialization failed"
                from .qt_smoke import run as run_qt
                ui_report = destination.with_name(destination.stem + "-qt.json")
                assert run_qt(ui_report) == 0, "Qt startup failed"
                report["ui"] = json.loads(ui_report.read_text(encoding="utf-8"))
                assert report["ui"]["ok"], "Qt startup did not pass"
            finally:
                for name, value in environment.items():
                    if value is None:
                        os.environ.pop(name, None)
                    else:
                        os.environ[name] = value
        report["temporary_data_removed"] = True
        report["ok"] = True
    except Exception as error:
        report["ok"] = False
        report["error"] = type(error).__name__
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if report["ok"] else 1

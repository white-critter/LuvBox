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
        try:
            import importlib
            import base64
            import subprocess
            import tempfile
            import tkinter as tk
            import certifi
            from gallery_dl import extractor, version
            from xlikesaver.gui import App, worker_command
            from xlikesaver import __version__, metadata
            from xlikesaver.core import check_gallery_hooks
            from xlikesaver.history import HistoryStore
            from xlikesaver.storage import migrate_records, resolve_storage

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
            assert app.auth.get()
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
                relocated = resolve_storage(Path(folder) / "새 저장 폴더", "example")
                migration = migrate_records(paths, relocated)
                assert migration.changed and migration.media_records == 1
                store = HistoryStore.for_storage(relocated)
                try:
                    store.import_legacy(refresh=True)
                    record = store.records()[0]
                    assert record["path"] == str(target) and store.is_media_path(target)
                    assert record["saved_at"] == previous["saved_at"]
                    assert record["metadata_status"] == "embedded"
                    assert record["author_metadata_status"] == "embedded"
                    assert record["author_id"] == author["author_id"]
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

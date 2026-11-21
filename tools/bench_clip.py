"""Benchmark LuvBox's SigLIP 2 image encoder and measure its zero-shot cuts (development tool).

Encodes photos of a folder (by default the active LuvBox library's) and reports
speed and the default tags' zero-shot results. ``--model`` points at another
ONNX file of the same model (for example ``vision_model_uint8.onnx``) to compare
weights. ``--calibrate`` reads the categories the person confirmed in the active
catalog (read-only) and prints, per default tag, the relative similarity cut that
balances precision and recall (F0.5), and how close photos of one kind are to
each other (for the nearest-neighbour rule of ``tag_learner``).

Only ``--apply-tags`` and ``--clear-tags`` write to the catalog.
"""

from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
from pathlib import Path
import random
import sqlite3
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

from luvbox.clip_classifier import (  # noqa: E402
    CATEGORY_NAMES,
    ZERO_SHOT_CUTS,
    classify_detailed,
    ensure_model,
    get_category_embeddings,
    get_session,
    is_available,
    release_session,
    resolve_model_path,
)
from luvbox.history import HistoryStore  # noqa: E402
from luvbox.settings import load_settings  # noqa: E402
from luvbox.storage import resolve_storage  # noqa: E402

VALID_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


def get_active_storage_info():
    """Return the active media folder and storage paths from the user settings."""
    try:
        s = load_settings()
        out_dir = s.get("output_dir", "")
        if out_dir:
            paths = resolve_storage(out_dir, s.get("account", ""), s.get("separate_accounts", False))
            return paths.media_dir, paths
    except Exception:
        pass
    return None, None


def session_for(model: Path | None):
    if model is None:
        if not is_available():
            print("Model not found or incomplete; downloading the SigLIP 2 vision model (~90 MB)...")
            ensure_model()
        return get_session(), resolve_model_path()
    import onnxruntime as ort

    options = ort.SessionOptions()
    options.inter_op_num_threads = 1
    options.intra_op_num_threads = 4
    options.enable_cpu_mem_arena = False
    return ort.InferenceSession(str(model), options, providers=["CPUExecutionProvider"]), model


def encode(paths, session, batch_size):
    vectors, kept = [], []
    start = time.perf_counter()
    for index in range(0, len(paths), batch_size):
        batch = paths[index:index + batch_size]
        for path, (_tag, _confidence, vector) in zip(batch, classify_detailed(batch, batch_size=batch_size,
                                                                              session=session)):
            if vector is not None:
                vectors.append(vector)
                kept.append(path)
        done = min(index + batch_size, len(paths))
        rate = done / max(1e-9, time.perf_counter() - start)
        sys.stdout.write(f"\rEncoding: {done:,} / {len(paths):,} ({rate:5.1f} photos/s)")
        sys.stdout.flush()
    print()
    elapsed = time.perf_counter() - start
    return (np.stack(vectors) if vectors else np.zeros((0, 768), np.float32)), kept, elapsed


def relative(matrix):
    similarity = matrix @ get_category_embeddings().T
    return similarity - similarity.mean(axis=1, keepdims=True)


def calibrate(paths, session, args):
    """Labels from the catalog the person confirmed; read-only."""
    catalog = paths.record_dir / ".luvbox-history.sqlite3"
    with closing(sqlite3.connect(catalog.as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "ai_tag_feedback" in tables:
            rows = db.execute("SELECT m.path AS path, f.tag AS tag, f.label AS label FROM ai_tag_feedback f JOIN media m "
                              "ON m.folder_mode=f.folder_mode AND m.tweet_id=f.tweet_id AND m.num=f.num "
                              "AND m.media_type=f.media_type WHERE m.media_type='photo' AND m.path!=''").fetchall()
            labelled = {}
            for row in rows:
                labelled.setdefault(row["path"], {})[row["tag"]] = row["label"]
        else:
            rows = db.execute("SELECT m.path AS path, f.category AS category FROM ai_feedback f JOIN media m "
                              "ON m.folder_mode=f.folder_mode AND m.tweet_id=f.tweet_id AND m.num=f.num "
                              "AND m.media_type=f.media_type WHERE m.media_type='photo' AND m.path!=''").fetchall()
            labelled = {row["path"]: ({row["category"]: 1} if row["category"] else
                                      {name: -1 for name in CATEGORY_NAMES}) for row in rows}
    items = [(Path(path), answers) for path, answers in labelled.items() if Path(path).is_file()]
    random.Random(20261120).shuffle(items)
    items = items[: args.count] if args.count > 0 else items
    if not items:
        print("The active catalog has no confirmed AI categories to measure against.")
        return
    print(f"Confirmed photos used: {len(items):,}")
    matrix, kept, elapsed = encode([path for path, _answers in items], session, args.batch_size)
    answers = {path: found for path, found in items}
    scores = relative(matrix)
    print(f"\n{len(kept) / max(elapsed, 1e-9):.1f} photos/s")
    print(f"\n{'tag':10s} {'yes':>5s} {'no':>5s}  {'cut':>7s} {'prec':>5s} {'rec':>5s}   current cut")
    for index, name in enumerate(CATEGORY_NAMES):
        truth = np.array([answers[path].get(name, 0) for path in kept])
        known = truth != 0
        if not known.any() or not (truth > 0).any():
            print(f"{name:10s} {int((truth > 0).sum()):5d} {int((truth < 0).sum()):5d}   (no 'yes' answers)"
                  f"   {ZERO_SHOT_CUTS[index]:.3f}")
            continue
        values, positive = scores[known, index], truth[known] > 0
        best = None
        for cut in np.unique(np.round(values, 4)):
            chosen = values >= cut
            tp = int((chosen & positive).sum())
            fp = int((chosen & ~positive).sum())
            precision = tp / max(1, tp + fp)
            recall = tp / max(1, int(positive.sum()))
            f05 = 1.25 * precision * recall / max(1e-9, 0.25 * precision + recall)
            if best is None or f05 > best[0]:
                best = (f05, cut, precision, recall)
        print(f"{name:10s} {int(positive.sum()):5d} {int((~positive).sum()):5d}  {best[1]:7.4f} {best[2]:5.2f} "
              f"{best[3]:5.2f}   {ZERO_SHOT_CUTS[index]:.3f}")
    similarity = matrix @ matrix.T
    np.fill_diagonal(similarity, -1)
    nearest = similarity.max(axis=1)
    print("\nNearest other photo (cosine) percentiles 50/90/99:",
          " / ".join(f"{value:.3f}" for value in np.percentile(nearest, [50, 90, 99])))


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dir", type=Path, default=None, help="Folder of images (default: the active library)")
    parser.add_argument("--model", type=Path, default=None, help="Another ONNX file of the same model to compare")
    parser.add_argument("--count", type=int, default=50, help="Number of images (0 for all, default: 50)")
    parser.add_argument("--batch-size", type=int, default=16, help="Batch size for inference (default: 16)")
    parser.add_argument("--calibrate", action="store_true",
                        help="Measure zero-shot cuts against the categories confirmed in the active catalog")
    parser.add_argument("--apply-tags", action="store_true", help="Save the best default tag of each photo with source='ai'")
    parser.add_argument("--clear-tags", action="store_true", help="Remove all AI tags from the catalog")
    parser.add_argument("--stats", action="store_true", help="Show current AI tag counts in the catalog")
    parser.add_argument("--verbose", "-v", action="store_true", help="Print per-image results")
    args = parser.parse_args()

    print("=" * 65)
    print("  LuvBox SigLIP 2 benchmark")
    print("=" * 65)
    default_media_dir, storage_paths = get_active_storage_info()

    if args.clear_tags or args.stats:
        if not storage_paths:
            sys.exit("Could not locate the active catalog.")
        with closing(HistoryStore.for_storage(storage_paths, create_media=False)) as store:
            if args.clear_tags:
                print(f"Cleared {store.clear_ai_tags():,} AI tags.")
            if args.stats:
                stats = store.ai_tag_stats()
                total = sum(stats.values())
                for tag, count in stats.items():
                    print(f"  #{tag:10s}: {count:6,} ({count / max(total, 1) * 100:5.1f}%)")
        if not (args.apply_tags or args.dir or args.calibrate or "--count" in sys.argv):
            return

    session, model_path = session_for(args.model)
    print(f"Model:           {model_path}")
    print(f"Batch size:      {args.batch_size}")
    try:
        if args.calibrate:
            if not storage_paths:
                sys.exit("Could not locate the active catalog.")
            calibrate(storage_paths, session, args)
            return
        target_dir = args.dir if args.dir else default_media_dir
        if not target_dir or not target_dir.is_dir():
            sys.exit(f"Folder does not exist: {target_dir}")
        files = [p for p in target_dir.glob("*.*") if p.suffix.lower() in VALID_EXTENSIONS]
        sample = files[: args.count] if args.count > 0 else files
        if not sample:
            sys.exit(f"No supported images in {target_dir}")
        print(f"Testing on:      {len(sample):,} images (of {len(files):,})\n")
        matrix, kept, elapsed = encode(sample, session, args.batch_size)
    finally:
        if args.model is None:
            release_session()

    from luvbox.clip_classifier import zero_shot_probabilities

    probabilities = zero_shot_probabilities(matrix)
    tags = [[CATEGORY_NAMES[i] for i in np.flatnonzero(row >= 0.65)] for row in probabilities]
    if args.verbose:
        for path, row in zip(kept, tags):
            print(f"{path.name[:40]:40s} -> {', '.join('#' + t for t in row) or '(none)'}")
    counts = Counter(tag for row in tags for tag in row)
    print("\n" + "=" * 65)
    for name in CATEGORY_NAMES:
        print(f"  #{name:8s}: {counts[name]:6,} ({counts[name] / max(len(kept), 1) * 100:5.1f}%)")
    print(f"  (none)   : {sum(1 for row in tags if not row):6,}")
    print(f"  several  : {sum(1 for row in tags if len(row) > 1):6,}")
    print("-" * 65)
    print(f"  Total time:  {elapsed:.2f} s   ({len(kept) / max(elapsed, 1e-9):.1f} photos/s,"
          f" {elapsed / max(len(kept), 1) * 1000:.1f} ms each)")

    if args.apply_tags:
        if not storage_paths:
            sys.exit("Cannot save tags: the active catalog was not found.")
        with closing(HistoryStore.for_storage(storage_paths, create_media=False)) as store:
            by_name = {r["filename"]: r for r in store.photo_records_for_scan() if r.get("filename")}
            rows = [(by_name[p.name]["folder_mode"], by_name[p.name]["tweet_id"], by_name[p.name]["num"],
                     by_name[p.name]["media_type"], row) for p, row in zip(kept, tags) if p.name in by_name]
            changed, tagged = store.replace_ai_tags(rows)
            print(f"\nSaved AI tags: {tagged:,} photos tagged ({changed:,} changed).")


if __name__ == "__main__":
    main()

"""Benchmark and test local ONNX CLIP Zero-shot image classification for LuvBox.

Evaluates throughput, latency, and category distributions on local images.
Optionally writes predicted tags to the LuvBox SQLite catalog with source='ai'.
"""

from __future__ import annotations

import argparse
from collections import Counter
import os
from pathlib import Path
import sys
import time

# Ensure project root is in sys.path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from luvbox.clip_classifier import (
    CATEGORY_NAMES,
    classify_batch,
    classify_image,
    ensure_model,
    is_available,
    resolve_model_path,
)
from luvbox.history import HistoryStore
from luvbox.preview import PHOTO_FORMATS
from luvbox.settings import load_settings
from luvbox.storage import resolve_storage


def get_active_storage_info() -> tuple[Path | None, object | None]:
    """Retrieve active media folder and storage paths from user settings."""
    try:
        s = load_settings()
        out_dir = s.get("output_dir", "")
        account = s.get("account", "")
        separate = s.get("separate_accounts", False)
        if out_dir:
            paths = resolve_storage(out_dir, account, separate)
            return paths.media_dir, paths
    except Exception:
        pass
    return None, None


def main():
    parser = argparse.ArgumentParser(description="Benchmark ONNX CLIP zero-shot image classification.")
    parser.add_argument("--dir", type=Path, default=None, help="Directory containing images (defaults to active LuvBox library)")
    parser.add_argument("--count", type=int, default=50, help="Number of images to benchmark (0 for all, default: 50)")
    parser.add_argument("--batch-size", type=int, default=16, help="Batch size for inference (default: 16)")
    parser.add_argument("--threshold", type=float, default=0.45, help="Confidence threshold cutoff (default: 0.45)")
    parser.add_argument("--apply-tags", action="store_true", help="Save predictions to active SQLite catalog with source='ai'")
    parser.add_argument("--clear-tags", action="store_true", help="Remove all AI tags from the catalog DB")
    parser.add_argument("--stats", action="store_true", help="Show current AI tag counts in the catalog DB")
    parser.add_argument("--verbose", "-v", action="store_true", help="Print per-image prediction details")
    args = parser.parse_args()

    print("=" * 65)
    print("  LuvBox AI CLIP Classification Benchmark")
    print("=" * 65)

    default_media_dir, storage_paths = get_active_storage_info()

    # Handle --clear-tags
    if args.clear_tags:
        if not storage_paths:
            print("Error: Could not locate active storage catalog to clear tags.")
            sys.exit(1)
        with HistoryStore.for_storage(storage_paths, create_media=False) as store:
            cleared = store.clear_ai_tags()
            print(f"Cleared {cleared:,} AI-generated tags from catalog.")
        if not args.stats and not args.apply_tags and args.count <= 0 and not args.dir:
            sys.exit(0)

    # Handle --stats
    if args.stats:
        if not storage_paths:
            print("Error: Could not locate active storage catalog.")
            sys.exit(1)
        with HistoryStore.for_storage(storage_paths, create_media=False) as store:
            stats = store.ai_tag_stats()
            print("\nCurrent AI Tag Statistics in Catalog:")
            if not stats:
                print("  No AI tags found.")
            else:
                total_ai = sum(stats.values())
                for tag, count in stats.items():
                    print(f"  #{tag:10s}: {count:6,} ({count/total_ai*100:5.1f}%)")
                print(f"  Total AI tags: {total_ai:,}")
        if not args.apply_tags and not args.dir and ("--count" not in sys.argv):
            sys.exit(0)

    # Check model availability
    model_path = resolve_model_path()
    if not is_available():
        print(f"Model not found or incomplete at {model_path}.")
        print("Downloading ONNX CLIP vision model (~85 MB)...")
        try:
            ensure_model()
            print("Model ready!")
        except Exception as e:
            print(f"Error preparing model: {e}")
            sys.exit(1)

    print(f"Model Path:      {model_path}")
    print(f"Categories:      {', '.join(CATEGORY_NAMES)}")
    print(f"Batch Size:      {args.batch_size}")
    print(f"Threshold:       {args.threshold:.2f}")

    target_dir = args.dir if args.dir else default_media_dir
    if not target_dir or not target_dir.is_dir():
        print(f"\nError: Target directory does not exist: {target_dir}")
        sys.exit(1)

    print(f"Image Directory: {target_dir}")

    # Gather images
    valid_exts = {f".{fmt.lower()}" for fmt in PHOTO_FORMATS}
    all_files = [p for p in target_dir.glob("*.*") if p.suffix.lower() in valid_exts]
    total_found = len(all_files)
    if total_found == 0:
        print(f"No supported images found in {target_dir}")
        sys.exit(1)

    if args.count > 0:
        sample_files = all_files[: args.count]
    else:
        sample_files = all_files

    eval_count = len(sample_files)
    print(f"Testing on:      {eval_count:,} images (out of {total_found:,} available)\n")

    t_start = time.perf_counter()
    results: list[tuple[str | None, float]] = []

    # Process in batches
    num_batches = (eval_count + args.batch_size - 1) // args.batch_size
    for b_idx in range(num_batches):
        batch = sample_files[b_idx * args.batch_size : (b_idx + 1) * args.batch_size]
        batch_res = classify_batch(batch, threshold=args.threshold, batch_size=args.batch_size)
        results.extend(batch_res)

        processed = len(results)
        pct = (processed / eval_count) * 100
        elapsed = time.perf_counter() - t_start
        rate = processed / elapsed if elapsed > 0 else 0
        sys.stdout.write(f"\rClassifying: {processed:,} / {eval_count:,} ({pct:5.1f}%) - {rate:5.1f} imgs/s")
        sys.stdout.flush()

    t_total = time.perf_counter() - t_start
    print()

    # Detailed report if verbose
    if args.verbose:
        print("\n--- Individual Results ---")
        for p, (tag, conf) in zip(sample_files, results):
            tag_str = f"#{tag}" if tag else "(미분류)"
            print(f"{p.name[:38]:38s} -> {tag_str:10s} (conf: {conf*100:5.1f}%)")

    # Summary distribution
    counts = Counter()
    unclassified = 0
    for tag, conf in results:
        if tag:
            counts[tag] += 1
        else:
            unclassified += 1

    print("\n" + "=" * 65)
    print("  Category Distribution Summary")
    print("=" * 65)
    for cat in CATEGORY_NAMES:
        c = counts[cat]
        pct = (c / eval_count) * 100 if eval_count > 0 else 0
        bar = "█" * int(pct / 4)
        print(f"  #{cat:8s}: {c:6,} ({pct:5.1f}%) {bar}")

    unclass_pct = (unclassified / eval_count) * 100 if eval_count > 0 else 0
    print(f"  (미분류) : {unclassified:6,} ({unclass_pct:5.1f}%)")

    print("-" * 65)
    avg_ms = (t_total / eval_count * 1000) if eval_count > 0 else 0
    throughput = eval_count / t_total if t_total > 0 else 0
    print(f"  Total Time:       {t_total:.2f} s")
    print(f"  Average Latency:  {avg_ms:.1f} ms / image")
    print(f"  Throughput:       {throughput:.1f} images / second")
    print("=" * 65)

    # Handle --apply-tags
    if args.apply_tags:
        if not storage_paths:
            print("\nError: Cannot save tags because active storage catalog was not found.")
            sys.exit(1)

        print("\nApplying tags to catalog database...")
        with HistoryStore.for_storage(storage_paths, create_media=False) as store:
            # Map filename to catalog record
            records = store.photo_records_for_scan()
            name_to_rec = {r["filename"]: r for r in records if r.get("filename")}

            tags_to_save: list[tuple] = []
            for p, (tag, conf) in zip(sample_files, results):
                if not tag:
                    continue
                rec = name_to_rec.get(p.name)
                if rec:
                    tags_to_save.append((rec["folder_mode"], rec["tweet_id"], rec["num"], rec["media_type"], tag))

            if tags_to_save:
                saved = store.save_ai_tags(tags_to_save)
                print(f"Successfully saved {saved:,} AI tags to catalog database!")
            else:
                print("No matching records found in catalog to tag.")


if __name__ == "__main__":
    main()

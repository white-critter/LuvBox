"""Give photos their AI tags with the shared AI profile: one pipeline for every kind of store.

The X catalog (``HistoryStore``) and a folder of the "폴더 정리" tab
(``LocalFolderStore``) offer the same small interface: what identifies a photo
(``row_identity``), its stored features, the person's answers and own tags as
examples (``ai_examples``), and a way to store estimates that never overrides a
decision of the person (``store_ai_tags``). This module only orchestrates:

1. encode the photos the person taught first (they are the examples),
2. make the shared profile's examples of this library match its photos,
3. learn again the tags whose examples changed,
4. encode the rest, scoring each batch as it comes, and
5. score everything again when the rules changed since it was scored.

Nothing heavy is imported at module level: numpy, onnxruntime, the classifier
and the learner are loaded by the work that classifies.
"""

from __future__ import annotations

from contextlib import ExitStack
import threading

from .clip_model import ACTIVE_MODEL_ID, embedding_bytes

ENCODE_BATCH = 16
_TRAIN_LOCK = threading.Lock()


def sync_examples(store, profile, kind, scope, example_id, tags=None) -> bool:
    """Make this store's examples in the shared profile match what its photos say now.

    Every answer about an AI tag, and every photo the person tagged with an AI
    tag themselves, is an example (with the photo's feature). ``tags`` limits
    the work to some tags; by default every tag of the profile.
    """
    names = list(tags) if tags is not None else profile.known_tag_names()
    if not names:
        return False
    rows = [(example_id(ident), tag, label, ACTIVE_MODEL_ID, blob)
            for ident, tag, label, blob in store.ai_examples(names)]
    written, removed = profile.sync_scope(kind, scope, names, rows)
    return bool(written or removed)


def train_dirty(store, profile, tags=None) -> list[str]:
    """Learn again every switched-on tag whose examples changed (or the given ``tags``).

    Returns the tags that found fewer untagged photos to compare with than they
    wanted: a store whose photos are still being encoded has few, and those
    tags are learned once more when the rest are in.
    """
    from . import tag_learner

    with _TRAIN_LOCK:
        active = profile.active_tags()
        built_in = {row["tag"]: row["built_in"] for row in active}
        if tags is None:
            dirty = profile.dirty_tags(ACTIVE_MODEL_ID)
            dirty_set = set(dirty)
            promote = [
                row["tag"] for row in active
                if row["tag"] not in dirty_set
                and row["pos_count"] >= tag_learner.LINEAR_FROM
                and row.get("regime") == "neighbor"
            ]
            candidates = dirty + promote
            promote_set = set(promote)
        else:
            candidates = list(tags)
            promote_set = set()
        short = []
        for tag in candidates:
            if tag not in built_in:
                continue
            positives, negatives = profile.training_data_for_tag(tag, ACTIVE_MODEL_ID)
            background = []
            if len(positives) >= tag_learner.LINEAR_FROM:
                wanted = tag_learner.background_size(len(positives))
                # Most photos do not carry a given tag: a spread of this store's photos without
                # it, and photos taught for other tags, stand in as weak "no" examples.
                background = profile.other_examples(tag, ACTIVE_MODEL_ID, limit=wanted // 4)
                background += store.background_embeddings(tag, wanted - len(background), ACTIVE_MODEL_ID)
                if tag in promote_set and not negatives and not background:
                    continue
                if len(background) < wanted:
                    short.append(tag)
            learned = tag_learner.train_tag(
                tag_learner.as_matrix(positives), tag_learner.as_matrix(negatives),
                tag_learner.as_matrix(background), built_in=bool(built_in.get(tag)))
            if tag in promote_set:
                if learned is not None and learned["regime"] == "linear":
                    profile.save_tag_weight(
                        tag, ACTIVE_MODEL_ID, learned["regime"], learned["weights"],
                        learned["bias"], learned["threshold"], learned["pos_count"], learned["neg_count"])
                continue
            if learned is None:
                profile.delete_tag_weight(tag)
            else:
                profile.save_tag_weight(
                    tag, ACTIVE_MODEL_ID, learned["regime"], learned["weights"],
                    learned["bias"], learned["threshold"], learned["pos_count"], learned["neg_count"])
        return short


def scoring_models(profile):
    """The switched-on tags as ``tag_learner`` models, with the examples the ones that compare need."""
    from . import tag_learner

    models = []
    for rule in profile.scoring_rules(ACTIVE_MODEL_ID):
        weight = rule.get("weight")
        if weight is None and not rule["built_in"]:
            positives, negatives = profile.training_data_for_tag(rule["tag"], ACTIVE_MODEL_ID)
            if not positives:
                continue        # a tag of the person's own waits for its first example
            regime = "neighbor"
            rule = {**rule, "weight": {"regime": "neighbor"}}
        else:
            regime = (weight or {}).get("regime", "zeroshot")
            positives, negatives = profile.training_data_for_tag(
                rule["tag"], ACTIVE_MODEL_ID, include_positives=(regime == "neighbor"))
        models.append(tag_learner.build_model(
            rule, tag_learner.as_matrix(positives) if regime == "neighbor" else None,
            tag_learner.as_matrix(negatives)))
    return [model for model in models if model.usable]


def score_rows(store, rows, models) -> tuple[int, int]:
    """Give stored features their AI tags. Returns ``(photos changed, photos tagged among them)``."""
    import numpy as np

    from . import tag_learner
    from .ai_anchors import zero_shot_probabilities

    size = embedding_bytes(ACTIVE_MODEL_ID)
    idents, vectors = [], []
    for row in rows:
        blob = row["embedding"]
        if isinstance(blob, memoryview):
            blob = blob.tobytes()
        if not isinstance(blob, (bytes, bytearray)) or len(blob) != size:
            continue
        idents.append(store.row_identity(row))
        vectors.append(np.frombuffer(bytes(blob), dtype=np.float32))
    if not vectors:
        return 0, 0
    matrix = np.ascontiguousarray(np.stack(vectors))
    zero_shot = zero_shot_probabilities(matrix) if any(model.anchor is not None for model in models) else None
    found = tag_learner.score_embeddings_matrix(matrix, models, zero_shot)
    return store.store_ai_tags([(ident, [tag for tag, _score in tags]) for ident, tags in zip(idents, found)])


class Run:
    """What one classification did, for the caller's message."""

    def __init__(self):
        self.tagged = 0            # photos that got new AI tags
        self.changed = 0           # photos whose AI tags changed
        self.wrote = False         # anything shown changed
        self.unreadable = 0        # photos found unreadable in this run


def classify(store, profile, *, kind, scope, example_id, items, skipped=0, check_cancel=lambda: None,
             progress=None, status=None, download=True, run=None, shared=True) -> Run:
    """Encode ``items`` (``[(ident, path)]`` without a current feature), then give every photo its AI tags.

    ``download=False`` never fetches the model: the caller drops ``items`` when
    the model is missing. ``shared=False`` only reads the shared profile: no
    example of this store goes into it and nothing is learned or saved there
    (a folder whose learning is kept to itself). ``skipped`` photos were left out as unreadable; they
    count in the progress. ``check_cancel`` raises to stop; what is done stays.
    """
    from .duplicates import ScanCancelled

    run = run or Run()
    stored = store.count_embeddings(ACTIVE_MODEL_ID)
    version_start = profile.rules_version()
    stale = store.ai_scored_state() != (str(version_start), ACTIVE_MODEL_ID)
    unlabeled = store.count_embeddings(ACTIVE_MODEL_ID, unlabeled=True)
    if not items and not stored:
        return run
    synced_early = False
    if not items and not stale and not unlabeled:
        if shared:
            sync_examples(store, profile, kind, scope, example_id)
            train_dirty(store, profile)
            version_start = profile.rules_version()
            stale = store.ai_scored_state() != (str(version_start), ACTIVE_MODEL_ID)
            synced_early = True
        if not stale:
            return run
    taught = store.taught_identities(profile.known_tag_names()) if items else set()
    first = [item for item in items if item[0] in taught]
    rest = [item for item in items if item[0] not in taught]
    unreadable_reader = getattr(store, "unreadable_signatures", None)
    known_unreadable = set(unreadable_reader()) if (items and callable(unreadable_reader)) else None
    grand = len(items) + skipped + (stored if stale else unlabeled)
    done = skipped
    if progress:
        progress(done, max(grand, 1))
    state = {"session": None}

    with ExitStack() as session_guard:
        def encode(chunk_items, models=None):
            nonlocal done
            if not chunk_items:
                return
            from .clip_classifier import (
                classify_detailed, encode_skip_signature, ensure_model, get_session, is_available, session_use,
            )

            if state["session"] is None:
                if not is_available():
                    if not download:
                        raise RuntimeError("AI 분류 모델이 이 PC에 없습니다.")
                    try:
                        def on_dl(dl_bytes, tot_bytes):
                            # "분류 중단" and closing the window end the download too.
                            check_cancel()
                            if status and tot_bytes:
                                status(f"AI 분류 모델 내려받는 중… ({dl_bytes / (1024 * 1024):.1f} / "
                                       f"{tot_bytes / (1024 * 1024):.1f} MB)")
                        ensure_model(progress_callback=on_dl)
                    except ScanCancelled:
                        raise
                    except Exception as exc:
                        raise RuntimeError("AI 분류 모델을 준비하지 못했습니다. 인터넷 연결과 저장 공간을 "
                                           "확인해 주세요.") from exc
                session_guard.enter_context(session_use())
                try:
                    state["session"] = get_session()
                except ScanCancelled:
                    raise
                except Exception as exc:
                    raise RuntimeError("AI 분류 기능을 시작하지 못했습니다. LuvBox.exe를 다시 받아 실행해 주세요.") from exc
                if state["session"] is None:
                    raise RuntimeError("AI 분류 기능을 시작하지 못했습니다. LuvBox.exe를 다시 받아 실행해 주세요.")
            for start in range(0, len(chunk_items), ENCODE_BATCH):
                check_cancel()
                chunk = chunk_items[start:start + ENCODE_BATCH]
                try:
                    predictions = classify_detailed([path for _ident, path in chunk], batch_size=ENCODE_BATCH,
                                                    session=state["session"])
                except Exception as exc:
                    kept = f" {run.tagged:,}장은 분류했습니다." if run.tagged else ""
                    raise RuntimeError("일부 사진을 분류하지 못했습니다." + kept + " 다시 실행해 주세요.") from exc
                features, forget, remember = [], [], []
                for (ident, path), (_tag, confidence, vector) in zip(chunk, predictions):
                    if vector is not None:
                        features.append((ident, vector.tobytes(), confidence))
                        forget.append(ident)
                    else:
                        signature = encode_skip_signature(path)
                        if signature is not None:
                            remember.append((ident, signature[0], signature[1]))
                            run.unreadable += 1
                if features:
                    store.save_features(features)
                    if models is not None:
                        changed, tagged = score_rows(
                            store, [store.feature_row(ident, blob) for ident, blob, _c in features], models)
                        run.changed += changed
                        run.tagged += tagged
                        run.wrote = run.wrote or bool(changed)
                if forget:
                    if known_unreadable is None:
                        store.forget_unreadable(forget)
                    else:
                        to_forget = [ident for ident in forget if ident in known_unreadable]
                        if to_forget:
                            store.forget_unreadable(to_forget)
                            known_unreadable.difference_update(to_forget)
                if remember:
                    store.remember_unreadable_idents(remember)
                    if known_unreadable is not None:
                        known_unreadable.update(ident for ident, *_rest in remember)
                done += len(chunk)
                if progress:
                    progress(min(done, grand), max(grand, 1))

        encode(first)
        check_cancel()
        if status and (first or stale):
            status("비슷한 사진에 분류를 맞추는 중…")
        short = []
        if shared and not synced_early:
            sync_examples(store, profile, kind, scope, example_id)
            short = train_dirty(store, profile)
        version_models = profile.rules_version()
        models = scoring_models(profile)
        rest_scored = not short
        encode(rest, models if rest_scored else None)
        if short and rest:
            # The photos encoded just now are what those tags lacked to compare with.
            train_dirty(store, profile, short)
        version = profile.rules_version()
        if version != version_models:
            # The rules changed while the rest was encoded (learned just now, or changed in another
            # tab or by a scheduled save): what is scored below and recorded as scored is the rules
            # as they are now.
            if shared:
                train_dirty(store, profile)
            version = profile.rules_version()
            models = scoring_models(profile)
        # What was stored before this run is scored again when the rules changed since it was
        # scored (here or in another library), and so is everything scored during this run with
        # rules that changed afterwards. Otherwise only what no classification has looked at yet.
        rescore_all = ((stored > 0 and (stale or version != version_start))
                       or version != version_models
                       or (not rest_scored and bool(rest)))
        remaining = store.count_embeddings(ACTIVE_MODEL_ID, unlabeled=not rescore_all)
        grand = max(grand, done + remaining)
        scored_rest_ids = ({ident for ident, _path in rest}
                           if (rest_scored and rest and rescore_all and version != version_models) else None)
        for chunk in store.iter_embeddings(ACTIVE_MODEL_ID, 512, unlabeled=not rescore_all):
            check_cancel()
            if scored_rest_ids:
                fresh_rows = [row for row in chunk if store.row_identity(row) not in scored_rest_ids]
                recheck_rows = [row for row in chunk if store.row_identity(row) in scored_rest_ids]
                if fresh_rows:
                    changed, tagged = score_rows(store, fresh_rows, models)
                    run.changed += changed
                    run.tagged += tagged
                    run.wrote = run.wrote or bool(changed)
                if recheck_rows:
                    changed, _tagged = score_rows(store, recheck_rows, models)
                    run.changed += changed
                    run.wrote = run.wrote or bool(changed)
            else:
                changed, tagged = score_rows(store, chunk, models)
                run.changed += changed
                run.tagged += tagged
                run.wrote = run.wrote or bool(changed)
            done += len(chunk)
            if progress:
                progress(min(done, grand), max(grand, 1))
        store.set_ai_scored_state(version, ACTIVE_MODEL_ID)
        if progress:
            progress(grand, max(grand, 1))
    return run


def teach(store, profile, *, kind, scope, example_id, idents, tag, positive, check_stop=lambda: False,
          shared=True):
    """Store one answer about ``tag`` on these photos (or "none" for ``tag=""``), learn, and spread it.

    The photos must have their features already. Returns ``(taught, propagated)``.
    ``shared=False`` keeps the answer in this store: the shared profile is only
    read (no new AI tag, no example, nothing learned there).
    """
    from .ai_profile import valid_tag

    clean = valid_tag(tag)
    if clean:
        if shared and positive and clean not in profile.known_tag_names():
            profile.add_tag(clean)     # a "yes" for a tag that is not an AI tag yet makes it one
        taught = store.set_tag_feedback(idents, clean, positive)
    else:
        store.refuse_ai_tags(idents)
        taught = len(idents)
    if shared:
        sync_examples(store, profile, kind, scope, example_id)
        train_dirty(store, profile)
    version = profile.rules_version()
    models = scoring_models(profile)
    own_set = set(idents)
    if not shared:
        own_blobs = store.embeddings_for(list(own_set), ACTIVE_MODEL_ID)
        own_rows = []
        for ident, value in own_blobs.items():
            blob = value[0] if isinstance(value, tuple) else value
            own_rows.append(store.feature_row(ident, blob))
        score_rows(store, own_rows, models)
        return taught, 0
    propagated = 0
    for chunk in store.iter_embeddings(ACTIVE_MODEL_ID, 512):
        if check_stop():
            return taught, propagated
        own, others = [], []
        for row in chunk:
            (own if store.row_identity(row) in own_set else others).append(row)
        score_rows(store, own, models)
        propagated += score_rows(store, others, models)[0]
    store.set_ai_scored_state(version, ACTIVE_MODEL_ID)
    return taught, propagated

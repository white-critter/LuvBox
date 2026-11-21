"""Learn each AI tag from the photos a person taught, and score stored features with what was learned.

Pure numpy, and loaded only by the work that classifies (never at start). Every
feature is an L2-normalized 768-d SigLIP 2 image embedding.

A tag is decided one of three ways, by how many "yes" examples it has (n+):

- n+ = 0, zero-shot: a default tag compares the photo with its text anchor
  (``clip_classifier.zero_shot_probabilities``). A tag of the person's own
  with no example is not guessed at all; it waits for its first example.
- 1 <= n+ <= 4, nearest neighbour: a photo very close to a taught example (a
  near copy, the same series) gets the tag. The bar falls a little with every
  example, but stays at near-copy level. A default tag that is undecided this
  way falls back to its zero-shot score.
- n+ >= 5, linear probe: an L2-regularized logistic regression learned from the
  examples, from the "not this" examples (strong negatives) and from a sample of
  other photos (weak negatives, as most photos do not carry a given tag; a sampled
  photo very close to an example is left out, as it is probably an untagged one of
  the same kind). A default tag blends with its zero-shot score until it has 15
  examples. The penalty (0.01) was measured: the plan's 0.1 kept every score
  under the 0.65 threshold on SigLIP features, which sit in a narrow cone.

In every regime a photo nearly identical to a "not this" example does not get
the tag (veto), and what a person decided on a photo itself is never overridden
by the catalog (see ``HistoryStore.replace_ai_tags``).

The thresholds were measured on 1,180 real photos of a saved library (1.5 % of
random unrelated photos are as close as 0.91 to a given photo; 0.1 % as close as
0.94; pairs at 0.95 and above are copies or edits of one picture).
"""

from __future__ import annotations

import math

import numpy as np

from .clip_model import CATEGORY_NAMES, EMBEDDING_DIM

DEFAULT_THRESHOLD = 0.65
MAX_TAGS_PER_PHOTO = 8
L2_PENALTY = 0.01
MAX_ITERATIONS = 120
TOLERANCE = 1e-4
NEIGHBOR_BASE = 0.91
NEIGHBOR_DELTA = 0.03
NEIGHBOR_MARGIN = 0.03
VETO_SIMILARITY = 0.95
WEAK_NEGATIVE_WEIGHT = 0.25
# A sampled untagged photo this close to a "yes" example is most likely an untagged
# one of the same kind: it is left out of the weak "no" examples.
BACKGROUND_NEAR_LIMIT = 0.90
STRONG_NEGATIVE_WEIGHT = 1.5
LINEAR_FROM = 5
BLEND_UNTIL = 15


def neighbor_threshold(count: int) -> float:
    """How close a photo must be to one of ``count`` taught examples to get the tag."""
    return NEIGHBOR_BASE + NEIGHBOR_DELTA / math.sqrt(max(1, int(count)))


def background_size(positives: int) -> int:
    """How many untagged photos stand in as weak "no" examples for a tag with this many "yes"."""
    return min(512, max(64, int(positives) * 10))


def as_matrix(blobs) -> np.ndarray:
    """Stack stored feature bytes into an ``(n, 768)`` float32 matrix; malformed ones are skipped."""
    rows = []
    for blob in blobs or ():
        if isinstance(blob, memoryview):
            blob = blob.tobytes()
        if isinstance(blob, (bytes, bytearray)) and len(blob) == EMBEDDING_DIM * 4:
            vec = np.frombuffer(bytes(blob), dtype=np.float32)
            if np.all(np.isfinite(vec)):
                rows.append(vec)
    if not rows:
        return np.zeros((0, EMBEDDING_DIM), dtype=np.float32)
    return np.ascontiguousarray(np.stack(rows), dtype=np.float32)


def _sigmoid(values):
    return 1.0 / (1.0 + np.exp(-np.clip(values, -60.0, 60.0)))


def train_logistic(features: np.ndarray, labels: np.ndarray, weights: np.ndarray,
                   l2: float = L2_PENALTY, max_iter: int = MAX_ITERATIONS, tol: float = TOLERANCE):
    """Weighted L2-regularized logistic regression by Nesterov's accelerated gradient.

    Minimizes ``sum_i a_i log(1 + exp(-y_i (w.x_i + b))) / sum_i a_i + l2/2 |w|^2``
    with ``y`` in {+1, -1}. The features are centred on their weighted mean while
    learning (the bias absorbs it afterwards): SigLIP features share one large
    common direction, which would otherwise slow the descent along the bias.
    Returns ``(w, b)`` for the raw features.
    """
    x = np.asarray(features, dtype=np.float64)
    y = np.asarray(labels, dtype=np.float64)
    a = np.asarray(weights, dtype=np.float64)
    a = a / a.sum()
    mean = a @ x
    xc = x - mean
    # For z_i = [xc_i, 1], the data term's Hessian along [w, b] has spectral bound
    # 1/4 * max_i (||xc_i||^2 + 1), plus l2 on w. Without the bias's +1.0, tightly
    # clustered SigLIP 2 cones (||xc_i||^2 << 1) pick steps > 4 and diverge in b.
    lipschitz = 0.25 * (float(np.max(np.sum(xc * xc, axis=1))) + 1.0) + l2 + 1e-12
    step = 1.0 / lipschitz
    w = np.zeros(x.shape[1])
    b = 0.0
    w_prev, b_prev = w.copy(), b
    for iteration in range(1, max_iter + 1):
        momentum = (iteration - 1) / (iteration + 2)
        vw = w + momentum * (w - w_prev)
        vb = b + momentum * (b - b_prev)
        margin = y * (xc @ vw + vb)
        coefficient = -a * y * _sigmoid(-margin)
        grad_w = xc.T @ coefficient + l2 * vw
        grad_b = float(coefficient.sum())
        w_prev, b_prev = w, b
        w = vw - step * grad_w
        b = vb - step * grad_b
        if math.sqrt(float(grad_w @ grad_w) + grad_b * grad_b) < tol:
            break
    return w.astype(np.float32), float(b - w @ mean)


def train_tag(positives: np.ndarray, negatives: np.ndarray, background: np.ndarray, *,
              built_in: bool, threshold: float | None = None) -> dict | None:
    """The classifier of one tag from its examples, as ``ai_profile.save_tag_weight`` stores it.

    None for a tag of the person's own with no "yes" example yet: it is not guessed.
    """
    n_pos = int(len(positives))
    n_neg = int(len(negatives))
    chosen = float(threshold) if threshold is not None else DEFAULT_THRESHOLD
    if n_pos == 0:
        if not built_in:
            return None
        return {"regime": "zeroshot", "weights": b"", "bias": 0.0, "threshold": chosen,
                "pos_count": 0, "neg_count": n_neg}
    if n_pos < LINEAR_FROM:
        return {"regime": "neighbor", "weights": b"", "bias": 0.0, "threshold": chosen,
                "pos_count": n_pos, "neg_count": n_neg}
    parts = [np.asarray(positives, dtype=np.float32)]
    labels = [np.ones(n_pos)]
    pos_total = float(n_pos)
    strong = np.asarray(negatives, dtype=np.float32).reshape(-1, EMBEDDING_DIM)
    weak = np.asarray(background, dtype=np.float32).reshape(-1, EMBEDDING_DIM)
    if len(weak):
        weak = weak[np.max(weak @ parts[0].T, axis=1) < BACKGROUND_NEAR_LIMIT]
    neg_weights = np.concatenate([np.full(len(strong), STRONG_NEGATIVE_WEIGHT),
                                  np.full(len(weak), WEAK_NEGATIVE_WEIGHT)])
    if len(neg_weights) == 0:
        # Nothing to tell the tag apart from: compare with the examples instead.
        return {"regime": "neighbor", "weights": b"", "bias": 0.0, "threshold": chosen,
                "pos_count": n_pos, "neg_count": n_neg}
    parts.extend([strong, weak])
    labels.append(-np.ones(len(neg_weights)))
    # Class balance: both sides weigh the same in total, whatever their counts.
    neg_weights = neg_weights * (pos_total / float(neg_weights.sum()))
    weights = np.concatenate([np.ones(n_pos), neg_weights])
    w, b = train_logistic(np.concatenate(parts), np.concatenate(labels), weights, l2=L2_PENALTY)
    if not (np.all(np.isfinite(w)) and math.isfinite(b)):
        return {"regime": "neighbor", "weights": b"", "bias": 0.0, "threshold": chosen,
                "pos_count": n_pos, "neg_count": n_neg}
    return {"regime": "linear", "weights": np.ascontiguousarray(w, dtype=np.float32).tobytes(), "bias": b,
            "threshold": chosen, "pos_count": n_pos, "neg_count": n_neg}


class TagModel:
    """Everything needed to score one switched-on tag."""

    __slots__ = ("tag", "built_in", "anchor", "threshold", "regime", "w", "b", "positives", "negatives",
                 "pos_count")

    def __init__(self, tag, *, built_in=False, threshold=None, regime="zeroshot", weights=b"", bias=0.0,
                 positives=None, negatives=None, pos_count=None):
        self.tag = tag
        self.built_in = bool(built_in)
        self.anchor = CATEGORY_NAMES.index(tag) if self.built_in and tag in CATEGORY_NAMES else None
        parsed_threshold = DEFAULT_THRESHOLD if threshold is None else float(threshold)
        self.threshold = parsed_threshold if math.isfinite(parsed_threshold) else DEFAULT_THRESHOLD
        self.regime = regime
        raw_w = (np.frombuffer(weights, dtype=np.float32) if regime == "linear" and weights
                 and len(weights) == EMBEDDING_DIM * 4 else None)
        parsed_b = float(bias)
        if raw_w is not None and np.all(np.isfinite(raw_w)) and math.isfinite(parsed_b):
            self.w = raw_w
            self.b = parsed_b
        else:
            self.w = None
            self.b = parsed_b if math.isfinite(parsed_b) else 0.0
        self.positives = positives if positives is not None else np.zeros((0, EMBEDDING_DIM), dtype=np.float32)
        self.negatives = negatives if negatives is not None else np.zeros((0, EMBEDDING_DIM), dtype=np.float32)
        self.pos_count = int(pos_count) if pos_count is not None else len(self.positives)

    @property
    def usable(self) -> bool:
        if self.regime == "linear":
            return self.w is not None
        if self.regime == "neighbor":
            return len(self.positives) > 0 or self.anchor is not None
        return self.anchor is not None


def build_model(rule: dict, positives=None, negatives=None) -> TagModel:
    """A model from one ``ai_profile.scoring_rules`` item and its example matrices."""
    weight = rule.get("weight") or {}
    regime = weight.get("regime") or ("zeroshot" if rule.get("built_in") else "none")
    threshold = rule.get("threshold")
    if threshold is None:
        threshold = weight.get("threshold")
    return TagModel(rule["tag"], built_in=rule.get("built_in", False), threshold=threshold, regime=regime,
                    weights=weight.get("weights") or b"", bias=weight.get("bias") or 0.0,
                    positives=positives, negatives=negatives, pos_count=weight.get("pos_count"))


def tag_probabilities(matrix: np.ndarray, model: TagModel, zero_shot: np.ndarray | None,
                      *, matrix64: np.ndarray | None = None):
    """``(score, accepted)`` arrays of one tag over an ``(m, 768)`` matrix."""
    count = matrix.shape[0]
    zs = None
    if model.anchor is not None and zero_shot is not None and zero_shot.shape[0] == count:
        zs = zero_shot[:, model.anchor].astype(np.float64)
    score = np.zeros(count)
    accepted = np.zeros(count, dtype=bool)
    against = None
    if model.regime == "linear" and model.w is not None:
        m64 = matrix64 if matrix64 is not None else matrix.astype(np.float64)
        score = _sigmoid(m64 @ model.w.astype(np.float64) + model.b)
        if zs is not None:
            n_pos = max(model.pos_count, LINEAR_FROM)
            alpha = min(1.0, max(0.0, (n_pos - (LINEAR_FROM - 1)) / float(BLEND_UNTIL - (LINEAR_FROM - 1))))
            score = alpha * score + (1.0 - alpha) * zs
        accepted = score >= model.threshold
    elif model.regime == "neighbor" and len(model.positives):
        near = np.max(matrix @ model.positives.T, axis=1).astype(np.float64)
        if len(model.negatives):
            against = np.max(matrix @ model.negatives.T, axis=1).astype(np.float64)
        else:
            against = np.full(count, -1.0)
        confident = (near >= neighbor_threshold(len(model.positives))) & (near >= against + NEIGHBOR_MARGIN)
        score = near
        accepted = confident
        if zs is not None:
            fallback = ~confident & (zs >= model.threshold)
            score = np.where(confident, near, zs)
            accepted = confident | fallback
    elif zs is not None:
        score = zs
        accepted = zs >= model.threshold
    if len(model.negatives) and accepted.any():
        if against is not None:
            accepted &= ~(against >= VETO_SIMILARITY)
        else:
            idx = np.flatnonzero(accepted)
            vetoed = np.max(matrix[idx] @ model.negatives.T, axis=1) >= VETO_SIMILARITY
            accepted[idx[vetoed]] = False
    return score, accepted


def score_embeddings_matrix(matrix: np.ndarray, models, zero_shot: np.ndarray | None = None,
                            limit: int = MAX_TAGS_PER_PHOTO) -> list[list[tuple[str, float]]]:
    """For each row, the tags it gets as ``(tag, score)``, best first, at most ``limit``."""
    matrix = np.asarray(matrix, dtype=np.float32)
    if matrix.ndim == 1:
        matrix = matrix.reshape(1, -1)
    if matrix.ndim != 2 or matrix.shape[1] != EMBEDDING_DIM:
        return []
    has_linear = any(model.usable and model.regime == "linear" for model in models)
    matrix64 = matrix.astype(np.float64) if has_linear else None
    found: list[list[tuple[str, float]]] = [[] for _ in range(matrix.shape[0])]
    for model in models:
        if not model.usable:
            continue
        score, accepted = tag_probabilities(matrix, model, zero_shot, matrix64=matrix64)
        for row in np.flatnonzero(accepted):
            found[int(row)].append((model.tag, float(score[int(row)])))
    for row in found:
        row.sort(key=lambda item: (-item[1], item[0]))
        del row[limit:]
    return found


def sample_rows(count: int, wanted: int, seed: str) -> np.ndarray:
    """``wanted`` row numbers out of ``count``, the same for the same seed."""
    if count <= wanted:
        return np.arange(count)
    digest = sum((index + 1) * ord(char) for index, char in enumerate(seed)) & 0xFFFFFFFF
    return np.sort(np.random.default_rng(digest).choice(count, size=wanted, replace=False))

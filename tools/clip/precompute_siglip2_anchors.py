"""Compute the text anchors of LuvBox's five default AI tags with SigLIP 2 (development only).

The application never runs a text model. This script is run once by a developer to
produce the ``_CATEGORY_EMBEDDINGS_B64`` constant in ``luvbox/clip_classifier.py``:
for each default tag it encodes several English and Korean prompts with the SigLIP 2
text encoder, averages the L2-normalized results and normalizes the mean again.

It needs the SigLIP 2 text encoder and its tokenizer from the same pinned upstream
commit as the vision model, and the ``tokenizers`` package (``pip install tokenizers``
in a development environment; it is not a dependency of LuvBox and is never shipped).
The files are downloaded over HTTPS into ``--cache`` and checked against the SHA-256
values the Hugging Face API reports for that commit.

The two scalars ``logit_scale`` and ``logit_bias`` of the upstream checkpoint are read
from ``google/siglip2-base-patch16-224``'s ``model.safetensors`` with three small
range requests (its header, then 8 bytes), so the 1.5 GB file is not downloaded.

Usage::

    python tools/clip/precompute_siglip2_anchors.py --cache .test-output/siglip2-text
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
from pathlib import Path
import struct
import sys
import urllib.parse
import urllib.request

ONNX_REPO = "onnx-community/siglip2-base-patch16-224-ONNX"
ONNX_COMMIT = "ba1f3b0843f24bc5417d38e19c37b287d719b2f4"
UPSTREAM_REPO = "google/siglip2-base-patch16-224"
UPSTREAM_COMMIT = "75de2d55ec2d0b4efc50b3e9ad70dba96a7b2fa2"
TEXT_FILES = ("onnx/text_model.onnx", "tokenizer.json")
MAX_LENGTH = 64          # SigLIP was trained with every caption padded to 64 tokens.
USER_AGENT = "LuvBox-dev (SigLIP 2 anchor precompute)"

# The default tags in the order clip_model.CATEGORIES lists them. SigLIP 2 reads
# several languages, so English and Korean descriptions are averaged together.
PROMPTS = {
    "일러스트": [
        "an illustration", "a digital illustration", "an anime-style drawing", "a manga drawing",
        "a cartoon drawing", "fan art of a character", "a digital painting", "a hand-drawn sketch",
        "a 2d illustration of an anime girl", "a colored drawing made by an artist",
        "일러스트", "애니메이션 그림", "그림 작품",
    ],
    "인물": [
        "a photo of a person", "a photo of a woman", "a photo of a man", "a selfie",
        "a portrait photo of a real person", "a photo of people", "a cosplay photo of a real person",
        "a photo of a celebrity", "a photo of an idol on stage",
        "사람 사진", "인물 사진", "셀카",
    ],
    "동물": [
        "a photo of an animal", "a photo of a cat", "a photo of a dog", "a photo of a pet",
        "a photo of a bird", "a photo of a wild animal", "a close-up photo of a cute animal",
        "동물 사진", "고양이 사진", "강아지 사진",
    ],
    "짤·텍스트": [
        "a meme", "a screenshot of text", "a screenshot of a tweet", "a screenshot of a chat",
        "an image with a lot of text", "a funny meme with a caption", "a reaction image",
        "a screenshot of a phone screen", "a notice written in text",
        "짤", "글자가 많은 이미지", "트윗 캡처",
    ],
    "풍경·배경": [
        "a landscape photo", "a photo of scenery", "a photo of a city street", "a photo of the sky",
        "a photo of a room interior", "a photo of nature", "a photo of food", "a photo of an object",
        "a photo of a building",
        "풍경 사진", "배경 사진", "음식 사진",
    ],
}


def _get(url: str, headers: dict | None = None) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def _expected(path: str) -> tuple[str | None, int | None]:
    folder = path.rsplit("/", 1)[0] if "/" in path else ""
    api = f"https://huggingface.co/api/models/{ONNX_REPO}/tree/{ONNX_COMMIT}" + (f"/{folder}" if folder else "")
    for item in json.loads(_get(api)):
        if item.get("path") == path:
            return (item.get("lfs") or {}).get("oid"), item.get("size")
    raise SystemExit(f"{path} is not in {ONNX_REPO}@{ONNX_COMMIT}")


def fetch(cache: Path) -> dict[str, Path]:
    """Download the text encoder files into ``cache`` (once) and verify them."""
    cache.mkdir(parents=True, exist_ok=True)
    found = {}
    for name in TEXT_FILES:
        digest, size = _expected(name)
        target = cache / name.replace("/", "__")
        if not (target.is_file() and target.stat().st_size == size):
            url = f"https://huggingface.co/{ONNX_REPO}/resolve/{ONNX_COMMIT}/{urllib.parse.quote(name)}"
            print(f"downloading {name} ({size / 1048576:.0f} MiB)…", file=sys.stderr)
            partial = target.with_name(target.name + ".part")
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": USER_AGENT}),
                                        timeout=60) as response, partial.open("wb") as out:
                while chunk := response.read(1 << 20):
                    out.write(chunk)
            partial.replace(target)
        hasher = hashlib.sha256()
        with target.open("rb") as source:
            while chunk := source.read(1 << 20):
                hasher.update(chunk)
        if target.stat().st_size != size or (digest and hasher.hexdigest() != digest):
            target.unlink()
            raise SystemExit(f"{name} did not match the pinned upstream file; it was removed.")
        found[name] = target
    return found


def logit_parameters() -> tuple[float, float]:
    """``(logit_scale, logit_bias)`` of the upstream checkpoint, read by range requests."""
    import numpy as np

    url = f"https://huggingface.co/{UPSTREAM_REPO}/resolve/{UPSTREAM_COMMIT}/model.safetensors"
    length = struct.unpack("<Q", _get(url, {"Range": "bytes=0-7"}))[0]
    header = json.loads(_get(url, {"Range": f"bytes=8-{8 + length - 1}"}))
    values = {}
    for key in ("logit_scale", "logit_bias"):
        entry = header[key]
        if entry["dtype"] != "F32" or entry["shape"] != [1]:
            raise SystemExit(f"unexpected {key} entry: {entry}")
        start, end = entry["data_offsets"]
        raw = _get(url, {"Range": f"bytes={8 + length + start}-{8 + length + end - 1}"})
        values[key] = float(np.frombuffer(raw, dtype=np.float32)[0])
    return values["logit_scale"], values["logit_bias"]


def encode(files: dict[str, Path], texts: list[str]):
    import numpy as np
    import onnxruntime as ort
    try:
        from tokenizers import Tokenizer
    except ImportError:
        raise SystemExit("This development script needs the 'tokenizers' package: pip install tokenizers")

    tokenizer = Tokenizer.from_file(str(files["tokenizer.json"]))
    eos = tokenizer.token_to_id("<eos>")
    # The upstream tokenizer file itself ends a caption with <eos> and pads it to 64 tokens,
    # which is how the checkpoint was trained (on lowercased captions).
    tokenizer.enable_padding(length=MAX_LENGTH, pad_id=tokenizer.token_to_id("<pad>"), pad_token="<pad>")
    tokenizer.enable_truncation(max_length=MAX_LENGTH)
    rows = []
    for text in texts:
        ids = tokenizer.encode(text.lower()).ids
        if len(ids) != MAX_LENGTH or eos not in ids:
            raise SystemExit(f"unexpected tokenization of {text!r}")
        rows.append(ids)
    session = ort.InferenceSession(str(files["onnx/text_model.onnx"]), providers=["CPUExecutionProvider"])
    output = [o.name for o in session.get_outputs()].index("pooler_output")
    embeds = session.run(None, {"input_ids": np.asarray(rows, dtype=np.int64)})[output]
    return embeds / np.maximum(np.linalg.norm(embeds, axis=-1, keepdims=True), 1e-12)


def anchors(files: dict[str, Path]):
    import numpy as np

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from luvbox.clip_model import CATEGORY_NAMES

    if list(PROMPTS) != CATEGORY_NAMES:
        raise SystemExit("PROMPTS must list the default tags in clip_model.CATEGORY_NAMES order")
    rows = []
    for name in CATEGORY_NAMES:
        mean = encode(files, PROMPTS[name]).mean(axis=0)
        rows.append(mean / np.linalg.norm(mean))
    return np.ascontiguousarray(np.stack(rows).astype(np.float32))


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cache", type=Path, required=True, help="Folder for the downloaded text encoder")
    parser.add_argument("--out", type=Path, help="Also write the anchors as a .npy file")
    args = parser.parse_args()
    files = fetch(args.cache)
    matrix = anchors(files)
    scale, bias = logit_parameters()
    if args.out:
        import numpy as np
        np.save(args.out, matrix)
    print(json.dumps({
        "shape": list(matrix.shape),
        "sha256": hashlib.sha256(matrix.tobytes()).hexdigest(),
        "logit_scale_log": scale,
        "logit_bias": bias,
        "base64": base64.b64encode(matrix.tobytes()).decode("ascii"),
    }))


if __name__ == "__main__":
    main()

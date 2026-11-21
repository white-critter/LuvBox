# LuvBox AI tags: SigLIP 2 vision model

LuvBox gives photos AI tags with a quantized ONNX **SigLIP 2** vision encoder, offline on the CPU.
The model file is not part of a release: the first AI classification the person starts downloads it.

## Model Details
- **Architecture**: SigLIP 2 Base, patch 16, 224×224 (`google/siglip2-base-patch16-224`), 8-bit weights (`int8`)
- **Source**: `onnx-community/siglip2-base-patch16-224-ONNX`, file `onnx/vision_model_int8.onnx`,
  pinned to commit `ba1f3b0843f24bc5417d38e19c37b287d719b2f4`
- **License**: Apache-2.0 (downloaded at runtime, never bundled)
- **File**: `siglip2_base_patch16_224_vision.onnx`
- **Size**: 94,553,333 bytes (~90.2 MiB)
- **SHA-256**: `0dd31785a2713f1113ef2272472165c69d580473dae38d7b47568ac587795e70`
- **Input**: `pixel_values` `[batch, 3, 224, 224]` float32; the whole picture resized to 224×224 (bilinear, no crop), scaled to [-1, 1]
- **Output used**: `pooler_output` `[batch, 768]` float32, L2-normalized by LuvBox (the export lists `last_hidden_state` first; the output is chosen by name)

## Why int8, and the thresholds
Measured on 1,180 photos of a real library (12-thread PC, 4 inference threads):
the `int8` file ran at 8.9 photos/s and the `uint8` one at 7.4 photos/s, with the same agreement with the
categories the owner had confirmed (268 vs 267 of 327) and 0.981 mean cosine between their features.

The five default tags are scored against precomputed text anchors (`tools/clip/precompute_siglip2_anchors.py`,
several English and Korean prompts per tag). The anchors share most of their direction, so a photo is compared
with each anchor relative to its mean similarity to all five; each tag applies from a measured cut
(`ZERO_SHOT_CUTS` in `luvbox/clip_classifier.py`). The learning thresholds (nearest neighbour 0.91 + 0.03/√n,
"not this" veto 0.95, logistic penalty 0.01) are in `luvbox/tag_learner.py`.
`tools/bench_clip.py --calibrate` repeats the measurement on the active library (read-only).

## Privacy and Local Execution
- Classification runs strictly on your local CPU using `onnxruntime`.
- No image bytes or metadata ever leave your computer.
- No tokenizer or text model is used at runtime.

## Cache Location
- Windows: `%LOCALAPPDATA%\LuvBox\models\siglip2\siglip2_base_patch16_224_vision.onnx`
- Development fallback: `tools/clip/runtime/siglip2_base_patch16_224_vision.onnx` (ignored by git, never packaged)
- A CLIP ViT-B/32 file of 1.11.9 and earlier (`models\clip\clip_vit_b32_vision.onnx`) is no longer used.

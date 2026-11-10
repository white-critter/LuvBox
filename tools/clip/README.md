# LuvBox AI CLIP Zero-Shot Vision Model

LuvBox uses a quantized ONNX CLIP Vision Encoder (`ViT-B/32`) for 100% offline, local zero-shot image classification.

## Model Details
- **Architecture**: CLIP Vision Transformer (ViT-B/32), INT8 Dynamic Quantization
- **Source**: `Xenova/clip-vit-base-patch32` (Hugging Face)
- **File**: `clip_vit_b32_vision.onnx`
- **Size**: 89,117,001 bytes (~85.0 MB)
- **SHA-256**: `583fd1110a514667812fee7d684952aaf82a99b959760c8d7dca7e0ab9839299`
- **Input**: `pixel_values` `[batch_size, 3, 224, 224]` float32
- **Output**: `image_embeds` `[batch_size, 512]` float32 (L2-normalized)

## Privacy and Local Execution
- Classification runs strictly on your local CPU using `onnxruntime`.
- No image bytes or metadata ever leave your computer.
- Precomputed text embeddings are embedded directly in code, eliminating the need for text tokenizers or text models at runtime.

## Cache Location
- Windows: `%LOCALAPPDATA%\LuvBox\models\clip\clip_vit_b32_vision.onnx`
- Fallback project path: `tools/clip/runtime/clip_vit_b32_vision.onnx`

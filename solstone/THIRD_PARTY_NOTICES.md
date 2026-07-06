# third-party notices

This file records third-party model weights bundled with solstone and
provider artifacts downloaded at runtime into the journal provider cache. The
solstone source code remains AGPL-3.0-only; the notices below apply only to the
listed model files and runtime provider artifacts.

## bundled model weights

| Bundled file | Upstream model | Source artifact | License | SHA-256 |
|---|---|---|---|---|
| `solstone_journal_models/assets/wespeaker-resnet34-256.onnx` | WeSpeaker ResNet34 speaker embedding model trained on VoxCeleb | `wespeaker_en_voxceleb_resnet34.onnx` from the k2-fsa/sherpa-onnx `speaker-recongition-models` release | CC-BY-4.0 | `5ef208a9da1453335308a6b6f4e6dfbd7e183a38b604de0a57664f45d257fe94` |
| `solstone_journal_models/assets/pyannote-segmentation-3.0.onnx` | `pyannote/segmentation-3.0` speaker segmentation model | `onnx/model.onnx` from `onnx-community/pyannote-segmentation-3.0` | MIT | `057ee564753071c0b09b5b611648b50ac188d50846bff5f01e9f7bbf1591ea25` |
| `solstone_journal_models/assets/silero_vad_v6.onnx` | Silero VAD voice activity detection model | ONNX model from `snakers4/silero-vad` | MIT | `4cbf549b8326f60f80f2536d9eefeb450a9abe83365a098031c89719f1be17d2` |

## runtime-downloaded provider artifacts (parakeet-cpp)

These artifacts are fetched on demand into the journal provider cache when an
owner opts into the `parakeet-cpp` transcription backend; they are not bundled
in this repository.

### parakeet.cpp server binary

Attribution: parakeet.cpp project (mudler).

Source:

- Release binaries: https://github.com/mudler/parakeet.cpp/releases/tag/v0.4.0
- Project: https://github.com/mudler/parakeet.cpp

License notice: MIT.

### parakeet TDT 0.6B v3 GGUF model

Attribution: parakeet-cpp-gguf (mudler), NVIDIA NeMo Parakeet TDT 0.6B v3.

Source:

- Model repository: https://huggingface.co/mudler/parakeet-cpp-gguf
- Pinned revision: bf0af9f425fa01809cadec671b3cb672709d13e9
- Downloaded file: tdt-0.6b-v3-q8_0.gguf

License notice: Creative Commons Attribution 4.0 International (CC-BY-4.0).
License text: https://creativecommons.org/licenses/by/4.0/legalcode.txt

## runtime-downloaded provider artifacts (ced.cpp sound-tag engine)

These artifacts are fetched on demand into the journal provider cache for local
ambient sound tagging; they are not bundled in this repository.

### ced.cpp v0.1.0 engine

Attribution: ced.cpp project (localai-org).

Source:

- Release binaries: https://github.com/localai-org/ced.cpp/releases/tag/v0.1.0
- Project: https://github.com/localai-org/ced.cpp
- Downloaded file: `ced-v0.1.0-lib-linux-cpu-x64.tar.gz`
- SHA-256: `915e0573bc4e17197a7a893d0eb98e1a851abb64451b2e1a8ad51f5f99040360`
- Downloaded file: `ced-v0.1.0-lib-linux-cpu-arm64.tar.gz`
- SHA-256: `a87de0a8b086429aa5d6544a6f881a70e62726d07901734640ac85dbf146181e`
- Downloaded file: `ced-v0.1.0-lib-macos-metal-arm64.tar.gz`
- SHA-256: `4c913ba0ece1d06ba2210da9fcaee3d8199ca3c62697c331810f224444e4054b`

License notice: MIT.

## runtime-downloaded provider artifacts (ced-tiny sound-tag model)

This artifact is fetched on demand into the journal provider cache for local
ambient sound tagging; it is not bundled in this repository.

### ced-tiny-q8_0 GGUF model

Attribution: `mudler/ced-gguf`.

Source:

- Model repository: https://huggingface.co/mudler/ced-gguf
- Pinned revision: b5e9a4aad6438763c8da16079d77563fbed35c65
- Downloaded file: `ced-tiny-q8_0.gguf`
- SHA-256: `48bee4e2fc3cc85d7806e03471db24e77fda6c2a2e81ffe9ef67caebaf2bd674`

License notice: Apache License 2.0 (Apache-2.0).

## runtime-downloaded provider artifacts (rerank cross-encoder)

These artifacts are fetched on demand into the journal provider cache for local
rerank scoring; they are not bundled in this repository.

### rerank cross-encoder ONNX model

Attribution: `Xenova/ms-marco-MiniLM-L-6-v2`, an ONNX export of
`cross-encoder/ms-marco-MiniLM-L-6-v2`.

Source:

- Model repository: https://huggingface.co/Xenova/ms-marco-MiniLM-L-6-v2
- Pinned revision: a09144355adeed5f58c8ed011d209bf8ee5a1fec
- Downloaded files: `onnx/model.onnx`, `tokenizer.json`

License notice: Apache License 2.0 (Apache-2.0).

## runtime-downloaded provider artifacts (rf-detr.cpp)

These artifacts are fetched on demand into the journal provider cache for local
object detection. They are not bundled in this repository.

### rf-detr.cpp engine binary

Attribution: rf-detr.cpp (Ettore Di Giacinto / mudler); binary built and
released by sol pbc.

Source:

- Release binary: https://github.com/solpbc/rf-detr.cpp/releases/download/bin-65c0ffcc-1/rfdetr-cli-65c0ffcc-linux-cpu-x64.tar.gz
- Project: https://github.com/mudler/rf-detr.cpp
- Pinned engine ref: 65c0ffcc
- Downloaded file: `rfdetr-cli` (extracted from the tarball)

License notice: Apache License 2.0 (Apache-2.0).

### RF-DETR nano GGUF model weights

Attribution: RF-DETR (Roboflow); GGUF conversion mudler/rfdetr-cpp-nano.

Source:

- Model repository: https://huggingface.co/mudler/rfdetr-cpp-nano
- Pinned revision: c3dc0c037df499f5503545247df6618415fca643
- Downloaded file: `rfdetr-nano-f16.gguf`

License notice: Apache License 2.0 (Apache-2.0).

## WeSpeaker ResNet34 / VoxCeleb

Attribution: WeSpeaker project, ResNet34 speaker embedding model trained on
VoxCeleb.

Source:

- Exact bundled artifact:
  https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/wespeaker_en_voxceleb_resnet34.onnx
- Release checksum file:
  https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/checksum.txt
- WeSpeaker project:
  https://github.com/wenet-e2e/wespeaker
- WeSpeaker pretrained-model license note:
  https://github.com/wenet-e2e/wespeaker/blob/master/docs/pretrained.md#model-license

License notice: Creative Commons Attribution 4.0 International (CC-BY-4.0).
WeSpeaker's pretrained-model documentation states that pretrained models follow
the license of the corresponding dataset, and that pretrained models on VoxCeleb
follow Creative Commons Attribution 4.0 International because VoxCeleb uses that
license. License text: https://creativecommons.org/licenses/by/4.0/legalcode.txt

## pyannote segmentation 3.0

Attribution: pyannote.audio project, `pyannote/segmentation-3.0` speaker
segmentation model.

Source:

- Exact bundled ONNX artifact:
  https://huggingface.co/onnx-community/pyannote-segmentation-3.0/resolve/main/onnx/model.onnx
- ONNX-community model card:
  https://huggingface.co/onnx-community/pyannote-segmentation-3.0
- Original pyannote model card:
  https://huggingface.co/pyannote/segmentation-3.0
- pyannote.audio source:
  https://github.com/pyannote/pyannote-audio

License notice: MIT. The retained MIT notice follows.

```text
MIT License

Copyright (c) 2020 CNRS

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

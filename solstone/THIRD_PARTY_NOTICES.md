# third-party notices

This file records third-party model weights bundled with solstone. The solstone
source code remains AGPL-3.0-only; the notices below apply only to the listed
model files.

## bundled model weights

| Bundled file | Upstream model | Source artifact | License | SHA-256 |
|---|---|---|---|---|
| `solstone/observe/transcribe/assets/wespeaker-resnet34-256.onnx` | WeSpeaker ResNet34 speaker embedding model trained on VoxCeleb | `wespeaker_en_voxceleb_resnet34.onnx` from the k2-fsa/sherpa-onnx `speaker-recongition-models` release | CC-BY-4.0 | `5ef208a9da1453335308a6b6f4e6dfbd7e183a38b604de0a57664f45d257fe94` |
| `solstone/observe/transcribe/assets/pyannote-segmentation-3.0.onnx` | `pyannote/segmentation-3.0` speaker segmentation model | `onnx/model.onnx` from `onnx-community/pyannote-segmentation-3.0` | MIT | `057ee564753071c0b09b5b611648b50ac188d50846bff5f01e9f7bbf1591ea25` |

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

# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Warm native journal-host imports for first-run verification."""

import logging
import platform
import sys

# Single source of truth: pyproject.toml [journal-host], plus the [journal] and
# [journal-cuda] onnxruntime entries. Package-to-import mapping:
# Pillow -> PIL; opencv-python-headless -> cv2; scikit-learn -> sklearn;
# faster-whisper -> faster_whisper; kaldi-native-fbank -> kaldi_native_fbank.
#
# Excluded intentionally: pyarrow (not a dependency), scipy (transitive through
# sklearn), and pure-Python dependencies (no native code-signing surface).
_WARM_ALL = [
    "numpy",
    "PIL",
    "cv2",
    "av",
    "soundfile",
    "onnxruntime",
    "kaldi_native_fbank",
    "sklearn",
    "faster_whisper",
]
_WARM_LINUX_X86_64 = ["onnx_asr"]
_WARM_DARWIN_ARM64 = ["mlx", "mlx_vlm"]


def warm_module_names() -> list[str]:
    """Return platform-filtered import names for bundled native warmup."""
    names = list(_WARM_ALL)
    machine = platform.machine()
    if sys.platform == "linux" and machine == "x86_64":
        names.extend(_WARM_LINUX_X86_64)
    if sys.platform == "darwin" and machine == "arm64":
        names.extend(_WARM_DARWIN_ARM64)
    return names


def warm() -> int:
    """Import native modules and the Convey app registry without starting services."""
    import importlib

    for name in warm_module_names():
        try:
            importlib.import_module(name)
        except ImportError as exc:
            logging.error("warm: failed to load native library %r: %s", name, exc)
            return 1

    from solstone.apps import AppRegistry

    AppRegistry().discover()
    return 0


def main(argv=None) -> int:
    """CLI entry point."""
    return warm()

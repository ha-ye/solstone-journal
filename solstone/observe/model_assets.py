# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from pathlib import Path

WESPEAKER_MODEL_FILENAME = "wespeaker-resnet34-256.onnx"
PYANNOTE_SEGMENTATION_MODEL_FILENAME = "pyannote-segmentation-3.0.onnx"
SILERO_VAD_MODEL_FILENAME = "silero_vad_v6.onnx"

_MISSING_MODELS_MESSAGE = (
    "solstone-journal-models is not installed; it ships solstone's bundled "
    "speaker/VAD model weights and is included with a journal-host install "
    "(for example: pip install solstone-journal)."
)


class ModelsDistributionUnavailable(RuntimeError):
    """Raised when the bundled model-weights distribution is unavailable."""


def resolve_model_asset(filename: str) -> Path:
    """Return the filesystem path for a bundled journal model asset."""
    try:
        import solstone_journal_models  # noqa: F401
    except ImportError as exc:
        raise ModelsDistributionUnavailable(_MISSING_MODELS_MESSAGE) from exc

    import importlib.resources

    return Path(
        str(
            importlib.resources.files("solstone_journal_models").joinpath(
                "assets", filename
            )
        )
    )


def resolve_wespeaker_model() -> Path:
    return resolve_model_asset(WESPEAKER_MODEL_FILENAME)


def resolve_pyannote_segmentation_model() -> Path:
    return resolve_model_asset(PYANNOTE_SEGMENTATION_MODEL_FILENAME)


def resolve_silero_vad_model() -> Path:
    return resolve_model_asset(SILERO_VAD_MODEL_FILENAME)

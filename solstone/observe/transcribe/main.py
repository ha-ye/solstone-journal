# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Transcribe audio files with pluggable STT backends and sentence-level embeddings.

Transcription pipeline:
1. VAD stage: Run Silero VAD to detect speech and filter silent files early
2. Audio reduction: Trim long silence gaps for faster processing
3. Transcription: Dispatch to the configured or resource-aware STT backend
4. Enrichment: Extract topics, setting, emotions, and warnings via LLM (optional)
5. Embeddings: Generate voice embeddings for each sentence using wespeaker-resnet34
6. Output: JSONL format compatible with format_audio() in observe/hear.py

Output files:
- <stem>.jsonl: Transcript with HH:MM:SS timestamps, topics, setting, emotions
- <stem>.npz: Sentence-level voice embeddings indexed by statement id

Configuration (journal config transcribe section):
- transcribe.backend: STT backend ("parakeet", "parakeet-cpp", "revai", "gemini"). If unset, auto-selected by free memory.
- transcribe.enrich: Enable/disable LLM enrichment (default: true)
- transcribe.preserve_all: Keep audio files even when no speech detected (default: false)
- transcribe.min_speech_seconds: Minimum speech duration to proceed. Default: 1.0
- transcribe.noise_upgrade: Auto-switch to Rev.ai for noisy recordings (default: true)
- transcribe.noise_upgrade_min_speech_ratio: Min speech/loud ratio required for noisy upgrade (default: 0.3). Filters out music and other non-speech noise.

Parakeet backend settings (transcribe.parakeet):
- model_version: Parakeet model version ("v3"). Default: "v3"
- cache_dir: Optional helper cache directory
- timeout_sec: Helper timeout in seconds. Default: 120.0

Rev.ai backend settings (transcribe.revai):
- model: Rev.ai transcriber ("fusion", "machine", "low_cost"). Default: "fusion"
- Automatically loads recent entity names as custom vocabulary for improved recognition

Gemini backend settings (transcribe.gemini):
- No configuration needed (model resolved by think.models context system)
- Includes speaker diarization

Platform optimizations:
- Apple Silicon hosts use the CoreML Parakeet helper.
- Linux hosts use a supervised parakeet.cpp server.

Failure semantics & telemetry:
- Exit 0 = output written or silence-filtered; EXIT_PROVIDER_BLOCKED (69) = honest
  deferral with the input preserved for the daily retry; 1 = hard failure.
- Every attempt emits one content-free observe.transcribed event carrying per-stage
  timings and a machine-readable reason.
- Full contract: solstone/observe/transcribe/failure-and-telemetry.md
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import json
import logging
import os
import platform
import resource
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING

from solstone.apps.settings.install_copy import (
    STT_DETECTED_MEMORY_TEMPLATE,
    STT_DETECTED_MEMORY_UNKNOWN,
    STT_EXPLICIT_LOCAL_LOW_TEMPLATE,
    STT_LOCAL_REQUIREMENTS_TEMPLATE,
    STT_LOCAL_UNSUPPORTED,
    STT_NO_KEY_RECOVERY,
)
from solstone.apps.speakers.encoder_config import (
    OVERLAP_DETECTOR_ID,
    OVERLAP_DETECTOR_SHA256,
)
from solstone.observe.exit_codes import EXIT_PROVIDER_BLOCKED
from solstone.observe.model_assets import resolve_wespeaker_model
from solstone.observe.processing_record import (
    HANDLER_TRANSCRIBE,
    REASON_CORRUPT_INPUT,
    REASON_NO_DECODABLE_AUDIO,
    REASON_OK,
    STATE_ANALYZED,
    STATE_EMPTY,
    STATE_FAILED,
    build_processing_record,
)
from solstone.observe.transcribe import (
    BACKEND_REGISTRY,
    ConfidentialAudioEgressError,
    get_backend,
)
from solstone.observe.transcribe import transcribe as stt_transcribe
from solstone.observe.transcribe.resource import (
    STT_SURFACE,
    local_stt_backend,
    select_stt_backend,
    stt_local_floor_bytes,
)
from solstone.observe.transcribe.sound_tags import is_salient, tag_audio
from solstone.observe.utils import (
    SAMPLE_RATE,
    AudioDecodeError,
    get_segment_key,
    load_audio,
)
from solstone.think.callosum import callosum_send
from solstone.think.journal_io import write_text
from solstone.think.journal_io.npz import write_npz
from solstone.think.media import AUDIO_EXTENSIONS as SUPPORTED_AUDIO_FORMATS
from solstone.think.providers.memory import gb, read_available_bytes
from solstone.think.providers.parakeet_install import ParakeetProviderError
from solstone.think.providers.parakeet_server import ParakeetServerNotReady
from solstone.think.utils import (
    day_dirs,
    day_from_path,
    get_config,
    get_journal,
    iter_segments,
    journal_relative_path,
    require_solstone,
    resolve_journal_path,
    setup_cli,
)

if TYPE_CHECKING:
    import numpy as np
    import onnxruntime as ort

    from solstone.observe.vad import AudioReduction, VadResult

# Re-export defaults for backwards compatibility
__all__ = [
    "DEFAULT_MIN_SPEECH_SECONDS",
    "MIN_STATEMENT_DURATION",
    "main",
]

# Default transcription settings
DEFAULT_BACKEND = "parakeet"
DEFAULT_MIN_SPEECH_SECONDS = 1.0

# Minimum statement duration for embedding (seconds)
MIN_STATEMENT_DURATION = 0.3

EMBEDDER_NAME = "wespeaker-resnet34-256"
WESPEAKER_MODEL_SHA256 = (
    "5ef208a9da1453335308a6b6f4e6dfbd7e183a38b604de0a57664f45d257fe94"
)
PYANNOTE_OVERLAP_MODEL_SHA256 = OVERLAP_DETECTOR_SHA256

# Number of recent entity names to load for transcription context
ENTITY_NAMES_LIMIT = 40

# Module-level embedder cache
_embedder_session: ort.InferenceSession | None = None


def resolve_default_backend(args: argparse.Namespace, transcribe_config: dict) -> str:
    """Resolve the effective default STT backend once, from a single free-RAM read.

    Honors explicit CLI/config choices, warns on an explicit local choice below
    the platform floor, and raises SystemExit(1) with a clear requirement when
    there is no viable backend.
    """
    available_bytes = read_available_bytes()
    floor_bytes = stt_local_floor_bytes()
    local_backend = local_stt_backend()
    google_key_present = bool(os.getenv("GOOGLE_API_KEY"))
    configured_backend = transcribe_config.get("backend")
    explicit_backend = args.backend or configured_backend
    if explicit_backend:
        if explicit_backend not in BACKEND_REGISTRY:
            logging.warning(
                "Configured STT backend %r is unavailable; using %s",
                explicit_backend,
                DEFAULT_BACKEND,
            )
            explicit_backend = DEFAULT_BACKEND
        _warn_if_local_below_floor(explicit_backend, available_bytes, floor_bytes)
        return explicit_backend
    from solstone.think.services import spp

    confidential_lane_active = spp.confidential_provenance() is not None
    backend = select_stt_backend(
        available_bytes,
        google_key_present=google_key_present,
        floor_bytes=floor_bytes,
        local_backend=local_backend,
        confidential_lane_active=confidential_lane_active,
    )
    if backend == STT_SURFACE:
        _surface_stt_requirement(available_bytes, floor_bytes)
        raise SystemExit(1)
    return backend


def _warn_if_local_below_floor(
    backend: str, available_bytes: int | None, floor_bytes: int | None
) -> None:
    if (
        backend == "parakeet"
        and floor_bytes is not None
        and available_bytes is not None
        and available_bytes < floor_bytes
    ):
        logging.warning(
            STT_EXPLICIT_LOCAL_LOW_TEMPLATE.format(ram_gb=floor_bytes // 1024**3)
        )


def _surface_stt_requirement(
    available_bytes: int | None, floor_bytes: int | None
) -> None:
    if floor_bytes is None:
        requirement = STT_LOCAL_UNSUPPORTED
    else:
        requirement = STT_LOCAL_REQUIREMENTS_TEMPLATE.format(
            ram_gb=floor_bytes // 1024**3
        )
    available_gb = gb(available_bytes)
    detected = (
        STT_DETECTED_MEMORY_UNKNOWN
        if available_gb is None
        else STT_DETECTED_MEMORY_TEMPLATE.format(available_gb=available_gb)
    )
    logging.error("%s %s %s", requirement, detected, STT_NO_KEY_RECOVERY)


def _select_onnx_providers() -> list[str]:
    """Return the ONNX Runtime provider list for this host.

    Darwin (any arch) prefers CoreML with CPU fallback; elsewhere, CPU only.
    """
    if platform.system() == "Darwin":
        return ["CoreMLExecutionProvider", "CPUExecutionProvider"]
    return ["CPUExecutionProvider"]


def _get_embedder_session() -> ort.InferenceSession:
    """Return a cached ONNX InferenceSession for the WeSpeaker encoder."""
    global _embedder_session

    if _embedder_session is None:
        import onnxruntime as ort

        wespeaker_model_path = resolve_wespeaker_model()
        if not wespeaker_model_path.is_file():
            raise FileNotFoundError(
                f"WeSpeaker model asset not found at {wespeaker_model_path}. "
                "Run `make install` to verify the bundled asset."
            )
        providers = _select_onnx_providers()
        start = time.monotonic()
        _embedder_session = ort.InferenceSession(
            str(wespeaker_model_path),
            providers=providers,
        )
        elapsed = time.monotonic() - start
        logging.info(
            "wespeaker session loaded providers=%s elapsed=%.2fs",
            _embedder_session.get_providers(),
            elapsed,
        )

    return _embedder_session


def _compute_wespeaker_features(audio: np.ndarray, sample_rate: int) -> np.ndarray:
    """Compute Kaldi-style fbank features for the bundled WeSpeaker encoder."""
    import kaldi_native_fbank as knf
    import numpy as np

    if sample_rate != SAMPLE_RATE:
        raise ValueError(
            f"WeSpeaker embedder requires {SAMPLE_RATE} Hz audio, got {sample_rate}"
        )

    opts = knf.FbankOptions()
    opts.frame_opts.samp_freq = float(sample_rate)
    opts.frame_opts.dither = 0.0
    opts.frame_opts.snip_edges = True
    opts.frame_opts.frame_length_ms = 25.0
    opts.frame_opts.frame_shift_ms = 10.0
    opts.mel_opts.num_bins = 80
    opts.energy_floor = 0.0
    opts.use_energy = False

    fbank = knf.OnlineFbank(opts)
    scaled = (audio.astype(np.float32) * 32768.0).tolist()
    fbank.accept_waveform(float(sample_rate), scaled)
    fbank.input_finished()

    frames = [fbank.get_frame(i) for i in range(fbank.num_frames_ready)]
    if not frames:
        return np.zeros((0, 80), dtype=np.float32)

    feats = np.stack(frames, axis=0).astype(np.float32)
    feats = feats - feats.mean(axis=0, keepdims=True)
    return feats


def _get_jsonl_path(audio_path: Path) -> Path:
    """Generate the corresponding JSONL path."""
    return audio_path.with_suffix(".jsonl")


def _get_embeddings_path(audio_path: Path) -> Path:
    """Generate the corresponding embeddings path."""
    return audio_path.with_suffix(".npz")


class _StageTimings:
    """Content-free per-stage wall-clock accumulator for observe.transcribed.

    Records only stages that actually ran, as integer milliseconds under a
    ``<stage>_ms`` key.  Holds no audio, no transcript, and no file content.
    """

    def __init__(self) -> None:
        self._stages: dict[str, int] = {}

    @contextlib.contextmanager
    def time(self, stage: str) -> Iterator[None]:
        """Time a pipeline stage, recording it as ``<stage>_ms``.

        Repeat entries for one stage accumulate, so a stage split across several
        calls (``write`` covers the jsonl and the npz) reports its total.
        """
        started = time.perf_counter()
        try:
            yield
        finally:
            key = f"{stage}_ms"
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            self._stages[key] = self._stages.get(key, 0) + elapsed_ms

    def set_ms(self, stage: str, value: int) -> None:
        """Record a stage duration measured outside this process."""
        self._stages[f"{stage}_ms"] = value

    def get_ms(self, stage: str) -> int | None:
        return self._stages.get(f"{stage}_ms")

    def as_dict(self) -> dict[str, int]:
        return dict(self._stages)


def _read_queue_wait_ms() -> int | None:
    """Read the queue wait sense.py measured for this file, if it set one."""
    raw = os.getenv("SOL_QUEUE_WAIT_MS")
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        logging.warning("Invalid SOL_QUEUE_WAIT_MS: %s", raw[:50])
        return None


def _peak_rss_mib() -> int:
    """Peak resident set size of this process, in MiB.

    ``ru_maxrss`` is KiB on Linux and bytes on macOS.  ``resource`` here is the
    stdlib module, not the sibling ``transcribe/resource.py`` (absolute imports).
    """
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    divisor = 1024 * 1024 if sys.platform == "darwin" else 1024
    return int(peak / divisor)


def _uses_parakeet_cpp(backend: str | None) -> bool:
    """Return whether this backend dispatches to the Linux parakeet.cpp path."""
    return backend == "parakeet-cpp" or (
        backend == "parakeet" and sys.platform.startswith("linux")
    )


def _emit_transcribed(
    event: dict,
    *,
    outcome: str,
    timings: _StageTimings | None = None,
    backend: str | None = None,
    model_info: dict | None = None,
    backend_config: dict | None = None,
    audio_seconds: float | None = None,
    reduced_seconds: float | None = None,
    reason: str | None = None,
    error: str | None = None,
) -> None:
    """Attach the content-free envelope to ``event`` and emit observe.transcribed.

    Every outcome (transcribed / deferred / failed / filtered / preserved) flows
    through here so the envelope is built exactly once.  Fields are attached only
    when they are actually known; nothing is fabricated.  No transcript text,
    words, topics, setting, or emotion is ever carried.
    """
    event["outcome"] = outcome
    if backend:
        event["backend"] = backend

    # device: backend-reported value first, then parakeet.cpp's supervisor placement
    # when that record exists, then configured value. Deferred events still omit
    # model because resolving it can cost a CoreML helper subprocess.
    device = (model_info or {}).get("device")
    if not device:
        if _uses_parakeet_cpp(backend):
            from solstone.observe.transcribe import _parakeet_cpp

            device = _parakeet_cpp.resolve_serving_device(backend_config or {})
        else:
            device = (backend_config or {}).get("device")
    if device:
        event["device"] = device
    model = (model_info or {}).get("model")
    if model:
        event["model"] = model

    if audio_seconds is not None:
        event["audio_seconds"] = round(audio_seconds, 1)
    if reduced_seconds is not None:
        event["reduced_seconds"] = round(reduced_seconds, 1)
    if reason:
        event["reason"] = reason
    if error:
        event["error"] = error

    if timings is not None:
        stages = timings.as_dict()
        if stages:
            event["timings"] = stages
        asr_ms = timings.get_ms("asr")
        if outcome == "transcribed" and audio_seconds is not None and asr_ms:
            event["rtfx"] = round(audio_seconds / (asr_ms / 1000), 2)

    event["peak_rss_mib"] = _peak_rss_mib()
    callosum_send("observe", "transcribed", **event)


def _emit_deferred(
    raw_path: Path,
    vad_result: VadResult,
    segment: str | None,
    observer: str | None,
    *,
    reason: str,
    timings: _StageTimings,
    backend: str | None,
    backend_config: dict | None,
    audio_seconds: float | None,
    reduced_seconds: float | None,
) -> None:
    """Emit the honest-deferral event for a provider that could not do the work.

    Deliberately swallows its own failure: the caller must still exit
    EXIT_PROVIDER_BLOCKED so the input is preserved for retry even if the bus is
    down.  ``model`` is not carried -- see the note in _emit_transcribed.
    """
    try:
        event = _build_base_event(raw_path, vad_result, segment, observer)
        _emit_transcribed(
            event,
            outcome="deferred",
            timings=timings,
            backend=backend,
            backend_config=backend_config,
            audio_seconds=audio_seconds,
            reduced_seconds=reduced_seconds,
            reason=reason,
        )
    except Exception:
        logging.exception("Failed to emit transcription deferral event")


def _failure_reason(exc: Exception) -> str:
    """Machine-readable classification for a hard transcription failure.

    Provider errors already carry a reason code; anything else is labelled by its
    exception type.
    """
    if isinstance(exc, ParakeetProviderError):
        return exc.reason_code
    return type(exc).__name__


def _failure_label(exc: Exception) -> str:
    """The exception's type name -- the only part of it safe to put on the bus.

    Exception *messages* are not safe: SchemaValidationError embeds a preview of the
    raw model output (think/models.py), and transcribe/gemini.py interpolates that
    into its own message, so a message could carry transcript text onto the event.
    The full message and traceback go to the handler log, which is where the health
    UI already deep-links.  Keeping only the type name makes the content-free
    guarantee structural instead of a per-exception audit that any new provider
    error could quietly break.
    """
    return type(exc).__name__


def _build_base_event(
    audio_path: Path,
    vad_result: VadResult,
    segment: str | None = None,
    observer: str | None = None,
) -> dict:
    """Build base event dict for callosum emission.

    Args:
        audio_path: Path to the audio file
        vad_result: VAD result with speech detection info
        segment: Optional segment key (e.g., "143022_300")
        observer: Optional observer name

    Returns:
        Event dict with common fields for observe.transcribed events
    """
    journal_path = Path(get_journal())
    day = day_from_path(audio_path)

    try:
        rel_input = journal_relative_path(journal_path, audio_path)
    except ValueError:
        rel_input = audio_path

    event = {
        "input": str(rel_input),
        "vad_duration": round(vad_result.duration, 1),
        "vad_speech": round(vad_result.speech_duration, 1),
        "noisy": vad_result.is_noisy(),
    }

    # Add RMS values if available
    if vad_result.noisy_rms is not None:
        event["noisy_rms"] = round(vad_result.noisy_rms, 4)
        event["noisy_s"] = round(vad_result.noisy_s, 1)
    if vad_result.loud_windows > 0:
        event["loud_windows"] = vad_result.loud_windows
        event["speech_loud_windows"] = vad_result.speech_loud_windows
        ratio = vad_result.loud_speech_ratio
        if ratio is not None:
            event["loud_speech_ratio"] = round(ratio, 2)

    if day:
        event["day"] = day
    if segment:
        event["segment"] = segment
    if observer:
        event["observer"] = observer

    return event


def _embed_statements(
    audio: np.ndarray,
    statements: list[dict],
    sample_rate: int,
) -> dict[str, np.ndarray] | None:
    """Generate voice embeddings for each statement.

    Args:
        audio: Audio buffer (float32, mono)
        statements: List of statements
        sample_rate: Sample rate in Hz

    Returns:
        Dict with embedding data or None on error:
        - embeddings: (N, 256) float32 array
        - statement_ids: (N,) int32 array of statement IDs
        - encoder: 0-d array naming the embedder
    """
    import numpy as np

    try:
        session = _get_embedder_session()
        audio_duration = len(audio) / sample_rate
        input_name = session.get_inputs()[0].name
        output_name = session.get_outputs()[0].name

        # Filter statements with valid timestamps and sufficient duration
        # Defensive: handle None timestamps, clamp to audio bounds
        valid_statements = []
        for s in statements:
            start = s.get("start")
            end = s.get("end")

            # Skip if timestamps are None or invalid
            if start is None or end is None:
                continue
            if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
                continue

            # Clamp to audio bounds
            start = max(0.0, min(start, audio_duration))
            end = max(0.0, min(end, audio_duration))

            # Check duration after clamping
            if end - start >= MIN_STATEMENT_DURATION:
                valid_statements.append({"id": s["id"], "start": start, "end": end})

        if not valid_statements:
            logging.info("No statements with sufficient duration for embedding")
            return None

        logging.info(f"Embedding {len(valid_statements)} statements...")
        t0 = time.perf_counter()

        embeddings = []
        statement_ids = []
        durations = []
        skipped = 0

        for stmt in valid_statements:
            start_sample = int(stmt["start"] * sample_rate)
            end_sample = int(stmt["end"] * sample_rate)
            stmt_audio = audio[start_sample:end_sample]

            # Skip if too short after slicing
            if len(stmt_audio) < int(MIN_STATEMENT_DURATION * sample_rate):
                skipped += 1
                continue

            try:
                feats = _compute_wespeaker_features(stmt_audio, sample_rate)
                if feats.shape[0] == 0:
                    skipped += 1
                    continue
                emb = session.run([output_name], {input_name: feats[None, :, :]})[0]
                embeddings.append(emb[0].astype(np.float32))
                statement_ids.append(stmt["id"])
                durations.append((end_sample - start_sample) / SAMPLE_RATE)
            except Exception:
                logging.exception(
                    "wespeaker embedding failed for statement %s", stmt["id"]
                )
                skipped += 1
                continue

        embed_time = time.perf_counter() - t0

        if not embeddings:
            logging.warning("No embeddings generated")
            return None

        logging.info(
            f"  Embedded {len(embeddings)} statements "
            f"(skipped {skipped}) in {embed_time:.2f}s"
        )

        return {
            "embeddings": np.stack(embeddings, axis=0).astype(np.float32),
            "statement_ids": np.asarray(statement_ids, dtype=np.int32),
            "durations_s": np.asarray(durations, dtype=np.float32),
            "encoder": np.array(EMBEDDER_NAME),
        }

    except Exception:
        logging.exception("failed to load WeSpeaker embedder")
        return None


def _statements_to_jsonl(
    statements: list[dict],
    raw_filename: str,
    base_datetime: datetime.datetime,
    model_info: dict,
    source: str | None = None,
    observer: str | None = None,
    enrichment: dict | None = None,
    vad_result: VadResult | None = None,
    segment_meta: dict | None = None,
    backend: str | None = None,
    *,
    overlap_fraction: float | None = None,
    overlap_detector: str | None = None,
    processing_record: dict | None = None,
    sound_tags: dict | None = None,
) -> list[str]:
    """Convert statements to JSONL lines.

    Args:
        statements: List of statements
        raw_filename: Original audio filename for metadata
        base_datetime: Base datetime for timestamp calculation
        model_info: Dict with model, device, compute_type from backend
        source: Optional source label (e.g., "mic", "sys")
        observer: Optional observer name for metadata
        enrichment: Optional enrichment data with topics, setting, warning, and
            per-statement corrected text and emotions
        vad_result: Optional VAD result for noise detection metadata
        segment_meta: Optional metadata dict from SEGMENT_META env var
            (facet, setting, host, platform, etc.). Setting overrides enrichment.
        backend: Optional STT backend name (e.g., "parakeet", "revai")
        overlap_fraction: Optional fraction of speech containing overlapping speakers
        overlap_detector: Optional overlap detector identifier
        processing_record: Optional _solstone_processing record
        sound_tags: Optional ambient sound-tag metadata

    Returns:
        List of JSON strings (metadata line first, then entries)
    """
    # Build metadata line with transcription config
    metadata = {
        "raw": raw_filename,
        "backend": backend or "unknown",
        "model": model_info.get("model", "unknown"),
        "device": model_info.get("device", "unknown"),
        "compute_type": model_info.get("compute_type", "unknown"),
    }
    if observer:
        metadata["observer"] = observer

    # Add noise detection metadata if available
    if vad_result:
        metadata["duration"] = round(vad_result.duration, 2)
        metadata["noisy"] = vad_result.is_noisy()
        if vad_result.noisy_rms is not None:
            metadata["noisy_rms"] = round(vad_result.noisy_rms, 4)
            metadata["noisy_s"] = round(vad_result.noisy_s, 1)
        if vad_result.loud_windows > 0:
            metadata["loud_windows"] = vad_result.loud_windows
            metadata["speech_loud_windows"] = vad_result.speech_loud_windows
            ratio = vad_result.loud_speech_ratio
            if ratio is not None:
                metadata["loud_speech_ratio"] = round(ratio, 2)
    if overlap_fraction is not None and overlap_detector is not None:
        metadata["overlap_fraction"] = round(float(overlap_fraction), 4)
        metadata["overlap_detector"] = overlap_detector

    # Add enrichment metadata if available
    if enrichment:
        if "topics" in enrichment:
            metadata["topics"] = enrichment["topics"]
        if "setting" in enrichment:
            metadata["setting"] = enrichment["setting"]
        if "warning" in enrichment and enrichment["warning"]:
            metadata["warning"] = enrichment["warning"]

    # Add segment metadata (from SEGMENT_META env var)
    # These fields override any enrichment values (e.g., setting)
    if segment_meta:
        for key, value in segment_meta.items():
            metadata[key] = value

    if processing_record is not None:
        metadata["_solstone_processing"] = processing_record
    if sound_tags is not None:
        metadata["sound_tags"] = sound_tags

    lines = [json.dumps(metadata)]

    # Get enriched statements list (positional matching)
    enriched_statements = []
    if enrichment and "statements" in enrichment:
        enriched_statements = enrichment["statements"]

    # Build entry lines
    for i, stmt in enumerate(statements):
        # Calculate absolute timestamp (handle None for invalid timestamps)
        start_seconds = stmt["start"] if stmt["start"] is not None else 0.0
        stmt_dt = base_datetime + datetime.timedelta(seconds=start_seconds)
        timestamp_str = stmt_dt.strftime("%H:%M:%S")

        entry = {
            "start": timestamp_str,
            "text": stmt["text"],
        }
        if source:
            entry["source"] = source

        # Pass through speaker ID if present (from diarized backends like Rev.ai, Gemini)
        if "speaker" in stmt:
            entry["speaker"] = stmt["speaker"]

        # Add corrected text and emotion from enrichment by position
        if i < len(enriched_statements):
            enriched = enriched_statements[i]
            if isinstance(enriched, dict):
                # Add corrected text only if different from original
                corrected = enriched.get("corrected", "")
                if corrected and corrected != stmt["text"]:
                    entry["corrected"] = corrected
                # Add emotion (overrides emotion from statement)
                emotion = enriched.get("emotion", "")
                if emotion:
                    entry["emotion"] = emotion

        lines.append(json.dumps(entry))

    return lines


def _write_empty_processing_jsonl(
    raw_path: Path,
    jsonl_path: Path,
    *,
    model_info: dict,
    observer: str | None,
    vad_result: VadResult | None,
    segment_meta: dict | None,
    backend: str | None,
    sound_tags: dict | None = None,
) -> None:
    record = build_processing_record(
        state=STATE_EMPTY,
        reason_code=REASON_NO_DECODABLE_AUDIO,
        handler=HANDLER_TRANSCRIBE,
        input_size=raw_path.stat().st_size,
    )
    lines = _statements_to_jsonl(
        [],
        f"{raw_path.stem}{raw_path.suffix}",
        datetime.datetime.min,
        model_info,
        observer=observer,
        vad_result=vad_result,
        segment_meta=segment_meta,
        backend=backend,
        processing_record=record,
        sound_tags=sound_tags,
    )
    write_text(jsonl_path, "\n".join(lines) + "\n")


def _write_failed_processing_jsonl(
    raw_path: Path,
    jsonl_path: Path,
    *,
    reason_code: str,
) -> None:
    record = build_processing_record(
        state=STATE_FAILED,
        reason_code=reason_code,
        handler=HANDLER_TRANSCRIBE,
        input_size=raw_path.stat().st_size,
    )
    lines = _statements_to_jsonl(
        [],
        f"{raw_path.stem}{raw_path.suffix}",
        datetime.datetime.min,
        {},
        processing_record=record,
        sound_tags=None,
    )
    write_text(jsonl_path, "\n".join(lines) + "\n")


def process_audio(
    raw_path: Path,
    audio_buffer: np.ndarray,
    vad_result: VadResult,
    backend_config: dict,
    redo: bool = False,
    reduction: AudioReduction | None = None,
    reduced_audio: np.ndarray | None = None,
    backend: str | None = None,
    entity_names: list[str] | None = None,
    *,
    sound_tags: dict | None = None,
    timings: _StageTimings | None = None,
) -> None:
    """Process a raw audio file with pre-computed VAD.

    This is the main orchestration function that coordinates:
    - STT backend dispatch
    - Enrichment (optional)
    - Embedding generation
    - Output file writing
    - Event emission

    Args:
        raw_path: Path to audio file in journal segment directory (HHMMSS_LEN/)
        audio_buffer: Full audio waveform (float32 mono at SAMPLE_RATE)
        vad_result: Pre-computed VAD result from run_vad()
        backend_config: Configuration for STT backend
        redo: If True, skip "already processed" check
        reduction: Optional AudioReduction mapping for timestamp restoration
        reduced_audio: Optional reduced audio buffer (used if reduction provided)
        backend: STT backend name. If omitted, uses DEFAULT_BACKEND.
        entity_names: Optional list of entity names for STT and enrichment context
        sound_tags: Optional ambient sound-tag metadata computed from full audio
        timings: Stage-timing accumulator carrying the pre-STT stages measured by
            _process_one. A fresh one is created when called without it.

    Raises:
        SystemExit: EXIT_PROVIDER_BLOCKED when the STT provider is not ready or the
            confidential lane refuses egress -- an honest deferral that preserves the
            input for the next run. 1 on hard failure.
    """
    start_time = time.time()
    resolved_backend = backend or DEFAULT_BACKEND
    if timings is None:
        timings = _StageTimings()

    audio_seconds = len(audio_buffer) / SAMPLE_RATE
    reduced_seconds = (
        len(reduced_audio) / SAMPLE_RATE if reduced_audio is not None else None
    )
    model_info: dict = {}

    # Derive segment from path
    segment = get_segment_key(raw_path)

    # Skip if already processed (unless redo mode)
    jsonl_path = _get_jsonl_path(raw_path)
    if not redo and jsonl_path.exists():
        logging.info(f"Already processed: {raw_path}")
        return

    # Get observer name once for use in metadata and events
    observer = os.getenv("OBSERVER_NAME")

    # Get segment metadata (from sense.py via SEGMENT_META env var)
    segment_meta = None
    segment_meta_str = os.getenv("SEGMENT_META")
    if segment_meta_str:
        try:
            segment_meta = json.loads(segment_meta_str)
        except json.JSONDecodeError:
            logging.warning(f"Invalid SEGMENT_META JSON: {segment_meta_str[:100]}")

    # Gemini uses chunk-based transcription with VAD segments for timestamp accuracy
    # Other backends use reduced audio with post-hoc timestamp restoration
    use_gemini_chunks = backend == "gemini" and vad_result.speech_segments

    if use_gemini_chunks:
        # Gemini: use full audio buffer with VAD segments for chunking
        stt_buffer = audio_buffer
    elif reduced_audio is not None:
        # Other backends: use reduced audio
        stt_buffer = reduced_audio
    else:
        stt_buffer = audio_buffer

    try:
        # Dispatch to STT backend
        with timings.time("asr"):
            if use_gemini_chunks:
                # Pass VAD segments to Gemini for chunk-based transcription
                statements = stt_transcribe(
                    resolved_backend,
                    stt_buffer,
                    SAMPLE_RATE,
                    backend_config,
                    speech_segments=vad_result.speech_segments,
                )
            else:
                statements = stt_transcribe(
                    resolved_backend, stt_buffer, SAMPLE_RATE, backend_config
                )

        # Get model info for metadata (dynamic import based on backend)
        backend_module = get_backend(resolved_backend)
        model_info = backend_module.get_model_info(backend_config)

        # Load config for preserve_all setting
        config = get_config()
        preserve_all = config.get("transcribe", {}).get("preserve_all", False)

        # Build base event fields (always emitted as observe.transcribed)
        event = _build_base_event(raw_path, vad_result, segment, observer)

        # Handle no speech detected
        if not statements:
            logging.info(
                "STT backend returned 0 statements, treating as silence "
                "(VAD: %.1fs speech of %.1fs)",
                vad_result.speech_duration,
                vad_result.duration,
            )
            if preserve_all:
                outcome = "preserved"
                _write_empty_processing_jsonl(
                    raw_path,
                    jsonl_path,
                    model_info=model_info,
                    observer=observer,
                    vad_result=vad_result,
                    segment_meta=segment_meta,
                    backend=resolved_backend,
                    sound_tags=sound_tags,
                )
                logging.info(
                    f"No speech detected in {raw_path}, preserving file "
                    f"(preserve_all=true, VAD: {vad_result.speech_duration:.1f}s "
                    f"of {vad_result.duration:.1f}s)"
                )
            elif sound_tags is not None and is_salient(sound_tags["tags"]):
                outcome = "filtered"
                _write_empty_processing_jsonl(
                    raw_path,
                    jsonl_path,
                    model_info=model_info,
                    observer=observer,
                    vad_result=vad_result,
                    segment_meta=segment_meta,
                    backend=resolved_backend,
                    sound_tags=sound_tags,
                )
                logging.info(
                    "No speech detected in %s, wrote salient sound metadata before removing file",
                    raw_path,
                )
                raw_path.unlink()
            else:
                outcome = "filtered"
                logging.info(f"No speech detected in {raw_path}, removing file")
                raw_path.unlink()

            _emit_transcribed(
                event,
                outcome=outcome,
                timings=timings,
                backend=resolved_backend,
                model_info=model_info,
                backend_config=backend_config,
                audio_seconds=audio_seconds,
                reduced_seconds=reduced_seconds,
            )
            return

        # Extract date and time from path structure
        journal_path = Path(get_journal())
        day = day_from_path(raw_path)
        time_part = segment.split("_")[0] if segment else "000000"
        if day is None:
            logging.error(f"Could not extract day from path: {raw_path}")
            time_obj = datetime.datetime.strptime(time_part, "%H%M%S").time()
            base_dt = datetime.datetime.combine(datetime.date.today(), time_obj)
        else:
            base_dt = datetime.datetime.strptime(f"{day}_{time_part}", "%Y%m%d_%H%M%S")

        # Extract source from <source>_audio pattern
        source = None
        suffix = raw_path.stem
        if suffix.endswith("_audio") and suffix != "audio":
            source = suffix[:-6]  # Remove "_audio" suffix

        # Run enrichment if enabled (extracts topics, setting, emotions, corrections)
        enrichment = None
        enrich_enabled = config.get("transcribe", {}).get("enrich", True)
        if enrich_enabled:
            from solstone.observe.enrich import enrich_transcript

            with timings.time("enrich"):
                enrichment = enrich_transcript(
                    stt_buffer, SAMPLE_RATE, statements, entity_names=entity_names
                )

        # Generate embeddings before timestamp restoration
        # Use reduced audio buffer if available for consistent timestamps
        with timings.time("embed"):
            embeddings_data = _embed_statements(stt_buffer, statements, SAMPLE_RATE)
        from solstone.observe.transcribe.overlap import compute_overlap_and_logprobs

        with timings.time("overlap"):
            overlap_fraction_value, pyannote_logprobs = compute_overlap_and_logprobs(
                audio_buffer
            )

        # Restore original timestamps if audio was reduced (non-Gemini backends only)
        # Gemini with chunks already has timestamps in original audio time
        if reduction and not use_gemini_chunks:
            from solstone.observe.vad import restore_statement_timestamps

            statements = restore_statement_timestamps(statements, reduction)
            logging.info(
                f"  Restored timestamps from reduced audio "
                f"({reduction.reduced_duration:.1f}s -> {reduction.original_duration:.1f}s)"
            )

        # Local speaker diarization for backends that produce no speaker labels.
        # Skip when overlap is near zero — the recording is effectively solo
        # speech and diarization adds no value.  Otherwise reuse the pyannote
        # log-probs computed above so the diarizer skips its own pyannote pass.
        _DIARIZE_MIN_OVERLAP = 0.05
        if resolved_backend == "parakeet":
            if overlap_fraction_value < _DIARIZE_MIN_OVERLAP:
                logging.info(
                    "  Skipping diarization: overlap=%.2f (threshold %.2f)",
                    overlap_fraction_value,
                    _DIARIZE_MIN_OVERLAP,
                )
            else:
                try:
                    from solstone.observe.transcribe.diarize import diarize_auto_k

                    with timings.time("diarize"):
                        labels = diarize_auto_k(
                            raw_path,
                            statements,
                            avg_log_probs=pyannote_logprobs,
                            audio=audio_buffer,
                        )
                    assigned = 0
                    for stmt, lbl in zip(statements, labels):
                        if lbl is not None:
                            stmt["speaker"] = lbl
                            assigned += 1
                    logging.info(
                        "  Local diarization: %d/%d sentences labeled (overlap=%.2f)",
                        assigned,
                        len(statements),
                        overlap_fraction_value,
                    )
                except Exception:
                    logging.exception(
                        "Local diarization failed; speaker labels will be absent"
                    )

        # Convert to JSONL format (now with original timestamps)
        raw_filename = f"{raw_path.stem}{raw_path.suffix}"
        processing_record = build_processing_record(
            state=STATE_ANALYZED,
            reason_code=REASON_OK,
            handler=HANDLER_TRANSCRIBE,
            input_size=raw_path.stat().st_size,
        )
        jsonl_lines = _statements_to_jsonl(
            statements,
            raw_filename,
            base_dt,
            model_info,
            source,
            observer,
            enrichment,
            vad_result,
            segment_meta,
            resolved_backend,
            overlap_fraction=overlap_fraction_value,
            overlap_detector=OVERLAP_DETECTOR_ID,
            processing_record=processing_record,
            sound_tags=sound_tags,
        )

        # Write JSONL
        with timings.time("write"):
            write_text(jsonl_path, "\n".join(jsonl_lines) + "\n")
        logging.info(f"Transcribed {raw_path} -> {jsonl_path}")

        # Save embeddings
        if embeddings_data:
            embeddings_path = _get_embeddings_path(raw_path)
            with timings.time("write"):
                write_npz(
                    embeddings_path,
                    embeddings_data,
                    expected_keys=tuple(embeddings_data.keys()),
                )
            logging.info(f"Saved embeddings: {embeddings_path}")
            try:
                from solstone.apps.speakers.candidate_tracker import CandidateTracker

                tracker_day = day or day_from_path(raw_path)
                tracker_segment = segment or get_segment_key(raw_path)
                tracker_stream = raw_path.parent.parent.name
                if tracker_day and tracker_segment and tracker_stream:
                    CandidateTracker().process_segment(
                        day=tracker_day,
                        segment_key=tracker_segment,
                        stream=tracker_stream,
                        source=raw_path.stem,
                        seg_dir=raw_path.parent,
                    )
            except Exception:
                logging.warning(
                    "Speaker candidate tracking failed for %s",
                    raw_path,
                    exc_info=True,
                )
        else:
            logging.warning(f"No embeddings generated for {raw_path}")

        # Add completion fields and emit event
        event["duration_ms"] = int((time.time() - start_time) * 1000)
        try:
            rel_output = journal_relative_path(journal_path, jsonl_path)
        except ValueError:
            rel_output = jsonl_path
        event["output"] = rel_output

        _emit_transcribed(
            event,
            outcome="transcribed",
            timings=timings,
            backend=resolved_backend,
            model_info=model_info,
            backend_config=backend_config,
            audio_seconds=audio_seconds,
            reduced_seconds=reduced_seconds,
        )

    except ParakeetServerNotReady as e:
        # The STT provider is unreachable -- a deferral, not a failure.  Nothing has
        # been written, so the audio stays on disk and the next sense scan re-picks
        # it.  Exit blocked so sense records neither a success nor a failure.
        logging.info(
            "Parakeet server not ready for %s (%s); deferring for retry: %s",
            raw_path,
            e.retry_reason,
            e,
        )
        _emit_deferred(
            raw_path,
            vad_result,
            segment,
            observer,
            reason=e.retry_reason,
            timings=timings,
            backend=resolved_backend,
            backend_config=backend_config,
            audio_seconds=audio_seconds,
            reduced_seconds=reduced_seconds,
        )
        raise SystemExit(EXIT_PROVIDER_BLOCKED) from e

    except ConfidentialAudioEgressError as e:
        logging.warning(
            "Confidential lane refused cloud STT for %s; deferring for retry: %s",
            raw_path,
            e,
        )
        _emit_deferred(
            raw_path,
            vad_result,
            segment,
            observer,
            reason="confidential_egress_blocked",
            timings=timings,
            backend=resolved_backend,
            backend_config=backend_config,
            audio_seconds=audio_seconds,
            reduced_seconds=reduced_seconds,
        )
        raise SystemExit(EXIT_PROVIDER_BLOCKED) from e

    except Exception as e:
        logging.error(f"Failed to transcribe {raw_path}: {e}", exc_info=True)
        try:
            event = _build_base_event(raw_path, vad_result, segment, observer)
            _emit_transcribed(
                event,
                outcome="failed",
                timings=timings,
                backend=resolved_backend,
                model_info=model_info,
                backend_config=backend_config,
                audio_seconds=audio_seconds,
                reduced_seconds=reduced_seconds,
                reason=_failure_reason(e),
                error=_failure_label(e),
            )
        except Exception:
            logging.exception("Failed to emit transcription failure event")
        from solstone.think.models import IncompleteJSONError

        if isinstance(e, IncompleteJSONError) and e.partial_text:
            text = e.partial_text
            logging.error(f"Partial response ({len(text)} chars) HEAD: {text[:1000]}")
            logging.error(f"Partial response TAIL: {text[-1000:]}")
        raise SystemExit(1) from e


def _process_one(
    audio_path: Path,
    args: argparse.Namespace,
    transcribe_config: dict,
    default_backend: str,
    entity_names: list[str],
) -> None:
    """Run the full transcription pipeline for a single audio file."""
    min_speech_seconds = transcribe_config.get(
        "min_speech_seconds", DEFAULT_MIN_SPEECH_SECONDS
    )
    preserve_all = transcribe_config.get("preserve_all", False)

    logging.info(f"Processing audio: {audio_path}")

    jsonl_path = _get_jsonl_path(audio_path)
    if not getattr(args, "redo", False) and jsonl_path.exists():
        logging.info(f"Already processed: {audio_path}")
        return

    from solstone.observe.vad import reduce_audio, run_vad

    timings = _StageTimings()
    queue_wait_ms = _read_queue_wait_ms()
    if queue_wait_ms is not None:
        timings.set_ms("queue_wait", queue_wait_ms)

    # Load audio once - handles M4A multi-stream mixing
    try:
        with timings.time("decode"):
            audio_buffer = load_audio(audio_path)
    except AudioDecodeError as e:
        logging.error("Failed to decode %s: %s", audio_path, e)
        _write_failed_processing_jsonl(
            audio_path,
            jsonl_path,
            reason_code=REASON_CORRUPT_INPUT,
        )
        try:
            journal_path = Path(get_journal())
            try:
                rel_input = journal_relative_path(journal_path, audio_path)
            except ValueError:
                rel_input = audio_path
            event = {"input": str(rel_input)}
            segment = get_segment_key(audio_path)
            day = day_from_path(audio_path)
            observer = os.getenv("OBSERVER_NAME")
            if day:
                event["day"] = day
            if segment:
                event["segment"] = segment
            if observer:
                event["observer"] = observer
            _emit_transcribed(
                event,
                outcome="failed",
                timings=timings,
                reason=_failure_reason(e),
                error=_failure_label(e),
            )
        except Exception:
            logging.exception("Failed to emit decode failure event")
        return

    # Stage 1: Run VAD to detect speech (lightweight, before loading STT model)
    with timings.time("vad"):
        vad_result = run_vad(audio_buffer, min_speech_seconds=min_speech_seconds)
    try:
        sound_tags = tag_audio(audio_buffer, SAMPLE_RATE)
    except Exception as exc:
        logging.warning(
            "sound tagging failed for %s: %s",
            audio_path,
            exc,
            exc_info=True,
        )
        sound_tags = None

    # Early exit if no speech detected (skip loading heavy STT model)
    if not vad_result.has_speech:
        observer = os.getenv("OBSERVER_NAME")
        segment = get_segment_key(audio_path)
        event = _build_base_event(audio_path, vad_result, segment, observer)

        if preserve_all:
            outcome = "preserved"
            _write_empty_processing_jsonl(
                audio_path,
                _get_jsonl_path(audio_path),
                model_info={},
                observer=observer,
                vad_result=vad_result,
                segment_meta=None,
                backend=None,
                sound_tags=sound_tags,
            )
            logging.info(
                f"Insufficient speech in {audio_path}, preserving file "
                f"(preserve_all=true, VAD: {vad_result.speech_duration:.1f}s "
                f"of {vad_result.duration:.1f}s, threshold: {min_speech_seconds:.1f}s)"
            )
        elif sound_tags is not None and is_salient(sound_tags["tags"]):
            outcome = "filtered"
            _write_empty_processing_jsonl(
                audio_path,
                _get_jsonl_path(audio_path),
                model_info={},
                observer=observer,
                vad_result=vad_result,
                segment_meta=None,
                backend=None,
                sound_tags=sound_tags,
            )
            logging.info(
                "Insufficient speech in %s, wrote salient sound metadata before removing file",
                audio_path,
            )
            audio_path.unlink()
        else:
            outcome = "filtered"
            logging.info(
                f"Insufficient speech in {audio_path}, removing file "
                f"(VAD: {vad_result.speech_duration:.1f}s of {vad_result.duration:.1f}s, "
                f"threshold: {min_speech_seconds:.1f}s)"
            )
            audio_path.unlink()

        _emit_transcribed(
            event,
            outcome=outcome,
            timings=timings,
            audio_seconds=len(audio_buffer) / SAMPLE_RATE,
        )
        return

    # Stage 2: Reduce audio by trimming long silence gaps (>2s)
    # Skip reduction for noisy clips with >70% speech — the "silence" gaps are
    # mostly noise and VAD boundaries are less reliable, so process the full audio.
    if vad_result.is_noisy() and vad_result.speech_ratio >= 0.7:
        logging.info(
            f"  Skipping audio reduction: noisy clip with "
            f"{vad_result.speech_ratio:.0%} speech"
        )
        reduced_audio, reduction = None, None
    else:
        with timings.time("reduce"):
            reduced_audio, reduction = reduce_audio(audio_buffer, vad_result)

    # Stage 3: Determine backend and build backend config
    # CLI --backend flag overrides the invocation-level default
    backend = args.backend or default_backend

    # Check for noise upgrade: auto-switch to Rev.ai for noisy recordings
    # Only applies when:
    # - No explicit CLI --backend flag (respect user's explicit choice)
    # - Not already using Rev.ai
    # - noise_upgrade is enabled (default: true)
    # - Audio is noisy
    # - Rev.ai token is available
    noise_upgrade = transcribe_config.get("noise_upgrade", True)
    min_ratio = transcribe_config.get("noise_upgrade_min_speech_ratio", 0.3)
    from solstone.think.services import spp

    confidential_lane_active = spp.confidential_provenance() is not None
    if (
        not args.backend
        and noise_upgrade
        and not confidential_lane_active
        and backend != "revai"
        and vad_result.is_noisy()
    ):
        from solstone.observe.transcribe.revai import has_token

        ratio = vad_result.loud_speech_ratio
        if ratio is not None and ratio < min_ratio:
            logging.info(
                "Noisy audio (RMS=%.4f) looks like non-speech (loud_speech_ratio=%.2f < %.2f), "
                "skipping Rev.ai upgrade",
                vad_result.noisy_rms,
                ratio,
                min_ratio,
            )
        elif has_token():
            logging.info(
                "Noisy audio detected (RMS=%.4f, loud_speech_ratio=%s), upgrading to Rev.ai backend",
                vad_result.noisy_rms,
                f"{ratio:.2f}" if ratio is not None else "n/a",
            )
            backend = "revai"

    # Get backend-specific config from nested structure
    if backend == "revai":
        from solstone.observe.transcribe.revai import (
            DEFAULT_MODEL as REVAI_DEFAULT_MODEL,
        )

        revai_config = transcribe_config.get("revai", {})
        model = revai_config.get("model", REVAI_DEFAULT_MODEL)
        backend_config = {
            "model": model,
        }
        # Pass entities to Rev.ai for custom vocabulary
        if entity_names:
            backend_config["entities"] = entity_names
    elif _uses_parakeet_cpp(backend):
        parakeet_cpp_config = transcribe_config.get("parakeet-cpp", {})
        backend_config = {k: v for k, v in parakeet_cpp_config.items() if k == "device"}
    elif backend == "parakeet":
        parakeet_config = transcribe_config.get("parakeet", {})
        backend_config = {
            k: v
            for k, v in parakeet_config.items()
            if k
            in (
                "model_version",
                "cache_dir",
                "timeout_sec",
                "device",
                "quantization",
            )
        }
    elif backend == "gemini":
        # Gemini backend - model resolved by think.models based on context
        # Entity names handled by enrich step, not passed to transcription
        backend_config = {}
    else:
        # Unknown backend - let get_backend() raise the error
        backend_config = {}

    # Stage 4: Process audio with STT backend
    process_audio(
        audio_path,
        audio_buffer,
        vad_result,
        backend_config,
        redo=args.redo,
        reduction=reduction,
        reduced_audio=reduced_audio,
        backend=backend,
        entity_names=entity_names,
        sound_tags=sound_tags,
        timings=timings,
    )


def main():
    parser = argparse.ArgumentParser(
        description="Transcribe audio files using pluggable STT with sentence embeddings"
    )
    parser.add_argument(
        "audio_path",
        nargs="?",
        type=str,
        help="Path to audio file in journal segment directory, e.g. HHMMSS_LEN/audio.flac",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        dest="all",
        help="Batch-transcribe all unprocessed audio segments in the journal",
    )
    parser.add_argument(
        "--redo",
        action="store_true",
        help="Reprocess file, overwriting existing outputs",
    )
    parser.add_argument(
        "--backend",
        type=str,
        choices=list(BACKEND_REGISTRY.keys()),
        help="STT backend to use (overrides config and resource-aware auto default)",
    )
    args = setup_cli(parser)
    require_solstone()

    if args.all and args.audio_path:
        parser.error("--all and audio_path are mutually exclusive")
    if not args.all and not args.audio_path:
        parser.error("provide audio_path or --all")

    config = get_config()
    transcribe_config = config.get("transcribe", {})
    default_backend = resolve_default_backend(args, transcribe_config)

    from solstone.think.entities import load_recent_entity_names

    entity_names = load_recent_entity_names(limit=ENTITY_NAMES_LIMIT)
    if entity_names:
        logging.info(f"Loaded {len(entity_names)} entities for transcription context")

    if args.all:
        processed = 0
        skipped = 0
        failed = 0
        deferred = 0

        for day_name, _day_path_str in sorted(day_dirs().items()):
            for _stream_name, _seg_key, seg_path in iter_segments(day_name):
                for audio_file in sorted(seg_path.iterdir()):
                    if audio_file.suffix.lower() not in SUPPORTED_AUDIO_FORMATS:
                        continue
                    jsonl_path = audio_file.with_suffix(".jsonl")
                    if jsonl_path.exists() and not args.redo:
                        logging.info(f"Skipping (already transcribed): {audio_file}")
                        skipped += 1
                        continue
                    try:
                        logging.info(f"Transcribing: {audio_file}")
                        _process_one(
                            audio_file,
                            args,
                            transcribe_config,
                            default_backend,
                            entity_names,
                        )
                        processed += 1
                    except SystemExit as exit_signal:
                        # A provider deferral is per-file, not per-batch: the audio is
                        # preserved for the next run and the batch moves on. SystemExit
                        # is a BaseException, so the `except Exception` below cannot
                        # see it -- without this, one deferred clip aborts everything.
                        if exit_signal.code != EXIT_PROVIDER_BLOCKED:
                            raise
                        logging.info("Deferred (provider not ready): %s", audio_file)
                        deferred += 1
                    except Exception:
                        logging.error(
                            f"Failed to transcribe {audio_file}", exc_info=True
                        )
                        failed += 1

        summary = f"{processed} processed, {skipped} skipped (already transcribed)"
        if deferred:
            summary += f", {deferred} deferred (provider not ready, will retry)"
        if failed:
            summary += f", {failed} failed"
        print(summary)
        return

    audio_path = Path(args.audio_path)
    if not audio_path.exists():
        if audio_path.is_absolute():
            journal_relative = Path(get_journal()) / audio_path.as_posix().lstrip("/")
        else:
            journal_relative = resolve_journal_path(get_journal(), args.audio_path)
        if journal_relative.exists():
            audio_path = journal_relative
        else:
            parser.error(
                f"Audio file not found.\n"
                f"  Tried absolute:         {audio_path}\n"
                f"  Tried journal-relative: {journal_relative}"
            )

    if audio_path.suffix.lower() not in SUPPORTED_AUDIO_FORMATS:
        parser.error(
            f"Unsupported audio format: {audio_path.suffix}. "
            f"Supported formats: {', '.join(sorted(SUPPORTED_AUDIO_FORMATS))}"
        )

    segment = get_segment_key(audio_path)
    if segment is None:
        parser.error(
            f"Audio file must be in a segment directory (HHMMSS_LEN/), "
            f"but parent is: {audio_path.parent.name}"
        )

    _process_one(audio_path, args, transcribe_config, default_backend, entity_names)


if __name__ == "__main__":
    main()

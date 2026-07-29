# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import argparse
import datetime as dt
import errno
import json
import os
import shutil
import subprocess
import sys
import tempfile
from importlib import resources
from pathlib import Path
from typing import Any

from solstone.apps.speakers.encoder_config import (
    OVERLAP_DETECTOR_SHA256,
    WESPEAKER_MODEL_SHA256,
)
from solstone.observe.transcribe.parakeet_hints import PACKAGED_COREML_HINT
from solstone.observe.utils import compute_file_sha256
from solstone.think import parakeet_readiness
from solstone.think.model_assets import (
    resolve_pyannote_segmentation_model,
    resolve_wespeaker_model,
)
from solstone.think.parakeet_readiness import (
    BACKEND,
    MODEL_VERSION,
    _cache_dir,
    _check_parakeet_ready,
    _load_sentinel,
    _platform_info,
    _sentinel_path,
    _sentinel_ready,
    _verify_mac_cache,
    _verify_variant_cache,
)
from solstone.think.provider_cache_seed import seed_provider_cache
from solstone.think.providers.fit_report import FitReport
from solstone.think.utils import is_packaged_install

JOURNAL_VARIANT_ENV = "JOURNAL_VARIANT"
HELPER_ENV_KEY = "SOLSTONE_PARAKEET_HELPER"
CED_DOWNLOAD_DISCLOSURE = (
    "ced assets: downloading ced.cpp v0.1.0 engine from github.com (MIT) "
    "and ced-tiny-q8_0 model from huggingface.co (Apache-2.0)"
)


def _now_utc() -> str:
    return (
        dt.datetime.now(dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _quarantine_suffix() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _detect_linux_variant() -> str:
    try:
        result = subprocess.run(
            ["nvidia-smi", "-L"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return "cpu"
    if result.returncode == 0 and result.stdout.strip():
        return "cuda"
    return "cpu"


def _resolve_variant(
    flag_value: str,
    env_value: str | None,
    os_name: str,
    arch: str,
) -> str | None:
    if flag_value in {"cpu", "cuda"}:
        if os_name != "linux":
            raise SystemExit(f"variant {flag_value!r} not supported on {os_name}")
        if flag_value == "cpu" and arch not in {"x86_64", "aarch64", "arm64"}:
            raise SystemExit(
                f"variant {flag_value!r} not supported on {os_name}/{arch}"
            )
        if flag_value == "cuda" and arch != "x86_64":
            raise SystemExit(
                f"variant {flag_value!r} not supported on {os_name}/{arch}"
            )
        return flag_value

    if flag_value == "coreml":
        if os_name != "darwin":
            raise SystemExit(f"variant 'coreml' not supported on {os_name}")
        if arch != "arm64":
            raise SystemExit(f"variant 'coreml' not supported on {os_name}/{arch}")
        return flag_value

    if os_name == "darwin" and arch == "arm64":
        return "coreml"
    if os_name == "linux" and arch in {"x86_64", "aarch64", "arm64"}:
        if env_value:
            if env_value not in {"cpu", "cuda"}:
                raise SystemExit(
                    f"invalid {JOURNAL_VARIANT_ENV}={env_value!r}; use 'cpu' or 'cuda'"
                )
            if env_value == "cuda" and arch != "x86_64":
                raise SystemExit(
                    f"invalid {JOURNAL_VARIANT_ENV}={env_value!r}; "
                    f"use 'cpu' on {os_name}/{arch}"
                )
            return env_value
        if arch != "x86_64":
            return "cpu"
        return _detect_linux_variant()
    return None


def _package_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _fixture_audio_path() -> Path:
    return Path(
        str(
            resources.files("solstone.observe.transcribe._fixtures")
            / "parakeet_sample.wav"
        )
    )


def _helper_path() -> Path:
    env_path = os.getenv(HELPER_ENV_KEY)
    if env_path:
        return Path(env_path).expanduser().resolve()
    base = _package_root() / "observe" / "transcribe" / "parakeet_helper"
    bundled = base / "_bin" / "parakeet-helper"
    if bundled.exists():
        return bundled
    return base / ".build" / "release" / "parakeet-helper"


def _write_sentinel(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", dir=path.parent, delete=False, encoding="utf-8"
    ) as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")
        tmp_path = Path(handle.name)
    tmp_path.replace(path)


def _remove_sentinel(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return


def _quarantine_path(cache_dir: Path) -> Path:
    base = cache_dir.with_name(f"{cache_dir.name}.partial-{_quarantine_suffix()}")
    candidate = base
    suffix = 1
    while candidate.exists():
        candidate = cache_dir.with_name(f"{base.name}-{suffix}")
        suffix += 1
    return candidate


def _fail(message: str, code: int = 1) -> int:
    print(message, file=sys.stderr)
    return code


def _fail_with_quarantine(message: str, cache_dir: Path, sentinel_path: Path) -> int:
    _remove_sentinel(sentinel_path)
    if cache_dir.exists():
        quarantine = _quarantine_path(cache_dir)
        cache_dir.rename(quarantine)
        print(
            f"{message}; quarantined partial cache to {quarantine}",
            file=sys.stderr,
        )
        print(f"reclaim space with: rm -rf {quarantine}", file=sys.stderr)
        return 1
    return _fail(message)


def _disk_full_message(cache_dir: Path) -> str:
    usage_root = cache_dir if cache_dir.exists() else cache_dir.parent
    free_bytes = shutil.disk_usage(usage_root).free
    return (
        f"parakeet install failed: disk full at {cache_dir} "
        f"(free {free_bytes} bytes); free space and retry"
    )


def _verify_bundled_assets() -> None:
    for asset_path, expected_sha256 in (
        (resolve_wespeaker_model(), WESPEAKER_MODEL_SHA256),
        (resolve_pyannote_segmentation_model(), OVERLAP_DETECTOR_SHA256),
    ):
        try:
            actual_sha256 = compute_file_sha256(asset_path)
        except OSError as exc:
            raise RuntimeError(
                f"bundled asset SHA mismatch: {asset_path}\n"
                f"  expected: {expected_sha256}\n"
                f"  actual:   unavailable ({exc})"
            ) from exc
        if actual_sha256 != expected_sha256:
            raise RuntimeError(
                f"bundled asset SHA mismatch: {asset_path}\n"
                f"  expected: {expected_sha256}\n"
                f"  actual:   {actual_sha256}"
            )


def _build_payload(
    os_name: str,
    arch: str,
    variant: str,
    cache_dir: Path,
    *,
    fluidaudio_version: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": 1,
        "backend": BACKEND,
        "platform": {"os": os_name, "arch": arch},
        "variant": variant,
        "model_version": MODEL_VERSION,
        "quantization": "fp32",
        "fetched_at": _now_utc(),
        "cache_dir": str(cache_dir),
    }
    if fluidaudio_version is not None:
        payload["fluidaudio_version"] = fluidaudio_version
    return payload


def _run_mac_helper(cache_dir: Path) -> dict[str, Any] | None:
    helper_env = os.getenv(HELPER_ENV_KEY)
    helper_path = _helper_path()
    if not helper_path.is_file() or not os.access(helper_path, os.X_OK):
        if not helper_env and is_packaged_install():
            print(PACKAGED_COREML_HINT, file=sys.stderr)
            return None
        raise RuntimeError(
            "parakeet install failed: helper not found or not executable at "
            f"{helper_path} run `make parakeet-helper` from the solstone repo to build it"
        )
    fixture_audio = _fixture_audio_path()
    if not fixture_audio.is_file():
        raise RuntimeError(
            f"parakeet install failed: fixture audio not found at {fixture_audio}"
        )

    try:
        result = subprocess.run(
            [
                str(helper_path),
                "--cache-dir",
                str(cache_dir),
                "--model",
                MODEL_VERSION,
                str(fixture_audio),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        if exc.errno == errno.ENOSPC:
            raise RuntimeError(_disk_full_message(cache_dir)) from exc
        raise

    if result.returncode != 0:
        stderr_text = (result.stderr or "").strip()
        try:
            stderr_payload = json.loads(stderr_text) if stderr_text else {}
        except json.JSONDecodeError:
            stderr_payload = {}
        message = (
            stderr_payload.get("message") or stderr_text or "unknown helper failure"
        )
        raise RuntimeError(f"parakeet install failed: {message}")

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"parakeet install failed: helper returned invalid JSON: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise RuntimeError("parakeet install failed: helper returned non-object JSON")
    return payload


def _check_linux_cpp_ready() -> dict[str, Path]:
    from solstone.think.providers import parakeet_install

    return parakeet_readiness.check_parakeet_cpp_files(
        parakeet_install.cache_root(),
        parakeet_install.parakeet_server_artifact_key(),
    )


def _install_rerank_model(*, check: bool, force: bool) -> int:
    from solstone.think.providers import rerank_install

    try:
        if check:
            rerank_install.check_rerank_model()
        else:
            rerank_install.install_rerank_model(force=force)
    except rerank_install.RerankInstallError as exc:
        return _fail(str(exc))
    return 0


def _install_ced_assets(*, check: bool, force: bool) -> int:
    from solstone.think.providers import ced_install

    os_name, arch = _platform_info()
    if ced_install.ced_engine_artifact_key(os_name, arch) is None:
        print(
            f"ced install: unsupported platform {os_name}/{arch}; "
            "skipping ced sound-tag assets"
        )
        return 0

    try:
        if check:
            record = ced_install.check_ced_assets()
            if record is not None:
                print(f"model ready: {ced_install.model_path()}")
            return 0

        if not force:
            try:
                record = ced_install.check_ced_assets()
            except ced_install.CedInstallError:
                record = None
            if record is not None:
                print(f"model ready: {ced_install.model_path()}")
                return 0

        print(CED_DOWNLOAD_DISCLOSURE)
        record = ced_install.install_ced_assets(force=force)
        if record is None:
            print(
                f"ced install: unsupported platform {os_name}/{arch}; "
                "skipping ced sound-tag assets"
            )
            return 0
    except ced_install.CedInstallError as exc:
        return _fail(str(exc))

    print(f"model ready: {ced_install.model_path()}")
    return 0


def _install_rfdetr_model(*, check: bool, force: bool) -> int:
    from solstone.think.providers import rfdetr_install

    try:
        if check:
            rfdetr_install.check_rfdetr_model()
        else:
            rfdetr_install.install_rfdetr(force=force)
    except rfdetr_install.RfdetrInstallError as exc:
        return _fail(str(exc))
    return 0


def _install_linux_cpp(*, force: bool = False) -> int:
    from solstone.think.providers import parakeet_install
    from solstone.think.providers.install_lease import acquire_install_lease
    from solstone.think.providers.install_state import (
        IN_FLIGHT_STATES,
        begin_or_replace_install_attempt,
        canonical_fingerprint,
        fingerprint_sha256,
        observe_install_attempt,
        read_install_status,
    )

    def progress_line(status: dict[str, Any]) -> None:
        received = status.get("progress_bytes_received")
        total = status.get("progress_bytes_total")
        suffix = ""
        if received is not None:
            suffix = f" {received}"
            if total is not None:
                suffix += f"/{total}"
        print(f"observing parakeet install: {status['install_state']}{suffix}")

    try:
        fingerprint = parakeet_install.target_fingerprint()
        target_sha = fingerprint_sha256(canonical_fingerprint(fingerprint))
        lease = acquire_install_lease("parakeet")
        if lease is None:
            status = read_install_status(name="parakeet")
            if (
                status["install_state"] not in IN_FLIGHT_STATES
                or status["target_fingerprint_sha256"] != target_sha
            ):
                return _fail("parakeet install already running for a different target")
            final = observe_install_attempt(
                "parakeet",
                target_fingerprint_sha256=target_sha,
                timeout_s=60.0 * 60.0,
                progress=progress_line,
            )
            if final is None:
                return _fail("timed out observing parakeet install")
            if final["install_state"] != "installed":
                return _fail(final.get("install_error") or "parakeet install failed")
        else:
            try:
                attempt_status = begin_or_replace_install_attempt(
                    "parakeet",
                    fingerprint,
                    initial_state="resolving",
                    owner={"entry": "install_models"},
                )
                parakeet_install.install_parakeet(
                    force=force,
                    lease=lease,
                    attempt_status=attempt_status,
                )
            finally:
                lease.release()
        paths = _check_linux_cpp_ready()
    except Exception as exc:
        return _fail(f"parakeet install failed: {exc}")
    print(f"model ready: {paths['model']}")
    return 0


def _install_models(
    os_name: str,
    arch: str,
    variant: str,
    *,
    force: bool = False,
) -> int:
    if variant in {"cpu", "cuda"}:
        return _install_linux_cpp(force=force)

    sentinel_path = _sentinel_path(variant)
    cache_dir = _cache_dir(variant)
    _remove_sentinel(sentinel_path)
    try:
        payload = _run_mac_helper(cache_dir)
    except RuntimeError as exc:
        return _fail_with_quarantine(str(exc), cache_dir, sentinel_path)
    except Exception as exc:
        return _fail_with_quarantine(
            f"parakeet install failed: {exc}",
            cache_dir,
            sentinel_path,
        )
    if payload is None:
        return 0
    if not _verify_mac_cache(cache_dir):
        return _fail_with_quarantine(
            "parakeet install failed: macOS cache verification failed",
            cache_dir,
            sentinel_path,
        )
    fluidaudio_version = payload.get("fluidaudio_version")
    if not isinstance(fluidaudio_version, str) or not fluidaudio_version:
        return _fail_with_quarantine(
            "parakeet install failed: helper success JSON missing fluidaudio_version",
            cache_dir,
            sentinel_path,
        )
    _write_sentinel(
        sentinel_path,
        _build_payload(
            os_name,
            arch,
            variant,
            cache_dir,
            fluidaudio_version=fluidaudio_version,
        ),
    )
    print(f"model ready: {cache_dir}")
    return 0


def _render_and_gate_fit_report(report: FitReport) -> int | None:
    from solstone.think.providers import fit_report

    rendered = fit_report.render_fit_report(report)
    if report.overall == "blocked":
        return _fail(rendered)
    print(rendered, file=sys.stderr)
    return None


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Install and verify solstone's bundled ML models (local STT plus "
            "bundled wespeaker/pyannote assets). Default action checks the "
            "local STT artifacts and fetches if missing; --force re-fetches; "
            "--check verifies only and exits nonzero on any problem."
        )
    )
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--check",
        action="store_true",
        help="Verify bundled assets and local STT artifacts without fetching.",
    )
    mode_group.add_argument(
        "--force",
        action="store_true",
        help="Ignore readiness and refetch/verify local STT artifacts.",
    )
    parser.add_argument(
        "--variant",
        choices=("auto", "cpu", "cuda", "coreml"),
        default="auto",
        help=(
            "Journal variant to install or verify. auto honors "
            "JOURNAL_VARIANT on linux/x86_64, then autodetects."
        ),
    )
    args = parser.parse_args()

    os_name, arch = _platform_info()
    variant = _resolve_variant(
        args.variant,
        os.getenv(JOURNAL_VARIANT_ENV),
        os_name,
        arch,
    )

    if not args.check and not args.force:
        seed_provider_cache()

    try:
        # Why: local asset corruption makes downloading parakeet pointless.
        _verify_bundled_assets()
    except RuntimeError as exc:
        return _fail(str(exc))

    result = _install_rerank_model(check=args.check, force=args.force)
    if result != 0:
        return result

    result = _install_ced_assets(check=args.check, force=args.force)
    if result != 0:
        return result

    result = _install_rfdetr_model(check=args.check, force=args.force)
    if result != 0:
        return result

    if variant is None:
        print(
            "parakeet install: unsupported platform "
            f"{os_name}/{arch}; supported: darwin/arm64, linux/x86_64"
        )
        return 0

    if variant in {"cpu", "cuda"}:
        if args.check:
            try:
                paths = _check_linux_cpp_ready()
            except RuntimeError as exc:
                return _fail(str(exc))
            print(f"model ready: {paths['model']}")
            return 0
        if not args.force:
            try:
                paths = _check_linux_cpp_ready()
            except RuntimeError:
                pass
            else:
                print(f"model ready: {paths['model']}")
                return 0
        from solstone.think.providers import fit_report

        gate_result = _render_and_gate_fit_report(
            fit_report.build_parakeet_fit_report()
        )
        if gate_result is not None:
            return gate_result
        return _install_models(os_name, arch, variant, force=args.force)

    sentinel_path = _sentinel_path(variant)
    if args.check:
        try:
            ready_cache = _check_parakeet_ready(
                os_name,
                arch,
                variant,
                sentinel_path,
            )
        except RuntimeError as exc:
            return _fail(str(exc))
        print(f"model ready: {ready_cache}")
        return 0

    if not args.force:
        ready_cache = _sentinel_ready(
            _load_sentinel(sentinel_path),
            os_name,
            arch,
            variant,
        )
        if ready_cache is not None and _verify_variant_cache(variant, ready_cache):
            print(f"model ready: {ready_cache}")
            return 0

    from solstone.think.providers import fit_report

    gate_result = _render_and_gate_fit_report(
        fit_report.build_coreml_parakeet_fit_report(
            os_name,
            arch,
            _cache_dir(variant),
        )
    )
    if gate_result is not None:
        return gate_result
    return _install_models(os_name, arch, variant, force=args.force)


if __name__ == "__main__":
    raise SystemExit(main())

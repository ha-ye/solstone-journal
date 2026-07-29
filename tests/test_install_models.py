# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

from solstone.think import install_models, parakeet_readiness
from solstone.think.providers import fit_report


@pytest.fixture(autouse=True)
def _skip_provider_cache_seed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(install_models, "seed_provider_cache", lambda: None)


def _fit(severity: fit_report.FitSeverity) -> fit_report.FitReport:
    return fit_report.FitReport(
        artifact="test install models",
        checks=(fit_report.FitCheck("test", severity, f"{severity} detail"),),
    )


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_model_files(base_dir: Path, relative_paths: tuple[str, ...]) -> None:
    for relative_path in relative_paths:
        target = base_dir / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"ok")


@pytest.mark.parametrize(
    ("flag_value", "env_value", "os_name", "arch", "expected"),
    [
        ("cpu", None, "linux", "x86_64", "cpu"),
        ("cpu", "cuda", "linux", "x86_64", "cpu"),
        ("auto", "cpu", "linux", "x86_64", "cpu"),
        ("auto", "cuda", "linux", "x86_64", "cuda"),
        ("auto", None, "linux", "aarch64", "cpu"),
        ("cpu", None, "linux", "aarch64", "cpu"),
        ("auto", None, "darwin", "arm64", "coreml"),
        ("auto", None, "windows", "amd64", None),
    ],
)
def test_resolve_variant_precedence(
    monkeypatch: pytest.MonkeyPatch,
    flag_value: str,
    env_value: str | None,
    os_name: str,
    arch: str,
    expected: str | None,
):
    monkeypatch.setattr(install_models, "_detect_linux_variant", lambda: "cpu")

    assert (
        install_models._resolve_variant(flag_value, env_value, os_name, arch)
        == expected
    )


def test_resolve_variant_autodetects_linux_gpu(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(install_models, "_detect_linux_variant", lambda: "cuda")

    assert install_models._resolve_variant("auto", None, "linux", "x86_64") == "cuda"


def test_resolve_variant_rejects_invalid_env_value():
    with pytest.raises(SystemExit, match="invalid JOURNAL_VARIANT='bogus'"):
        install_models._resolve_variant("auto", "bogus", "linux", "x86_64")


def test_resolve_variant_rejects_incompatible_explicit_variant():
    with pytest.raises(SystemExit, match="variant 'coreml' not supported on linux"):
        install_models._resolve_variant("coreml", None, "linux", "x86_64")
    with pytest.raises(SystemExit, match="variant 'cpu' not supported on darwin"):
        install_models._resolve_variant("cpu", None, "darwin", "arm64")
    with pytest.raises(SystemExit, match="variant 'cuda' not supported"):
        install_models._resolve_variant("cuda", None, "linux", "aarch64")


def test_verify_bundled_assets_returns_when_hashes_match(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    wespeaker = tmp_path / "wespeaker.onnx"
    pyannote = tmp_path / "pyannote.onnx"
    wespeaker.write_bytes(b"wespeaker")
    pyannote.write_bytes(b"pyannote")
    monkeypatch.setattr(install_models, "resolve_wespeaker_model", lambda: wespeaker)
    monkeypatch.setattr(install_models, "WESPEAKER_MODEL_SHA256", _sha256(b"wespeaker"))
    monkeypatch.setattr(
        install_models, "resolve_pyannote_segmentation_model", lambda: pyannote
    )
    monkeypatch.setattr(
        install_models,
        "OVERLAP_DETECTOR_SHA256",
        _sha256(b"pyannote"),
    )

    install_models._verify_bundled_assets()


def test_verify_bundled_assets_reports_mutated_asset(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    wespeaker = tmp_path / "wespeaker.onnx"
    pyannote = tmp_path / "pyannote.onnx"
    wespeaker.write_bytes(b"mutated")
    pyannote.write_bytes(b"pyannote")
    expected = _sha256(b"original")
    actual = _sha256(b"mutated")
    monkeypatch.setattr(install_models, "resolve_wespeaker_model", lambda: wespeaker)
    monkeypatch.setattr(install_models, "WESPEAKER_MODEL_SHA256", expected)
    monkeypatch.setattr(
        install_models, "resolve_pyannote_segmentation_model", lambda: pyannote
    )
    monkeypatch.setattr(
        install_models,
        "OVERLAP_DETECTOR_SHA256",
        _sha256(b"pyannote"),
    )

    with pytest.raises(RuntimeError) as exc_info:
        install_models._verify_bundled_assets()

    message = str(exc_info.value)
    assert f"bundled asset SHA mismatch: {wespeaker}" in message
    assert f"expected: {expected}" in message
    assert f"actual:   {actual}" in message


def test_verify_returns_true_when_files_at_fluidaudio_sibling(tmp_path: Path):
    cache_dir = tmp_path / "models"
    repo_dir = tmp_path / parakeet_readiness.MAC_FLUIDAUDIO_REPO_NAME
    _write_model_files(repo_dir, parakeet_readiness.MAC_MODEL_FILES)

    assert parakeet_readiness._verify_mac_cache(cache_dir) is True


def test_verify_returns_false_when_sibling_empty(tmp_path: Path):
    cache_dir = tmp_path / "models"
    cache_dir.mkdir()
    (tmp_path / parakeet_readiness.MAC_FLUIDAUDIO_REPO_NAME).mkdir()

    assert parakeet_readiness._verify_mac_cache(cache_dir) is False


def test_verify_returns_false_when_files_at_literal_path(tmp_path: Path):
    cache_dir = tmp_path / "models"
    _write_model_files(cache_dir, parakeet_readiness.MAC_MODEL_FILES)

    assert parakeet_readiness._verify_mac_cache(cache_dir) is False


def test_helper_path_env_override_wins(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    fake = tmp_path / "custom" / "parakeet-helper"
    monkeypatch.setenv(install_models.HELPER_ENV_KEY, str(fake))
    monkeypatch.setattr(install_models, "_package_root", lambda: tmp_path)
    assert install_models._helper_path() == fake.expanduser().resolve()


def test_helper_path_prefers_bundled_bin(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    monkeypatch.delenv(install_models.HELPER_ENV_KEY, raising=False)
    monkeypatch.setattr(install_models, "_package_root", lambda: tmp_path)
    bundled = (
        tmp_path
        / "observe"
        / "transcribe"
        / "parakeet_helper"
        / "_bin"
        / "parakeet-helper"
    )
    bundled.parent.mkdir(parents=True)
    bundled.write_text("")
    assert install_models._helper_path() == bundled


def test_helper_path_falls_back_to_swift_build(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    monkeypatch.delenv(install_models.HELPER_ENV_KEY, raising=False)
    monkeypatch.setattr(install_models, "_package_root", lambda: tmp_path)
    expected = (
        tmp_path
        / "observe"
        / "transcribe"
        / "parakeet_helper"
        / ".build"
        / "release"
        / "parakeet-helper"
    )
    assert install_models._helper_path() == expected


def _ready_paths(tmp_path: Path) -> dict[str, Path]:
    return {
        "binary_cpu": tmp_path / "bin" / "cpu" / "parakeet-server",
        "binary_vulkan": tmp_path / "bin" / "vulkan" / "parakeet-server",
        "model": tmp_path / "models" / "model.gguf",
    }


def _prepare_check_main(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["sol install-models", "--check"])
    monkeypatch.delenv(install_models.JOURNAL_VARIANT_ENV, raising=False)
    monkeypatch.setattr(install_models, "_platform_info", lambda: ("linux", "x86_64"))
    monkeypatch.setattr(install_models, "_detect_linux_variant", lambda: "cpu")
    monkeypatch.setattr(install_models, "_verify_bundled_assets", lambda: None)
    monkeypatch.setattr(
        install_models, "_install_rerank_model", lambda *, check, force: 0
    )
    monkeypatch.setattr(
        install_models, "_install_ced_assets", lambda *, check, force: 0
    )
    monkeypatch.setattr(
        install_models, "_install_rfdetr_model", lambda *, check, force: 0
    )


def test_main_check_missing_cpp_artifacts_returns_nonzero(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    _prepare_check_main(monkeypatch)
    monkeypatch.setattr(
        install_models,
        "_check_linux_cpp_ready",
        lambda: (_ for _ in ()).throw(RuntimeError("model missing")),
    )

    assert install_models.main() == 1
    assert "model missing" in capsys.readouterr().err


def test_main_check_ready_cpp_artifacts_returns_zero(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    paths = _ready_paths(tmp_path)
    _prepare_check_main(monkeypatch)
    monkeypatch.setattr(install_models, "_check_linux_cpp_ready", lambda: paths)

    assert install_models.main() == 0
    assert f"model ready: {paths['model']}" in capsys.readouterr().out


def test_main_rerank_failure_short_circuits_before_parakeet(
    monkeypatch: pytest.MonkeyPatch,
):
    calls = []
    monkeypatch.setattr(sys, "argv", ["sol install-models", "--check"])
    monkeypatch.delenv(install_models.JOURNAL_VARIANT_ENV, raising=False)
    monkeypatch.setattr(install_models, "_platform_info", lambda: ("linux", "x86_64"))
    monkeypatch.setattr(install_models, "_detect_linux_variant", lambda: "cpu")
    monkeypatch.setattr(install_models, "_verify_bundled_assets", lambda: None)
    monkeypatch.setattr(
        install_models,
        "_install_rerank_model",
        lambda *, check, force: calls.append((check, force)) or 7,
    )
    monkeypatch.setattr(
        install_models,
        "_install_ced_assets",
        lambda *, check, force: pytest.fail("ced install should not start"),
    )
    monkeypatch.setattr(
        install_models,
        "_install_rfdetr_model",
        lambda *, check, force: pytest.fail("rf-detr check should not start"),
    )
    monkeypatch.setattr(
        install_models,
        "_check_linux_cpp_ready",
        lambda: pytest.fail("parakeet check should not start"),
    )
    monkeypatch.setattr(
        install_models,
        "_install_models",
        lambda *_args, **_kwargs: pytest.fail("parakeet install should not start"),
    )

    assert install_models.main() == 7
    assert calls == [(True, False)]


def test_main_runs_ced_after_rerank_before_parakeet(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    calls = []
    paths = _ready_paths(tmp_path)
    monkeypatch.setattr(sys, "argv", ["sol install-models", "--check"])
    monkeypatch.delenv(install_models.JOURNAL_VARIANT_ENV, raising=False)
    monkeypatch.setattr(install_models, "_platform_info", lambda: ("linux", "x86_64"))
    monkeypatch.setattr(install_models, "_detect_linux_variant", lambda: "cpu")
    monkeypatch.setattr(install_models, "_verify_bundled_assets", lambda: None)
    monkeypatch.setattr(
        install_models,
        "_install_rerank_model",
        lambda *, check, force: calls.append(("rerank", check, force)) or 0,
    )
    monkeypatch.setattr(
        install_models,
        "_install_ced_assets",
        lambda *, check, force: calls.append(("ced", check, force)) or 0,
    )
    monkeypatch.setattr(
        install_models,
        "_check_linux_cpp_ready",
        lambda: calls.append(("parakeet",)) or paths,
    )
    monkeypatch.setattr(
        install_models, "_install_rfdetr_model", lambda *, check, force: 0
    )

    assert install_models.main() == 0
    assert calls == [
        ("rerank", True, False),
        ("ced", True, False),
        ("parakeet",),
    ]


def test_main_ced_failure_short_circuits_before_parakeet(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(sys, "argv", ["sol install-models", "--check"])
    monkeypatch.delenv(install_models.JOURNAL_VARIANT_ENV, raising=False)
    monkeypatch.setattr(install_models, "_platform_info", lambda: ("linux", "x86_64"))
    monkeypatch.setattr(install_models, "_detect_linux_variant", lambda: "cpu")
    monkeypatch.setattr(install_models, "_verify_bundled_assets", lambda: None)
    monkeypatch.setattr(
        install_models, "_install_rerank_model", lambda *, check, force: 0
    )
    monkeypatch.setattr(
        install_models, "_install_ced_assets", lambda *, check, force: 8
    )
    monkeypatch.setattr(
        install_models,
        "_check_linux_cpp_ready",
        lambda: pytest.fail("parakeet check should not start"),
    )

    assert install_models.main() == 8


def test_install_ced_assets_unsupported_platform_prints_skip(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    from solstone.think.providers import ced_install

    monkeypatch.setattr(install_models, "_platform_info", lambda: ("windows", "amd64"))
    monkeypatch.setattr(
        ced_install, "ced_engine_artifact_key", lambda os_name=None, arch=None: None
    )
    monkeypatch.setattr(
        ced_install,
        "install_ced_assets",
        lambda **_kwargs: pytest.fail("ced install should not start"),
    )

    assert install_models._install_ced_assets(check=False, force=False) == 0
    assert (
        "ced install: unsupported platform windows/amd64; skipping ced sound-tag assets"
    ) in capsys.readouterr().out


def test_install_ced_assets_threads_check_and_force(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    from solstone.think.providers import ced_install

    calls = []
    record = object()
    monkeypatch.setattr(install_models, "_platform_info", lambda: ("linux", "x86_64"))
    monkeypatch.setattr(
        ced_install,
        "ced_engine_artifact_key",
        lambda os_name=None, arch=None: "linux-cpu-x64",
    )
    monkeypatch.setattr(ced_install, "model_path", lambda: tmp_path / "ced.gguf")
    monkeypatch.setattr(
        ced_install, "check_ced_assets", lambda: calls.append(("check",)) or record
    )
    monkeypatch.setattr(
        ced_install,
        "install_ced_assets",
        lambda *, force: calls.append(("install", force)) or record,
    )

    assert install_models._install_ced_assets(check=True, force=False) == 0
    assert f"model ready: {tmp_path / 'ced.gguf'}" in capsys.readouterr().out

    assert install_models._install_ced_assets(check=False, force=True) == 0
    stdout = capsys.readouterr().out
    assert install_models.CED_DOWNLOAD_DISCLOSURE in stdout
    assert f"model ready: {tmp_path / 'ced.gguf'}" in stdout
    assert calls == [("check",), ("install", True)]


def test_install_ced_assets_downloads_when_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    from solstone.think.providers import ced_install

    calls = []
    record = object()
    monkeypatch.setattr(install_models, "_platform_info", lambda: ("linux", "x86_64"))
    monkeypatch.setattr(
        ced_install,
        "ced_engine_artifact_key",
        lambda os_name=None, arch=None: "linux-cpu-x64",
    )
    monkeypatch.setattr(ced_install, "model_path", lambda: tmp_path / "ced.gguf")

    def missing_check():
        calls.append(("check",))
        raise ced_install.CedInstallError("sidecar_missing", "missing")

    monkeypatch.setattr(ced_install, "check_ced_assets", missing_check)
    monkeypatch.setattr(
        ced_install,
        "install_ced_assets",
        lambda *, force: calls.append(("install", force)) or record,
    )

    assert install_models._install_ced_assets(check=False, force=False) == 0
    stdout = capsys.readouterr().out
    assert install_models.CED_DOWNLOAD_DISCLOSURE in stdout
    assert f"model ready: {tmp_path / 'ced.gguf'}" in stdout
    assert calls == [("check",), ("install", False)]


def test_main_rfdetr_failure_short_circuits_before_parakeet(
    monkeypatch: pytest.MonkeyPatch,
):
    calls = []
    monkeypatch.setattr(sys, "argv", ["sol install-models", "--check"])
    monkeypatch.delenv(install_models.JOURNAL_VARIANT_ENV, raising=False)
    monkeypatch.setattr(install_models, "_platform_info", lambda: ("linux", "x86_64"))
    monkeypatch.setattr(install_models, "_detect_linux_variant", lambda: "cpu")
    monkeypatch.setattr(install_models, "_verify_bundled_assets", lambda: None)
    monkeypatch.setattr(
        install_models, "_install_rerank_model", lambda *, check, force: 0
    )
    monkeypatch.setattr(
        install_models, "_install_ced_assets", lambda *, check, force: 0
    )
    monkeypatch.setattr(
        install_models,
        "_install_rfdetr_model",
        lambda *, check, force: calls.append((check, force)) or 7,
    )
    monkeypatch.setattr(
        install_models,
        "_check_linux_cpp_ready",
        lambda: pytest.fail("parakeet check should not start"),
    )
    monkeypatch.setattr(
        install_models,
        "_install_models",
        lambda *_args, **_kwargs: pytest.fail("parakeet install should not start"),
    )

    assert install_models.main() == 7
    assert calls == [(True, False)]


def test_main_rfdetr_success_continues_to_parakeet(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    calls = []
    paths = _ready_paths(tmp_path)
    monkeypatch.setattr(sys, "argv", ["sol install-models", "--check"])
    monkeypatch.delenv(install_models.JOURNAL_VARIANT_ENV, raising=False)
    monkeypatch.setattr(install_models, "_platform_info", lambda: ("linux", "x86_64"))
    monkeypatch.setattr(install_models, "_detect_linux_variant", lambda: "cpu")
    monkeypatch.setattr(install_models, "_verify_bundled_assets", lambda: None)
    monkeypatch.setattr(
        install_models,
        "_install_rerank_model",
        lambda *, check, force: calls.append(("rerank", check, force)) or 0,
    )
    monkeypatch.setattr(
        install_models, "_install_ced_assets", lambda *, check, force: 0
    )
    monkeypatch.setattr(
        install_models,
        "_install_rfdetr_model",
        lambda *, check, force: calls.append(("rfdetr", check, force)) or 0,
    )

    def ready_paths() -> dict[str, Path]:
        calls.append(("parakeet", True, False))
        return paths

    monkeypatch.setattr(install_models, "_check_linux_cpp_ready", ready_paths)

    assert install_models.main() == 0
    assert calls == [
        ("rerank", True, False),
        ("rfdetr", True, False),
        ("parakeet", True, False),
    ]


def test_run_mac_helper_soft_fails_on_packaged_install(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    cache_dir = tmp_path / "cache"
    sentinel_path = tmp_path / "sentinel.json"
    missing_helper = tmp_path / "missing" / "parakeet-helper"

    monkeypatch.delenv(install_models.HELPER_ENV_KEY, raising=False)
    monkeypatch.setattr(install_models, "_helper_path", lambda: missing_helper)
    monkeypatch.setattr(install_models, "is_packaged_install", lambda: True)
    monkeypatch.setattr(install_models, "_sentinel_path", lambda variant: sentinel_path)
    monkeypatch.setattr(install_models, "_cache_dir", lambda variant: cache_dir)

    assert install_models._run_mac_helper(cache_dir) is None
    stderr = capsys.readouterr().err
    assert "Apple Silicon Macs running macOS 14" in stderr
    assert "Intel Mac" in stderr
    assert "source checkout" in stderr

    assert install_models._install_models("darwin", "arm64", "coreml") == 0


def test_install_models_linux_routes_through_parakeet_provider(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    from solstone.think.providers import parakeet_install

    calls = []
    paths = _ready_paths(tmp_path)
    journal = tmp_path / "journal"
    (journal / "config").mkdir(parents=True)
    (journal / "config" / "journal.json").write_text(
        '{"providers": {}}\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))
    import solstone.think.utils as think_utils

    think_utils._journal_path_cache = None

    monkeypatch.setattr(
        parakeet_install,
        "install_parakeet",
        lambda **_kwargs: calls.append("install"),
    )
    monkeypatch.setattr(install_models, "_check_linux_cpp_ready", lambda: paths)

    assert install_models._install_models("linux", "x86_64", "cpu") == 0
    assert calls == ["install"]
    assert f"model ready: {paths['model']}" in capsys.readouterr().out


def test_main_force_reinstalls_linux_cpp(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    calls = []
    monkeypatch.setattr(sys, "argv", ["sol install-models", "--force"])
    monkeypatch.setattr(install_models, "_platform_info", lambda: ("linux", "x86_64"))
    monkeypatch.setattr(install_models, "_detect_linux_variant", lambda: "cpu")
    monkeypatch.setattr(install_models, "_verify_bundled_assets", lambda: None)
    monkeypatch.setattr(
        install_models, "_install_rerank_model", lambda *, check, force: 0
    )
    monkeypatch.setattr(
        install_models, "_install_ced_assets", lambda *, check, force: 0
    )
    monkeypatch.setattr(
        install_models, "_install_rfdetr_model", lambda *, check, force: 0
    )
    monkeypatch.setattr(
        install_models, "_check_linux_cpp_ready", lambda: _ready_paths(tmp_path)
    )
    monkeypatch.setattr(fit_report, "build_parakeet_fit_report", lambda: _fit("ok"))
    monkeypatch.setattr(
        install_models,
        "_install_models",
        lambda os_name, arch, variant, **kwargs: (
            calls.append((os_name, arch, variant, kwargs)) or 0
        ),
    )

    assert install_models.main() == 0
    assert calls == [("linux", "x86_64", "cpu", {"force": True})]


def test_main_linux_blocks_before_install_models(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    monkeypatch.setattr(sys, "argv", ["sol install-models", "--force"])
    monkeypatch.setattr(install_models, "_platform_info", lambda: ("linux", "x86_64"))
    monkeypatch.setattr(install_models, "_detect_linux_variant", lambda: "cpu")
    monkeypatch.setattr(install_models, "_verify_bundled_assets", lambda: None)
    monkeypatch.setattr(
        install_models, "_install_rerank_model", lambda *, check, force: 0
    )
    monkeypatch.setattr(
        install_models, "_install_ced_assets", lambda *, check, force: 0
    )
    monkeypatch.setattr(
        install_models, "_install_rfdetr_model", lambda *, check, force: 0
    )
    monkeypatch.setattr(
        fit_report, "build_parakeet_fit_report", lambda: _fit("blocked")
    )
    monkeypatch.setattr(
        install_models,
        "_install_models",
        lambda *_args, **_kwargs: pytest.fail("install should not start"),
    )

    assert install_models.main() == 1
    assert "blocked detail" in capsys.readouterr().err


def test_main_linux_warning_continues_to_install_models(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    calls = []
    monkeypatch.setattr(sys, "argv", ["sol install-models", "--force"])
    monkeypatch.setattr(install_models, "_platform_info", lambda: ("linux", "x86_64"))
    monkeypatch.setattr(install_models, "_detect_linux_variant", lambda: "cpu")
    monkeypatch.setattr(install_models, "_verify_bundled_assets", lambda: None)
    monkeypatch.setattr(
        install_models, "_install_rerank_model", lambda *, check, force: 0
    )
    monkeypatch.setattr(
        install_models, "_install_ced_assets", lambda *, check, force: 0
    )
    monkeypatch.setattr(
        install_models, "_install_rfdetr_model", lambda *, check, force: 0
    )
    monkeypatch.setattr(
        fit_report, "build_parakeet_fit_report", lambda: _fit("warning")
    )
    monkeypatch.setattr(
        install_models,
        "_install_models",
        lambda os_name, arch, variant, **kwargs: (
            calls.append((os_name, arch, variant, kwargs)) or 0
        ),
    )

    assert install_models.main() == 0
    assert calls == [("linux", "x86_64", "cpu", {"force": True})]
    assert "warning detail" in capsys.readouterr().err


def test_main_coreml_blocks_before_install_models(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    monkeypatch.setattr(sys, "argv", ["sol install-models", "--force"])
    monkeypatch.setattr(install_models, "_platform_info", lambda: ("darwin", "arm64"))
    monkeypatch.setattr(install_models, "_verify_bundled_assets", lambda: None)
    monkeypatch.setattr(
        install_models, "_install_rerank_model", lambda *, check, force: 0
    )
    monkeypatch.setattr(
        install_models, "_install_ced_assets", lambda *, check, force: 0
    )
    monkeypatch.setattr(
        install_models, "_install_rfdetr_model", lambda *, check, force: 0
    )
    monkeypatch.setattr(
        fit_report,
        "build_coreml_parakeet_fit_report",
        lambda os_name, arch, cache_dir: _fit("blocked"),
    )
    monkeypatch.setattr(
        install_models,
        "_install_models",
        lambda *_args, **_kwargs: pytest.fail("install should not start"),
    )

    assert install_models.main() == 1
    assert "blocked detail" in capsys.readouterr().err


def test_main_skips_install_when_linux_cpp_ready(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    monkeypatch.setattr(sys, "argv", ["sol install-models"])
    monkeypatch.setattr(install_models, "_platform_info", lambda: ("linux", "x86_64"))
    monkeypatch.setattr(install_models, "_detect_linux_variant", lambda: "cpu")
    monkeypatch.setattr(install_models, "_verify_bundled_assets", lambda: None)
    monkeypatch.setattr(
        install_models, "_install_rerank_model", lambda *, check, force: 0
    )
    monkeypatch.setattr(
        install_models, "_install_ced_assets", lambda *, check, force: 0
    )
    monkeypatch.setattr(
        install_models, "_install_rfdetr_model", lambda *, check, force: 0
    )
    monkeypatch.setattr(
        install_models, "_check_linux_cpp_ready", lambda: _ready_paths(tmp_path)
    )
    monkeypatch.setattr(
        install_models,
        "_install_models",
        lambda *_args: pytest.fail("ready artifacts should not reinstall"),
    )

    assert install_models.main() == 0

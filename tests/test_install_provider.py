# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
import sys

import pytest

from solstone.think import install_provider
from solstone.think.providers import fit_report
from solstone.think.providers.artifact_proof import ReadinessOutcome


def _fit(severity: fit_report.FitSeverity) -> fit_report.FitReport:
    return fit_report.FitReport(
        artifact="test provider",
        checks=(fit_report.FitCheck("test", severity, f"{severity} detail"),),
    )


class _FakeLease:
    def __init__(self) -> None:
        self.released = False

    def release(self) -> None:
        self.released = True


def _readiness(provider: str, *, ready: bool) -> ReadinessOutcome:
    return ReadinessOutcome(
        provider=provider,
        status="ready" if ready else "missing-or-mismatched",
        reason_code="ready" if ready else "manifest_missing",
        target={"model_id": "model"},
        install={
            "install_state": "idle",
            "install_error": None,
            "error_code": None,
            "attempt_id": None,
            "progress_bytes_received": None,
            "progress_bytes_total": None,
            "last_transition_at": None,
            "last_progress_at": None,
        },
        host={},
        artifacts={
            "binary_installed": ready,
            "model_installed": ready,
        },
        proof={
            "binary": {
                "status": "ready" if ready else "missing-or-mismatched",
                "reason_code": "ready" if ready else "manifest_missing",
                "cache_hit": False,
            },
            "model": {
                "status": "ready" if ready else "missing-or-mismatched",
                "reason_code": "ready" if ready else "manifest_missing",
                "cache_hit": False,
            },
        },
    )


def _patch_lease_and_attempt(
    monkeypatch: pytest.MonkeyPatch, provider: str
) -> _FakeLease:
    lease = _FakeLease()
    monkeypatch.setattr(
        install_provider,
        "acquire_install_lease",
        lambda name: lease if name == provider else None,
    )
    monkeypatch.setattr(
        install_provider,
        "begin_or_replace_install_attempt",
        lambda name, fingerprint, **_kwargs: {
            "provider": name,
            "install_state": "resolving",
            "attempt_id": "attempt",
            "target_fingerprint_sha256": install_provider._target_sha(fingerprint),
        },
    )
    return lease


@pytest.fixture(autouse=True)
def local_backend_is_llama(monkeypatch):
    monkeypatch.setattr(install_provider, "_is_mlx_backend", lambda: False)


def test_install_provider_local_prints_install_status(monkeypatch, capsys):
    calls = []
    parakeet_calls = []

    def install_local(**_kwargs):
        calls.append(True)
        return {"provider": "local", "install_state": "installed"}

    def install_parakeet(**_kwargs):
        parakeet_calls.append(True)
        return {"provider": "parakeet", "install_state": "installed"}

    monkeypatch.setattr(sys, "argv", ["journal install-provider", "local"])
    monkeypatch.setattr(
        install_provider.local_install,
        "inspect_readiness",
        lambda: _readiness("local", ready=False),
    )
    lease = _patch_lease_and_attempt(monkeypatch, "local")
    monkeypatch.setattr(fit_report, "build_local_fit_report", lambda _model: _fit("ok"))
    monkeypatch.setattr(install_provider.local_install, "install_local", install_local)
    monkeypatch.setattr(
        install_provider.parakeet_install,
        "install_parakeet",
        install_parakeet,
    )

    assert install_provider.main() == 0

    assert calls == [True]
    assert parakeet_calls == []
    assert lease.released is True
    captured = capsys.readouterr()
    assert install_provider.LOCAL_DOWNLOAD_DISCLOSURE in captured.err
    assert json.loads(captured.out) == {
        "provider": "local",
        "install_state": "installed",
    }


def test_install_provider_local_uses_mlx_on_apple_silicon(monkeypatch, capsys):
    calls = []
    fingerprint = {
        "provider": "local",
        "runtime": "mlx",
        "model_pin": {"model_id": "qwen3.5:9b"},
    }

    def install_local_mlx(model_id, **_kwargs):
        calls.append(model_id)
        return {"provider": "local", "install_state": "installed"}

    monkeypatch.setattr(sys, "argv", ["journal install-provider", "local"])
    monkeypatch.setattr(install_provider, "_is_mlx_backend", lambda: True)
    monkeypatch.setattr(
        install_provider.mlx_install,
        "inspect_readiness",
        lambda _model: _readiness("local", ready=False),
    )
    monkeypatch.setattr(
        install_provider.mlx_install,
        "target_fingerprint",
        lambda _model: fingerprint,
    )
    lease = _patch_lease_and_attempt(monkeypatch, "local")
    monkeypatch.setattr(fit_report, "build_mlx_fit_report", lambda _model: _fit("ok"))
    monkeypatch.setattr(
        install_provider.mlx_install,
        "install_local_mlx",
        install_local_mlx,
    )
    monkeypatch.setattr(
        install_provider.local_install,
        "install_local",
        lambda **_kwargs: pytest.fail("MLX host must not use llama installer"),
    )

    assert install_provider.main() == 0

    assert calls == ["qwen3.5:9b"]
    assert lease.released is True
    assert json.loads(capsys.readouterr().out) == {
        "provider": "local",
        "install_state": "installed",
    }


def test_install_provider_local_terminal_failed_returns_nonzero(monkeypatch, capsys):
    def install_local(**_kwargs):
        return {
            "provider": "local",
            "install_state": "failed",
            "install_error": "manifest_io_error",
        }

    monkeypatch.setattr(sys, "argv", ["journal install-provider", "local"])
    monkeypatch.setattr(
        install_provider.local_install,
        "inspect_readiness",
        lambda: _readiness("local", ready=False),
    )
    lease = _patch_lease_and_attempt(monkeypatch, "local")
    monkeypatch.setattr(fit_report, "build_local_fit_report", lambda _model: _fit("ok"))
    monkeypatch.setattr(install_provider.local_install, "install_local", install_local)

    assert install_provider.main() == 1

    assert lease.released is True
    assert json.loads(capsys.readouterr().out) == {
        "provider": "local",
        "install_state": "failed",
        "install_error": "manifest_io_error",
    }


def test_install_provider_parakeet_prints_disclosure_and_status(monkeypatch, capsys):
    calls = []

    def install_parakeet(**_kwargs):
        calls.append(True)
        return {"provider": "parakeet", "install_state": "installed"}

    monkeypatch.setattr(sys, "argv", ["journal install-provider", "parakeet"])
    monkeypatch.setattr(
        install_provider.parakeet_install,
        "inspect_readiness",
        lambda: _readiness("parakeet", ready=False),
    )
    monkeypatch.setattr(
        install_provider.parakeet_install,
        "target_fingerprint",
        lambda: {"provider": "parakeet"},
    )
    lease = _patch_lease_and_attempt(monkeypatch, "parakeet")
    monkeypatch.setattr(fit_report, "build_parakeet_fit_report", lambda: _fit("ok"))
    monkeypatch.setattr(
        install_provider.parakeet_install,
        "install_parakeet",
        install_parakeet,
    )

    assert install_provider.main() == 0

    captured = capsys.readouterr()
    assert calls == [True]
    assert lease.released is True
    assert json.loads(captured.out) == {
        "provider": "parakeet",
        "install_state": "installed",
    }
    assert install_provider.PARAKEET_DOWNLOAD_DISCLOSURE in captured.err
    assert "github.com" in captured.err
    assert "huggingface.co" in captured.err


def test_install_provider_parakeet_terminal_failed_returns_nonzero(monkeypatch, capsys):
    def install_parakeet(**_kwargs):
        return {
            "provider": "parakeet",
            "install_state": "failed",
            "install_error": "manifest_io_error",
        }

    monkeypatch.setattr(sys, "argv", ["journal install-provider", "parakeet"])
    monkeypatch.setattr(
        install_provider.parakeet_install,
        "inspect_readiness",
        lambda: _readiness("parakeet", ready=False),
    )
    monkeypatch.setattr(
        install_provider.parakeet_install,
        "target_fingerprint",
        lambda: {"provider": "parakeet"},
    )
    lease = _patch_lease_and_attempt(monkeypatch, "parakeet")
    monkeypatch.setattr(fit_report, "build_parakeet_fit_report", lambda: _fit("ok"))
    monkeypatch.setattr(
        install_provider.parakeet_install,
        "install_parakeet",
        install_parakeet,
    )

    assert install_provider.main() == 1

    assert lease.released is True
    assert json.loads(capsys.readouterr().out) == {
        "provider": "parakeet",
        "install_state": "failed",
        "install_error": "manifest_io_error",
    }


def test_install_provider_local_skips_fit_report_when_ready(monkeypatch, capsys):
    calls = []

    def install_local(**_kwargs):
        calls.append(True)
        return {"provider": "local", "install_state": "installed"}

    monkeypatch.setattr(sys, "argv", ["journal install-provider", "local"])
    monkeypatch.setattr(
        install_provider.local_install,
        "inspect_readiness",
        lambda: _readiness("local", ready=True),
    )
    monkeypatch.setattr(
        install_provider,
        "read_install_status",
        lambda name: {"provider": name, "install_state": "installed"},
    )
    monkeypatch.setattr(
        fit_report,
        "build_local_fit_report",
        lambda _model: pytest.fail("fit report should not render"),
    )
    monkeypatch.setattr(install_provider.local_install, "install_local", install_local)

    assert install_provider.main() == 0

    captured = capsys.readouterr()
    assert calls == []
    assert "local already installed" in captured.err
    assert json.loads(captured.out) == {
        "provider": "local",
        "install_state": "installed",
    }


def test_install_provider_local_expected_error_returns_nonzero(monkeypatch, capsys):
    persisted = {
        "provider": "local",
        "install_state": "failed",
        "install_error": "blocked detail",
        "error_code": "host_unfit",
    }

    def install_local(**_kwargs):
        raise install_provider.local_install.LocalProviderError(
            "host_unfit", "blocked detail"
        )

    monkeypatch.setattr(sys, "argv", ["journal install-provider", "local"])
    monkeypatch.setattr(
        install_provider.local_install,
        "inspect_readiness",
        lambda: _readiness("local", ready=False),
    )
    lease = _patch_lease_and_attempt(monkeypatch, "local")
    monkeypatch.setattr(
        fit_report, "build_local_fit_report", lambda _model: _fit("blocked")
    )
    monkeypatch.setattr(install_provider.local_install, "install_local", install_local)
    monkeypatch.setattr(
        install_provider,
        "read_install_status",
        lambda name: persisted if name == "local" else pytest.fail(name),
    )

    assert install_provider.main() == 1

    captured = capsys.readouterr()
    assert lease.released is True
    assert "blocked detail" in captured.err
    assert json.loads(captured.out) == persisted


def test_install_provider_parakeet_expected_error_prints_persisted_status(
    monkeypatch,
    capsys,
):
    persisted = {
        "provider": "parakeet",
        "install_state": "failed",
        "install_error": "blocked detail",
        "error_code": "host_unfit",
    }

    def install_parakeet(**_kwargs):
        raise install_provider.parakeet_install.ParakeetProviderError(
            "host_unfit",
            "blocked detail",
        )

    monkeypatch.setattr(sys, "argv", ["journal install-provider", "parakeet"])
    monkeypatch.setattr(
        install_provider.parakeet_install,
        "inspect_readiness",
        lambda: _readiness("parakeet", ready=False),
    )
    monkeypatch.setattr(
        install_provider.parakeet_install,
        "target_fingerprint",
        lambda: {"provider": "parakeet"},
    )
    lease = _patch_lease_and_attempt(monkeypatch, "parakeet")
    monkeypatch.setattr(fit_report, "build_parakeet_fit_report", lambda: _fit("ok"))
    monkeypatch.setattr(
        install_provider.parakeet_install,
        "install_parakeet",
        install_parakeet,
    )
    monkeypatch.setattr(
        install_provider,
        "read_install_status",
        lambda name: persisted if name == "parakeet" else pytest.fail(name),
    )

    assert install_provider.main() == 1

    captured = capsys.readouterr()
    assert lease.released is True
    assert "blocked detail" in captured.err
    assert json.loads(captured.out) == persisted


def test_install_provider_mlx_expected_error_prints_persisted_status(
    monkeypatch,
    capsys,
):
    persisted = {
        "provider": "local",
        "install_state": "failed",
        "install_error": "mlx unavailable",
        "error_code": "mlx_unavailable",
    }

    def install_local_mlx(*_args, **_kwargs):
        raise install_provider.mlx_install.MLXInstallUnavailableError("mlx unavailable")

    monkeypatch.setattr(sys, "argv", ["journal install-provider", "local"])
    monkeypatch.setattr(install_provider, "_is_mlx_backend", lambda: True)
    monkeypatch.setattr(
        install_provider.mlx_install,
        "resolve_model_spec",
        lambda: type("Spec", (), {"name": "model"})(),
    )
    monkeypatch.setattr(
        install_provider.mlx_install,
        "inspect_readiness",
        lambda _model: _readiness("local", ready=False),
    )
    monkeypatch.setattr(
        install_provider.mlx_install,
        "target_fingerprint",
        lambda _model: {"provider": "local", "runtime": "mlx"},
    )
    lease = _patch_lease_and_attempt(monkeypatch, "local")
    monkeypatch.setattr(fit_report, "build_mlx_fit_report", lambda _model: _fit("ok"))
    monkeypatch.setattr(
        install_provider.mlx_install,
        "install_local_mlx",
        install_local_mlx,
    )
    monkeypatch.setattr(
        install_provider,
        "read_install_status",
        lambda name: persisted if name == "local" else pytest.fail(name),
    )

    assert install_provider.main() == 1

    captured = capsys.readouterr()
    assert lease.released is True
    assert "mlx unavailable" in captured.err
    assert json.loads(captured.out) == persisted


def test_install_provider_unexpected_error_prints_persisted_status(
    monkeypatch,
    capsys,
):
    persisted = {
        "provider": "local",
        "install_state": "failed",
        "install_error": "boom",
        "error_code": None,
    }

    def install_local(**_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(sys, "argv", ["journal install-provider", "local"])
    monkeypatch.setattr(
        install_provider.local_install,
        "inspect_readiness",
        lambda: _readiness("local", ready=False),
    )
    lease = _patch_lease_and_attempt(monkeypatch, "local")
    monkeypatch.setattr(fit_report, "build_local_fit_report", lambda _model: _fit("ok"))
    monkeypatch.setattr(install_provider.local_install, "install_local", install_local)
    monkeypatch.setattr(
        install_provider,
        "read_install_status",
        lambda name: persisted if name == "local" else pytest.fail(name),
    )

    assert install_provider.main() == 1

    captured = capsys.readouterr()
    assert lease.released is True
    assert "boom" in captured.err
    assert json.loads(captured.out) == persisted


def test_install_provider_failure_status_read_error_keeps_stdout_empty(
    monkeypatch,
    capsys,
):
    def install_local(**_kwargs):
        raise install_provider.local_install.LocalProviderError(
            "host_unfit",
            "blocked detail",
        )

    def fail_read_status(*_args, **_kwargs):
        raise ValueError("malformed status")

    monkeypatch.setattr(sys, "argv", ["journal install-provider", "local"])
    monkeypatch.setattr(
        install_provider.local_install,
        "inspect_readiness",
        lambda: _readiness("local", ready=False),
    )
    lease = _patch_lease_and_attempt(monkeypatch, "local")
    monkeypatch.setattr(fit_report, "build_local_fit_report", lambda _model: _fit("ok"))
    monkeypatch.setattr(install_provider.local_install, "install_local", install_local)
    monkeypatch.setattr(install_provider, "read_install_status", fail_read_status)

    assert install_provider.main() == 1

    captured = capsys.readouterr()
    assert lease.released is True
    assert "blocked detail" in captured.err
    assert "could not read persisted local install status" in captured.err
    assert captured.out == ""


def test_install_provider_unsupported_rejects_without_install(monkeypatch, capsys):
    local_calls = []
    parakeet_calls = []

    def install_local(**_kwargs):
        local_calls.append(True)
        return {"provider": "local", "install_state": "installed"}

    def install_parakeet(**_kwargs):
        parakeet_calls.append(True)
        return {"provider": "parakeet", "install_state": "installed"}

    monkeypatch.setattr(sys, "argv", ["journal install-provider", "foo"])
    monkeypatch.setattr(install_provider.local_install, "install_local", install_local)
    monkeypatch.setattr(
        install_provider.parakeet_install,
        "install_parakeet",
        install_parakeet,
    )

    assert install_provider.main() == 2

    captured = capsys.readouterr()
    assert local_calls == []
    assert parakeet_calls == []
    assert "unsupported provider 'foo'" in captured.err
    assert "local" in captured.err
    assert "parakeet" in captured.err

# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
import sys

import pytest

from solstone.think import install_provider
from solstone.think.providers import fit_report


def _fit(severity: fit_report.FitSeverity) -> fit_report.FitReport:
    return fit_report.FitReport(
        artifact="test provider",
        checks=(fit_report.FitCheck("test", severity, f"{severity} detail"),),
    )


def test_install_provider_local_prints_install_status(monkeypatch, capsys):
    calls = []
    parakeet_calls = []

    def install_local():
        calls.append(True)
        return {"name": "local", "install_state": "installed"}

    def install_parakeet():
        parakeet_calls.append(True)
        return {"name": "parakeet", "install_state": "installed"}

    monkeypatch.setattr(sys, "argv", ["journal install-provider", "local"])
    monkeypatch.setattr(
        install_provider.local_install,
        "inspect_readiness",
        lambda: {"binary_installed": False, "model_installed": False},
    )
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
    assert json.loads(capsys.readouterr().out) == {
        "name": "local",
        "install_state": "installed",
    }


def test_install_provider_parakeet_prints_disclosure_and_status(monkeypatch, capsys):
    calls = []

    def install_parakeet():
        calls.append(True)
        return {"name": "parakeet", "install_state": "installed"}

    monkeypatch.setattr(sys, "argv", ["journal install-provider", "parakeet"])
    monkeypatch.setattr(
        install_provider.parakeet_install,
        "inspect_readiness",
        lambda: {"binary_installed": False, "model_installed": False},
    )
    monkeypatch.setattr(fit_report, "build_parakeet_fit_report", lambda: _fit("ok"))
    monkeypatch.setattr(
        install_provider.parakeet_install,
        "install_parakeet",
        install_parakeet,
    )

    assert install_provider.main() == 0

    captured = capsys.readouterr()
    assert calls == [True]
    assert json.loads(captured.out) == {
        "name": "parakeet",
        "install_state": "installed",
    }
    assert install_provider.PARAKEET_DOWNLOAD_DISCLOSURE in captured.err
    assert "github.com" in captured.err
    assert "huggingface.co" in captured.err
    banned = {"capture", "watch", "record", "monitor", "track", "collect"}
    assert not (banned & set(captured.err.lower().split()))


def test_install_provider_local_skips_fit_report_when_ready(monkeypatch, capsys):
    calls = []

    def install_local():
        calls.append(True)
        return {"name": "local", "install_state": "installed"}

    monkeypatch.setattr(sys, "argv", ["journal install-provider", "local"])
    monkeypatch.setattr(
        install_provider.local_install,
        "inspect_readiness",
        lambda: {"binary_installed": True, "model_installed": True},
    )
    monkeypatch.setattr(
        fit_report,
        "build_local_fit_report",
        lambda _model: pytest.fail("fit report should not render"),
    )
    monkeypatch.setattr(install_provider.local_install, "install_local", install_local)

    assert install_provider.main() == 0

    captured = capsys.readouterr()
    assert calls == [True]
    assert "local already installed" in captured.err
    assert json.loads(captured.out) == {
        "name": "local",
        "install_state": "installed",
    }


def test_install_provider_local_expected_error_returns_nonzero(monkeypatch, capsys):
    def install_local():
        raise install_provider.local_install.LocalProviderError(
            "host_unfit", "blocked detail"
        )

    monkeypatch.setattr(sys, "argv", ["journal install-provider", "local"])
    monkeypatch.setattr(
        install_provider.local_install,
        "inspect_readiness",
        lambda: {"binary_installed": False, "model_installed": False},
    )
    monkeypatch.setattr(
        fit_report, "build_local_fit_report", lambda _model: _fit("blocked")
    )
    monkeypatch.setattr(install_provider.local_install, "install_local", install_local)

    assert install_provider.main() == 1

    captured = capsys.readouterr()
    assert "blocked detail" in captured.err
    assert captured.out == ""


def test_install_provider_unsupported_rejects_without_install(monkeypatch, capsys):
    local_calls = []
    parakeet_calls = []

    def install_local():
        local_calls.append(True)
        return {"name": "local", "install_state": "installed"}

    def install_parakeet():
        parakeet_calls.append(True)
        return {"name": "parakeet", "install_state": "installed"}

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

# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Static settings CLI command tests not covered by HTTP parity."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from solstone.think.call import call_app

runner = CliRunner()


class TestProvidersInstall:
    @pytest.mark.parametrize("args", [[], ["local"], ["anthropic"]])
    def test_install_redirects_to_journal_install_provider(self, settings_env, args):
        settings_env()

        result = runner.invoke(call_app, ["settings", "providers", "install", *args])

        assert result.exit_code != 0
        combined = result.output + result.stderr
        assert "journal install-provider" in combined

    @pytest.mark.parametrize("verb", ["uninstall", "disable", "enable", "validate-key"])
    @pytest.mark.parametrize("name", ["anthropic", "openai", "openhands"])
    def test_retired_verbs_return_no_such_command(self, settings_env, verb, name):
        settings_env()

        result = runner.invoke(call_app, ["settings", "providers", verb, name])

        assert result.exit_code != 0
        assert "No such command" in (result.output + result.stderr)

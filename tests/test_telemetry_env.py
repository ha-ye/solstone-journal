# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Guard package-import defaults for telemetry-off environment variables."""

import os
import subprocess
import sys

TELEMETRY_ENV_KEYS = {
    "HF_HUB_DISABLE_TELEMETRY",
    "DO_NOT_TRACK",
    "OTEL_SDK_DISABLED",
}


def test_telemetry_env_defaults_set_at_import():
    env = {
        key: value for key, value in os.environ.items() if key not in TELEMETRY_ENV_KEYS
    }

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import os; "
                "import solstone; "
                "assert os.environ['HF_HUB_DISABLE_TELEMETRY'] == '1'; "
                "assert os.environ['DO_NOT_TRACK'] == '1'; "
                "assert os.environ['OTEL_SDK_DISABLED'] == 'true'"
            ),
        ],
        env=env,
        check=False,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr


def test_otel_env_default_preserves_explicit_value():
    env = {**os.environ, "OTEL_SDK_DISABLED": "false"}

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import os; "
                "import solstone; "
                "assert os.environ['OTEL_SDK_DISABLED'] == 'false'"
            ),
        ],
        env=env,
        check=False,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr

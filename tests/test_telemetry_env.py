# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Guard package-import defaults for telemetry-off environment variables."""

import os
import subprocess
import sys


def test_huggingface_telemetry_env_defaults_set_at_import():
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"HF_HUB_DISABLE_TELEMETRY", "DO_NOT_TRACK"}
    }

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import os; "
                "import solstone; "
                "assert os.environ['HF_HUB_DISABLE_TELEMETRY'] == '1'; "
                "assert os.environ['DO_NOT_TRACK'] == '1'"
            ),
        ],
        env=env,
        check=False,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr

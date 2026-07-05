# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import sys
import textwrap
import time

import pytest


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal semantics")
def test_talent_main_sigterm_exits_without_cancelled_traceback(tmp_path):
    journal = tmp_path / "journal"
    journal.mkdir()

    talent_dir = tmp_path / "talent"
    talent_dir.mkdir()
    (talent_dir / "test_cogitate.md").write_text(
        '{\n  "type": "cogitate",\n  "title": "Test Cogitate"\n}\n\nTest prompt\n',
        encoding="utf-8",
    )

    sitecustomize = tmp_path / "sitecustomize.py"
    sitecustomize.write_text(
        textwrap.dedent(
            f"""
            import asyncio
            import pathlib
            import sys
            import types

            import solstone.think.providers as providers
            import solstone.think.talent as talent

            talent.TALENT_DIR = pathlib.Path({str(talent_dir)!r})

            fake_provider = types.ModuleType("solstone_test_provider")

            async def run_cogitate(config, on_event=None):
                sys.stderr.write("provider-awaiting\\n")
                sys.stderr.flush()
                await asyncio.sleep(30)

            fake_provider.run_cogitate = run_cogitate
            providers.PROVIDER_REGISTRY["test"] = "solstone_test_provider"
            providers.PROVIDER_METADATA["test"] = {{
                "label": "Test",
                "env_key": "",
                "cogitate_cli": "",
            }}
            sys.modules["solstone_test_provider"] = fake_provider
            """
        ).lstrip(),
        encoding="utf-8",
    )

    env = os.environ.copy()
    env["SOLSTONE_JOURNAL"] = str(journal)
    env["SOL_SKIP_SUPERVISOR_CHECK"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONPATH"] = (
        str(tmp_path)
        if not env.get("PYTHONPATH")
        else str(tmp_path) + os.pathsep + env["PYTHONPATH"]
    )

    proc = subprocess.Popen(
        [sys.executable, "-m", "solstone.think.talents"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )

    request = {
        "name": "test_cogitate",
        "prompt": "hello",
        "provider": "test",
        "model": "test-model",
    }
    assert proc.stdin is not None
    proc.stdin.write(json.dumps(request).encode("utf-8") + b"\n")
    proc.stdin.flush()

    assert proc.stderr is not None
    stderr_seen = bytearray()
    deadline = time.monotonic() + 5
    while b"provider-awaiting" not in stderr_seen and time.monotonic() < deadline:
        remaining = max(0, deadline - time.monotonic())
        readable, _, _ = select.select([proc.stderr], [], [], remaining)
        if not readable:
            break
        line = proc.stderr.readline()
        if not line:
            break
        stderr_seen.extend(line)

    if b"provider-awaiting" not in stderr_seen:
        try:
            stdout, stderr_rest = proc.communicate(timeout=1)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr_rest = proc.communicate()
        pytest.fail(
            "talent process did not reach provider await\n"
            f"stdout={stdout.decode(errors='replace')}\n"
            f"stderr={(bytes(stderr_seen) + stderr_rest).decode(errors='replace')}"
        )

    proc.send_signal(signal.SIGTERM)

    try:
        stdout, stderr_rest = proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr_rest = proc.communicate()
        pytest.fail(
            "talent process did not exit after SIGTERM\n"
            f"stdout={stdout.decode(errors='replace')}\n"
            f"stderr={(bytes(stderr_seen) + stderr_rest).decode(errors='replace')}"
        )

    stderr = bytes(stderr_seen) + stderr_rest
    assert proc.returncode == 0, stderr.decode(errors="replace")
    assert b"Traceback" not in stderr
    assert b"CancelledError" not in stderr

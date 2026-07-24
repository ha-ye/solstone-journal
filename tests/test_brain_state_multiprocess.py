# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from solstone.think.models import LOCAL_MODEL
from solstone.think.providers.brain_state import (
    DEFAULT_READY_EVIDENCE_TTL,
    begin_brain_refresh,
    finish_brain_refresh,
    inspect_brain_state,
)

NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


def _env(journal: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["SOLSTONE_JOURNAL"] = str(journal)
    return env


def _write_config(journal: Path) -> None:
    path = journal / "config" / "journal.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "providers": {"active": {"provider": "openai", "model": "gpt-5"}},
                "env": {"OPENAI_API_KEY": "secret"},
            }
        ),
        encoding="utf-8",
    )


def _write_spp_config(journal: Path) -> None:
    credential = "endpoint-secret"
    path = journal / "config" / "journal.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "providers": {
                    "active": {"provider": "local", "model": LOCAL_MODEL},
                    "local": {
                        "endpoint_url": "https://brain.example.test/v1",
                        "served_model_id": "served-model",
                        "credential": credential,
                    },
                },
                "services": {
                    "confidential": {
                        "enabled_at": NOW.isoformat(),
                        "account_id": "acct-a",
                        "endpoint_url": "https://brain.example.test/v1",
                        "served_model_id": "served-model",
                        "credential_created_at": NOW.isoformat(),
                        "credential_fingerprint_sha256": hashlib.sha256(
                            credential.encode("utf-8")
                        ).hexdigest(),
                        "prior_active": {
                            "provider": "google",
                            "model": "gemini-flash-latest",
                        },
                        "prior_local_endpoint": None,
                    }
                },
                "env": {},
            }
        ),
        encoding="utf-8",
    )


def _component(now: datetime) -> dict[str, str]:
    return {
        "status": "ok",
        "observed_at": now.isoformat(),
        "expires_at": (now + DEFAULT_READY_EVIDENCE_TTL).isoformat(),
    }


def _write_spp_ready_record(journal: Path) -> None:
    _write_spp_config(journal)
    permit = begin_brain_refresh(NOW, journal_path=journal)
    assert permit is not None
    finish_brain_refresh(
        permit,
        {
            "configuration": _component(NOW),
            "lane_prerequisites": _component(NOW),
            "generate": _component(NOW),
            "cogitate": _component(NOW),
        },
        NOW,
        journal_path=journal,
    )


def test_refresh_permit_excludes_contender_and_crash_releases(tmp_path: Path) -> None:
    _write_config(tmp_path)
    ready = tmp_path / "ready"
    holder_code = f"""
import pathlib
import time
from datetime import datetime, timezone
from solstone.think.providers.brain_state import begin_brain_refresh
now = datetime.fromisoformat({NOW.isoformat()!r})
permit = begin_brain_refresh(now, journal_path={str(tmp_path)!r})
assert permit is not None
pathlib.Path({str(ready)!r}).write_text("ready")
while True:
    time.sleep(0.05)
"""
    contender_code = f"""
from datetime import datetime
from solstone.think.providers.brain_state import begin_brain_refresh
now = datetime.fromisoformat({NOW.isoformat()!r})
permit = begin_brain_refresh(now, journal_path={str(tmp_path)!r})
if permit is None:
    print("busy", flush=True)
else:
    print("free", flush=True)
    permit.release()
"""
    holder = subprocess.Popen(
        [sys.executable, "-c", holder_code],
        cwd=Path.cwd(),
        env=_env(tmp_path),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert ready.exists()

        busy = subprocess.run(
            [sys.executable, "-c", contender_code],
            cwd=Path.cwd(),
            env=_env(tmp_path),
            capture_output=True,
            text=True,
            check=True,
        )
        assert busy.stdout.strip() == "busy"

        holder.terminate()
        stdout, stderr = holder.communicate(timeout=10)
        assert holder.returncode is not None, (stdout, stderr)

        projection = inspect_brain_state(NOW, journal_path=tmp_path)["projection"]
        assert projection["aggregate_state"] == "unknown"
        assert projection["reason_code"] == "brain_check_interrupted"

        free = subprocess.run(
            [sys.executable, "-c", contender_code],
            cwd=Path.cwd(),
            env=_env(tmp_path),
            capture_output=True,
            text=True,
            check=True,
        )
        assert free.stdout.strip() == "free"
    finally:
        if holder.poll() is None:
            holder.terminate()
            holder.wait(timeout=5)


def test_prerequisite_renewal_permit_excludes_contender_and_crash_releases(
    tmp_path: Path,
) -> None:
    _write_spp_ready_record(tmp_path)
    ready = tmp_path / "ready"
    holder_code = f"""
import pathlib
import time
from datetime import datetime
from solstone.think.providers.brain_state import begin_brain_prerequisite_renewal
now = datetime.fromisoformat({(NOW + timedelta(seconds=1)).isoformat()!r})
result = begin_brain_prerequisite_renewal(now, journal_path={str(tmp_path)!r})
assert result["status"] == "started", result
pathlib.Path({str(ready)!r}).write_text("ready")
while True:
    time.sleep(0.05)
"""
    contender_code = f"""
from datetime import datetime
from solstone.think.providers.brain_state import begin_brain_prerequisite_renewal
now = datetime.fromisoformat({(NOW + timedelta(seconds=2)).isoformat()!r})
result = begin_brain_prerequisite_renewal(now, journal_path={str(tmp_path)!r})
print(result["status"], flush=True)
if result["status"] == "started":
    result["permit"].release()
"""
    holder = subprocess.Popen(
        [sys.executable, "-c", holder_code],
        cwd=Path.cwd(),
        env=_env(tmp_path),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert ready.exists()

        busy = subprocess.run(
            [sys.executable, "-c", contender_code],
            cwd=Path.cwd(),
            env=_env(tmp_path),
            capture_output=True,
            text=True,
            check=True,
        )
        assert busy.stdout.strip() == "busy"

        holder.terminate()
        stdout, stderr = holder.communicate(timeout=10)
        assert holder.returncode is not None, (stdout, stderr)

        free = subprocess.run(
            [sys.executable, "-c", contender_code],
            cwd=Path.cwd(),
            env=_env(tmp_path),
            capture_output=True,
            text=True,
            check=True,
        )
        assert free.stdout.strip() == "started"
    finally:
        if holder.poll() is None:
            holder.terminate()
            holder.wait(timeout=5)

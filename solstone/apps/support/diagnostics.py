# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Diagnostic collector for support tickets.

Gathers system state — version, OS, active services, recent errors, and
configuration (secrets stripped) — for the ``user_context`` field on support
tickets.  All collection is local; nothing is transmitted.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 168h = 7 days.
RECENCY_WINDOW_HOURS = 168

# Config keys that must never leave the device.
_SECRET_KEYS = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "GOOGLE_API_KEY",
        "REVAI_ACCESS_TOKEN",
        "PLAUD_ACCESS_TOKEN",
        "password",
        "secret",
        "token",
        "key",
    }
)
_ENV_SECRET_NAME_RE = re.compile(
    r"\b[A-Z0-9_]*(?:API_KEY|ACCESS_TOKEN|TOKEN|SECRET|PASSWORD)[A-Z0-9_]*\b"
)
_SECRET_VALUE_RE = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]+|sk-ant-[A-Za-z0-9_-]+|AIza[A-Za-z0-9_-]+)\b"
)
_POSIX_PATH_RE = re.compile(r"(?<!\w)/(?:[^\s;]+)")
_WINDOWS_PATH_RE = re.compile(r"\b[A-Za-z]:\\[^\s;]+")
_RESET_AT_RE = re.compile(r"(?:^|[;\s])reset_at_ms=(?P<value>\d+)")


def _is_secret_key(key: str) -> bool:
    """Return True if *key* looks like it holds sensitive data."""
    lower = key.lower()
    return any(s in lower for s in ("key", "token", "secret", "password"))


def _strip_secrets(obj: Any) -> Any:
    """Recursively redact values whose keys look secret."""
    if isinstance(obj, dict):
        return {
            k: "***" if _is_secret_key(k) else _strip_secrets(v) for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_strip_secrets(v) for v in obj]
    return obj


def _bounded_redacted_text(value: Any, *, limit: int = 240) -> str | None:
    if value is None:
        return None
    clean = " ".join(str(value).split())
    clean = _ENV_SECRET_NAME_RE.sub("<secret>", clean)
    clean = _SECRET_VALUE_RE.sub("<secret>", clean)
    clean = _WINDOWS_PATH_RE.sub("<path>", clean)
    clean = _POSIX_PATH_RE.sub("<path>", clean)
    clean = clean.replace("Traceback (most recent call last):", "traceback redacted")
    if len(clean) <= limit:
        return clean
    return clean[: limit - 1] + "…"


def _reset_at_ms_from_operator_detail(operator_detail: Any) -> int | None:
    if not isinstance(operator_detail, str):
        return None
    match = _RESET_AT_RE.search(operator_detail)
    if match is None:
        return None
    try:
        return int(match.group("value"))
    except ValueError:
        return None


def _redacted_readiness_view(view: dict[str, Any]) -> dict[str, Any]:
    operator_detail = _bounded_redacted_text(view.get("operator_detail"))
    redacted = {
        "provider": _bounded_redacted_text(view.get("provider"), limit=120),
        "model": _bounded_redacted_text(view.get("model"), limit=160),
        "reason_code": _bounded_redacted_text(view.get("reason_code"), limit=120),
        "status": _bounded_redacted_text(view.get("status"), limit=80),
        "severity": _bounded_redacted_text(view.get("severity"), limit=80),
        "reset_at_ms": _reset_at_ms_from_operator_detail(operator_detail),
        "summary": _bounded_redacted_text(view.get("summary")),
        "operator_detail": operator_detail,
    }
    return {key: value for key, value in redacted.items() if value is not None}


# -- Individual collectors ---------------------------------------------------


def collect_version() -> str | None:
    """Return the installed solstone version string."""
    try:
        from importlib.metadata import version

        return version("solstone")
    except Exception:
        return None


def collect_revision() -> str | None:
    """Return the source git short-HEAD, rooted at the solstone package dir.

    Reports the running commit even when the frozen package ``version`` is
    stale (it does not advance on ``git pull``).  Returns ``None`` when git is
    unavailable or the source tree is not a checkout (e.g. a wheel install).
    Rooted at the package dir, not the process CWD, so it works from a service
    whose CWD differs from the checkout.  Never raises.
    """
    import subprocess

    # parents[2] of .../solstone/apps/support/diagnostics.py is the solstone
    # package dir; git rev-parse walks up from there to the checkout's .git.
    package_dir = Path(__file__).resolve().parents[2]
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=package_dir,
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.stdout.strip() if result.returncode == 0 else None
    except Exception:
        return None


def collect_platform() -> dict[str, str]:
    """Return OS / platform info."""
    return {
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "python": platform.python_version(),
    }


def collect_services() -> dict[str, str]:
    """Check which solstone services are running.

    Looks at PID files under ``journal/health/``.
    """
    from solstone.think.utils import get_journal

    journal = get_journal()
    health_dir = Path(journal) / "health"
    if not health_dir.is_dir():
        return {}

    statuses: dict[str, str] = {}
    for pid_file in health_dir.glob("*.pid"):
        service = pid_file.stem
        try:
            pid = int(pid_file.read_text().strip())
            # Check if process is alive
            os.kill(pid, 0)
            statuses[service] = "running"
        except (ValueError, ProcessLookupError, PermissionError):
            statuses[service] = "stopped"
        except OSError:
            statuses[service] = "unknown"

    return statuses


def collect_recent_errors(limit: int = 10) -> list[dict[str, Any]]:
    """Return recent ERROR log lines within the recency window, newest-first."""
    from solstone.think.utils import get_journal

    journal = get_journal()
    health_dir = Path(journal) / "health"
    if not health_dir.is_dir():
        return []

    cutoff = datetime.now() - timedelta(hours=RECENCY_WINDOW_HOURS)
    candidates: list[tuple[datetime, dict[str, Any]]] = []
    for log_file in health_dir.glob("*.log"):
        file_mtime: datetime | None = None
        last_parsed_dt: datetime | None = None
        try:
            lines = log_file.read_text(errors="replace").splitlines()
            for line in lines:
                if "ERROR" not in line:
                    continue

                parts = line.split(maxsplit=1)
                try:
                    if not parts:
                        raise ValueError
                    dt = datetime.fromisoformat(parts[0])
                    last_parsed_dt = dt
                    approx = False
                    # Head slice is intentional: the prefix carries ERROR details.
                    message = (parts[1] if len(parts) > 1 else "").strip()[:500]
                except ValueError:
                    if last_parsed_dt is not None:
                        dt = last_parsed_dt
                    else:
                        if file_mtime is None:
                            file_mtime = datetime.fromtimestamp(
                                log_file.stat().st_mtime
                            )
                        dt = file_mtime
                    approx = True
                    message = line.strip()[:500]

                if dt < cutoff:
                    continue

                candidates.append(
                    (
                        dt,
                        {
                            "service": log_file.stem,
                            "message": message,
                            "time": dt.isoformat(timespec="seconds"),
                            "time_approximate": approx,
                        },
                    )
                )
        except OSError:
            continue

    candidates.sort(key=lambda c: c[0], reverse=True)
    return [entry for _, entry in candidates[:limit]]


def collect_config() -> dict[str, Any]:
    """Return journal config with secrets stripped."""
    from solstone.think.utils import get_journal

    journal = get_journal()
    config_path = Path(journal) / "config" / "config.json"
    if not config_path.is_file():
        return {}

    try:
        config = json.loads(config_path.read_text())
        return _strip_secrets(config)
    except (json.JSONDecodeError, OSError):
        return {}


def collect_provider_readiness() -> dict[str, Any]:
    """Return redacted provider-readiness diagnostics."""
    try:
        from solstone.convey.readiness_snapshot import build_readiness_snapshot

        snapshot = build_readiness_snapshot()
        redacted = {
            "summary": {
                "status": _bounded_redacted_text(
                    snapshot.get("summary", {}).get("status"), limit=80
                ),
                "severity": _bounded_redacted_text(
                    snapshot.get("summary", {}).get("severity"), limit=80
                ),
                "active_groups": snapshot.get("summary", {}).get("active_groups"),
                "blocked_count": snapshot.get("summary", {}).get("blocked_count"),
            },
            "interfaces": {
                name: _redacted_readiness_view(view)
                for name, view in (snapshot.get("interfaces") or {}).items()
                if isinstance(view, dict)
            },
            "groups": [
                _redacted_readiness_view(view)
                for view in (snapshot.get("groups") or [])
                if isinstance(view, dict)
            ],
        }
        if snapshot.get("unavailable"):
            redacted["unavailable"] = True
        if isinstance(snapshot.get("local"), dict):
            redacted["local"] = _redacted_readiness_view(snapshot["local"])
        return _strip_secrets(redacted)
    except Exception:
        logger.debug("provider readiness collection failed", exc_info=True)
        return {"unavailable": True}


# -- Public API --------------------------------------------------------------


def collect_all() -> dict[str, Any]:
    """Gather all diagnostics and return as a JSON-serialisable dict.

    This is the value for the ``user_context`` field on support tickets.
    The user sees *exactly* this dict before approving submission.
    """
    diagnostics: dict[str, Any] = {}

    try:
        diagnostics["version"] = collect_version()
    except Exception as exc:
        logger.debug("version collection failed: %s", exc)

    try:
        diagnostics["revision"] = collect_revision()
    except Exception as exc:
        logger.debug("revision collection failed: %s", exc)

    try:
        diagnostics["platform"] = collect_platform()
    except Exception as exc:
        logger.debug("platform collection failed: %s", exc)

    try:
        diagnostics["services"] = collect_services()
    except Exception as exc:
        logger.debug("service collection failed: %s", exc)

    try:
        diagnostics["recent_errors"] = collect_recent_errors()
    except Exception as exc:
        logger.debug("error collection failed: %s", exc)

    try:
        diagnostics["config"] = collect_config()
    except Exception as exc:
        logger.debug("config collection failed: %s", exc)

    try:
        diagnostics["provider_readiness"] = collect_provider_readiness()
    except Exception as exc:
        logger.debug("provider readiness collection failed: %s", exc)

    return diagnostics


def collect_all_json() -> str:
    """Convenience: return :func:`collect_all` as a formatted JSON string."""
    return json.dumps(collect_all(), indent=2, default=str)

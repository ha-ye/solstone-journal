# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Root blueprint: access gate and core routes."""

from __future__ import annotations

import logging
import os
import queue
import time
from datetime import date
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path
from typing import Any

from flask import (
    Blueprint,
    Response,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    stream_with_context,
    url_for,
)

from solstone.think.cluster import cluster_segments
from solstone.think.journal_config import write_journal_config
from solstone.think.utils import (
    day_dirs,
    ensure_journal_config,
    get_config,
    get_journal,
)

from . import bridge as convey_bridge
from .config import (
    locked_modify_convey_config,
    seed_default_app_navigation,
)
from .reasons import INVALID_CONFIG_VALUE, PL_REVOKED
from .secure_listener import get_authorized_clients
from .utils import error_response, error_response_with_reason

logger = logging.getLogger(__name__)


def _is_setup_complete() -> bool:
    """Check if initial setup has been completed."""
    try:
        config = get_config()
        return bool(config.get("setup", {}).get("completed_at"))
    except Exception:
        # Intended fail-closed-on-unreadable-config: require setup flow.
        return False


bp = Blueprint(
    "root",
    __name__,
    template_folder="templates",
    static_folder="static",
)


@bp.before_app_request
def require_access() -> Any:
    if request.endpoint is None:
        return None

    if request.endpoint in {
        "root.init",
        "root.init_validate_provider",
        "root.init_observers",
        "root.init_finalize",
        "static",
        "root.static",
        "root.favicon",
        # Observer ingest endpoints use key-based auth, not session
        "app:observer.ingest_upload",
        "app:observer.ingest_event",
        "app:observer.ingest_segments",
        "app:observer.ingest_manifest",
        "app:observer.ingest_manifest_day",
        "app:observer.register",
        # Journal-source manifest and ingest endpoints use key-based auth, not session
        "app:import.journal_source_manifest",
        "app:import.ingest_segments",
        "app:import.ingest_entities",
        "app:import.ingest_facets",
        "app:import.ingest_imports",
        "app:import.ingest_config",
    }:
        return None

    identity = getattr(g, "identity", None)
    if (
        request.endpoint == "app:link.pair"
        and identity is not None
        and identity.mode in {"pl-via-spl", "pl-direct"}
        and identity.fingerprint is None
    ):
        # Cert-less pairing stream; structurally confined to /pair by the C2 gate.
        return None

    if identity is not None and identity.mode in {"pl-direct", "pl-via-spl"}:
        if identity.fingerprint and get_authorized_clients().is_authorized(
            identity.fingerprint
        ):
            return None
        return error_response_with_reason(
            PL_REVOKED,
            detail="paired device revoked",
        )

    # Check setup state
    if not _is_setup_complete():
        return redirect(url_for("root.init"))

    return None


@bp.route("/sse/events", methods=["GET"], endpoint="callosum_sse")
def callosum_sse() -> Response:
    def generate():
        handle = convey_bridge.register_sse_subscriber("convey-ui")
        disconnect_event = request.environ.get("pl.disconnect_event")

        def disconnected() -> bool:
            is_set = getattr(disconnect_event, "is_set", None)
            return bool(is_set is not None and is_set())

        try:
            yield ": heartbeat\n\n"
            next_heartbeat_at = time.monotonic() + convey_bridge._SSE_HEARTBEAT_SECONDS
            while True:
                if disconnected():
                    break
                timeout = max(0.0, next_heartbeat_at - time.monotonic())
                if disconnect_event is not None:
                    timeout = min(timeout, 0.1)
                try:
                    message = handle.queue.get(timeout=timeout)
                except queue.Empty:
                    if disconnected():
                        break
                    if time.monotonic() < next_heartbeat_at:
                        continue
                    if handle.dropped.is_set():
                        break
                    yield ": heartbeat\n\n"
                    next_heartbeat_at = (
                        time.monotonic() + convey_bridge._SSE_HEARTBEAT_SECONDS
                    )
                    continue
                if handle.dropped.is_set() or disconnected():
                    break
                yield f"data: {message}\n\n"
                next_heartbeat_at = (
                    time.monotonic() + convey_bridge._SSE_HEARTBEAT_SECONDS
                )
        finally:
            convey_bridge.unregister_sse_subscriber(handle)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@bp.route("/init")
def init() -> Any:
    if _is_setup_complete():
        return redirect(url_for("root.index"))

    config = ensure_journal_config()
    identity = config.get("identity", {})
    identity_name = identity.get("name", "") or ""
    identity_preferred = identity.get("preferred", "") or ""
    retention = config.get("retention", {})
    retention_mode = retention.get("raw_media") or "keep"
    retention_days = retention.get("raw_media_days")
    try:
        version = _pkg_version("solstone")
    except PackageNotFoundError:
        version = "dev"
    journal_path = str(Path(get_journal()))
    return render_template(
        "init.html",
        version=version,
        journal_path=journal_path,
        identity_name=identity_name,
        identity_preferred=identity_preferred,
        retention_mode=retention_mode,
        retention_days=retention_days,
    )


@bp.route("/init/validate-provider", methods=["POST"])
def init_validate_provider() -> Any:
    data = request.get_json(silent=True) or {}
    key = data.get("key", "")

    from solstone.think.providers import validate_key

    try:
        result = validate_key("google", key)
    except Exception as e:
        result = {"valid": False, "error": str(e)}
    return jsonify(result)


@bp.route("/init/observers")
def init_observers() -> Any:
    from solstone.apps.observer.routes import (
        ACTIVE_THRESHOLD_MS,
        STALE_THRESHOLD_MS,
        _serialize_observer,
    )
    from solstone.apps.observer.utils import list_observers
    from solstone.think.utils import now_ms

    current_now = now_ms()
    observers_list = []
    for observer in list_observers():
        if observer.get("revoked", False):
            continue
        observers_list.append(_serialize_observer(observer, current_now))
    return jsonify(
        {
            "thresholds": {
                "active_ms": ACTIVE_THRESHOLD_MS,
                "stale_ms": STALE_THRESHOLD_MS,
            },
            "observers": observers_list,
        }
    )


@bp.route("/init/finalize", methods=["POST"])
def init_finalize() -> Any:
    data = request.get_json(silent=True) or {}

    from solstone.think.utils import now_ms

    config = get_config()
    config.setdefault("convey", {}).pop("allow_network_access", None)
    config.setdefault("identity", {}).update(
        {
            k: v
            for k, v in {
                "name": data.get("name"),
                "preferred": data.get("preferred"),
                "timezone": data.get("timezone"),
            }.items()
            if v
        }
    )
    gemini_key = data.get("gemini_key")
    if gemini_key:
        config.setdefault("env", {})["GOOGLE_API_KEY"] = gemini_key
    config.setdefault("setup", {})["completed_at"] = now_ms()
    retention_mode = data.get("retention_mode", "keep")
    retention_days = data.get("retention_days")
    if retention_mode == "days" and (
        not isinstance(retention_days, int) or retention_days < 1
    ):
        return error_response(
            INVALID_CONFIG_VALUE,
            detail="retention_days must be a positive integer",
        )
    config.setdefault("retention", {}).update(
        {
            "raw_media": retention_mode,
            "raw_media_days": retention_days if retention_mode == "days" else None,
        }
    )

    write_journal_config(config)

    def _seed(config: dict[str, Any]) -> dict[str, Any] | None:
        return config if seed_default_app_navigation(config) else None

    try:
        locked_modify_convey_config(_seed)
    except Exception:
        logger.error("default app navigation seed convey-config PERSIST failed")

    return jsonify({"success": True, "redirect": url_for("app:thinking.index")})


@bp.route("/favicon.ico")
def favicon() -> Any:
    """Serve the favicon from the project root."""
    project_root = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    return send_from_directory(project_root, "favicon.ico", mimetype="image/x-icon")


@bp.route("/app/today")
def app_today() -> Any:
    """Redirect /app/today to the most recent day with journal data."""
    today = date.today().strftime("%Y%m%d")
    for day in sorted(day_dirs().keys(), reverse=True):
        if cluster_segments(day):
            return redirect(url_for("app:transcripts.transcripts_day", day=day))
    return redirect(url_for("app:transcripts.transcripts_day", day=today))


@bp.route("/")
def index() -> Any:
    """Root redirect — always to home; the app handles new journals there."""
    return redirect(url_for("app:home.index"))

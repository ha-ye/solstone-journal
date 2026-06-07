# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Health API for journal-data trust signals.

This surface reports on capture, synthesis, and consumer-facing trust signals
derived from journal data. It is distinct from the service-liveness app at
``/app/health`` and from ``journal health`` infrastructure checks.
"""

from __future__ import annotations

import dataclasses
import logging

from flask import Blueprint, jsonify, request

from solstone.convey.reasons import HEALTH_REPORT_FAILED, INVALID_REQUEST_VALUE
from solstone.convey.utils import error_response
from solstone.think.surfaces import health as health_surface

logger = logging.getLogger(__name__)

bp = Blueprint("health", __name__, url_prefix="/api/health")


@bp.route("/summary")
def health_summary():
    try:
        report = health_surface.summary(request.args.get("day"))
    except ValueError as exc:
        return error_response(INVALID_REQUEST_VALUE, detail=str(exc))
    except Exception:
        logger.exception("health summary report failed")
        return error_response(HEALTH_REPORT_FAILED, detail="health report unavailable")
    return jsonify(dataclasses.asdict(report))


@bp.route("/full")
def health_full():
    try:
        report = health_surface.full(request.args.get("day"))
    except ValueError as exc:
        return error_response(INVALID_REQUEST_VALUE, detail=str(exc))
    except Exception:
        logger.exception("health full report failed")
        return error_response(HEALTH_REPORT_FAILED, detail="health report unavailable")
    return jsonify(dataclasses.asdict(report))


@bp.route("/range")
def health_range():
    try:
        report = health_surface.for_range(
            request.args.get("day_from"),
            request.args.get("day_to"),
        )
    except ValueError as exc:
        return error_response(INVALID_REQUEST_VALUE, detail=str(exc))
    except Exception:
        logger.exception("health range report failed")
        return error_response(HEALTH_REPORT_FAILED, detail="health report unavailable")
    return jsonify(dataclasses.asdict(report))

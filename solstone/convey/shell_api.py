# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Shell hydration API."""

from __future__ import annotations

from typing import TYPE_CHECKING

from flask import Blueprint, jsonify

from solstone.convey.shell_data import build_shell_data

if TYPE_CHECKING:
    from solstone.apps import AppRegistry


def create_shell_api_blueprint(registry: AppRegistry) -> Blueprint:
    """Create the shell hydration API blueprint."""
    bp = Blueprint("shell_api", __name__)

    @bp.get("/api/shell", endpoint="shell")
    def shell():
        return jsonify(build_shell_data(registry))

    return bp

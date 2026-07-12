# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from flask import Flask
from flask.testing import FlaskClient

from solstone.apps.awareness.routes import awareness_bp


def make_awareness_test_client() -> FlaskClient:
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.register_blueprint(awareness_bp)
    return app.test_client()

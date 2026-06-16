# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Web interface for navigating and interacting with journal data."""

from __future__ import annotations

import os
from datetime import timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from flask import Flask

__all__ = [
    "create_app",
    "emit",
]


def __getattr__(name: str):
    # PEP 562: resolve `emit` lazily so a bare `import solstone.convey.state`
    # does not drag bridge/callosum (and the rest of the web stack) into
    # sys.modules. The AttributeError for every other name is load-bearing:
    # it lets `from solstone.convey import <submodule>` fall through to
    # normal submodule import.
    if name == "emit":
        from .bridge import emit

        return emit
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def install_identity_stamper(app: Flask) -> None:
    from flask import g, request

    from solstone.convey.secure_listener import ConveyIdentity

    @app.before_request
    def _stamp_identity() -> None:
        stamped = request.environ.get("pl.identity")
        if stamped is not None:
            g.identity = stamped
            return
        g.identity = ConveyIdentity(
            mode="dl",
            fingerprint=None,
            device_label=None,
            paired_at=None,
            session_id=None,
        )


def create_app(journal: str = "") -> Flask:
    """Create and configure the Convey Flask application."""
    from flask import Flask
    from jinja2 import ChoiceLoader, FileSystemLoader

    from solstone.apps import AppRegistry
    from solstone.think.link.runtime import start_link_runtime
    from solstone.think.push.runtime import start_push_runtime
    from solstone.think.voice.runtime import start_voice_runtime

    from . import state, system
    from .apps import register_app_context
    from .chat import chat_bp, start_chat_runtime
    from .config import bp as config_bp
    from .health import bp as health_bp
    from .ledger import bp as ledger_bp
    from .profile import bp as profile_bp
    from .profile import profiles_bp
    from .push import push_bp
    from .request_id import install_request_id_stamper
    from .root import bp as root_bp
    from .voice import voice_bp

    app = Flask(
        __name__,
        template_folder=os.path.join(os.path.dirname(__file__), "templates"),
        static_folder=os.path.join(os.path.dirname(__file__), "static"),
    )

    from solstone.think.utils import CorruptConfigError

    from .reasons import CORRUPT_CONFIG
    from .utils import error_response

    @app.errorhandler(CorruptConfigError)
    def _handle_corrupt_config(exc: CorruptConfigError):
        return error_response(CORRUPT_CONFIG, detail=str(exc))

    # Add apps directory to template search path so apps can have their templates
    # in apps/{name}/workspace.html instead of needing a templates/ subfolder
    convey_templates = os.path.join(os.path.dirname(__file__), "templates")
    apps_root = os.path.join(os.path.dirname(os.path.dirname(__file__)), "apps")
    app.jinja_loader = ChoiceLoader(
        [
            FileSystemLoader(convey_templates),
            FileSystemLoader(apps_root),
        ]
    )

    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = timedelta(seconds=300)
    app.config.setdefault("SECURE_LISTENER_ENABLED", False)
    install_identity_stamper(app)
    install_request_id_stamper(app)

    # Register root blueprint (/, favicon)
    app.register_blueprint(root_bp)

    # Register config API blueprint
    app.register_blueprint(config_bp)

    # Register chat API blueprint (universal chat bar)
    app.register_blueprint(chat_bp)

    # Register system health API blueprint
    app.register_blueprint(system.bp)

    # Register ledger + profile tool-group API blueprints
    app.register_blueprint(ledger_bp)
    app.register_blueprint(profile_bp)
    app.register_blueprint(profiles_bp)

    # Register data-trust health API blueprint
    app.register_blueprint(health_bp)

    # Register voice API blueprint
    app.register_blueprint(voice_bp)

    # Register push API blueprint
    app.register_blueprint(push_bp)

    # Initialize and register app system
    registry = AppRegistry()
    registry.discover()
    registry.register_blueprints(app)

    # Register app system context processors
    register_app_context(app, registry)

    start_voice_runtime(app)
    start_push_runtime(app)
    start_chat_runtime(app)
    start_link_runtime(app)

    if journal:
        state.journal_root = journal
    return app

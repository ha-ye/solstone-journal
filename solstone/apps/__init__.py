# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""App plugin system for solstone.

Convention-based app discovery with minimal configuration:

Directory Structure:
    apps/my_app/           # Use underscores, not hyphens!
      workspace.html       # Required: Main template
      routes.py            # Optional: Flask blueprint (for custom routes beyond index)
      background.html      # Optional: Background service
      app.json             # Optional: Metadata overrides
      agents/              # Optional: Custom agents
      tests/               # Optional: App-specific tests

Naming Rules:
    - App directory names must use underscores (my_app), not hyphens (my-app)
    - App name = directory name (e.g., "my_app")
    - Blueprint variable must be named {app_name}_bp (e.g., my_app_bp)
    - Blueprint name must use "app:{name}" pattern for consistency
      (e.g., Blueprint("app:home", ...), use url_for('app:home.index'))
    - URL prefix convention: /app/{app_name}

app.json fields (all optional):
    {
      "icon": "🏠",           # Emoji icon for menu bar (default: "📦")
      "label": "Custom Label", # Display label (default: title-cased app name)
      "facets": {},            # Facet options: {"disabled": true} to hide facet bar
      "date_nav": {"mount": "content", "unit": {"one": "item", ...}},
                              # Normalized {unit, allow_future, mount} config;
                              # legacy true -> {unit: None,
                              # allow_future: <allow_future_dates>,
                              # mount: "chrome"}
      "app_bar": false,        # Hide the universal chat bar on this app (default: true)
      "allow_future_dates": true, # Allow future dates in month picker (default: false)
    }

    See the App dataclass below for the complete field list with types and defaults.

Apps are automatically discovered and registered.
All apps are served at /app/{name} via shared handler.
Apps with routes.py can define custom routes beyond the index route.
"""

from __future__ import annotations

import importlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from flask import Blueprint

logger = logging.getLogger(__name__)


class AppConfigError(ValueError):
    """Raised when app metadata contains an invalid configuration."""


def _parse_date_nav(metadata: dict) -> dict | None:
    """Normalize app date navigation metadata."""
    date_nav = metadata.get("date_nav", False)
    if not date_nav:
        return None

    if date_nav is True:
        return {
            "unit": None,
            "allow_future": bool(metadata.get("allow_future_dates", False)),
            "mount": "chrome",
        }

    if not isinstance(date_nav, dict):
        raise AppConfigError("date_nav must be a boolean or object")

    mount = date_nav.get("mount", "chrome")
    if mount not in {"chrome", "content"}:
        raise AppConfigError("date_nav.mount must be 'chrome' or 'content'")

    unit = date_nav.get("unit")
    allow_future = bool(
        date_nav.get("allow_future", metadata.get("allow_future_dates", False))
    )
    if mount == "content" and unit is None:
        raise AppConfigError("content date_nav requires an explicit unit")

    return {"unit": unit, "allow_future": allow_future, "mount": mount}


@dataclass
class App:
    """Convention-based app configuration."""

    name: str
    icon: str
    label: str
    blueprint: Optional[Blueprint] = None

    # Template paths (relative to Flask template root)
    workspace_template: str = ""
    background_template: Optional[str] = None

    # Facet configuration (optional, default {})
    # Options:
    #   - disabled: If true, facets bar is hidden for this app
    facets_config: dict = field(default_factory=dict)

    # Date navigation config; chrome mounts use the shared notch, content mounts
    # are rendered inside app workspace content.
    date_nav: dict | None = None

    # Hide the universal chat bar on this app
    app_bar: bool = True

    # Transitional; coexists with date_nav.allow_future and is removed in L4.
    allow_future_dates: bool = False

    def facets_enabled(self) -> bool:
        """Check if facets are enabled for this app."""
        return not self.facets_config.get("disabled", False)

    def date_nav_enabled(self) -> bool:
        """Check if date nav is enabled for this app."""
        return self.date_nav is not None

    def get_blueprint(self) -> Optional[Blueprint]:
        """Return Flask Blueprint with app routes, or None if app has no custom routes."""
        return self.blueprint

    def get_background_template(self) -> Optional[str]:
        """Return path to background service template, or None."""
        return self.background_template


class AppRegistry:
    """Registry for discovering and managing solstone apps."""

    def __init__(self):
        self.apps: dict[str, App] = {}
        self.api_blueprints: list[Blueprint] = []

    def discover(self) -> None:
        """Auto-discover apps using convention over configuration.

        For each directory in apps/:
        1. With workspace.html: load a full menu app
        2. With routes.py but no workspace.html: register an API-only blueprint
           (no menu entry, no injected index route)
        3. Load app.json if present (for icon, label overrides)
        4. Import routes.py and get blueprint (optional - for custom routes)
        5. Check for background.html (optional)
        """
        apps_dir = Path(__file__).parent

        for app_path in sorted(apps_dir.iterdir()):
            # Skip non-directories and private/internal directories
            if not app_path.is_dir() or app_path.name.startswith("_"):
                continue

            app_name = app_path.name

            has_workspace = (app_path / "workspace.html").exists()
            has_routes = (app_path / "routes.py").exists()
            if not has_workspace:
                if has_routes:
                    try:
                        _routes_module, blueprint = self._load_routes_blueprint(
                            app_name, app_path
                        )
                        self.api_blueprints.append(blueprint)
                        logger.info(f"Discovered API-only app: {app_name}")
                    except Exception as e:
                        logger.error(
                            f"Failed to load API-only app {app_name}: {e}",
                            exc_info=True,
                        )
                else:
                    logger.debug(f"Skipping {app_name}/ - no workspace.html found")
                continue

            try:
                app = self._load_app(app_name, app_path)
                self.apps[app_name] = app
                logger.info(f"Discovered app: {app_name}")
            except Exception as e:
                logger.error(f"Failed to load app {app_name}: {e}", exc_info=True)

    def _load_routes_blueprint(
        self, app_name: str, app_path: Path
    ) -> tuple[Any, Blueprint]:
        from flask import Blueprint

        routes_module = importlib.import_module(f"solstone.apps.{app_name}.routes")

        # Find blueprint - look for *_bp attribute
        expected_bp_var = f"{app_name}_bp"
        blueprint = None

        for attr_name in dir(routes_module):
            if attr_name.endswith("_bp"):
                bp = getattr(routes_module, attr_name)
                if isinstance(bp, Blueprint):
                    blueprint = bp

                    # Warn if variable name doesn't match convention
                    if attr_name != expected_bp_var:
                        logger.warning(
                            f"App '{app_name}': Blueprint variable '{attr_name}' should be '{expected_bp_var}'"
                        )

                    break

        if not blueprint:
            raise ValueError(
                f"No blueprint found in apps.{app_name}.routes - "
                f"expected variable named '{expected_bp_var}'"
            )

        # Verify blueprint name uses "app:{name}" pattern for consistency
        expected_name = f"app:{app_name}"
        if blueprint.name != expected_name:
            raise ValueError(
                f"App '{app_name}': Blueprint name must be '{expected_name}', "
                f"got '{blueprint.name}'. Update Blueprint() declaration in routes.py"
            )

        return routes_module, blueprint

    def _load_app(self, app_name: str, app_path: Path) -> App:
        """Load a single app from its directory.

        Args:
            app_name: Name of the app (directory name)
            app_path: Path to app directory

        Returns:
            App instance

        Raises:
            Exception: If app cannot be loaded
        """
        # Validate app name
        if "-" in app_name:
            logger.warning(
                f"App '{app_name}' uses hyphens. Use underscores instead (e.g., 'my_app')"
            )

        # Load metadata from app.json (optional)
        metadata = self._load_metadata(app_path)

        # Get icon and label (with defaults)
        icon = metadata.get("icon", "📦")
        label = metadata.get("label", app_name.replace("_", " "))

        # Parse facets config
        facets_config = metadata.get("facets", {})
        if not isinstance(facets_config, dict):
            facets_config = {}

        # Date navigation
        date_nav = _parse_date_nav(metadata)

        # Universal app bar
        app_bar = metadata.get("app_bar", True)

        # Allow future dates in month picker
        allow_future_dates = metadata.get("allow_future_dates", False)

        # Import routes module and get blueprint (optional)
        blueprint = None
        routes_module = None
        routes_file = app_path / "routes.py"

        if routes_file.exists():
            routes_module, blueprint = self._load_routes_blueprint(app_name, app_path)
        else:
            # No routes.py - create a minimal blueprint
            blueprint = self._create_minimal_blueprint(app_name)
            logger.debug(
                f"Created minimal blueprint for app '{app_name}' (no routes.py)"
            )

        # Inject default index route if app doesn't define one
        self._inject_index_if_needed(blueprint, routes_module, app_name)
        self._inject_fragment_routes(blueprint, app_name, app_path)

        # Resolve template paths (relative to apps/ directory since that's in the loader)
        workspace_template = f"{app_name}/workspace.html"

        background_template = None
        if (app_path / "background.html").exists():
            background_template = f"{app_name}/background.html"

        return App(
            name=app_name,
            icon=icon,
            label=label,
            blueprint=blueprint,
            workspace_template=workspace_template,
            background_template=background_template,
            facets_config=facets_config,
            date_nav=date_nav,
            app_bar=app_bar,
            allow_future_dates=allow_future_dates,
        )

    def _load_metadata(self, app_path: Path) -> dict[str, Any]:
        """Load app.json metadata file if it exists.

        Args:
            app_path: Path to app directory

        Returns:
            Dict with metadata, or empty dict if no app.json
        """
        metadata_file = app_path / "app.json"
        if metadata_file.exists():
            try:
                with open(metadata_file) as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"Failed to load {metadata_file}: {e}")
        return {}

    def _create_minimal_blueprint(self, app_name: str) -> Blueprint:
        """Create a minimal blueprint for apps without routes.py.

        Args:
            app_name: Name of the app

        Returns:
            Blueprint with proper naming and URL prefix
        """
        from flask import Blueprint

        blueprint = Blueprint(
            f"app:{app_name}",
            __name__,
            url_prefix=f"/app/{app_name}",
        )
        return blueprint

    def _inject_index_if_needed(
        self, blueprint: Blueprint, routes_module: Any, app_name: str
    ) -> None:
        """Inject default index route if app doesn't define one.

        Checks if routes module has an 'index' function. If not, adds a
        default index route that serves the static SPA shell using blueprint.record()
        to support multiple app registrations.

        Args:
            blueprint: The Flask blueprint to inject into
            routes_module: The imported routes module (or None if no routes.py)
            app_name: Name of the app
        """
        import inspect

        has_index = False

        if routes_module:
            # Get functions defined in this module (not imported)
            module_functions = [
                name
                for name, obj in inspect.getmembers(routes_module)
                if inspect.isfunction(obj) and obj.__module__ == routes_module.__name__
            ]
            has_index = "index" in module_functions

        if not has_index:
            # No index function, inject default one using record() for deferred setup
            # Only inject if blueprint hasn't been registered yet
            if not blueprint._got_registered_once and not getattr(
                blueprint, "_solstone_default_index_injected", False
            ):

                def index():
                    from flask import current_app

                    return current_app.send_static_file("shell.html")

                def setup_index(state):
                    """Deferred setup function called when blueprint is registered."""
                    state.app.add_url_rule(
                        f"{blueprint.url_prefix}/",
                        endpoint=f"{blueprint.name}.index",
                        view_func=index,
                    )

                blueprint.record(setup_index)
                blueprint._solstone_default_index_injected = True
                logger.debug(f"Injected default index route for app '{app_name}'")

    def _inject_fragment_routes(
        self, blueprint: Blueprint, app_name: str, app_path: Path
    ) -> None:
        """Inject static workspace/background fragment routes for a menu app."""
        if blueprint._got_registered_once or getattr(
            blueprint, "_solstone_fragment_routes_injected", False
        ):
            return

        registry = self

        def workspace_fragment(app_path=app_path):
            from flask import send_from_directory

            return send_from_directory(app_path, "workspace.html")

        def background_fragment(
            app_name=app_name, registry=registry, app_path=app_path
        ):
            from flask import abort, send_from_directory

            if not registry.apps[app_name].get_background_template():
                abort(404)
            return send_from_directory(app_path, "background.html")

        def setup_fragments(state):
            """Deferred setup function called when blueprint is registered."""
            url_prefix = state.url_prefix or blueprint.url_prefix
            endpoint_prefix = getattr(state, "name", blueprint.name)
            fragment_rules = (
                (
                    f"{url_prefix}/workspace",
                    f"{endpoint_prefix}.workspace_fragment",
                    workspace_fragment,
                ),
                (
                    f"{url_prefix}/background",
                    f"{endpoint_prefix}.background_fragment",
                    background_fragment,
                ),
            )
            for rule, endpoint, view_func in fragment_rules:
                if endpoint in state.app.view_functions:
                    continue
                state.app.add_url_rule(
                    rule,
                    endpoint=endpoint,
                    view_func=view_func,
                )

        blueprint.record(setup_fragments)
        blueprint._solstone_fragment_routes_injected = True
        logger.debug(f"Injected fragment routes for app '{app_name}'")

    def register_blueprints(self, flask_app) -> None:
        """Register all app blueprints with Flask.

        Args:
            flask_app: Flask application instance
        """
        for app in self.apps.values():
            if not app.blueprint:
                logger.error(
                    f"App '{app.name}' has no blueprint - this should not happen"
                )
                continue

            try:
                flask_app.register_blueprint(app.blueprint)
                logger.info(f"Registered blueprint: {app.blueprint.name}")
            except Exception as e:
                logger.error(
                    f"Failed to register blueprint for app {app.name}: {e}",
                    exc_info=True,
                )

        for blueprint in self.api_blueprints:
            try:
                flask_app.register_blueprint(blueprint)
                logger.info(f"Registered API-only blueprint: {blueprint.name}")
            except Exception as e:
                logger.error(
                    f"Failed to register API-only blueprint {blueprint.name}: {e}",
                    exc_info=True,
                )

# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""App plugin system context processors and helpers."""

from __future__ import annotations

from flask import Flask, g, request, url_for

from solstone.apps import AppRegistry
from solstone.convey.shell_data import build_shell_data


def register_app_context(app: Flask, registry: AppRegistry) -> None:
    """Register app system context processors and template filters."""
    from .utils import DATE_RE, format_date_short

    # Register Jinja2 filters
    app.jinja_env.filters["format_date_short"] = format_date_short

    @app.context_processor
    def inject_app_context() -> dict:
        """Inject app registry and facets context for new app system."""
        from solstone.convey import copy as convey_copy
        from solstone.convey.provider_readiness import chat_view

        shell = build_shell_data(registry)

        # Parse URL path: /app/{app_name}/{day}/...
        path_parts = request.path.split("/")

        # Auto-extract app name from URL for /app/{app_name}/... routes
        current_app_name = None
        if (
            len(path_parts) > 2
            and path_parts[1] == "app"
            and path_parts[2] in registry.apps
        ):
            current_app_name = path_parts[2]

        # Auto-extract day from URL for apps with date_nav enabled
        # Pattern: /app/{app_name}/{YYYYMMDD} or /app/{app_name}/{YYYYMMDD}/*
        day = None
        if (
            current_app_name
            and registry.apps[current_app_name].date_nav_enabled()
            and len(path_parts) > 3
            and DATE_RE.fullmatch(path_parts[3])
        ):
            day = path_parts[3]

        apps_dict = {
            app_entry["name"]: {
                "icon": app_entry["icon"],
                "icon_svg": app_entry["icon_svg"],
                "label": app_entry["label"],
            }
            for app_entry in shell["apps"]
        }
        starred_apps = [
            app_entry["name"] for app_entry in shell["apps"] if app_entry["starred"]
        ]
        chat_bar = shell["chat_bar"]

        return {
            "app_registry": registry,
            "app": current_app_name,
            "apps": apps_dict,
            "facets": shell["facets"],
            "selected_facet": shell["selected_facet"],
            "starred_apps": starred_apps,
            "day": day,
            "chat_bar_placeholder": chat_bar["placeholder"],
            "chat_bar_attention": chat_bar["attention"],
            "chat_bar_sol_request": chat_bar["sol_request"],
            # Shared renderer keeps chat error SSR in parity with chat chrome JS.
            "chat_view": chat_view,
            "convey_settings": shell["settings"],
            "CONVEY_COPY": {
                name.removeprefix("CONVEY_"): getattr(convey_copy, name)
                for name in convey_copy.__all__
                if name.startswith("CONVEY_")
            },
        }

    @app.context_processor
    def inject_vendor_helper() -> dict:
        """Provide convenient vendor library helper for templates."""

        def vendor_lib(library_name: str, file: str | None = None) -> str:
            """Generate URL for vendor library.

            Args:
                library_name: Name of vendor library (e.g., 'marked')
                file: Optional specific file, defaults to {library}.min.js

            Returns:
                URL to the vendor library file

            Example:
                {{ vendor_lib('marked') }}
                → /static/vendor/marked/marked.min.js
            """
            if file is None:
                file = f"{library_name}.min.js"
            return url_for("root.static", filename=f"vendor/{library_name}/{file}")

        return {"vendor_lib": vendor_lib}

    @app.after_request
    def clear_stale_facet_cookie(response):
        if getattr(g, "clear_facet_cookie", False):
            response.delete_cookie("selectedFacet", path="/", samesite="Lax")
        return response

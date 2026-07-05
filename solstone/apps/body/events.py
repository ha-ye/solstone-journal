# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Warm the Body archive's dedupe-stats cache at service startup.

Convey's startup sequence (``run_service`` in ``solstone/convey/cli.py``)
imports this module through ``discover_handlers()`` right before the web
server starts serving — that import is the app's per-process startup
hook, so the kick at the bottom runs once per convey process, after
``create_app`` has set ``state.journal_root``. Tests that build the Flask
app via ``create_app`` never import this module, so unit-test app
construction stays warm-free; only code that deliberately runs the
startup discovery path triggers it.
"""

from solstone.apps.body import routes
from solstone.apps.events import EventContext, on_event


@on_event("importer", "completed")
def rewarm_stats_after_import(ctx: EventContext) -> None:
    """Re-warm the stats cache when an import run completes.

    An import rewrites the dedupe database, which invalidates the
    signature-keyed cache — without this the next owner visit after an
    import would pay the cold scan. For imports that never touch the
    database the warm is a cache hit and costs nothing.
    """
    routes.warm_dedupe_stats_cache()


# Startup warm: module import is the app's per-process startup hook (see
# module docstring) — kick the cold scan now so the first owner request
# after a restart is served from the warm cache.
routes.warm_dedupe_stats_cache()

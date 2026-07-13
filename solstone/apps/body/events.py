# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Warm the Body archive's aggregate caches at service startup.

Convey's startup sequence (``run_service`` in ``solstone/convey/cli.py``)
imports this module through ``discover_handlers()`` right before the web
server starts serving — that import is the app's per-process startup
hook, so the kick at the bottom runs once per convey process, after
``create_app`` has set ``state.journal_root``. Tests that build the Flask
app via ``create_app`` never import this module, so unit-test app
construction stays warm-free; only code that deliberately runs the
startup discovery path triggers it.

Two caches warm here: the dedupe-stats fold (the archive overview) and
the trends fold (the trends ribbons). Both are single-flight background
threads; the trends build joins the stats thread first so the two heavy
folds run one after the other instead of contending.
"""

from solstone.apps.body import routes
from solstone.apps.events import EventContext, on_event


def _kick_cache_warms() -> None:
    stats_thread = routes.warm_dedupe_stats_cache()
    routes.warm_trends_cache(after=stats_thread)


@on_event("importer", "completed")
def rewarm_caches_after_import(ctx: EventContext) -> None:
    """Re-warm both caches when an import run completes.

    An import rewrites the dedupe database, which invalidates the
    signature-keyed caches — without this the next owner visit after an
    import would pay the cold folds. For imports that never touch the
    database both warms are cache hits and cost nothing.
    """
    _kick_cache_warms()


# Startup warm: module import is the app's per-process startup hook (see
# module docstring) — kick the cold folds now so the first owner request
# after a restart is served from the warm caches.
_kick_cache_warms()

# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""solstone namespace package."""

import logging
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

try:
    __version__ = _pkg_version("solstone")
except PackageNotFoundError:
    __version__ = "0.0.0+source"

# httpx logs the full request URL at INFO; the Gemini API authenticates via
# `?key=AIzaSy...`, so INFO leaks live keys into describe.log / transcribe.log.
# Set the level on the named logger so it survives later basicConfig() calls
# from individual CLI entry points.
logging.getLogger("httpx").setLevel(logging.WARNING)

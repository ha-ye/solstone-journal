# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""solstone namespace package."""

import os
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

from solstone.log_policy import apply_http_logging_policy

# HuggingFace Hub reads this at import time. Default telemetry off before any
# optional provider path can import huggingface_hub.
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("DO_NOT_TRACK", "1")

try:
    __version__ = _pkg_version("solstone")
except PackageNotFoundError:
    __version__ = "0.0.0+source"

# Keep httpx URL logging below the key-leaking INFO level.
apply_http_logging_policy()

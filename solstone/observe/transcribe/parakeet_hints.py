# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Shared Parakeet owner-facing hint copy."""

PACKAGED_COREML_HINT = """solstone packaged installs ship the CoreML transcription helper on
supported Apple Silicon Macs running macOS 14 or newer. Your install does
not include it — macOS requires Apple Silicon, or pip selected the
cross-platform fallback wheel.

On supported Linux hosts, transcription uses the supervised parakeet.cpp path.

If you want CoreML-accelerated parakeet transcription, install solstone
from a source checkout: see https://github.com/solpbc/solstone-journal/blob/main/CONTRIBUTING.md."""

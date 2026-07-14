# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Canonical filenames for journal segment marker files."""

STREAM_MARKER_NAME = "stream.json"
INGEST_MANIFEST_NAME = "ingest.json"
RESERVED_SEGMENT_FILENAMES = frozenset(
    {
        STREAM_MARKER_NAME,
        INGEST_MANIFEST_NAME,
    }
)

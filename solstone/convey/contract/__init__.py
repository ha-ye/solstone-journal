# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Generated OpenAPI contract assembly for Convey native clients."""

from __future__ import annotations

from .assemble import CALLOSUM_REGISTRY, all_reason_codes, assemble, build_document
from .spec import FieldSpec, OperationSpec, ParamSpec, RequestSpec, ResponseSpec

__all__ = [
    "CALLOSUM_REGISTRY",
    "FieldSpec",
    "OperationSpec",
    "ParamSpec",
    "RequestSpec",
    "ResponseSpec",
    "all_reason_codes",
    "assemble",
    "build_document",
]

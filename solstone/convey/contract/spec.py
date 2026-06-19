# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Small frozen DSL for the generated native-client OpenAPI contract."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class FieldSpec:
    name: str
    type: str
    required: bool = False
    description: str = ""
    item_type: str | None = None
    raw_schema: dict[str, Any] | None = None


@dataclass(frozen=True)
class ParamSpec:
    name: str
    location: str
    type: str = "string"
    required: bool = False
    description: str = ""


@dataclass(frozen=True)
class RequestSpec:
    content_type: str = "application/json"
    fields: tuple[FieldSpec, ...] = field(default=())
    raw_schema: dict[str, Any] | None = None
    example: dict[str, Any] | None = None
    description: str = ""


@dataclass(frozen=True)
class ResponseSpec:
    status: int
    description: str = ""
    content_type: str = "application/json"
    named_fields: tuple[FieldSpec, ...] = field(default=())
    free_form: bool = False
    raw_schema: dict[str, Any] | None = None
    reason_codes: tuple[str, ...] = field(default=())
    example: object | None = None
    extensions: dict[str, Any] | None = None


@dataclass(frozen=True)
class OperationSpec:
    operation_id: str
    method: str
    rule: str
    summary: str
    description: str
    request: RequestSpec | None = None
    parameters: tuple[ParamSpec, ...] = field(default=())
    responses: tuple[ResponseSpec, ...] = field(default=())
    auth: str = ""


__all__ = [
    "FieldSpec",
    "OperationSpec",
    "ParamSpec",
    "RequestSpec",
    "ResponseSpec",
]

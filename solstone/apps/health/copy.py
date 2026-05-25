# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Owner-facing copy for the health service logs surface."""

LOGS_SERVICE_FILTER_LABEL = "service"
LOGS_STREAM_FILTER_LABEL = "stream"
LOGS_LEVEL_FILTER_LABEL = "level"
LOGS_LEVEL_OPTION_ALL = "all levels"
LOGS_LEVEL_OPTION_ERROR = "errors only"
LOGS_LEVEL_OPTION_WARNING = "warnings & errors"
LOGS_LEVEL_OPTION_INFO = "info & above"
LOGS_SERVICE_COLLAPSED = "── {service} ── ({n} lines, ★ {errors} errors)"

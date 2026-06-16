# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

EXIT_PROVIDER_BLOCKED = 69

# Surfaced when the sense watchdog kills a handler subprocess that exceeded
# its per-job wall-clock cap. This is an error-classification label, NOT a
# subprocess return code: a SIGKILLed child returns a negative signal value,
# so the kill itself is the signal — there is nothing to compare against.
WATCHDOG_TIMEOUT = "timed out"

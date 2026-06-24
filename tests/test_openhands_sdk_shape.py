# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import inspect

from tests._logging_isolation import preserve_global_logging


def test_local_conversation_methods_match_provider_await_sites(monkeypatch):
    monkeypatch.setenv("OPENHANDS_SUPPRESS_BANNER", "1")
    with preserve_global_logging():
        from openhands.sdk.conversation.impl.local_conversation import (
            LocalConversation,
        )

        assert inspect.iscoroutinefunction(LocalConversation.arun) is True
        assert inspect.iscoroutinefunction(LocalConversation.send_message) is False

# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import inspect

from openhands.sdk.conversation.impl.local_conversation import LocalConversation

# Audit AC2: pin OpenHands SDK method shapes used by the provider.
# solstone/think/providers/openhands.py:810 calls send_message without await.
# solstone/think/providers/openhands.py:812 awaits arun.


def test_local_conversation_methods_match_provider_await_sites():
    assert inspect.iscoroutinefunction(LocalConversation.arun) is True
    assert inspect.iscoroutinefunction(LocalConversation.send_message) is False

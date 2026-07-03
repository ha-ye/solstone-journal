# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""prepare_config pins security-relevant fields against request override.

access_tier selects tool capability and type steers provider/model
resolution; neither may be overridden by a request body (the same rule the
cwd guard already enforces). The `partner` talent is type=cogitate with
access_tier=synthesis, so a request tier of "normal" is a privilege
escalation that must be refused.
"""

import pytest

from solstone.think.talents import prepare_config


def test_request_cannot_override_access_tier():
    with pytest.raises(ValueError, match="access_tier"):
        prepare_config({"name": "partner", "access_tier": "normal"})


def test_request_cannot_override_type():
    with pytest.raises(ValueError, match="type"):
        prepare_config({"name": "partner", "type": "generate"})


def test_matching_access_tier_is_a_noop():
    config = prepare_config({"name": "partner", "access_tier": "synthesis"})
    assert config["access_tier"] == "synthesis"


def test_no_override_leaves_definition_values():
    config = prepare_config({"name": "partner"})
    assert config["access_tier"] == "synthesis"
    assert config["type"] == "cogitate"

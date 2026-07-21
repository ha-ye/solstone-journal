# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import scripts.check_rust_release_manifest as checker
import scripts.release_public_evidence as public_evidence
import scripts.release_tool_pins as pins


def _errors(failures: list[checker.Failure]) -> set[str]:
    return {failure.error for failure in failures}


def test_validate_public_evidence_tree_recurses_over_keys_and_values() -> None:
    failures = public_evidence.validate_public_evidence_tree(
        "ledger",
        {
            "safe": [
                {"nested": "ENVROOT/bin/solstone-core"},
                {"bad_value": "--plat-name=macosx_14_0_arm64"},
            ],
            "--flag=value": "ok",
        },
    )

    errors = _errors(failures)
    assert "ledger.safe[1].bad_value contains disallowed content" in errors
    assert "ledger.<key[1]> key contains disallowed content" in errors


def test_validate_public_evidence_tree_allows_non_string_scalars() -> None:
    assert (
        public_evidence.validate_public_evidence_tree(
            "proof", {"count": 3, "ok": True, "none": None}
        )
        == []
    )


def test_public_evidence_canaries_match_release_constraints() -> None:
    assert (
        public_evidence.validate_public_evidence_tree(
            "proof", {"path": "ENVROOT/bin/solstone-core"}
        )
        == []
    )
    assert (
        public_evidence.validate_public_evidence_tree(
            "proof", {"checked": "2026-07-20T12:34:56Z"}
        )
        == []
    )
    assert (
        public_evidence.validate_public_evidence_tree(
            "proof", {"checked": "2026-07-20T12:34:56+00:00"}
        )
        == []
    )
    assert (
        public_evidence.validate_public_evidence_tree(
            "proof", {"swift": pins.MACOS_SWIFT_PIN}
        )
        == []
    )

    failures = public_evidence.validate_public_evidence_tree(
        "proof", {"argv": ["--flag=value"]}
    )

    assert "proof.argv[0] contains disallowed content" in _errors(failures)


def test_private_signing_policy_values_are_rejected_in_keys_and_values() -> None:
    assert (
        checker.validate_public_evidence_text("team", pins.MACOS_TEAM_IDENTIFIER) == []
    )

    failures = public_evidence.validate_public_evidence_tree(
        "ledger",
        {
            "outer": {"team": f"policy {pins.MACOS_TEAM_IDENTIFIER}"},
            f"key-{pins.MACOS_TEAM_IDENTIFIER}": "public",
        },
    )

    errors = _errors(failures)
    assert "ledger.outer.team contains private signing policy" in errors
    assert "ledger.<key[1]> key contains private signing policy" in errors
    for failure in failures:
        assert pins.MACOS_TEAM_IDENTIFIER not in failure.error
        assert pins.MACOS_TEAM_IDENTIFIER not in failure.expected
        assert pins.MACOS_TEAM_IDENTIFIER not in failure.actual
        assert pins.MACOS_TEAM_IDENTIFIER not in failure.repair

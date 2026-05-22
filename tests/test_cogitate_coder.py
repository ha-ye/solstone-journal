# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for cogitate coder mode: write flag, coder agent."""

# ---------------------------------------------------------------------------
# Write flag — Google provider
# ---------------------------------------------------------------------------


class TestGoogleWriteFlag:
    """Verify Google SDK policy behavior is controlled by config write flag."""

    def test_no_write_uses_yolo_with_policy(self, tmp_path):
        """Without write flag, policy denies writes and non-sol shell commands."""
        from solstone.think.cogitate_policy import CogitatePolicy

        policy = CogitatePolicy(write=False, allowed_roots=[tmp_path])

        allowed, reason = policy.check("write_file", {"file_path": "x"})
        assert allowed is False
        assert reason.startswith("policy_deny:")
        assert policy.check("run_shell_command", {"command": "rm -rf /tmp/x"})[0] is (
            False
        )
        assert policy.check(
            "run_shell_command", {"command": "sol call activities list"}
        ) == (True, "ok")
        assert policy.check("read_file", {"file_path": str(tmp_path / "x")}) == (
            True,
            "ok",
        )

    def test_write_true_uses_yolo_mode(self, tmp_path):
        """With write=True, policy allows all tool calls."""
        from solstone.think.cogitate_policy import CogitatePolicy

        policy = CogitatePolicy(write=True, allowed_roots=[tmp_path])

        assert policy.check("write_file", {"file_path": "x"}) == (True, "ok")
        assert policy.check("run_shell_command", {"command": "rm -rf /tmp/x"}) == (
            True,
            "ok",
        )


# ---------------------------------------------------------------------------
# talent/coder.md existence and frontmatter
# ---------------------------------------------------------------------------


class TestCoderAgent:
    """Verify talent/coder.md exists with correct frontmatter."""

    def test_coder_md_exists(self):
        """talent/coder.md must exist in the repo."""
        from pathlib import Path

        coder_path = Path(__file__).parent.parent / "solstone" / "talent" / "coder.md"
        assert coder_path.exists(), "talent/coder.md not found"

    def test_coder_frontmatter(self):
        """coder.md must have write: true and type: cogitate."""
        from pathlib import Path

        import frontmatter

        coder_path = Path(__file__).parent.parent / "solstone" / "talent" / "coder.md"
        post = frontmatter.load(coder_path)

        assert post.metadata.get("type") == "cogitate"
        assert post.metadata.get("write") is True
        assert post.metadata.get("title") == "Coder"
        assert "description" in post.metadata

    def test_coder_references_coding_skill(self):
        """coder.md must reference the developer docs instead of inlining guidelines."""
        from pathlib import Path

        coder_path = Path(__file__).parent.parent / "solstone" / "talent" / "coder.md"
        content = coder_path.read_text(encoding="utf-8")

        # Should reference the developer guide/docs, not inline dev guidelines
        assert "AGENTS.md" in content
        assert "docs/project-structure.md" in content
        assert "single source of truth" in content

        docs_dir = Path(__file__).parent.parent / "docs"
        assert (docs_dir / "coding-standards.md").exists()
        assert (docs_dir / "project-structure.md").exists()
        assert (docs_dir / "testing.md").exists()
        assert (docs_dir / "environment.md").exists()

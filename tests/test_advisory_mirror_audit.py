# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import base64
import hashlib
import json
import subprocess
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import scripts.advisory_mirror_audit as audit
from scripts.transparency_signing import FakeTransparencySigner

NOW = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
RECEIPT_UTC = "2026-07-24T11:30:00Z"
TEST_KEY_ID = "A1B2C3D4E5F60708"
DERIVED_NAME = "rustsec-advisory-db.git-testderived"


def _run_git(repo: Path, argv: Sequence[str]) -> str:
    result = subprocess.run(
        ["git", *argv],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _audit_root(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    (root / "core").mkdir(parents=True)
    (root / "core" / "deny.toml").write_text(
        '[licenses]\nallow = ["MIT"]\n', encoding="utf-8"
    )
    (root / "core" / "Cargo.lock").write_text("fixture lock\n", encoding="utf-8")
    (root / "core" / "Cargo.toml").write_text(
        "[workspace]\nmembers = []\n",
        encoding="utf-8",
    )
    (root / "target" / "release-evidence").mkdir(parents=True)
    (root / "target" / "release-evidence" / "preserve.txt").write_text(
        "release evidence\n",
        encoding="utf-8",
    )
    (root / "dist" / "release-candidate").mkdir(parents=True)
    (root / "dist" / "release-candidate" / "preserve.txt").write_text(
        "release candidate\n",
        encoding="utf-8",
    )
    return root


def _advisory_repo(
    tmp_path: Path, *, advisory_count: int = 1
) -> tuple[Path, str, Path]:
    repo = tmp_path / f"repo-{advisory_count}"
    repo.mkdir()
    _run_git(repo, ["init", "-b", "main"])
    _run_git(repo, ["config", "user.name", "Audit Test"])
    _run_git(repo, ["config", "user.email", "audit-test@example.invalid"])
    if advisory_count:
        for index in range(advisory_count):
            path = repo / "crates" / f"probe{index}" / f"RUSTSEC-2026-{index:04d}.md"
            path.parent.mkdir(parents=True)
            path.write_text(
                "```toml\n"
                "[advisory]\n"
                f'id = "RUSTSEC-2026-{index:04d}"\n'
                f'package = "probe{index}"\n'
                'date = "2026-01-01"\n'
                'url = "https://example.invalid/RUSTSEC-2026-0001"\n'
                'categories = ["unmaintained"]\n'
                "keywords = []\n\n"
                "[versions]\n"
                "patched = []\n"
                "```\n",
                encoding="utf-8",
            )
    else:
        (repo / "README.md").write_text("empty advisory db\n", encoding="utf-8")
    _run_git(repo, ["add", "."])
    _run_git(repo, ["commit", "-m", "fixture advisory db"])
    commit = _run_git(repo, ["rev-parse", "HEAD"])
    bundle = tmp_path / f"advisory-{advisory_count}.bundle"
    _run_git(repo, ["bundle", "create", str(bundle), "HEAD", "refs/heads/main"])
    return repo, commit, bundle


def _pubkey_bytes(key_id: str = TEST_KEY_ID) -> bytes:
    raw_id = bytes.fromhex(key_id)[::-1]
    blob = b"Ed" + raw_id + (b"\x11" * 32)
    return (
        f"untrusted comment: minisign public key {key_id}\n".encode("ascii")
        + base64.b64encode(blob)
        + b"\n"
    )


def _write_pubkey(path: Path, *, key_id: str = TEST_KEY_ID) -> str:
    raw = _pubkey_bytes(key_id)
    path.write_bytes(raw)
    return hashlib.sha256(raw).hexdigest()


def _receipt_bytes(
    commit: str, utc: str = RECEIPT_UTC, *, max_age: int = 86400
) -> bytes:
    return (
        f'{{"max_age":{max_age},"synced_commit":"{commit}","utc":"{utc}"}}\n'
    ).encode("utf-8")


def _trusted_comment(commit: str, utc: str = RECEIPT_UTC) -> str:
    return (
        f"{audit.TRUSTED_COMMENT_SCHEME} synced_commit={commit} utc={utc} max_age=86400"
    )


def _write_packet(
    tmp_path: Path,
    *,
    commit: str,
    utc: str = RECEIPT_UTC,
    key_id: str = TEST_KEY_ID,
    signer: FakeTransparencySigner | None = None,
    trusted_comment: str | None = None,
) -> tuple[Path, Path, Path, str, FakeTransparencySigner]:
    pubkey = tmp_path / "pub.key"
    pubkey_sha = _write_pubkey(pubkey, key_id=key_id)
    receipt = tmp_path / "freshness.json"
    receipt.write_bytes(_receipt_bytes(commit, utc))
    signature = receipt.parent / f"{receipt.name}.minisig"
    fake = signer or FakeTransparencySigner()
    fake.sign_file(
        receipt,
        signature,
        trusted_comment=trusted_comment or _trusted_comment(commit, utc),
    )
    return receipt, signature, pubkey, pubkey_sha, fake


class NoCallRunner:
    events: list[list[str]]

    def __init__(self) -> None:
        self.events = []

    def __call__(self, argv, **kwargs) -> subprocess.CompletedProcess[str]:
        self.events.append(list(argv))
        raise AssertionError(f"unexpected command: {argv}")


class HybridRunner:
    def __init__(
        self,
        *,
        derived_name: str = DERIVED_NAME,
        discovery_exit: int = 1,
        final_exit: int = 0,
        final_stderr_extra: str = "",
        final_scanned_path: Path | None = None,
        version: str = audit.CARGO_DENY_VERSION,
    ) -> None:
        self.derived_name = derived_name
        self.discovery_exit = discovery_exit
        self.final_exit = final_exit
        self.final_stderr_extra = final_stderr_extra
        self.final_scanned_path = final_scanned_path
        self.version = version
        self.events: list[list[str]] = []
        self.cargo_envs: list[Mapping[str, str]] = []
        self.cargo_cwds: list[Path | None] = []
        self.config_bytes: bytes | None = None
        self.check_count = 0

    def __call__(self, argv, **kwargs) -> subprocess.CompletedProcess[str]:
        command = list(argv)
        self.events.append(command)
        if command[0] == "git":
            return subprocess.run(command, **kwargs)
        if command[0] == "cargo-deny":
            if command == ["cargo-deny", "--version"]:
                return subprocess.CompletedProcess(
                    command, 0, f"cargo-deny {self.version}\n", ""
                )
            if command[-2:] == ["check", "advisories"]:
                self.check_count += 1
                self.cargo_envs.append(kwargs.get("env", {}))
                self.cargo_cwds.append(kwargs.get("cwd"))
                config_path = Path(command[command.index("--config") + 1])
                self.config_bytes = config_path.read_bytes()
                db_parent = _config_db_path(config_path)
                scanned = self.final_scanned_path if self.check_count == 2 else None
                if scanned is None:
                    scanned = db_parent / self.derived_name
                stderr = (
                    f"2026-07-24 [DEBUG] Opening advisory database at '{scanned}'\n"
                )
                if self.check_count == 1:
                    return subprocess.CompletedProcess(
                        command, self.discovery_exit, "", stderr
                    )
                stderr += self.final_stderr_extra
                return subprocess.CompletedProcess(command, self.final_exit, "", stderr)
            raise AssertionError(f"unexpected cargo-deny command: {command}")
        raise AssertionError(f"unexpected command: {command}")


class FakeGitRunner:
    def __init__(
        self,
        *,
        heads_commit: str,
        clone_commit: str | None = None,
        fail: str | None = None,
    ) -> None:
        self.heads_commit = heads_commit
        self.clone_commit = clone_commit or heads_commit
        self.fail = fail
        self.events: list[list[str]] = []

    def __call__(self, argv, **kwargs) -> subprocess.CompletedProcess[str]:
        command = list(argv)
        self.events.append(command)
        if self.fail and self.fail in " ".join(command):
            return subprocess.CompletedProcess(command, 1, "", "failed")
        if command == ["cargo-deny", "--version"]:
            return subprocess.CompletedProcess(command, 0, "cargo-deny 0.20.2\n", "")
        if command[:3] == ["git", "bundle", "verify"]:
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[:3] == ["git", "bundle", "list-heads"]:
            return subprocess.CompletedProcess(
                command,
                0,
                f"{self.heads_commit} HEAD\n{self.heads_commit} refs/heads/main\n",
                "",
            )
        if command[:2] == ["git", "clone"]:
            Path(command[3]).mkdir(parents=True)
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[:3] == ["git", "-C", command[2]] and command[-2:] == [
            "rev-parse",
            "HEAD",
        ]:
            return subprocess.CompletedProcess(command, 0, self.clone_commit + "\n", "")
        if command[-2:] == ["check", "advisories"]:
            config_path = Path(command[command.index("--config") + 1])
            db_parent = _config_db_path(config_path)
            return subprocess.CompletedProcess(
                command,
                1,
                "",
                f"Opening advisory database at '{db_parent / DERIVED_NAME}'\n",
            )
        raise AssertionError(f"unexpected command: {command}")


def _config_db_path(config_path: Path) -> Path:
    import tomllib

    parsed = tomllib.loads(config_path.read_text(encoding="utf-8"))
    return Path(parsed["advisories"]["db-path"])


def _invoke_green(
    tmp_path: Path,
    *,
    runner: HybridRunner | None = None,
    advisory_count: int = 1,
    utc: str = RECEIPT_UTC,
) -> tuple[bytes, HybridRunner, Path, Path, Path, Path]:
    root = _audit_root(tmp_path)
    _repo, commit, bundle = _advisory_repo(tmp_path, advisory_count=advisory_count)
    receipt, _signature, pubkey, pubkey_sha, fake = _write_packet(
        tmp_path,
        commit=commit,
        utc=utc,
    )
    active_runner = runner or HybridRunner()
    output = audit.audit_advisory_mirror(
        root,
        bundle=bundle,
        receipt=receipt,
        pubkey=pubkey,
        locator="ssh://mirror.example.invalid/rustsec-advisory-db.git",
        runner=active_runner,
        verifier=fake,
        clock=lambda: NOW,
        pinned_key_id=TEST_KEY_ID,
        pinned_pubkey_sha256=pubkey_sha,
    )
    return output, active_runner, root, bundle, receipt, pubkey


def _inventory(root: Path) -> dict[str, tuple[str, int]]:
    result: dict[str, tuple[str, int]] = {}
    if not root.exists():
        return result
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        rel = path.relative_to(root).as_posix()
        raw = path.read_bytes()
        result[rel] = (hashlib.sha256(raw).hexdigest(), len(raw))
    return result


def test_green_packet_emits_exact_success_json_and_uses_bound_snapshot(
    tmp_path: Path,
) -> None:
    output, runner, root, _bundle, _receipt, _pubkey = _invoke_green(tmp_path)

    payload = json.loads(output)
    assert list(payload) == [
        "product",
        "advisory_cohort",
        "synced_commit",
        "receipt_utc",
        "max_age",
        "checked_at",
        "cargo_lock_sha256",
        "cargo_deny_version",
        "verdict",
    ]
    assert payload["product"] == "solstone-journal"
    assert payload["advisory_cohort"] == audit.ADVISORY_COHORT_ID
    assert payload["receipt_utc"] == RECEIPT_UTC
    assert payload["max_age"] == 86400
    assert payload["checked_at"] == "2026-07-24T12:00:00Z"
    assert payload["cargo_deny_version"] == "0.20.2"
    assert (
        payload["cargo_lock_sha256"]
        == hashlib.sha256((root / "core" / "Cargo.lock").read_bytes()).hexdigest()
    )
    expected = json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n"
    assert output == expected
    assert output.count(b"\n") == 1
    assert runner.check_count == 2
    assert all(env.get("CARGO_NET_OFFLINE") == "true" for env in runner.cargo_envs)
    assert runner.config_bytes is not None
    config_text = runner.config_bytes.decode("utf-8")
    assert "git-fetch-with-cli" not in config_text
    assert "maximum-db-staleness" not in config_text


def test_green_packet_with_real_git_bundle_materialization(tmp_path: Path) -> None:
    output, runner, _root, _bundle, _receipt, _pubkey = _invoke_green(tmp_path)

    assert json.loads(output)["verdict"] == "pass"
    assert any(command[:3] == ["git", "bundle", "verify"] for command in runner.events)
    assert any(
        command[:3] == ["git", "bundle", "list-heads"] for command in runner.events
    )
    assert any(command[:2] == ["git", "clone"] for command in runner.events)
    assert runner.check_count == 2


@pytest.mark.parametrize("missing", ["bundle", "receipt", "pubkey", "locator"])
def test_required_inputs_fail_before_git_or_cargo(tmp_path: Path, missing: str) -> None:
    root = _audit_root(tmp_path)
    _repo, commit, bundle = _advisory_repo(tmp_path)
    receipt, _signature, pubkey, pubkey_sha, fake = _write_packet(
        tmp_path, commit=commit
    )
    if missing == "bundle":
        bundle = tmp_path / "missing.bundle"
    elif missing == "receipt":
        receipt = tmp_path / "missing.json"
    elif missing == "pubkey":
        pubkey = tmp_path / "missing.pub"
    locator = (
        ""
        if missing == "locator"
        else "ssh://mirror.example.invalid/rustsec-advisory-db.git"
    )
    runner = NoCallRunner()

    with pytest.raises(audit.ReleasePolicyError):
        audit.audit_advisory_mirror(
            root,
            bundle=bundle,
            receipt=receipt,
            pubkey=pubkey,
            locator=locator,
            runner=runner,
            verifier=fake,
            clock=lambda: NOW,
            pinned_key_id=TEST_KEY_ID,
            pinned_pubkey_sha256=pubkey_sha,
        )
    assert runner.events == []


@pytest.mark.parametrize("kind", ["missing", "symlink", "directory"])
def test_adjacent_signature_is_required_and_regular_before_git_or_cargo(
    tmp_path: Path,
    kind: str,
) -> None:
    root = _audit_root(tmp_path)
    _repo, commit, bundle = _advisory_repo(tmp_path)
    receipt, signature, pubkey, pubkey_sha, fake = _write_packet(
        tmp_path, commit=commit
    )
    signature.unlink()
    if kind == "symlink":
        target = tmp_path / "sig-target"
        target.write_text("signature", encoding="utf-8")
        signature.symlink_to(target)
    elif kind == "directory":
        signature.mkdir()
    runner = NoCallRunner()

    with pytest.raises(audit.ReleasePolicyError):
        audit.audit_advisory_mirror(
            root,
            bundle=bundle,
            receipt=receipt,
            pubkey=pubkey,
            locator="ssh://mirror.example.invalid/rustsec-advisory-db.git",
            runner=runner,
            verifier=fake,
            clock=lambda: NOW,
            pinned_key_id=TEST_KEY_ID,
            pinned_pubkey_sha256=pubkey_sha,
        )
    assert runner.events == []


@pytest.mark.parametrize("target_name", ["bundle", "receipt", "pubkey"])
@pytest.mark.parametrize("kind", ["missing", "symlink", "directory"])
def test_unsafe_input_paths_and_symlinks_fail_before_git_or_cargo(
    tmp_path: Path,
    target_name: str,
    kind: str,
) -> None:
    root = _audit_root(tmp_path)
    _repo, commit, bundle = _advisory_repo(tmp_path)
    receipt, _signature, pubkey, pubkey_sha, fake = _write_packet(
        tmp_path, commit=commit
    )
    paths = {"bundle": bundle, "receipt": receipt, "pubkey": pubkey}
    target = paths[target_name]
    if target.exists() or target.is_symlink():
        target.unlink()
    if kind == "symlink":
        link_target = tmp_path / f"{target_name}-target"
        link_target.write_text("target", encoding="utf-8")
        target.symlink_to(link_target)
    elif kind == "directory":
        target.mkdir()
    runner = NoCallRunner()

    with pytest.raises(audit.ReleasePolicyError):
        audit.audit_advisory_mirror(
            root,
            bundle=paths["bundle"],
            receipt=paths["receipt"],
            pubkey=paths["pubkey"],
            locator="ssh://mirror.example.invalid/rustsec-advisory-db.git",
            runner=runner,
            verifier=fake,
            clock=lambda: NOW,
            pinned_key_id=TEST_KEY_ID,
            pinned_pubkey_sha256=pubkey_sha,
        )
    assert runner.events == []


@pytest.mark.parametrize(
    ("locator", "accepted"),
    [
        ("https://github.com/rustsec/advisory-db", False),
        ("https://github.com/rustsec/advisory-db.git", False),
        ("https://github.com/rustsec/advisory-db/", False),
        ("https://github.com/rustsec/advisory-db.git/", False),
        ("github.com:rustsec/advisory-db.git", False),
        ("github.com:rustsec/advisory-db.git/", False),
        ("ssh://github.com/rustsec/advisory-db.git", False),
        ("ssh://github.com:22/rustsec/advisory-db.git", False),
        ("git://github.com/rustsec/advisory-db.git", False),
        ("http://github.com/rustsec/advisory-db", False),
        ("git+ssh://github.com/rustsec/advisory-db.git", False),
        ("https://raw.github.com/rustsec/advisory-db", False),
        ("https://mirror.github.com/rustsec/advisory-db", False),
        ("https://foo.github.com/rustsec/advisory-db.git", False),
        ("https://github.com/rustsec/advisory-db?x=1", False),
        ("https://github.com/rustsec/advisory-db#frag", False),
        ("github.com/rustsec/advisory-db.git", False),
        ("", False),
        (" ", False),
        ("https://mirror.example.invalid/rustsec/advisory-db", True),
        ("https://mirror.example.invalid/rustsec/advisory-db.git", False),
        ("https://mirror.example.invalid/rustsec/rustsec-advisory-db.git", True),
        ("https://mirror.example.invalid/rustsec/rustsec-advisory-db", False),
        ("https://mirror.example.invalid/rustsec/advisory-db/", False),
        ("https://mirror.example.invalid/rustsec/rustsec-advisory-db.git/", False),
        ("https://mirror.example.invalid/rustsec/advisory-db?x=1", False),
        ("https://mirror.example.invalid/rustsec/advisory-db#frag", False),
        ("ssh://git@mirror.example.invalid/rustsec/advisory-db", True),
        ("git@mirror.example.invalid:rustsec/advisory-db", True),
        ("git@mirror.example.invalid:rustsec/rustsec-advisory-db.git", True),
        ("git@mirror.example.invalid:rustsec/advisory-db.git", False),
        ("https://mirror.example.invalid/rustsec/advisory-db\n", False),
        ("https://mirror.example.invalid/rustsec/advisory-db\t", False),
        ("https://mirror.example.invalid/rustsec/advisory-db\x00", False),
        ("https://github.com.evil/rustsec/advisory-db", True),
    ],
)
def test_validate_locator_q3_oracle(locator: str, accepted: bool) -> None:
    assert (audit.validate_locator(locator) == []) is accepted


@pytest.mark.parametrize(
    "raw",
    [
        b'{"synced_commit":"{commit}","max_age":86400,"utc":"2026-07-24T11:30:00Z"}\n',
        b'{ "max_age": 86400, "synced_commit": "{commit}", "utc": "2026-07-24T11:30:00Z" }\n',
        b'{"max_age":86400,"synced_commit":"{commit}","utc":"2026-07-24T11:30:00Z"}',
        b'{"max_age":86400,"synced_commit":"{commit}","utc":"2026-07-24T11:30:00Z"}\n\n',
        b'{"max_age":86400,"synced_commit":"{commit}","utc":"2026-07-24T11:30:00Z","x":1}\n',
    ],
)
def test_receipt_body_requires_canonical_bytes(tmp_path: Path, raw: bytes) -> None:
    _repo, commit, _bundle = _advisory_repo(tmp_path)
    receipt = tmp_path / "freshness.json"
    receipt.write_bytes(raw.replace(b"{commit}", commit.encode("ascii")))

    with pytest.raises(audit.ReleasePolicyError):
        audit._read_receipt_authority(receipt)


@pytest.mark.parametrize(
    "payload",
    [
        {"max_age": 1, "synced_commit": "a" * 40, "utc": RECEIPT_UTC},
        {"max_age": True, "synced_commit": "a" * 40, "utc": RECEIPT_UTC},
        {"max_age": "86400", "synced_commit": "a" * 40, "utc": RECEIPT_UTC},
        {"max_age": 86400, "synced_commit": "a" * 64, "utc": RECEIPT_UTC},
        {"max_age": 86400, "synced_commit": "A" * 40, "utc": RECEIPT_UTC},
        {"max_age": 86400, "synced_commit": "a" * 40, "utc": "2026-99-99T00:00:00Z"},
        {
            "max_age": 86400,
            "synced_commit": "a" * 40,
            "utc": "2026-07-24T11:30:00+00:00",
        },
    ],
)
def test_receipt_fields_are_strict(tmp_path: Path, payload: dict[str, Any]) -> None:
    receipt = tmp_path / "freshness.json"
    receipt.write_text(
        json.dumps(payload, separators=(",", ":")) + "\n", encoding="utf-8"
    )

    with pytest.raises(audit.ReleasePolicyError):
        audit._read_receipt_authority(receipt)


def test_trusted_comment_mismatch_fails(tmp_path: Path) -> None:
    root = _audit_root(tmp_path)
    _repo, commit, bundle = _advisory_repo(tmp_path)
    receipt, _signature, pubkey, pubkey_sha, fake = _write_packet(
        tmp_path,
        commit=commit,
        trusted_comment="wrong",
    )

    with pytest.raises(audit.ReleasePolicyError) as exc:
        audit.audit_advisory_mirror(
            root,
            bundle=bundle,
            receipt=receipt,
            pubkey=pubkey,
            locator="ssh://mirror.example.invalid/rustsec-advisory-db.git",
            runner=NoCallRunner(),
            verifier=fake,
            clock=lambda: NOW,
            pinned_key_id=TEST_KEY_ID,
            pinned_pubkey_sha256=pubkey_sha,
        )
    assert "trusted comment mismatch" in exc.value.failures[0].error


def test_pubkey_sha256_mismatch_fails_before_minisign(tmp_path: Path) -> None:
    _repo, commit, _bundle = _advisory_repo(tmp_path)
    _receipt, _signature, pubkey, _pubkey_sha, _fake = _write_packet(
        tmp_path, commit=commit
    )

    with pytest.raises(audit.ReleasePolicyError):
        audit._validate_pubkey_binding(
            pubkey,
            pinned_key_id=TEST_KEY_ID,
            pinned_pubkey_sha256="0" * 64,
        )


def test_pubkey_key_id_mismatch_fails_before_minisign(tmp_path: Path) -> None:
    pubkey = tmp_path / "pub.key"
    pubkey_sha = _write_pubkey(pubkey)

    with pytest.raises(audit.ReleasePolicyError):
        audit._validate_pubkey_binding(
            pubkey,
            pinned_key_id="0000000000000000",
            pinned_pubkey_sha256=pubkey_sha,
        )


@pytest.mark.parametrize(
    "raw",
    [
        b"untrusted comment\nnot base64\n",
        b"untrusted comment\nRWQ=\n",
        b"untrusted comment\n" + base64.b64encode(b"XX" + b"\x00" * 40) + b"\n",
        b"only one line\n",
    ],
)
def test_pubkey_blob_shape_is_strict(tmp_path: Path, raw: bytes) -> None:
    pubkey = tmp_path / "pub.key"
    pubkey.write_bytes(raw)
    pubkey_sha = hashlib.sha256(raw).hexdigest()

    with pytest.raises(audit.ReleasePolicyError):
        audit._validate_pubkey_binding(
            pubkey,
            pinned_key_id=TEST_KEY_ID,
            pinned_pubkey_sha256=pubkey_sha,
        )


def test_signature_mutation_fails(tmp_path: Path) -> None:
    root = _audit_root(tmp_path)
    _repo, commit, bundle = _advisory_repo(tmp_path)
    receipt, signature, pubkey, pubkey_sha, fake = _write_packet(
        tmp_path, commit=commit
    )
    signature.write_text(
        signature.read_text(encoding="utf-8").replace("=", "A", 1), encoding="utf-8"
    )

    with pytest.raises(audit.ReleasePolicyError):
        audit.audit_advisory_mirror(
            root,
            bundle=bundle,
            receipt=receipt,
            pubkey=pubkey,
            locator="ssh://mirror.example.invalid/rustsec-advisory-db.git",
            runner=NoCallRunner(),
            verifier=fake,
            clock=lambda: NOW,
            pinned_key_id=TEST_KEY_ID,
            pinned_pubkey_sha256=pubkey_sha,
        )


@pytest.mark.parametrize(
    "utc",
    ["2026-07-24T12:06:00Z", "2026-07-23T11:59:59Z"],
)
def test_receipt_future_and_stale_times_fail(tmp_path: Path, utc: str) -> None:
    _repo, commit, _bundle = _advisory_repo(tmp_path)
    receipt = tmp_path / "freshness.json"
    receipt.write_bytes(_receipt_bytes(commit, utc))
    authority = audit._read_receipt_authority(receipt)

    with pytest.raises(audit.ReleasePolicyError):
        audit._validate_receipt_freshness(authority, clock=lambda: NOW)


def test_minisign_preflight_uses_product_binary_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, object]] = []

    def check(minisign: str) -> str:
        calls.append(("check", minisign))
        return "minisign 0.12"

    class Local:
        def __init__(
            self, *, secret_key: Path, public_key: Path, minisign: str
        ) -> None:
            calls.append(("init", (secret_key, public_key, minisign)))

        def verify_file(
            self,
            message_path: Path,
            signature_path: Path,
            *,
            expected_trusted_comment: str,
        ) -> None:
            calls.append(
                ("verify", (message_path, signature_path, expected_trusted_comment))
            )

    monkeypatch.setattr(audit, "check_minisign_binary", check)
    monkeypatch.setattr(audit, "LocalMinisignSigner", Local)
    verifier = audit.PublicKeyMinisignVerifier(
        tmp_path / "pub.key", minisign="minisign-test"
    )
    verifier.check()
    verifier.verify_file(
        tmp_path / "freshness.json",
        tmp_path / "freshness.json.minisig",
        expected_trusted_comment="comment",
    )

    assert calls[0] == ("check", "minisign-test")
    assert calls[1][0] == "init"
    assert calls[2][0] == "verify"


def test_bundle_verify_failure_stops_before_clone_and_cargo(tmp_path: Path) -> None:
    root = _audit_root(tmp_path)
    _repo, commit, bundle = _advisory_repo(tmp_path)
    receipt, _signature, pubkey, pubkey_sha, fake = _write_packet(
        tmp_path, commit=commit
    )
    runner = FakeGitRunner(heads_commit=commit, fail="bundle verify")

    with pytest.raises(audit.ReleasePolicyError):
        audit.audit_advisory_mirror(
            root,
            bundle=bundle,
            receipt=receipt,
            pubkey=pubkey,
            locator="ssh://mirror.example.invalid/rustsec-advisory-db.git",
            runner=runner,
            verifier=fake,
            clock=lambda: NOW,
            pinned_key_id=TEST_KEY_ID,
            pinned_pubkey_sha256=pubkey_sha,
        )
    assert not any(command[:2] == ["git", "clone"] for command in runner.events)
    assert not any(command[-2:] == ["check", "advisories"] for command in runner.events)


@pytest.mark.parametrize(
    "stdout",
    [
        "a" * 40 + " HEAD\n",
        "a" * 40 + " refs/heads/main\n",
        "a" * 40 + " HEAD\n" + "a" * 40 + " refs/heads/main\n" + "a" * 40 + " refs/x\n",
        "b" * 40 + " HEAD\n" + "a" * 40 + " refs/heads/main\n",
        "malformed\n" + "a" * 40 + " refs/heads/main\n",
    ],
)
def test_bundle_heads_must_be_exact_head_and_main(stdout: str) -> None:
    with pytest.raises(audit.ReleasePolicyError):
        audit._parse_bundle_heads(stdout, synced_commit="a" * 40)


def test_clone_head_must_match_receipt_commit(tmp_path: Path) -> None:
    root = _audit_root(tmp_path)
    _repo, commit, bundle = _advisory_repo(tmp_path)
    receipt, _signature, pubkey, pubkey_sha, fake = _write_packet(
        tmp_path, commit=commit
    )
    runner = FakeGitRunner(heads_commit=commit, clone_commit="b" * 40)

    with pytest.raises(audit.ReleasePolicyError):
        audit.audit_advisory_mirror(
            root,
            bundle=bundle,
            receipt=receipt,
            pubkey=pubkey,
            locator="ssh://mirror.example.invalid/rustsec-advisory-db.git",
            runner=runner,
            verifier=fake,
            clock=lambda: NOW,
            pinned_key_id=TEST_KEY_ID,
            pinned_pubkey_sha256=pubkey_sha,
        )


def test_zero_advisory_clone_fails_before_cargo(tmp_path: Path) -> None:
    root = _audit_root(tmp_path)
    _repo, commit, bundle = _advisory_repo(tmp_path, advisory_count=0)
    receipt, _signature, pubkey, pubkey_sha, fake = _write_packet(
        tmp_path, commit=commit
    )
    runner = HybridRunner()

    with pytest.raises(audit.ReleasePolicyError):
        audit.audit_advisory_mirror(
            root,
            bundle=bundle,
            receipt=receipt,
            pubkey=pubkey,
            locator="ssh://mirror.example.invalid/rustsec-advisory-db.git",
            runner=runner,
            verifier=fake,
            clock=lambda: NOW,
            pinned_key_id=TEST_KEY_ID,
            pinned_pubkey_sha256=pubkey_sha,
        )
    assert runner.check_count == 0


def test_discovery_run_nonzero_is_expected_when_debug_line_present(
    tmp_path: Path,
) -> None:
    output, runner, _root, _bundle, _receipt, _pubkey = _invoke_green(
        tmp_path,
        runner=HybridRunner(discovery_exit=23),
    )

    assert json.loads(output)["verdict"] == "pass"
    assert runner.check_count == 2


@pytest.mark.parametrize("scanned", [Path("/outside/db"), Path("nested/child")])
def test_discovery_path_must_be_direct_child_of_temp_parent(
    tmp_path: Path,
    scanned: Path,
) -> None:
    parent = tmp_path / "parent"
    parent.mkdir()
    if not scanned.is_absolute():
        scanned = parent / scanned

    with pytest.raises(audit.ReleasePolicyError):
        audit._assert_direct_child(scanned, parent)


def test_discovered_path_must_not_preexist(tmp_path: Path) -> None:
    root = _audit_root(tmp_path)
    _repo, commit, bundle = _advisory_repo(tmp_path)
    receipt, _signature, pubkey, pubkey_sha, fake = _write_packet(
        tmp_path, commit=commit
    )
    temp_root = tmp_path / "temp"
    preexisting = temp_root / "db-root" / DERIVED_NAME
    preexisting.mkdir(parents=True)

    with pytest.raises(audit.ReleasePolicyError):
        audit.audit_advisory_mirror(
            root,
            bundle=bundle,
            receipt=receipt,
            pubkey=pubkey,
            locator="ssh://mirror.example.invalid/rustsec-advisory-db.git",
            runner=HybridRunner(),
            verifier=fake,
            clock=lambda: NOW,
            temp_path_factory=lambda _label: temp_root,
            cleanup_rmdir=lambda path: None,
            pinned_key_id=TEST_KEY_ID,
            pinned_pubkey_sha256=pubkey_sha,
        )


def test_alternate_or_ambient_database_substitution_is_rejected(tmp_path: Path) -> None:
    runner = HybridRunner(final_scanned_path=tmp_path / "other-db")

    with pytest.raises(audit.ReleasePolicyError) as exc:
        _invoke_green(tmp_path, runner=runner)
    assert exc.value.failures[0].actual == "redacted"


def test_runner_never_sees_remote_git_or_cargo_fetch_operations(tmp_path: Path) -> None:
    _output, runner, _root, _bundle, _receipt, _pubkey = _invoke_green(tmp_path)
    flattened = [" ".join(command) for command in runner.events]

    assert not any("fetch db" in item for item in flattened)
    assert not any(item.startswith("git fetch") for item in flattened)
    assert not any(item.startswith("git pull") for item in flattened)
    assert not any(item.startswith("git ls-remote") for item in flattened)
    assert not any("github.com" in item for item in flattened)


def test_final_cargo_deny_failure_is_redacted_and_no_success(tmp_path: Path) -> None:
    runner = HybridRunner(
        final_exit=1,
        final_stderr_extra=(
            "/private/path TOKEN=abc ghp_abcdefghijklmnopqrst localhost "
            "ssh://mirror.example.invalid/rustsec-advisory-db.git"
        ),
    )

    with pytest.raises(audit.ReleasePolicyError) as exc:
        _invoke_green(tmp_path, runner=runner)
    text = "\n".join(failure.actual for failure in exc.value.failures)
    assert "mirror.example.invalid" not in text
    assert "/private/path" not in text
    assert "TOKEN=" not in text
    assert "ghp_" not in text


def test_child_output_redaction_masks_locator_temp_path_and_token_canaries() -> None:
    redacted = audit._redact_child_output(
        "secret /tmp/private TOKEN=value ghp_abcdefghijklmnopqrst host.local",
        secrets={"/tmp/private"},
    )

    assert "/tmp/private" not in redacted
    assert "TOKEN=" not in redacted
    assert "ghp_" not in redacted
    assert audit.validate_public_evidence_text("child-output", redacted) == []


def test_cleanup_failure_suppresses_success_and_combines_errors(tmp_path: Path) -> None:
    def fail_cleanup(_path: Path) -> None:
        raise OSError("cleanup failed")

    root = _audit_root(tmp_path / "cleanup")
    _repo, commit, bundle = _advisory_repo(tmp_path / "cleanup")
    receipt, _signature, pubkey, pubkey_sha, fake = _write_packet(
        tmp_path / "cleanup", commit=commit
    )
    with pytest.raises(audit.ReleasePolicyError) as cleanup_exc:
        audit.audit_advisory_mirror(
            root,
            bundle=bundle,
            receipt=receipt,
            pubkey=pubkey,
            locator="ssh://mirror.example.invalid/rustsec-advisory-db.git",
            runner=HybridRunner(),
            verifier=fake,
            clock=lambda: NOW,
            cleanup_rmdir=fail_cleanup,
            pinned_key_id=TEST_KEY_ID,
            pinned_pubkey_sha256=pubkey_sha,
        )
    assert any(
        "cleanup failed" in failure.error for failure in cleanup_exc.value.failures
    )


def test_cleanup_failure_combines_with_primary_error(tmp_path: Path) -> None:
    def fail_cleanup(_path: Path) -> None:
        raise OSError("cleanup failed")

    root = _audit_root(tmp_path)
    _repo, commit, bundle = _advisory_repo(tmp_path)
    receipt, _signature, pubkey, pubkey_sha, fake = _write_packet(
        tmp_path, commit=commit
    )

    with pytest.raises(audit.ReleasePolicyError) as exc:
        audit.audit_advisory_mirror(
            root,
            bundle=bundle,
            receipt=receipt,
            pubkey=pubkey,
            locator="ssh://mirror.example.invalid/rustsec-advisory-db.git",
            runner=HybridRunner(final_exit=1),
            verifier=fake,
            clock=lambda: NOW,
            cleanup_rmdir=fail_cleanup,
            pinned_key_id=TEST_KEY_ID,
            pinned_pubkey_sha256=pubkey_sha,
        )
    errors = [failure.error for failure in exc.value.failures]
    assert "advisory mirror cargo-deny final check failed" in errors
    assert any("cleanup failed" in error for error in errors)


def test_exact_success_schema_and_witness_binding(tmp_path: Path) -> None:
    output, _runner, root, _bundle, _receipt, _pubkey = _invoke_green(tmp_path)
    payload = json.loads(output)

    assert payload == {
        "product": "solstone-journal",
        "advisory_cohort": audit.ADVISORY_COHORT_ID,
        "synced_commit": payload["synced_commit"],
        "receipt_utc": RECEIPT_UTC,
        "max_age": 86400,
        "checked_at": "2026-07-24T12:00:00Z",
        "cargo_lock_sha256": hashlib.sha256(
            (root / "core" / "Cargo.lock").read_bytes()
        ).hexdigest(),
        "cargo_deny_version": "0.20.2",
        "verdict": "pass",
    }
    serialized = json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n"
    assert output == serialized
    assert b"mirror.example.invalid" not in output


def test_success_inventory_is_non_destructive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _audit_root(tmp_path)
    packet_root = tmp_path / "packet"
    packet_root.mkdir()
    cargo_home = tmp_path / "cargo-home"
    cargo_home.mkdir()
    (cargo_home / "preserve.txt").write_text("ambient cargo\n", encoding="utf-8")
    monkeypatch.setenv("CARGO_HOME", str(cargo_home))
    temp_root = tmp_path / "audit-temp"
    _repo, commit, bundle = _advisory_repo(packet_root)
    receipt, _signature, pubkey, pubkey_sha, fake = _write_packet(
        packet_root, commit=commit
    )
    before_root = _inventory(root)
    before_packet = _inventory(packet_root)
    before_cargo = _inventory(cargo_home)

    audit.audit_advisory_mirror(
        root,
        bundle=bundle,
        receipt=receipt,
        pubkey=pubkey,
        locator="ssh://mirror.example.invalid/rustsec-advisory-db.git",
        runner=HybridRunner(),
        verifier=fake,
        clock=lambda: NOW,
        temp_path_factory=lambda _label: temp_root,
        pinned_key_id=TEST_KEY_ID,
        pinned_pubkey_sha256=pubkey_sha,
    )

    assert _inventory(root) == before_root
    assert _inventory(packet_root) == before_packet
    assert _inventory(cargo_home) == before_cargo
    assert not temp_root.exists()


@pytest.mark.parametrize(
    "stage",
    ["input", "locator", "pubkey", "signature", "time", "bundle", "cargo", "cleanup"],
)
def test_failure_inventory_is_non_destructive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    root = _audit_root(tmp_path)
    packet_root = tmp_path / "packet"
    packet_root.mkdir()
    cargo_home = tmp_path / "cargo-home"
    cargo_home.mkdir()
    (cargo_home / "preserve.txt").write_text("ambient cargo\n", encoding="utf-8")
    monkeypatch.setenv("CARGO_HOME", str(cargo_home))
    temp_root = tmp_path / "audit-temp"
    _repo, commit, bundle = _advisory_repo(packet_root)
    receipt, _signature, pubkey, pubkey_sha, fake = _write_packet(
        packet_root, commit=commit
    )
    locator = "ssh://mirror.example.invalid/rustsec-advisory-db.git"
    runner: object = HybridRunner(final_exit=1)
    pinned_pubkey_sha = pubkey_sha
    cleanup_rmdir = audit._remove_tree
    if stage == "input":
        bundle = packet_root / "missing.bundle"
        runner = NoCallRunner()
    elif stage == "locator":
        locator = ""
        runner = NoCallRunner()
    elif stage == "pubkey":
        pinned_pubkey_sha = "0" * 64
        runner = NoCallRunner()
    elif stage == "signature":
        (receipt.parent / f"{receipt.name}.minisig").write_text(
            "bad signature\n",
            encoding="utf-8",
        )
        runner = NoCallRunner()
    elif stage == "time":
        utc = "2026-07-24T12:06:00Z"
        receipt.write_bytes(_receipt_bytes(commit, utc))
        fake.sign_file(
            receipt,
            receipt.parent / f"{receipt.name}.minisig",
            trusted_comment=_trusted_comment(commit, utc),
        )
        runner = NoCallRunner()
    elif stage == "bundle":
        runner = FakeGitRunner(heads_commit=commit, fail="bundle verify")
    elif stage == "cleanup":

        def fail_cleanup(_path: Path) -> None:
            raise OSError("cleanup failed")

        runner = HybridRunner()
        cleanup_rmdir = fail_cleanup
    before_root = _inventory(root)
    before_packet = _inventory(packet_root)
    before_cargo = _inventory(cargo_home)

    with pytest.raises(audit.ReleasePolicyError):
        audit.audit_advisory_mirror(
            root,
            bundle=bundle,
            receipt=receipt,
            pubkey=pubkey,
            locator=locator,
            runner=runner,
            verifier=fake,
            clock=lambda: NOW,
            temp_path_factory=lambda _label: temp_root,
            cleanup_rmdir=cleanup_rmdir,
            pinned_key_id=TEST_KEY_ID,
            pinned_pubkey_sha256=pinned_pubkey_sha,
        )

    assert _inventory(root) == before_root
    assert _inventory(packet_root) == before_packet
    assert _inventory(cargo_home) == before_cargo
    if stage == "cleanup":
        assert temp_root.exists()
    else:
        assert not temp_root.exists()


def test_audit_config_bytes_omits_fetch_head_and_staleness_fields(
    tmp_path: Path,
) -> None:
    cfg = audit.audit_config_bytes(
        b'[licenses]\nallow = ["MIT"]\n',
        db_root=tmp_path / "db-root",
        db_urls=("ssh://mirror.example.invalid/rustsec-advisory-db.git",),
    ).decode("utf-8")

    assert "git-fetch-with-cli" not in cfg
    assert "maximum-db-staleness" not in cfg
    assert "[advisories]" in cfg
    with pytest.raises(audit.ReleasePolicyError):
        audit.audit_config_bytes(
            b'[advisories]\ndb-path = "x"\n',
            db_root=tmp_path / "db-root",
            db_urls=("ssh://mirror.example.invalid/rustsec-advisory-db.git",),
        )


def test_main_prints_success_bytes_only_on_success(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        audit, "audit_advisory_mirror", lambda *args, **kwargs: b'{"ok":1}\n'
    )

    result = audit.main(
        [
            "--bundle",
            "bundle",
            "--receipt",
            "receipt",
            "--pubkey",
            "pubkey",
            "--locator",
            "ssh://mirror.example.invalid/rustsec-advisory-db.git",
        ]
    )

    captured = capsys.readouterr()
    assert result == 0
    assert captured.out == '{"ok":1}\n'
    assert captured.err == ""


@pytest.mark.parametrize("empty_name", ["bundle", "receipt", "pubkey", "locator"])
def test_main_empty_inputs_fail_before_audit_orchestration(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    empty_name: str,
) -> None:
    calls = []

    def unexpected(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("audit should not run")

    monkeypatch.setattr(audit, "audit_advisory_mirror", unexpected)
    values = {
        "bundle": "bundle",
        "receipt": "receipt",
        "pubkey": "pubkey",
        "locator": "ssh://mirror.example.invalid/rustsec-advisory-db.git",
    }
    values[empty_name] = ""

    result = audit.main(
        [
            "--bundle",
            values["bundle"],
            "--receipt",
            values["receipt"],
            "--pubkey",
            values["pubkey"],
            "--locator",
            values["locator"],
        ]
    )

    captured = capsys.readouterr()
    assert result == 1
    assert calls == []
    assert captured.out == ""
    assert f"input {empty_name} is empty" in captured.err


def test_main_prints_redacted_failures_only_on_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail(*args, **kwargs):
        raise audit.ReleasePolicyError(
            [
                audit._failure_record(
                    "failed",
                    expected="public",
                    actual="/private/path TOKEN=value",
                )
            ]
        )

    monkeypatch.setattr(audit, "audit_advisory_mirror", fail)
    result = audit.main(
        [
            "--bundle",
            "bundle",
            "--receipt",
            "receipt",
            "--pubkey",
            "pubkey",
            "--locator",
            "ssh://mirror.example.invalid/rustsec-advisory-db.git",
        ]
    )

    captured = capsys.readouterr()
    assert result == 1
    assert captured.out == ""
    assert "/private/path" not in captured.err
    assert "TOKEN=" not in captured.err

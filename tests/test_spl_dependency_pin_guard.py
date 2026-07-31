# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc
from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

import scripts.check_spl_dependency_pin as guard

BASE_TAG = "v9.8.7-test"
SECOND_TAG = "v9.8.8-test"
BASE_COMMIT = "0123456789abcdef0123456789abcdef01234567"
OTHER_COMMIT = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

Mutator = Callable[[Path], None]
ExpectedMessage = Callable[[Path], str]


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _write(repo: Path, rel_path: str, text: str) -> None:
    path = repo / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _read(repo: Path, rel_path: str) -> str:
    return (repo / rel_path).read_text(encoding="utf-8")


def _replace(repo: Path, rel_path: str, old: str, new: str) -> None:
    path = repo / rel_path
    source = path.read_text(encoding="utf-8")
    assert old in source
    path.write_text(source.replace(old, new), encoding="utf-8")


def _append(repo: Path, rel_path: str, text: str) -> None:
    path = repo / rel_path
    source = path.read_text(encoding="utf-8")
    path.write_text(f"{source}{text}", encoding="utf-8")


def _workspace_spl_line(package: str, tag: str = BASE_TAG) -> str:
    return f'{package} = {{ git = "{guard.APPROVED_SOURCE_URL}", tag = "{tag}" }}'


def _lock_source(tag: str, commit: str) -> str:
    return f"git+{guard.APPROVED_SOURCE_URL}?tag={tag}#{commit}"


def _lock_spl_record(
    package: str, tag: str = BASE_TAG, commit: str = BASE_COMMIT
) -> str:
    return (
        "[[package]]\n"
        f'name = "{package}"\n'
        'version = "0.1.0"\n'
        f'source = "{_lock_source(tag, commit)}"\n'
        "dependencies = []\n"
        "\n"
    )


def _workspace_manifest(tag: str = BASE_TAG) -> str:
    return (
        "[workspace]\n"
        'members = ["crates/member"]\n'
        "\n"
        "[workspace.dependencies]\n"
        f"{_workspace_spl_line('spl-core', tag)}\n"
        f"{_workspace_spl_line('spl-transport', tag)}\n"
    )


def _member_manifest() -> str:
    return (
        "[package]\n"
        'name = "member"\n'
        'version = "0.1.0"\n'
        'edition = "2024"\n'
        "\n"
        "[dependencies]\n"
        "spl-core = { workspace = true }\n"
        "spl-transport = { workspace = true }\n"
    )


def _lockfile(tag: str = BASE_TAG, commit: str = BASE_COMMIT) -> str:
    return (
        "version = 4\n"
        "\n"
        "[[package]]\n"
        'name = "member"\n'
        'version = "0.1.0"\n'
        "dependencies = [\n"
        ' "spl-core",\n'
        ' "spl-transport",\n'
        "]\n"
        "\n"
        f"{_lock_spl_record('spl-core', tag, commit)}"
        f"{_lock_spl_record('spl-transport', tag, commit)}"
    )


def _write_valid_repo(
    root: Path,
    *,
    tag: str = BASE_TAG,
    commit: str = BASE_COMMIT,
) -> Path:
    repo = root / "repo"
    repo.mkdir()
    _git(repo, "init")
    _write(repo, "core/Cargo.toml", _workspace_manifest(tag))
    _write(repo, "core/crates/member/Cargo.toml", _member_manifest())
    _write(repo, "core/Cargo.lock", _lockfile(tag, commit))
    _git(repo, "add", ".")
    return repo


def _run_guard(repo: Path) -> int:
    return guard.main(["--root", str(repo)])


def _expect_failure(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    mutator: Mutator,
    expected_message: str,
) -> None:
    repo = _write_valid_repo(tmp_path)
    mutator(repo)

    exit_code = _run_guard(repo)

    captured = capsys.readouterr()
    assert exit_code == 1
    assert expected_message in captured.err


def test_valid_repo_passes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = _write_valid_repo(tmp_path)

    exit_code = _run_guard(repo)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.err == ""


def test_valid_repo_passes_with_derived_second_tag(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _write_valid_repo(tmp_path, tag=SECOND_TAG, commit=BASE_COMMIT)

    exit_code = _run_guard(repo)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.err == ""


def test_python_spl_surfaces_do_not_trip_in_tree_package_copy(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _write_valid_repo(tmp_path)
    _write(repo, "solstone/think/spl/foo.py", "VALUE = 1\n")
    _write(repo, "tests/spl/bar.py", "VALUE = 1\n")
    _write(repo, "docs/design/spl-fixture.md", "# SPL fixture\n")
    _git(repo, "add", ".")

    exit_code = _run_guard(repo)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.err == ""


@pytest.mark.parametrize(
    ("mutator", "expected"),
    [
        (
            lambda repo: _replace(
                repo,
                "core/Cargo.toml",
                f"{_workspace_spl_line('spl-core')}\n",
                "",
            ),
            lambda _repo: guard.W001_WORKSPACE_DEPENDENCY_MISSING.format(
                package="spl-core"
            ),
        ),
        (
            lambda repo: _replace(
                repo,
                "core/Cargo.toml",
                _workspace_spl_line("spl-core"),
                'spl-core = "0.1.0"',
            ),
            lambda _repo: guard.W002_WORKSPACE_DEPENDENCY_TABLE.format(
                package="spl-core"
            ),
        ),
        (
            lambda repo: _replace(
                repo,
                "core/Cargo.toml",
                guard.APPROVED_SOURCE_URL,
                "https://example.invalid/spl-rust",
            ),
            lambda _repo: guard.W003_WORKSPACE_SOURCE_URL.format(
                package="spl-core",
                found="https://example.invalid/spl-rust",
            ),
        ),
        (
            lambda repo: _replace(
                repo,
                "core/Cargo.toml",
                _workspace_spl_line("spl-core"),
                (
                    f'spl-core = {{ git = "{guard.APPROVED_SOURCE_URL}", '
                    f'rev = "{BASE_COMMIT}" }}'
                ),
            ),
            lambda _repo: guard.W004_WORKSPACE_SELECTOR_TAG_ONLY.format(
                package="spl-core",
                keys="missing tag, rev",
            ),
        ),
        (
            lambda repo: _replace(
                repo,
                "core/Cargo.toml",
                _workspace_spl_line("spl-core"),
                f'spl-core = {{ git = "{guard.APPROVED_SOURCE_URL}", tag = "" }}',
            ),
            lambda _repo: guard.W005_WORKSPACE_TAG_EMPTY.format(package="spl-core"),
        ),
        (
            lambda repo: _replace(
                repo,
                "core/Cargo.toml",
                _workspace_spl_line("spl-transport"),
                _workspace_spl_line("spl-transport", SECOND_TAG),
            ),
            lambda _repo: guard.W006_WORKSPACE_TAGS_SPLIT,
        ),
        (
            lambda repo: _append(
                repo,
                "core/Cargo.toml",
                (
                    f'myspl = {{ package = "spl-core", '
                    f'git = "{guard.APPROVED_SOURCE_URL}", tag = "{BASE_TAG}" }}\n'
                ),
            ),
            lambda _repo: guard.W007_WORKSPACE_ALIAS.format(
                dependency="myspl",
                package="spl-core",
            ),
        ),
        (
            lambda repo: _replace(
                repo,
                "core/Cargo.toml",
                _workspace_spl_line("spl-core"),
                (
                    f'spl-core = {{ package = "other", '
                    f'git = "{guard.APPROVED_SOURCE_URL}", tag = "{BASE_TAG}" }}'
                ),
            ),
            lambda _repo: guard.W007_WORKSPACE_ALIAS.format(
                dependency="spl-core",
                package="other",
            ),
        ),
    ],
)
def test_workspace_properties_fail(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    mutator: Mutator,
    expected: ExpectedMessage,
) -> None:
    _expect_failure(tmp_path, capsys, mutator, expected(tmp_path))


def test_absent_workspace_tag_reports_w004_without_w005(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _write_valid_repo(tmp_path)
    _replace(
        repo,
        "core/Cargo.toml",
        _workspace_spl_line("spl-core"),
        f'spl-core = {{ git = "{guard.APPROVED_SOURCE_URL}", rev = "{BASE_COMMIT}" }}',
    )

    exit_code = _run_guard(repo)

    captured = capsys.readouterr()
    assert exit_code == 1
    assert (
        guard.W004_WORKSPACE_SELECTOR_TAG_ONLY.format(
            package="spl-core",
            keys="missing tag, rev",
        )
        in captured.err
    )
    assert guard.W005_WORKSPACE_TAG_EMPTY.format(package="spl-core") not in captured.err


@pytest.mark.parametrize(
    ("mutator", "expected"),
    [
        (
            lambda repo: _replace(
                repo,
                "core/crates/member/Cargo.toml",
                "spl-core = { workspace = true }",
                'spl-core = "0.1.0"',
            ),
            lambda _repo: guard.M001_MEMBER_OVERRIDE.format(
                manifest="core/crates/member/Cargo.toml",
                table="dependencies",
                dependency="spl-core",
                package="spl-core",
                keys="version",
            ),
        ),
        (
            lambda repo: _append(
                repo,
                "core/crates/member/Cargo.toml",
                (
                    f'\nmyspl = {{ package = "spl-core", '
                    f'git = "{guard.APPROVED_SOURCE_URL}", tag = "{BASE_TAG}" }}\n'
                ),
            ),
            lambda _repo: guard.M001_MEMBER_OVERRIDE.format(
                manifest="core/crates/member/Cargo.toml",
                table="dependencies",
                dependency="myspl",
                package="spl-core",
                keys="git, tag",
            ),
        ),
        (
            lambda repo: _append(
                repo,
                "core/crates/member/Cargo.toml",
                "\n[target.'cfg(unix)'.build-dependencies]\n"
                'spl-transport = { path = "../spl-transport" }\n',
            ),
            lambda _repo: guard.M001_MEMBER_OVERRIDE.format(
                manifest="core/crates/member/Cargo.toml",
                table="target.cfg(unix).build-dependencies",
                dependency="spl-transport",
                package="spl-transport",
                keys="path",
            ),
        ),
    ],
)
def test_member_properties_fail(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    mutator: Mutator,
    expected: ExpectedMessage,
) -> None:
    _expect_failure(tmp_path, capsys, mutator, expected(tmp_path))


@pytest.mark.parametrize(
    ("mutator", "expected"),
    [
        (
            lambda repo: _replace(
                repo,
                "core/Cargo.lock",
                _lock_spl_record("spl-core"),
                "",
            ),
            lambda _repo: guard.L001_LOCK_PACKAGE_MISSING.format(package="spl-core"),
        ),
        (
            lambda repo: _append(repo, "core/Cargo.lock", _lock_spl_record("spl-core")),
            lambda _repo: guard.L002_LOCK_PACKAGE_DUPLICATED.format(
                package="spl-core",
                count=2,
            ),
        ),
        (
            lambda repo: _replace(
                repo,
                "core/Cargo.lock",
                f'source = "{_lock_source(BASE_TAG, BASE_COMMIT)}"\n',
                "",
            ),
            lambda _repo: guard.L003_LOCK_SOURCE_MISSING.format(package="spl-core"),
        ),
        (
            lambda repo: _replace(
                repo,
                "core/Cargo.lock",
                _lock_source(BASE_TAG, BASE_COMMIT),
                "registry+https://github.com/rust-lang/crates.io-index",
            ),
            lambda _repo: guard.L004_LOCK_SOURCE_NOT_GIT.format(
                package="spl-core",
                source="registry+https://github.com/rust-lang/crates.io-index",
            ),
        ),
        (
            lambda repo: _replace(
                repo,
                "core/Cargo.lock",
                guard.APPROVED_SOURCE_URL,
                "https://example.invalid/spl-rust",
            ),
            lambda _repo: guard.L005_LOCK_GIT_URL.format(
                package="spl-core",
                url="https://example.invalid/spl-rust",
            ),
        ),
        (
            lambda repo: _replace(
                repo,
                "core/Cargo.lock",
                f"?tag={BASE_TAG}",
                f"?rev={BASE_COMMIT}",
            ),
            lambda _repo: guard.L006_LOCK_SELECTOR_TAG.format(
                package="spl-core",
                selector="rev",
            ),
        ),
        (
            lambda repo: _replace(
                repo,
                "core/Cargo.lock",
                f"?tag={BASE_TAG}",
                f"?tag={SECOND_TAG}",
            ),
            lambda _repo: guard.L007_LOCK_TAG_WORKSPACE.format(
                package="spl-core",
                lock_tag=SECOND_TAG,
            ),
        ),
        (
            lambda repo: _replace(
                repo,
                "core/Cargo.lock",
                BASE_COMMIT,
                BASE_COMMIT[:-1],
            ),
            lambda _repo: guard.L008_LOCK_COMMIT_INVALID.format(
                package="spl-core",
                commit=BASE_COMMIT[:-1],
            ),
        ),
        (
            lambda repo: _replace(
                repo,
                "core/Cargo.lock",
                _lock_spl_record("spl-transport"),
                _lock_spl_record("spl-transport", BASE_TAG, OTHER_COMMIT),
            ),
            lambda _repo: guard.L009_LOCK_COMMITS_SPLIT,
        ),
    ],
)
def test_lockfile_properties_fail(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    mutator: Mutator,
    expected: ExpectedMessage,
) -> None:
    _expect_failure(tmp_path, capsys, mutator, expected(tmp_path))


@pytest.mark.parametrize(
    ("mutator", "expected"),
    [
        (
            lambda repo: _append(
                repo,
                "core/Cargo.toml",
                f'\n[patch."{guard.APPROVED_SOURCE_URL}"]\n',
            ),
            lambda _repo: guard.R001_WORKSPACE_PATCH_SOURCE.format(
                source=guard.APPROVED_SOURCE_URL
            ),
        ),
        (
            lambda repo: _append(
                repo,
                "core/Cargo.toml",
                '\n[patch.crates-io]\nspl-core = { path = "../spl-core" }\n',
            ),
            lambda _repo: guard.R002_WORKSPACE_PATCH_PACKAGE.format(
                source="crates-io",
                dependency="spl-core",
                package="spl-core",
            ),
        ),
        (
            lambda repo: _append(
                repo,
                "core/Cargo.toml",
                '\n[replace]\n"spl-core:0.1.0" = { path = "../spl-core" }\n',
            ),
            lambda _repo: guard.R003_REPLACE_PACKAGE.format(
                replace_key="spl-core:0.1.0",
                package="spl-core",
            ),
        ),
        (
            lambda repo: _write(
                repo,
                ".cargo/config.toml",
                (
                    "[source.spl-rust]\n"
                    f'git = "{guard.APPROVED_SOURCE_URL}"\n'
                    'replace-with = "local-spl"\n'
                    "\n"
                    "[source.local-spl]\n"
                    'directory = "vendor"\n'
                ),
            ),
            lambda _repo: guard.R004_CONFIG_SOURCE_REPLACEMENT.format(
                config_path=".cargo/config.toml",
                source_name="spl-rust",
                keys="replace-with",
            ),
        ),
        (
            lambda repo: _write(
                repo,
                "core/.cargo/config",
                f'[patch."{guard.APPROVED_SOURCE_URL}"]\n',
            ),
            lambda _repo: guard.R005_CONFIG_PATCH_SOURCE.format(
                config_path="core/.cargo/config",
                source=guard.APPROVED_SOURCE_URL,
            ),
        ),
        (
            lambda repo: _write(
                repo,
                "core/.cargo/config.toml",
                (
                    "[patch.crates-io]\n"
                    'myspl = { package = "spl-transport", '
                    'path = "vendor/spl-transport" }\n'
                ),
            ),
            lambda _repo: guard.R006_CONFIG_PATCH_PACKAGE.format(
                config_path="core/.cargo/config.toml",
                source="crates-io",
                dependency="myspl",
                package="spl-transport",
            ),
        ),
        (
            lambda repo: (
                _write(
                    repo,
                    "vendor/spl-core/Cargo.toml",
                    '[package]\nname = "spl-core"\nversion = "0.1.0"\n',
                ),
                _git(repo, "add", "."),
            ),
            lambda _repo: guard.R007_IN_TREE_PACKAGE_COPY.format(
                manifest="vendor/spl-core/Cargo.toml",
                package="spl-core",
            ),
        ),
    ],
)
def test_local_route_properties_fail(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    mutator: Mutator,
    expected: ExpectedMessage,
) -> None:
    _expect_failure(tmp_path, capsys, mutator, expected(tmp_path))


def test_core_unlisted_member_manifest_is_still_checked(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _write_valid_repo(tmp_path)
    _write(
        repo,
        "core/crates/unlisted/Cargo.toml",
        (
            "[package]\n"
            'name = "unlisted"\n'
            'version = "0.1.0"\n'
            "\n"
            "[dependencies]\n"
            'spl-core = "0.1.0"\n'
        ),
    )
    _git(repo, "add", ".")

    exit_code = _run_guard(repo)

    captured = capsys.readouterr()
    assert exit_code == 1
    assert (
        guard.M001_MEMBER_OVERRIDE.format(
            manifest="core/crates/unlisted/Cargo.toml",
            table="dependencies",
            dependency="spl-core",
            package="spl-core",
            keys="version",
        )
        in captured.err
    )


def test_config_source_name_url_replacement_is_rejected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _write_valid_repo(tmp_path)
    _write(
        repo,
        ".cargo/config",
        (
            f'[source."{guard.APPROVED_SOURCE_URL}"]\n'
            'registry = "sparse+https://example.invalid/index"\n'
        ),
    )

    exit_code = _run_guard(repo)

    captured = capsys.readouterr()
    assert exit_code == 1
    assert (
        guard.R004_CONFIG_SOURCE_REPLACEMENT.format(
            config_path=".cargo/config",
            source_name=guard.APPROVED_SOURCE_URL,
            keys="registry",
        )
        in captured.err
    )

# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts import install_speakers_analyze_helper as installer


def _write_leaf_pyproject(root: Path, dependencies: list[str]) -> None:
    leaf = root / "packages" / "solstone-journal"
    leaf.mkdir(parents=True)
    deps = "\n".join(f'    "{dependency}",' for dependency in dependencies)
    (leaf / "pyproject.toml").write_text(
        f"[project]\ndependencies = [\n{deps}\n]\n",
        encoding="utf-8",
    )


def _venv_python(root: Path) -> Path:
    return root / ".venv" / "bin" / "python"


def _linux_x86_64_env() -> dict[str, str]:
    return {"sys_platform": "linux", "platform_machine": "x86_64"}


def _covered_pin(version: str = "7.8.9") -> str:
    return (
        f"{installer.HELPER_DIST_NAME}=={version}; "
        "sys_platform == 'linux' and platform_machine == 'x86_64'"
    )


def test_derive_helper_pin_rejects_multiple_distinct_versions() -> None:
    dependencies = [
        _covered_pin("7.8.9"),
        (
            f"{installer.HELPER_DIST_NAME}==8.0.0; "
            "sys_platform == 'darwin' and platform_machine == 'arm64'"
        ),
    ]

    with pytest.raises(installer.SpeakersAnalyzeHelperInstallError) as exc_info:
        installer.derive_helper_pin(dependencies)

    message = str(exc_info.value)
    assert "exactly one name==version" in message
    assert f"{installer.HELPER_DIST_NAME}==7.8.9" in message
    assert f"{installer.HELPER_DIST_NAME}==8.0.0" in message


def test_derive_helper_pin_rejects_zero_helper_pins() -> None:
    with pytest.raises(
        installer.SpeakersAnalyzeHelperInstallError,
        match=f"must contain {installer.HELPER_DIST_NAME}; found none",
    ):
        installer.derive_helper_pin(["onnxruntime>=1.25.0"])


def test_derive_helper_pin_reports_invalid_requirement() -> None:
    raw = f"{installer.HELPER_DIST_NAME} =="

    with pytest.raises(installer.SpeakersAnalyzeHelperInstallError) as exc_info:
        installer.derive_helper_pin([raw])

    message = str(exc_info.value)
    assert "project.dependencies contains invalid requirement" in message
    assert repr(raw) in message


def test_read_project_dependencies_reports_missing_dependencies(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "fixture"\n', encoding="utf-8")

    with pytest.raises(installer.SpeakersAnalyzeHelperInstallError) as exc_info:
        installer.read_project_dependencies(pyproject)

    message = str(exc_info.value)
    assert "project.dependencies must be a list of strings" in message
    assert "found NoneType: None" in message


def test_read_project_dependencies_reports_non_string_entry(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\ndependencies = ["onnxruntime>=1.25.0", 42]\n',
        encoding="utf-8",
    )

    with pytest.raises(installer.SpeakersAnalyzeHelperInstallError) as exc_info:
        installer.read_project_dependencies(pyproject)

    message = str(exc_info.value)
    assert "project.dependencies must be a list of strings" in message
    assert "found non-string entry int: 42" in message


def test_uncovered_environment_skips_without_uv(tmp_path: Path, capsys) -> None:
    root = tmp_path / "repo"
    _write_leaf_pyproject(
        root,
        [
            (
                f"{installer.HELPER_DIST_NAME}==7.8.9; "
                "sys_platform == 'darwin' and platform_machine == 'arm64'"
            )
        ],
    )

    def fail_runner(
        *_args: object, **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        raise AssertionError("uv must not run for uncovered environments")

    def fail_uv_finder(_name: str) -> str | None:
        raise AssertionError("uv must not be located for uncovered environments")

    rc = installer.run_installation(
        repo_root=root,
        running_python=_venv_python(root),
        environment=_linux_x86_64_env(),
        runner=fail_runner,
        uv_finder=fail_uv_finder,
    )

    captured = capsys.readouterr()
    assert rc == 0
    assert "not covered" in captured.out


def test_install_uses_no_config_no_deps_target_python_and_derived_pin(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    _write_leaf_pyproject(root, [_covered_pin()])
    python = _venv_python(root)
    python.parent.mkdir(parents=True)
    helper = installer.speakers_analyze_path_for_executable(python)
    helper.write_text("#!/bin/sh\n", encoding="utf-8")
    helper.chmod(0o755)
    calls: list[list[str]] = []

    def fake_runner(
        argv: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    rc = installer.run_installation(
        repo_root=root,
        running_python=python,
        environment=_linux_x86_64_env(),
        runner=fake_runner,
        uv_finder=lambda _name: "/usr/bin/uv",
        version_reader=lambda dist_name: "7.8.9",
    )

    assert rc == 0
    assert calls == [
        [
            "/usr/bin/uv",
            "pip",
            "install",
            "--no-config",
            "--no-deps",
            "--python",
            str(python),
            f"{installer.HELPER_DIST_NAME}==7.8.9",
        ]
    ]


def test_running_python_mismatch_reports_expected_and_actual(tmp_path: Path) -> None:
    target_python = _venv_python(tmp_path / "repo")
    running_python = tmp_path / "other" / "python"

    with pytest.raises(installer.SpeakersAnalyzeHelperInstallError) as exc_info:
        installer.ensure_running_target_python(running_python, target_python)

    message = str(exc_info.value)
    assert str(target_python.resolve()) in message
    assert str(running_python.resolve()) in message


def test_missing_uv_fails_loudly(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _write_leaf_pyproject(root, [_covered_pin()])
    python = _venv_python(root)

    with pytest.raises(
        installer.SpeakersAnalyzeHelperInstallError,
        match="uv not found on PATH",
    ):
        installer.run_installation(
            repo_root=root,
            running_python=python,
            environment=_linux_x86_64_env(),
            uv_finder=lambda _name: None,
        )


def test_assert_helper_installed_reports_version_mismatch(tmp_path: Path) -> None:
    with pytest.raises(
        installer.SpeakersAnalyzeHelperInstallError,
        match=f"{installer.HELPER_DIST_NAME} is 0.0.1 but expected 7.8.9",
    ):
        installer.assert_helper_installed(
            f"{installer.HELPER_DIST_NAME}==7.8.9",
            python=_venv_python(tmp_path),
            version_reader=lambda _dist_name: "0.0.1",
        )


def test_assert_helper_installed_reports_invalid_pin_requirement(
    tmp_path: Path,
) -> None:
    pin = f"{installer.HELPER_DIST_NAME} =="

    with pytest.raises(installer.SpeakersAnalyzeHelperInstallError) as exc_info:
        installer.assert_helper_installed(
            pin,
            python=_venv_python(tmp_path),
            version_reader=lambda _dist_name: "7.8.9",
        )

    message = str(exc_info.value)
    assert "installed helper pin contains invalid requirement" in message
    assert repr(pin) in message


def test_assert_helper_installed_reports_missing_binary(tmp_path: Path) -> None:
    with pytest.raises(
        installer.SpeakersAnalyzeHelperInstallError,
        match="missing executable",
    ):
        installer.assert_helper_installed(
            f"{installer.HELPER_DIST_NAME}==7.8.9",
            python=_venv_python(tmp_path),
            version_reader=lambda _dist_name: "7.8.9",
        )


def test_assert_helper_installed_reports_non_executable_binary(tmp_path: Path) -> None:
    python = _venv_python(tmp_path)
    python.parent.mkdir(parents=True)
    helper = installer.speakers_analyze_path_for_executable(python)
    helper.write_text("#!/bin/sh\n", encoding="utf-8")
    helper.chmod(0o644)

    with pytest.raises(
        installer.SpeakersAnalyzeHelperInstallError,
        match="executable is not executable",
    ):
        installer.assert_helper_installed(
            f"{installer.HELPER_DIST_NAME}==7.8.9",
            python=python,
            version_reader=lambda _dist_name: "7.8.9",
            executable_predicate=lambda _path: False,
        )

# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import scripts.release_candidate_driver as driver
from scripts.stage_speakers_analyze_runtime import (
    DEFAULT_LINK_ROOT as STAGE_DEFAULT_LINK_ROOT,
)
from scripts.stage_speakers_analyze_runtime import ROOT as STAGE_ROOT
from scripts.stage_speakers_analyze_runtime import TARGETS as STAGE_TARGETS

REPO_ROOT = Path(__file__).resolve().parents[1]
MAKEFILE = REPO_ROOT / "Makefile"
STAGE_SCRIPT_ARGV = ("python3", "scripts/stage_speakers_analyze_runtime.py")
ORT_LIB_PATH_KEY = "ORT_LIB_PATH"
ORT_PREFER_DYNAMIC_LINK_KEY = "ORT_PREFER_DYNAMIC_LINK"
ORT_PREFER_DYNAMIC_LINK_VALUE = "true"

MAKEFILE_MATURIN_ARG_RE = re.compile(
    r"^(?P<var>SPEAKERS_ANALYZE_LINUX_[A-Z0-9_]+_MATURIN_ARGS) := "
    r"(?P<args>.+)$",
    re.MULTILINE,
)
MAKEFILE_HELPER_TARGET_RE = re.compile(
    r"^(?P<make_target>wheel-speakers-analyze-linux-[A-Za-z0-9_]+):\n"
    r"(?P<body>(?:\t[^\n]*\n)+)",
    re.MULTILINE,
)
MAKEFILE_STAGE_RE = re.compile(
    r"^\tpython3 scripts/stage_speakers_analyze_runtime.py --target "
    r"(?P<target>\S+)$",
    re.MULTILINE,
)
MAKEFILE_BUILD_RE = re.compile(
    r"^\t(?P<dynamic_key>[A-Z0-9_]+)=(?P<dynamic_value>\S+) "
    r"(?P<lib_key>[A-Z0-9_]+)=\"(?P<lib_path>\$\(CURDIR\)/[^\"]+)\" "
    r"MATURIN_PEP517_ARGS=\"\$\((?P<maturin_var>"
    r"SPEAKERS_ANALYZE_LINUX_[A-Z0-9_]+_MATURIN_ARGS)\)\" "
    r"\$\(UV\) build --package solstone-core-speakers-analyze --wheel$",
    re.MULTILINE,
)


@dataclass(frozen=True)
class MakefileHelperRecipe:
    make_target: str
    stage_target: str
    dynamic_key: str
    dynamic_value: str
    lib_key: str
    lib_path: str
    maturin_var: str
    maturin_args: str


def _maturin_args_by_var(makefile: str) -> dict[str, str]:
    return {
        match.group("var"): match.group("args").strip()
        for match in MAKEFILE_MATURIN_ARG_RE.finditer(makefile)
    }


def _parse_makefile_helper_recipes(makefile: str) -> dict[str, MakefileHelperRecipe]:
    maturin_args = _maturin_args_by_var(makefile)
    recipes: dict[str, MakefileHelperRecipe] = {}
    for target_match in MAKEFILE_HELPER_TARGET_RE.finditer(makefile):
        body = target_match.group("body")
        stage_match = MAKEFILE_STAGE_RE.search(body)
        assert stage_match is not None, (
            f"{target_match.group('make_target')} missing runtime staging command"
        )
        build_match = MAKEFILE_BUILD_RE.search(body)
        assert build_match is not None, (
            f"{target_match.group('make_target')} helper build recipe did not match "
            "the ORT linkage contract"
        )
        maturin_var = build_match.group("maturin_var")
        assert maturin_var in maturin_args, (
            f"{target_match.group('make_target')} references undefined {maturin_var}"
        )
        stage_target = stage_match.group("target")
        assert stage_target not in recipes, f"duplicate helper target {stage_target}"
        recipes[stage_target] = MakefileHelperRecipe(
            make_target=target_match.group("make_target"),
            stage_target=stage_target,
            dynamic_key=build_match.group("dynamic_key"),
            dynamic_value=build_match.group("dynamic_value"),
            lib_key=build_match.group("lib_key"),
            lib_path=build_match.group("lib_path"),
            maturin_var=maturin_var,
            maturin_args=maturin_args[maturin_var],
        )
    return recipes


def _driver_helper_build_envs(
    root: Path,
) -> dict[str, tuple[tuple[str, ...], dict[str, str]]]:
    entries = driver._expected_local_build_commands(
        include_models=False,
        version=driver._project_version(root),
    )
    helper_envs: dict[str, tuple[tuple[str, ...], dict[str, str]]] = {}
    pending_stage_target: str | None = None
    for index, entry in enumerate(entries):
        assert len(entry) == 3, (
            f"release build plan entry {index} must be (argv, maturin_args, ort_target)"
        )
        argv, maturin_args, ort_target = entry
        assert isinstance(argv, tuple)
        assert isinstance(maturin_args, str)
        if argv[:2] == STAGE_SCRIPT_ARGV:
            try:
                target_index = argv.index("--target") + 1
                pending_stage_target = argv[target_index]
            except (ValueError, IndexError) as exc:
                raise AssertionError(f"stage argv is missing --target: {argv}") from exc
        if ort_target is None:
            continue
        assert isinstance(ort_target, str)
        assert pending_stage_target == ort_target
        helper_envs[ort_target] = (
            argv,
            driver._scrubbed_build_env(root, maturin_args, ort_target),
        )
        pending_stage_target = None
    return helper_envs


def _ort_env_keys(env: dict[str, str]) -> set[str]:
    return {key for key in env if key.startswith("ORT_")}


def test_release_driver_helper_ort_env_matches_makefile_and_staging() -> None:
    makefile = MAKEFILE.read_text(encoding="utf-8")
    recipes = _parse_makefile_helper_recipes(makefile)
    helper_envs = _driver_helper_build_envs(REPO_ROOT)
    link_root_relative = STAGE_DEFAULT_LINK_ROOT.relative_to(STAGE_ROOT)

    assert len(recipes) == 2
    assert set(helper_envs) == set(recipes)
    assert driver.SPEAKERS_ANALYZE_LINK_ROOT_RELATIVE == link_root_relative

    for target, recipe in sorted(recipes.items()):
        assert target in STAGE_TARGETS
        spec = STAGE_TARGETS[target]
        assert spec.key == target
        expected_make_lib_path = f"$(CURDIR)/{link_root_relative.as_posix()}/{spec.key}"

        assert recipe.dynamic_key == ORT_PREFER_DYNAMIC_LINK_KEY
        assert recipe.dynamic_value == ORT_PREFER_DYNAMIC_LINK_VALUE
        assert recipe.lib_key == ORT_LIB_PATH_KEY
        assert recipe.lib_path == expected_make_lib_path

        _argv, env = helper_envs[target]
        expected_driver_link_path = str(
            (REPO_ROOT / link_root_relative / spec.key).resolve()
        )
        assert _ort_env_keys(env) == {ORT_LIB_PATH_KEY, ORT_PREFER_DYNAMIC_LINK_KEY}
        assert env[ORT_PREFER_DYNAMIC_LINK_KEY] == recipe.dynamic_value
        assert env[ORT_LIB_PATH_KEY] == expected_driver_link_path
        assert (
            Path(env[ORT_LIB_PATH_KEY])
            == (STAGE_DEFAULT_LINK_ROOT / spec.key).resolve()
        )
        assert env["MATURIN_PEP517_ARGS"] == recipe.maturin_args

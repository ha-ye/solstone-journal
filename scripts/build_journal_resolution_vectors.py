#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Build cross-language journal path resolution vectors."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from solstone.think import utils as think_utils

ROOT = Path(__file__).resolve().parent.parent
ARTIFACT_PATH = ROOT / "tests" / "fixtures" / "journal_path_resolution_vectors.json"
TMP_TOKEN = "$TMP"


@dataclass(frozen=True)
class CaseSpec:
    id: str
    solstone_journal: dict[str, str]
    home: dict[str, str]
    config: dict[str, str]
    checkout_root: dict[str, str]


CASES: tuple[CaseSpec, ...] = (
    CaseSpec(
        id="env_spaces_wins_unstripped",
        solstone_journal={"state": "set", "value": "   "},
        home={"state": "set", "value": f"{TMP_TOKEN}/home/env-spaces"},
        config={
            "state": "text",
            "value": f'journal = "{TMP_TOKEN}/config/env-spaces"\n',
        },
        checkout_root={"state": "present", "path": f"{TMP_TOKEN}/checkout/env-spaces"},
    ),
    CaseSpec(
        id="env_path_wins_over_config_and_source",
        solstone_journal={"state": "set", "value": f"{TMP_TOKEN}/env/path"},
        home={"state": "set", "value": f"{TMP_TOKEN}/home/env-path"},
        config={"state": "text", "value": f'journal = "{TMP_TOKEN}/config/env-path"\n'},
        checkout_root={"state": "present", "path": f"{TMP_TOKEN}/checkout/env-path"},
    ),
    CaseSpec(
        id="env_empty_falls_to_config",
        solstone_journal={"state": "set", "value": ""},
        home={"state": "set", "value": f"{TMP_TOKEN}/home/env-empty"},
        config={
            "state": "text",
            "value": f'journal = "{TMP_TOKEN}/config/env-empty"\n',
        },
        checkout_root={"state": "present", "path": f"{TMP_TOKEN}/checkout/env-empty"},
    ),
    CaseSpec(
        id="config_spaced_tilde_literal",
        solstone_journal={"state": "absent"},
        home={"state": "set", "value": f"{TMP_TOKEN}/home/config-tilde"},
        config={"state": "text", "value": 'journal = "  ~/journal  "\n'},
        checkout_root={"state": "absent", "path": f"{TMP_TOKEN}/checkout/config-tilde"},
    ),
    CaseSpec(
        id="config_python_control_whitespace_stripped",
        solstone_journal={"state": "absent"},
        home={"state": "set", "value": f"{TMP_TOKEN}/home/config-control"},
        config={
            "state": "text",
            "value": f'journal = "\\u001c{TMP_TOKEN}/config/control\\u001f"\n',
        },
        checkout_root={
            "state": "absent",
            "path": f"{TMP_TOKEN}/checkout/config-control",
        },
    ),
    CaseSpec(
        id="config_whitespace_only_falls_to_source",
        solstone_journal={"state": "absent"},
        home={"state": "set", "value": f"{TMP_TOKEN}/home/config-spaces"},
        config={"state": "text", "value": 'journal = "   "\n'},
        checkout_root={
            "state": "present",
            "path": f"{TMP_TOKEN}/checkout/config-spaces",
        },
    ),
    CaseSpec(
        id="config_non_string_falls_to_source",
        solstone_journal={"state": "absent"},
        home={"state": "set", "value": f"{TMP_TOKEN}/home/config-non-string"},
        config={"state": "text", "value": "journal = 123\n"},
        checkout_root={
            "state": "present",
            "path": f"{TMP_TOKEN}/checkout/config-non-string",
        },
    ),
    CaseSpec(
        id="config_invalid_toml_falls_to_source",
        solstone_journal={"state": "absent"},
        home={"state": "set", "value": f"{TMP_TOKEN}/home/config-invalid-toml"},
        config={"state": "text", "value": 'journal = "\n'},
        checkout_root={
            "state": "present",
            "path": f"{TMP_TOKEN}/checkout/config-invalid-toml",
        },
    ),
    CaseSpec(
        id="config_invalid_utf8_errors",
        solstone_journal={"state": "absent"},
        home={"state": "set", "value": f"{TMP_TOKEN}/home/config-invalid-utf8"},
        config={"state": "hex", "value": "ff"},
        checkout_root={
            "state": "present",
            "path": f"{TMP_TOKEN}/checkout/config-invalid-utf8",
        },
    ),
    CaseSpec(
        id="source_checkout_wins_over_default",
        solstone_journal={"state": "absent"},
        home={"state": "set", "value": f"{TMP_TOKEN}/home/source"},
        config={"state": "absent"},
        checkout_root={"state": "present", "path": f"{TMP_TOKEN}/checkout/source"},
    ),
    CaseSpec(
        id="default_normal_home",
        solstone_journal={"state": "absent"},
        home={"state": "set", "value": f"{TMP_TOKEN}/home/default"},
        config={"state": "absent"},
        checkout_root={"state": "absent", "path": f"{TMP_TOKEN}/checkout/default"},
    ),
    CaseSpec(
        id="default_home_empty",
        solstone_journal={"state": "absent"},
        home={"state": "set", "value": ""},
        config={"state": "absent"},
        checkout_root={"state": "absent", "path": f"{TMP_TOKEN}/checkout/home-empty"},
    ),
    CaseSpec(
        id="default_home_trailing_slash",
        solstone_journal={"state": "absent"},
        home={"state": "set", "value": f"{TMP_TOKEN}/home/trailing/"},
        config={"state": "absent"},
        checkout_root={
            "state": "absent",
            "path": f"{TMP_TOKEN}/checkout/home-trailing",
        },
    ),
    CaseSpec(
        id="default_home_repeated_slashes",
        solstone_journal={"state": "absent"},
        home={"state": "set", "value": f"{TMP_TOKEN}//home//repeated"},
        config={"state": "absent"},
        checkout_root={
            "state": "absent",
            "path": f"{TMP_TOKEN}/checkout/home-repeated",
        },
    ),
    CaseSpec(
        id="default_home_root_slashes",
        solstone_journal={"state": "absent"},
        home={"state": "set", "value": "///"},
        config={"state": "absent"},
        checkout_root={"state": "absent", "path": f"{TMP_TOKEN}/checkout/home-root"},
    ),
    CaseSpec(
        id="default_home_dot_component",
        solstone_journal={"state": "absent"},
        home={"state": "set", "value": f"{TMP_TOKEN}/home/./dot"},
        config={"state": "absent"},
        checkout_root={"state": "absent", "path": f"{TMP_TOKEN}/checkout/home-dot"},
    ),
    CaseSpec(
        id="default_home_double_leading_slash",
        solstone_journal={"state": "absent"},
        home={"state": "set", "value": "//server//share"},
        config={"state": "absent"},
        checkout_root={"state": "absent", "path": f"{TMP_TOKEN}/checkout/home-double"},
    ),
    CaseSpec(
        id="default_relative_home_repeated_slashes",
        solstone_journal={"state": "absent"},
        home={"state": "set", "value": "rel//home"},
        config={"state": "absent"},
        checkout_root={
            "state": "absent",
            "path": f"{TMP_TOKEN}/checkout/relative-repeated",
        },
    ),
    CaseSpec(
        id="default_relative_home_dot_component",
        solstone_journal={"state": "absent"},
        home={"state": "set", "value": "./relative"},
        config={"state": "absent"},
        checkout_root={"state": "absent", "path": f"{TMP_TOKEN}/checkout/relative-dot"},
    ),
    CaseSpec(
        id="default_relative_home_single_dot",
        solstone_journal={"state": "absent"},
        home={"state": "set", "value": "."},
        config={"state": "absent"},
        checkout_root={
            "state": "absent",
            "path": f"{TMP_TOKEN}/checkout/relative-single-dot",
        },
    ),
)


def _expand_tokens(value: str, tmp_root: Path) -> str:
    return value.replace(TMP_TOKEN, str(tmp_root))


def _tokenize(value: str, tmp_root: Path) -> str:
    return value.replace(str(tmp_root), TMP_TOKEN)


def _materialize_mapping(mapping: dict[str, str], tmp_root: Path) -> dict[str, str]:
    return {
        key: _expand_tokens(value, tmp_root) if isinstance(value, str) else value
        for key, value in mapping.items()
    }


def _tokenize_mapping(mapping: dict[str, str], tmp_root: Path) -> dict[str, str]:
    return {
        key: _tokenize(value, tmp_root) if isinstance(value, str) else value
        for key, value in mapping.items()
    }


@contextmanager
def _isolated_case_env(
    *,
    cwd: Path,
    solstone_journal: dict[str, str],
    home: dict[str, str],
    project_root: str,
) -> Iterator[None]:
    old_env = dict(os.environ)
    old_cwd = Path.cwd()
    old_get_project_root = think_utils.get_project_root
    try:
        os.chdir(cwd)
        if solstone_journal["state"] == "absent":
            os.environ.pop("SOLSTONE_JOURNAL", None)
        else:
            os.environ["SOLSTONE_JOURNAL"] = solstone_journal["value"]
        if home["state"] == "absent":
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = home["value"]
        think_utils.get_project_root = lambda: project_root
        yield
    finally:
        think_utils.get_project_root = old_get_project_root
        os.chdir(old_cwd)
        os.environ.clear()
        os.environ.update(old_env)


def _write_config(home: dict[str, str], config: dict[str, str]) -> None:
    if config["state"] == "absent":
        return
    if home["state"] != "set":
        raise RuntimeError("config vectors require HOME to be set")
    cfg = Path(home["value"]) / ".config" / "solstone" / "config.toml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    if config["state"] == "text":
        cfg.write_text(config["value"], encoding="utf-8")
    elif config["state"] == "hex":
        cfg.write_bytes(bytes.fromhex(config["value"]))
    else:
        raise RuntimeError(f"unknown config state: {config['state']}")


def _write_checkout_root(checkout_root: dict[str, str]) -> None:
    root = Path(checkout_root["path"])
    if checkout_root["state"] == "absent":
        root.mkdir(parents=True, exist_ok=True)
        return
    if checkout_root["state"] != "present":
        raise RuntimeError(f"unknown checkout_root state: {checkout_root['state']}")
    root.mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text(
        '[project]\nname = "probe"\n', encoding="utf-8"
    )
    (root / ".git").mkdir()


def _render_case(spec: CaseSpec, tmp_root: Path) -> dict[str, Any]:
    solstone_journal = _materialize_mapping(spec.solstone_journal, tmp_root)
    home = _materialize_mapping(spec.home, tmp_root)
    config = _materialize_mapping(spec.config, tmp_root)
    checkout_root = _materialize_mapping(spec.checkout_root, tmp_root)
    cwd = tmp_root / "cwd" / spec.id
    cwd.mkdir(parents=True)

    _write_config(home, config)
    _write_checkout_root(checkout_root)

    with _isolated_case_env(
        cwd=cwd,
        solstone_journal=solstone_journal,
        home=home,
        project_root=checkout_root["path"],
    ):
        try:
            path, source = think_utils.get_journal_info()
        except Exception as exc:
            outcome = {"type": "error", "python_class": exc.__class__.__name__}
        else:
            outcome = {
                "type": "ok",
                "path": _tokenize(path, tmp_root),
                "source": source,
            }

    return {
        "id": spec.id,
        "solstone_journal": _tokenize_mapping(solstone_journal, tmp_root),
        "home": _tokenize_mapping(home, tmp_root),
        "config": _tokenize_mapping(config, tmp_root),
        "checkout_root": _tokenize_mapping(checkout_root, tmp_root),
        "cwd": _tokenize(str(cwd), tmp_root),
        "outcome": outcome,
    }


def render_vectors_json() -> str:
    with tempfile.TemporaryDirectory(prefix="solstone-journal-vectors-") as tmp:
        tmp_root = Path(tmp)
        cases = [
            _render_case(spec, tmp_root)
            for spec in sorted(CASES, key=lambda case: case.id)
        ]
    payload = {
        "schema_version": 1,
        "tokens": {
            TMP_TOKEN: "Per-run temporary root substituted by generator and consumers.",
        },
        "cases": cases,
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def write_outputs() -> None:
    ARTIFACT_PATH.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT_PATH.write_text(render_vectors_json(), encoding="utf-8")
    print(f"wrote {ARTIFACT_PATH.relative_to(ROOT)}")


def check_outputs() -> int:
    expected = render_vectors_json()
    try:
        current = ARTIFACT_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        current = ""
    if current != expected:
        print(
            "Journal resolution vectors are stale: "
            f"{ARTIFACT_PATH.relative_to(ROOT)}. "
            "Run: make journal-resolution-vectors",
            file=sys.stderr,
        )
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Check generated vector output without writing it.",
    )
    args = parser.parse_args()
    if args.check:
        return check_outputs()
    write_outputs()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

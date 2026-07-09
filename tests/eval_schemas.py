#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Opt-in local structured-output schema eval harness."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from solstone.think.models import generate_with_result  # noqa: E402
from solstone.think.providers.local import LocalProviderError  # noqa: E402
from solstone.think.schema_eval import (  # noqa: E402
    content_preservation,
    schema_validity,
)
from solstone.think.talent import hydrate_runtime_enums  # noqa: E402

DEFAULT_CASES = ROOT / "tests" / "fixtures" / "schema_eval" / "cases.jsonl"
DEFAULT_OUT = ROOT / "tmp" / "schema-eval"
LOCAL_NOT_READY = (
    "Local schema eval requires the bundled local provider. Run "
    "`journal install-provider local`, then start it with `journal start` "
    "(or `journal service start` for an installed service)."
)


def _resolve_path(path: Path) -> Path:
    if path.is_absolute():
        return path
    return ROOT / path


def load_cases(path: Path) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for lineno, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            case = json.loads(stripped)
            if "schema_path" in case:
                schema_path = _resolve_path(Path(case["schema_path"]))
                schema = json.loads(schema_path.read_text(encoding="utf-8"))
                # Match runtime provider schemas; empty facet journals drop the enum.
                case["schema"] = hydrate_runtime_enums(schema)
            cases.append(case)
    return cases


def run_case(case: dict[str, Any]) -> dict[str, Any]:
    result = generate_with_result(
        contents=case["input"],
        context="schema.eval",
        provider="local",
        temperature=0.0,
        max_output_tokens=int(case.get("max_output_tokens", 512)),
        system_instruction=case["system_instruction"],
        json_output=True,
        json_schema=case["schema"],
    )
    text = result["text"]
    return {
        "name": case["name"],
        "text": text,
        "schema_validity": schema_validity(text, case["schema"]),
        "content_preservation": content_preservation(
            text, case.get("expect_contains", [])
        ),
        "finish_reason": result.get("finish_reason"),
        "model": result.get("model"),
        "usage": result.get("usage"),
    }


def _atomic_write_text(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def write_outputs(out_dir: Path, results: list[dict[str, Any]]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    jsonl = "".join(json.dumps(result, sort_keys=True) + "\n" for result in results)
    _atomic_write_text(out_dir / "results.jsonl", jsonl)

    valid_count = sum(1 for result in results if result["schema_validity"]["valid"])
    average_content = (
        sum(result["content_preservation"]["fraction"] for result in results)
        / len(results)
        if results
        else 1.0
    )
    summary = (
        f"cases: {len(results)}\n"
        f"schema_valid: {valid_count}/{len(results)}\n"
        f"average_content_preservation: {average_content:.3f}\n"
    )
    _atomic_write_text(out_dir / "summary.txt", summary)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="local structured schema eval")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)

    cases = load_cases(_resolve_path(args.cases))
    results: list[dict[str, Any]] = []
    for case in cases:
        try:
            results.append(run_case(case))
        except LocalProviderError as exc:
            if exc.reason_code == "local_model_not_ready":
                print(LOCAL_NOT_READY, file=sys.stderr)
                return 2
            raise

    out_dir = _resolve_path(args.out)
    write_outputs(out_dir, results)

    valid_count = sum(1 for result in results if result["schema_validity"]["valid"])
    average_content = (
        sum(result["content_preservation"]["fraction"] for result in results)
        / len(results)
        if results
        else 1.0
    )
    print(f"schema eval wrote {out_dir}")
    print(f"schema-valid: {valid_count}/{len(results)}")
    print(f"average content preservation: {average_content:.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import ast
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from solstone.apps.chat.copy import CHAT_CLOSER_TALENT_ERRORED_FORMAT
from solstone.convey.chat import _clean_talent_errored_reason
from solstone.think import responsiveness

REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "solstone" / "think" / "responsiveness.py"
EXEMPLAR_1 = "I cannot directly view or process images"
EXEMPLAR_2 = (
    "I cannot provide external software support or access third-party tools like "
    "solstone"
)


@dataclass(frozen=True)
class CorpusCase:
    row: str
    payload: object
    expected_non_responsive: bool
    schema_path: Path | None = None
    expected_empty_corpus: bool | None = None


def _schema_path(name: str) -> Path:
    talent_path = REPO_ROOT / "solstone" / "talent" / f"{name}.schema.json"
    if talent_path.exists():
        return talent_path
    observe_path = REPO_ROOT / "solstone" / "observe" / f"{name}.schema.json"
    if observe_path.exists():
        return observe_path
    category_path = (
        REPO_ROOT / "solstone" / "observe" / "categories" / f"{name}.schema.json"
    )
    if category_path.exists():
        return category_path
    raise AssertionError(f"schema not found: {name}")


CHAT_SCHEMA = _schema_path("chat")
DESCRIBE_SCHEMA = _schema_path("describe")
MESSAGING_SCHEMA = _schema_path("messaging")
STEWARD_SCHEMA = _schema_path("steward")


def _json_payload(payload: dict[str, Any]) -> str:
    return json.dumps(payload)


def _case(row: str) -> CorpusCase:
    return next(case for case in RESPONSIVENESS_CORPUS if case.row == row)


def _load_schema(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _validate_schema(case: CorpusCase) -> None:
    if case.schema_path is None:
        return
    schema = _load_schema(case.schema_path)
    payload = (
        json.loads(case.payload) if isinstance(case.payload, str) else case.payload
    )
    Draft202012Validator(schema).validate(payload)


def _assert_case(row: str) -> responsiveness.ResponsivenessVerdict:
    case = _case(row)
    _validate_schema(case)
    verdict = responsiveness.classify_output_responsiveness(case.payload)
    assert verdict.non_responsive is case.expected_non_responsive
    if case.expected_empty_corpus is not None:
        assert verdict.empty_corpus is case.expected_empty_corpus
    if verdict.non_responsive:
        assert verdict.matched_signal is not None
    else:
        assert verdict.matched_signal is None
    return verdict


RESPONSIVENESS_CORPUS = [
    CorpusCase("N1", EXEMPLAR_1, True),
    CorpusCase("N2", EXEMPLAR_2, True),
    CorpusCase(
        "N3",
        EXEMPLAR_2
        + " I recommend contacting their support team directly at their official "
        "website. Additionally, you may want to check their documentation.",
        True,
    ),
    CorpusCase(
        "N4",
        _json_payload(
            {
                "message": EXEMPLAR_2
                + " If you are having trouble with the app, the best path is to "
                "reach out to the team that maintains it.",
                "notes": (
                    "The owner asked about a third-party product, so I explained "
                    "the limitation and pointed them elsewhere."
                ),
                "talent_request": None,
            }
        ),
        True,
        CHAT_SCHEMA,
    ),
    CorpusCase(
        "N5",
        _json_payload(
            {
                "visual_description": "I cannot view images.",
                "primary": "browsing",
                "secondary": "none",
                "overlap": False,
            }
        ),
        True,
        DESCRIBE_SCHEMA,
    ),
    CorpusCase(
        "N6",
        _json_payload(
            {
                "visual_description": EXEMPLAR_1,
                "primary": "browsing",
                "secondary": "none",
                "overlap": False,
            }
        ),
        True,
        DESCRIBE_SCHEMA,
    ),
    CorpusCase("N7", "Unfortunately, I cannot process this image.", True),
    CorpusCase(
        "N8",
        _json_payload(
            {
                "app": "Slack",
                "thread": "general",
                "view": "conversation",
                "messages": [
                    {
                        "sender": "bot",
                        "timestamp": "2PM",
                        "subject": "status",
                        "text": "I cannot assist with that request.",
                    }
                ],
            }
        ),
        True,
        MESSAGING_SCHEMA,
    ),
    CorpusCase("R1", "A terminal window showing a green test run.", False),
    CorpusCase(
        "R2",
        "A browser window shows a documentation page for the local setup flow, "
        "with a sidebar, a main article, and a terminal panel visible beside it. "
        "The page highlights completed configuration steps and a pending provider "
        "choice. I can't read the small text in the corner, but the rest of the "
        "screen is clear.",
        False,
    ),
    CorpusCase(
        "R3",
        _json_payload(
            {
                "visual_description": (
                    "A terminal window shows a passing test run, though I can't "
                    "read the tiny status text in the corner."
                ),
                "primary": "terminal",
                "secondary": "code",
                "overlap": True,
            }
        ),
        False,
        DESCRIBE_SCHEMA,
    ),
    CorpusCase(
        "R4",
        _json_payload(
            {
                "app": "Slack",
                "thread": "general",
                "view": "conversation",
                "messages": [
                    {
                        "sender": "bot",
                        "timestamp": "2PM",
                        "subject": "status",
                        "text": (
                            "look what the bot said: I cannot help with that "
                            "request, as I am not able to access external systems."
                        ),
                    }
                ],
            }
        ),
        False,
        MESSAGING_SCHEMA,
    ),
    CorpusCase(
        "R5",
        'The screen shows an assistant reply reading "I cannot help with that."',
        False,
    ),
    CorpusCase(
        "R6",
        _json_payload(
            {
                "headline": "Endpoint unavailable",
                "summary_sentence": (
                    "Screen descriptions are paused because I don't have access "
                    "to the local endpoint right now."
                ),
                "suggested_action": "open_health_detail",
            }
        ),
        False,
        STEWARD_SCHEMA,
    ),
    CorpusCase(
        "R7",
        "# Bluesky - Feed\n\n"
        "**@alice**: release check passed\n"
        "**@bot**: I cannot assist with that",
        False,
    ),
    CorpusCase(
        "R8",
        "# VS Code - src/check.py\n\n"
        "```python\n"
        "# I cannot reach the network here, so skip\n"
        'print("offline")\n'
        "```",
        False,
    ),
    CorpusCase(
        "R9",
        _json_payload(
            {
                "app": "Slack",
                "thread": "general",
                "view": "conversation",
                "messages": [
                    {
                        "sender": "Mira",
                        "timestamp": "2PM",
                        "subject": "status",
                        "text": (
                            "The release check passed and Noah owns the follow-up."
                        ),
                    }
                ],
            }
        ),
        False,
        MESSAGING_SCHEMA,
    ),
    CorpusCase("R10", "The test run passed.", False),
]

R11_GARBAGE_INPUTS = [
    (None, True, "None has no string leaves"),
    ("", True, "empty text is not prose-like"),
    ("  \n\t  ", True, "whitespace-only text is not prose-like"),
    ("[]", True, "parsed JSON array has no leaves"),
    ("42", True, "parsed non-string JSON scalar yields no leaves"),
    (
        '{"message": "I cannot finish"',
        False,
        "truncated JSON becomes one prose-like leaf whose opening is not a head",
    ),
    ("\ud800", True, "lone surrogate is not alphabetic prose"),
]


def _all_cases_with_r11() -> list[CorpusCase]:
    cases = list(RESPONSIVENESS_CORPUS)
    for index, (payload, expected_empty, _reason) in enumerate(R11_GARBAGE_INPUTS):
        cases.append(
            CorpusCase(
                f"R11.{index}",
                payload,
                False,
                expected_empty_corpus=expected_empty,
            )
        )
    return cases


def test_n1_bare_image_refusal():
    _assert_case("N1")


def test_n2_bare_support_refusal():
    _assert_case("N2")


def test_n3_verbose_refusal_not_length_limited():
    _assert_case("N3")


def test_n4_chat_message_refusal_with_real_notes():
    _assert_case("N4")


def test_n5_short_describe_refusal():
    _assert_case("N5")


def test_n6_exact_exemplar_describe_refusal():
    _assert_case("N6")


def test_n7_lead_in_refusal():
    _assert_case("N7")


def test_n8_nested_messaging_refusal():
    _assert_case("N8")


def test_r1_real_description():
    _assert_case("R1")


def test_r2_later_sentence_incidental_negation():
    _assert_case("R2")


def test_r3_describe_incidental_hedge():
    _assert_case("R3")


def test_r4_messaging_quoted_refusal_after_colon():
    _assert_case("R4")


def test_r5_quoted_refusal_after_real_opening():
    _assert_case("R5")


def test_r6_steward_honest_state_copy():
    _assert_case("R6")


def test_r7_social_markdown_single_leaf():
    _assert_case("R7")


def test_r8_code_markdown_single_leaf():
    _assert_case("R8")


def test_r9_messaging_real_answer_with_short_leaves():
    _assert_case("R9")


def test_r10_real_answer_without_negation():
    _assert_case("R10")


def test_r11_garbage_inputs_raise_nothing_and_are_responsive():
    for payload, expected_empty, reason in R11_GARBAGE_INPUTS:
        verdict = responsiveness.classify_output_responsiveness(payload)
        assert verdict.non_responsive is False, reason
        assert verdict.matched_signal is None, reason
        assert verdict.empty_corpus is expected_empty, reason


def test_regression_json_string_scalar_refusal_is_seen():
    verdict = responsiveness.classify_output_responsiveness(
        json.dumps("I cannot view images.")
    )

    assert verdict.non_responsive is True
    assert verdict.matched_signal == "i cannot"
    assert verdict.empty_corpus is False


def test_regression_stacked_lead_in_refusal_is_seen():
    verdict = responsiveness.classify_output_responsiveness(
        "Sorry, unfortunately I cannot process this image."
    )

    assert verdict.non_responsive is True
    assert verdict.matched_signal == "i cannot"
    assert verdict.empty_corpus is False


def test_regression_lead_in_only_first_sentence_refusal_is_seen():
    verdict = responsiveness.classify_output_responsiveness("Sorry. I cannot help.")

    assert verdict.non_responsive is True
    assert verdict.matched_signal == "i cannot"
    assert verdict.empty_corpus is False


def test_regression_lead_in_only_leaf_sets_empty_corpus():
    verdict = responsiveness.classify_output_responsiveness("Sorry.")

    assert verdict.non_responsive is False
    assert verdict.matched_signal is None
    assert verdict.empty_corpus is True


def test_ac1_module_imports_no_solstone_modules_by_ast():
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"), filename=str(MODULE_PATH))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    assert imported == {"__future__", "dataclasses", "json", "re"}
    assert [
        name for name in imported if name == "solstone" or name.startswith("solstone.")
    ] == []
    assert [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and node.id == "open"
    ] == []


def _journal_snapshot(
    journal: Path,
) -> tuple[set[tuple[str, int, int, str]], dict[str, bytes]]:
    structural: set[tuple[str, int, int, str]] = set()
    contents: dict[str, bytes] = {}
    for path in sorted(journal.rglob("*")):
        rel = path.relative_to(journal).as_posix()
        st = os.lstat(path)
        kind = stat.S_IFMT(st.st_mode)
        link_target = os.readlink(path) if stat.S_ISLNK(st.st_mode) else ""
        structural.add((rel, kind, stat.S_IMODE(st.st_mode), link_target))
        if (
            stat.S_ISREG(kind)
            and not stat.S_ISLNK(st.st_mode)
            and stat.S_IMODE(st.st_mode) != 0
        ):
            contents[rel] = path.read_bytes()
    return structural, contents


def test_ac2_classifier_performs_no_filesystem_access(tmp_path, monkeypatch):
    journal = tmp_path / "journal"
    journal.mkdir()
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))
    before = _journal_snapshot(journal)

    for case in _all_cases_with_r11():
        responsiveness.classify_output_responsiveness(case.payload)

    assert _journal_snapshot(journal) == before


def test_ac3_classifier_is_deterministic():
    for case in _all_cases_with_r11():
        first = responsiveness.classify_output_responsiveness(case.payload)
        second = responsiveness.classify_output_responsiveness(case.payload)
        assert second == first


def test_ac4_classifier_has_no_numeric_threshold_and_does_not_read_raw_cap():
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"), filename=str(MODULE_PATH))

    numeric_compares = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        if any(
            isinstance(comparator, ast.Constant)
            and isinstance(comparator.value, (int, float))
            for comparator in node.comparators
        ):
            numeric_compares.append(node.lineno)

    cap_assigns = 0
    cap_loads = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            cap_assigns += sum(
                1
                for target in node.targets
                if isinstance(target, ast.Name)
                and target.id == "NON_RESPONSIVE_RAW_OUTPUT_CAP_CHARS"
            )
        elif (
            isinstance(node, ast.Name)
            and node.id == "NON_RESPONSIVE_RAW_OUTPUT_CAP_CHARS"
            and isinstance(node.ctx, ast.Load)
        ):
            cap_loads.append(node.lineno)

    assert numeric_compares == []
    assert cap_assigns == 1
    assert cap_loads == []


def test_ac5_signal_table_identifiers_exist_in_one_module():
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"), filename=str(MODULE_PATH))
    identifiers = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not isinstance(node.value, ast.Tuple):
            continue
        if not all(
            isinstance(item, ast.Constant) and isinstance(item.value, str)
            for item in node.value.elts
        ):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id.startswith("_"):
                identifiers.add(target.id)

    assert identifiers
    roots = [REPO_ROOT / "solstone", REPO_ROOT / "scripts"]
    text_suffixes = {".css", ".html", ".js", ".json", ".md", ".py", ".txt"}
    hits = {identifier: set() for identifier in identifiers}

    for root in roots:
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix not in text_suffixes:
                continue
            if "tests" in path.parts:
                continue
            text = path.read_text(encoding="utf-8")
            for identifier in identifiers:
                if identifier in text:
                    hits[identifier].add(path.relative_to(REPO_ROOT).as_posix())

    assert hits == {
        identifier: {"solstone/think/responsiveness.py"} for identifier in identifiers
    }


def test_ac6_all_corpus_rows_match_expected_verdicts():
    for case in RESPONSIVENESS_CORPUS:
        _assert_case(case.row)


def test_ac7_json_fixtures_validate_real_schemas_before_verdict():
    for case in RESPONSIVENESS_CORPUS:
        if isinstance(case.payload, str):
            try:
                parsed = json.loads(case.payload)
            except ValueError:
                parsed = None
            if isinstance(parsed, dict):
                assert case.schema_path is not None
        _validate_schema(case)
        verdict = responsiveness.classify_output_responsiveness(case.payload)
        assert verdict.non_responsive is case.expected_non_responsive


def test_ac8_r11_inputs_raise_nothing():
    for payload, _expected_empty, reason in R11_GARBAGE_INPUTS:
        try:
            responsiveness.classify_output_responsiveness(payload)
        except Exception as exc:  # pragma: no cover - assertion path
            raise AssertionError(reason) from exc


def test_ac9_schema_valid_json_with_no_prose_sets_empty_corpus():
    schema = _load_schema(MESSAGING_SCHEMA)
    assert "minItems" not in schema["properties"]["messages"]
    payload = {
        "app": "Slack",
        "thread": "general",
        "view": "unknown",
        "messages": [],
    }
    Draft202012Validator(schema).validate(payload)

    verdict = responsiveness.classify_output_responsiveness(_json_payload(payload))

    assert verdict.non_responsive is False
    assert verdict.matched_signal is None
    assert verdict.empty_corpus is True


def test_ac10_error_is_runtime_error_not_value_error():
    exc = responsiveness.NonResponsiveOutputError()

    assert isinstance(exc, RuntimeError)
    assert not isinstance(exc, ValueError)


def test_ac11_provider_classifier_uses_reason_code_shortcut():
    from solstone.think.providers.shared import classify_provider_error

    assert (
        classify_provider_error(
            responsiveness.NonResponsiveOutputError(),
            "local",
        )
        == responsiveness.NON_RESPONSIVE_REASON_CODE
    )


def test_ac12_detect_transcript_segment_propagates_nonresponsive(monkeypatch):
    from solstone.think import detect_transcript, models

    def fake_generate(**_kwargs):
        raise responsiveness.NonResponsiveOutputError()

    monkeypatch.setattr(models, "generate", fake_generate)

    with pytest.raises(responsiveness.NonResponsiveOutputError):
        detect_transcript.detect_transcript_segment("01\n02\n", "12:00:00")


def test_ac12_detect_transcript_json_propagates_nonresponsive(monkeypatch):
    from solstone.think import detect_transcript, models

    def fake_generate(**_kwargs):
        raise responsiveness.NonResponsiveOutputError()

    monkeypatch.setattr(models, "generate", fake_generate)

    with pytest.raises(responsiveness.NonResponsiveOutputError):
        detect_transcript.detect_transcript_json("some text", "12:00:00")


def _copy_constants() -> tuple[str, ...]:
    return (
        responsiveness.NON_RESPONSIVE_OUTPUT_MESSAGE,
        responsiveness.NON_RESPONSIVE_OUTPUT_FRAGMENT,
        responsiveness.NON_RESPONSIVE_READINESS_SUMMARY,
        responsiveness.NON_RESPONSIVE_READINESS_DETAIL,
    )


def _ngrams(text: str, size: int) -> set[str]:
    words = text.lower().split()
    return {
        " ".join(words[index : index + size]) for index in range(len(words) - size + 1)
    }


def test_ac13_copy_omits_exemplar_substrings_and_exception_omits_raw_output():
    exemplar_ngrams = set()
    for exemplar in (EXEMPLAR_1, EXEMPLAR_2):
        words = exemplar.split()
        for size in range(2, len(words) + 1):
            exemplar_ngrams |= _ngrams(exemplar, size)

    for copy in _copy_constants():
        lowered = copy.lower()
        assert EXEMPLAR_1.lower() not in lowered
        assert EXEMPLAR_2.lower() not in lowered
        copy_ngrams = set()
        words = copy.split()
        for size in range(2, len(words) + 1):
            copy_ngrams |= _ngrams(copy, size)
        assert exemplar_ngrams.isdisjoint(copy_ngrams)

    exc = responsiveness.NonResponsiveOutputError()
    assert str(exc) == responsiveness.NON_RESPONSIVE_OUTPUT_MESSAGE
    assert EXEMPLAR_1 not in str(exc)

    with pytest.raises(TypeError):
        responsiveness.NonResponsiveOutputError(EXEMPLAR_1)


def test_ac14_fragment_survives_chat_reason_cleaner():
    fragment = responsiveness.NON_RESPONSIVE_OUTPUT_FRAGMENT
    python_path_re = re.compile(r"/[A-Za-z0-9_./-]+\.py")

    assert len(fragment) <= 160
    assert "Traceback (most recent call last)" not in fragment
    assert python_path_re.search(fragment) is None
    assert _clean_talent_errored_reason(fragment) == fragment


def test_ac15_composed_chat_copy_is_not_redundant():
    fragment = _clean_talent_errored_reason(
        responsiveness.NON_RESPONSIVE_OUTPUT_FRAGMENT
    )
    assert fragment is not None

    composed = CHAT_CLOSER_TALENT_ERRORED_FORMAT.format(reason=fragment)

    assert composed.count("couldn't finish") == 1
    assert composed.count("try") <= 1


def test_ac16_readiness_copy_matches_presenter_rules():
    assert (
        responsiveness.NON_RESPONSIVE_REASON_CODE
        not in responsiveness.NON_RESPONSIVE_READINESS_SUMMARY
    )
    summary = responsiveness.NON_RESPONSIVE_READINESS_SUMMARY
    assert summary[0].islower()
    assert not summary.endswith(".")

    detail = responsiveness.NON_RESPONSIVE_READINESS_DETAIL
    assert detail[0].isupper()
    assert detail.endswith(".")


def test_public_all_exact_surface():
    assert responsiveness.__all__ == [
        "NON_RESPONSIVE_OUTPUT_FRAGMENT",
        "NON_RESPONSIVE_OUTPUT_MESSAGE",
        "NON_RESPONSIVE_RAW_OUTPUT_CAP_CHARS",
        "NON_RESPONSIVE_READINESS_DETAIL",
        "NON_RESPONSIVE_READINESS_SUMMARY",
        "NON_RESPONSIVE_REASON_CODE",
        "NonResponsiveOutputError",
        "ResponsivenessVerdict",
        "classify_output_responsiveness",
    ]

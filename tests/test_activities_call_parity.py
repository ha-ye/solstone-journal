# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

import json
from datetime import datetime
from pathlib import Path

import pytest
import requests
from typer.testing import CliRunner

import solstone.apps.activities.routes as activities_routes
from solstone.apps.activities.call import app
from solstone.convey.reasons import ACTIVITIES_BUSY
from solstone.think.convey_client import ConveyClient
from solstone.think.journal_io import LockTimeout
from tests._baseline_harness import make_logged_in_test_client

DAY = "20260418"
PREV_DAY = "20260417"
FACET = "work"


@pytest.fixture
def journal(tmp_path, monkeypatch):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    monkeypatch.setenv("SOL_DAY", DAY)
    monkeypatch.setenv("SOL_FACET", FACET)

    import solstone.think.utils as think_utils

    think_utils._journal_path_cache = None
    _seed_journal(tmp_path)
    return tmp_path


@pytest.fixture
def runner(journal, monkeypatch):
    client = ConveyClient(session=make_logged_in_test_client(journal), base_url="")
    monkeypatch.setattr("solstone.apps.activities.call.get_client", lambda: client)
    return CliRunner()


def _seed_journal(
    journal: Path,
    *,
    facets: tuple[str, ...] = (FACET,),
    activity_config: list[dict] | None = None,
) -> None:
    config_dir = journal / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "journal.json").write_text(
        json.dumps(
            {
                "convey": {"trust_localhost": True},
                "setup": {"completed_at": 1700000000000},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    config_entries = activity_config or [{"id": "coding"}, {"id": "meeting"}]
    for facet in facets:
        facet_dir = journal / "facets" / facet
        activities_dir = facet_dir / "activities"
        activities_dir.mkdir(parents=True, exist_ok=True)
        (facet_dir / "facet.json").write_text(
            json.dumps({"title": f"Test {facet}", "description": "Test facet"}) + "\n",
            encoding="utf-8",
        )
        (activities_dir / "activities.jsonl").write_text(
            "".join(
                json.dumps(entry, ensure_ascii=False) + "\n" for entry in config_entries
            ),
            encoding="utf-8",
        )


def _write_records(journal: Path, facet: str, day: str, records: list[dict]) -> None:
    path = journal / "facets" / facet / "activities" / f"{day}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )


def _read_records(journal: Path, facet: str = FACET, day: str = DAY) -> list[dict]:
    path = journal / "facets" / facet / "activities" / f"{day}.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _base_record(**overrides) -> dict:
    record = {
        "id": "coding_090000_300",
        "activity": "coding",
        "title": "Focused coding",
        "description": "Implementing the CLI",
        "segments": ["090000_300"],
        "active_entities": ["Ada Lovelace"],
        "created_at": 1,
        "source": "user",
    }
    record.update(overrides)
    return record


def _valid_participation_entry(**overrides) -> dict:
    entry = {
        "name": "JB",
        "role": "attendee",
        "source": "voice",
        "confidence": 0.98,
        "context": "Spoke during the meeting",
    }
    entry.update(overrides)
    return entry


def _busy(*_args, **_kwargs):
    raise LockTimeout(Path("busy"), 0.01)


def test_list_empty_text_and_json(runner):
    text = runner.invoke(app, ["list", "--day", DAY, "--facet", FACET])
    as_json = runner.invoke(app, ["list", "--day", DAY, "--facet", FACET, "--json"])

    assert text.exit_code == 0
    assert text.stdout == "No activities found.\n"
    assert text.stderr == ""
    assert as_json.exit_code == 0
    assert as_json.stdout == "[]\n"


def test_list_text_render_and_json_enrichment(runner, journal):
    _write_records(journal, FACET, DAY, [_base_record()])

    text = runner.invoke(app, ["list", "--day", DAY, "--facet", FACET])
    as_json = runner.invoke(app, ["list", "--day", DAY, "--facet", FACET, "--json"])

    assert text.exit_code == 0
    assert text.stdout == (
        "### Focused coding\n"
        "- Activity: coding\n"
        "- Facet: work\n"
        "- Day: 20260418\n"
        "- Time: 09:00-09:05\n"
        "- Description: Implementing the CLI\n"
    )
    payload = json.loads(as_json.stdout)
    assert payload[0]["facet"] == FACET
    assert payload[0]["day"] == DAY


def test_list_defaults_to_today_without_day_or_env(runner, journal, monkeypatch):
    monkeypatch.delenv("SOL_DAY", raising=False)
    today = datetime.now().strftime("%Y%m%d")
    _write_records(
        journal,
        FACET,
        today,
        [_base_record(title="Today coding", description="Using today's default")],
    )

    result = runner.invoke(app, ["list", "--facet", FACET])

    assert result.exit_code == 0
    assert "Today coding" in result.stdout


@pytest.mark.parametrize(
    ("args", "stderr"),
    [
        (
            ["list", "--day", DAY, "--from", PREV_DAY, "--facet", FACET],
            "Error: --day is incompatible with --from/--to.\n",
        ),
        (
            ["list", "--from", DAY, "--to", PREV_DAY, "--facet", FACET],
            f"Error: --to ({PREV_DAY}) must not be before --from ({DAY})\n",
        ),
        (
            ["list", "--from", "bad", "--facet", FACET],
            "Error: invalid day 'bad'\n",
        ),
        (
            ["list", "--day", DAY, "--facet", FACET, "--source", "calendar"],
            "Error: --source must be 'anticipated', 'cogitate', or 'user'.\n",
        ),
    ],
)
def test_list_errors(runner, args, stderr):
    result = runner.invoke(app, args)

    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr == stderr


def test_list_bad_day_argument_preserves_empty_lookup(runner):
    result = runner.invoke(app, ["list", "--day", "bad", "--facet", FACET])

    assert result.exit_code == 0
    assert result.stdout == "No activities found.\n"


def test_list_filters_hidden_all_facets_and_sort_order(runner, journal, monkeypatch):
    _seed_journal(journal, facets=("personal",))
    monkeypatch.delenv("SOL_FACET", raising=False)
    _write_records(
        journal,
        FACET,
        DAY,
        [
            _base_record(id="coding_090000_300", created_at=2),
            _base_record(
                id="meeting_100000_300",
                activity="meeting",
                title="Muted meeting",
                description="Hidden",
                active_entities=["Grace Hopper"],
                segments=["100000_300"],
                source="cogitate",
                hidden=True,
                created_at=1,
            ),
        ],
    )
    _write_records(
        journal,
        "personal",
        DAY,
        [_base_record(id="coding_080000_300", title="Personal coding", created_at=1)],
    )
    _write_records(
        journal,
        FACET,
        PREV_DAY,
        [_base_record(id="coding_070000_300", title="Previous coding", created_at=9)],
    )

    all_facets = runner.invoke(app, ["list", "--day", DAY, "--json"])
    single_facet = runner.invoke(
        app, ["list", "--day", DAY, "--facet", FACET, "--json"]
    )
    hidden = runner.invoke(
        app, ["list", "--day", DAY, "--facet", FACET, "--all", "--json"]
    )
    filtered = runner.invoke(
        app,
        [
            "list",
            "--day",
            DAY,
            "--facet",
            FACET,
            "--activity",
            "coding",
            "--entity",
            "lovel",
            "--source",
            "user",
            "--json",
        ],
    )
    sorted_range = runner.invoke(
        app, ["list", "--from", PREV_DAY, "--to", DAY, "--all", "--json"]
    )

    assert [item["facet"] for item in json.loads(all_facets.stdout)] == [
        "personal",
        "work",
    ]
    assert [item["facet"] for item in json.loads(single_facet.stdout)] == ["work"]
    assert any(item.get("hidden") for item in json.loads(hidden.stdout))
    assert [item["id"] for item in json.loads(filtered.stdout)] == ["coding_090000_300"]
    assert [item["id"] for item in json.loads(sorted_range.stdout)] == [
        "coding_070000_300",
        "coding_080000_300",
        "meeting_100000_300",
        "coding_090000_300",
    ]


def test_get_found_text_json_and_missing(runner, journal):
    _write_records(journal, FACET, DAY, [_base_record()])

    text = runner.invoke(
        app, ["get", "coding_090000_300", "--day", DAY, "--facet", FACET]
    )
    as_json = runner.invoke(
        app, ["get", "coding_090000_300", "--day", DAY, "--facet", FACET, "--json"]
    )
    missing = runner.invoke(app, ["get", "missing", "--day", DAY, "--facet", FACET])

    assert text.exit_code == 0
    assert text.stdout.startswith("### Focused coding\n- Activity: coding\n")
    assert json.loads(as_json.stdout)["id"] == "coding_090000_300"
    assert missing.exit_code == 1
    assert missing.stdout == ""
    assert missing.stderr == "activity not found: missing\n"


def test_create_success_json_and_text(runner):
    as_json = runner.invoke(
        app,
        [
            "create",
            "--day",
            DAY,
            "--facet",
            FACET,
            "--since-segment",
            "090000_300",
            "--source",
            "cogitate",
            "--json",
        ],
        input=json.dumps({"title": "Team sync", "activity": "meeting"}),
    )
    text = runner.invoke(
        app,
        [
            "create",
            "--day",
            DAY,
            "--facet",
            FACET,
            "--since-segment",
            "100000_300",
        ],
        input=json.dumps({"title": "Focused coding", "activity": "coding"}),
    )

    payload = json.loads(as_json.stdout)
    assert as_json.exit_code == 0
    assert payload["id"] == "meeting_090000_300"
    assert payload["source"] == "cogitate"
    assert payload["segments"] == ["090000_300"]
    assert payload["edits"][-1]["actor"] == "cogitate:activities"
    assert payload["edits"][-1]["fields"] == [
        "activity",
        "title",
        "description",
        "details",
        "source",
    ]
    assert text.exit_code == 0
    assert text.stdout.startswith("### Focused coding\n- Activity: coding\n")


def test_create_argv_success(runner):
    result = runner.invoke(
        app,
        [
            "create",
            "--day",
            DAY,
            "--facet",
            FACET,
            "--source",
            "cogitate",
            "--title",
            "Team sync",
            "--activity",
            "meeting",
            "--description",
            "Synced",
            "--details",
            "notes",
            "--since-segment",
            "090000_300",
            "--json",
        ],
    )

    payload = json.loads(result.stdout)
    assert result.exit_code == 0
    assert payload["id"] == "meeting_090000_300"
    assert payload["source"] == "cogitate"
    assert payload["segments"] == ["090000_300"]
    assert payload["description"] == "Synced"
    assert payload["details"] == "notes"
    assert payload["edits"][-1]["actor"] == "cogitate:activities"
    assert payload["edits"][-1]["fields"] == [
        "activity",
        "title",
        "description",
        "details",
        "source",
    ]


def test_create_argv_missing_required_flags(runner):
    missing_title = runner.invoke(
        app,
        [
            "create",
            "--day",
            DAY,
            "--facet",
            FACET,
            "--activity",
            "meeting",
        ],
    )
    missing_activity = runner.invoke(
        app,
        [
            "create",
            "--day",
            DAY,
            "--facet",
            FACET,
            "--title",
            "X",
        ],
    )

    assert missing_title.exit_code == 1
    assert missing_title.stdout == ""
    assert missing_title.stderr == "Error: --title is required.\n"
    assert missing_activity.exit_code == 1
    assert missing_activity.stdout == ""
    assert missing_activity.stderr == "Error: --activity is required.\n"


def test_create_argv_surfaces_server_title_rejection(runner):
    result = runner.invoke(
        app,
        [
            "create",
            "--day",
            DAY,
            "--facet",
            FACET,
            "--source",
            "cogitate",
            "--title",
            "",
            "--activity",
            "meeting",
        ],
    )

    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr == "Error: title must not be empty\n"


def test_create_argv_flags_ignore_stdin(runner):
    result = runner.invoke(
        app,
        [
            "create",
            "--day",
            DAY,
            "--facet",
            FACET,
            "--source",
            "cogitate",
            "--title",
            "Flag title",
            "--activity",
            "meeting",
            "--description",
            "Flag description",
            "--json",
        ],
        input=json.dumps({"title": "IGNORED", "activity": "coding"}),
    )

    payload = json.loads(result.stdout)
    assert result.exit_code == 0
    assert payload["title"] == "Flag title"
    assert payload["activity"] == "meeting"
    assert payload["description"] == "Flag description"


@pytest.mark.parametrize(
    ("args", "stdin", "stderr"),
    [
        (
            ["create", "--day", DAY, "--facet", FACET, "--source", "calendar"],
            "{}",
            "Error: --source must be 'cogitate' or 'user'.\n",
        ),
        (
            ["create", "--day", DAY, "--facet", FACET],
            "",
            "Error: expected JSON object on stdin.\n",
        ),
        (
            ["create", "--day", DAY, "--facet", FACET],
            "{not valid",
            "Error: invalid JSON on stdin: Expecting property name enclosed in double quotes: line 1 column 2 (char 1)\n",
        ),
        (
            ["create", "--day", DAY, "--facet", FACET],
            "[]",
            "Error: expected JSON object on stdin.\n",
        ),
        (
            ["create", "--day", DAY, "--facet", FACET],
            json.dumps({"activity": "coding"}),
            "Error: title is required.\n",
        ),
        (
            ["create", "--day", DAY, "--facet", FACET],
            json.dumps({"title": "No type"}),
            "Error: activity is required.\n",
        ),
        (
            ["create", "--day", DAY, "--facet", FACET, "--since-segment", "bad"],
            json.dumps({"title": "Bad segment", "activity": "coding"}),
            "Error: invalid --since-segment 'bad' (expected HHMMSS_LEN)\n",
        ),
    ],
)
def test_create_cli_side_errors(runner, args, stdin, stderr):
    result = runner.invoke(app, args, input=stdin)

    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr == stderr


def test_create_unknown_duplicate_and_busy(runner, monkeypatch):
    unknown = runner.invoke(
        app,
        ["create", "--day", DAY, "--facet", FACET],
        input=json.dumps({"title": "Unknown", "activity": "unknown"}),
    )
    first = runner.invoke(
        app,
        ["create", "--day", DAY, "--facet", FACET, "--since-segment", "090000_300"],
        input=json.dumps({"title": "Team sync", "activity": "meeting"}),
    )
    duplicate = runner.invoke(
        app,
        ["create", "--day", DAY, "--facet", FACET, "--since-segment", "090000_300"],
        input=json.dumps({"title": "Team sync", "activity": "meeting"}),
    )
    monkeypatch.setattr(activities_routes, "append_activity_record", _busy)
    busy = runner.invoke(
        app,
        ["create", "--day", DAY, "--facet", FACET, "--since-segment", "100000_300"],
        input=json.dumps({"title": "Team sync", "activity": "meeting"}),
    )

    assert unknown.exit_code == 1
    assert unknown.stderr == "Error: unknown activity for facet 'work': unknown\n"
    assert first.exit_code == 0
    assert duplicate.exit_code == 1
    assert duplicate.stderr == "Error: activity already exists: meeting_090000_300\n"
    assert busy.exit_code == 1
    assert busy.stderr == f"{ACTIVITIES_BUSY.message}\n"


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ({"name": "JB"}, "Error: participation[0] has invalid role 'None'"),
        ("JB", "Error: participation[0] must be an object"),
        (
            _valid_participation_entry(name=""),
            "Error: participation[0] requires a non-empty string 'name'",
        ),
        (
            _valid_participation_entry(role="observer"),
            "Error: participation[0] has invalid role 'observer' (must be one of ['attendee', 'mentioned'])",
        ),
        (
            _valid_participation_entry(source="calendar"),
            "Error: participation[0] has invalid source 'calendar' (must be one of ['other', 'screen', 'speaker_label', 'transcript', 'voice'])",
        ),
        (
            _valid_participation_entry(confidence=True),
            "Error: participation[0] 'confidence' must be a number",
        ),
        (
            _valid_participation_entry(context=7),
            "Error: participation[0] 'context' must be a string",
        ),
    ],
)
def test_create_participation_structural_errors(runner, entry, message):
    payload = {"title": "Team sync", "activity": "meeting", "participation": [entry]}

    result = runner.invoke(
        app, ["create", "--day", DAY, "--facet", FACET], input=json.dumps(payload)
    )

    assert result.exit_code == 1
    assert message in result.stderr


def test_create_rejects_non_array_participation(runner):
    result = runner.invoke(
        app,
        ["create", "--day", DAY, "--facet", FACET],
        input=json.dumps(
            {"title": "Team sync", "activity": "meeting", "participation": {}}
        ),
    )

    assert result.exit_code == 1
    assert result.stderr == "Error: participation must be an array\n"


def test_create_participation_entity_resolution_and_empty_persistence(runner, journal):
    entities_path = journal / "facets" / FACET / "entities" / f"{DAY}.jsonl"
    entities_path.parent.mkdir(parents=True, exist_ok=True)
    entities_path.write_text(
        json.dumps(
            {
                "id": "john_borthwick",
                "type": "Person",
                "name": "John Borthwick",
                "aka": ["JB"],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    resolved = runner.invoke(
        app,
        [
            "create",
            "--day",
            DAY,
            "--facet",
            FACET,
            "--since-segment",
            "090000_300",
            "--json",
        ],
        input=json.dumps(
            {
                "title": "Team sync",
                "activity": "meeting",
                "participation": [
                    _valid_participation_entry(entity_id="fake", extra="keep")
                ],
            }
        ),
    )
    empty = runner.invoke(
        app,
        [
            "create",
            "--day",
            DAY,
            "--facet",
            FACET,
            "--since-segment",
            "100000_300",
            "--json",
        ],
        input=json.dumps(
            {"title": "Empty", "activity": "meeting", "participation": []}
        ),
    )

    resolved_payload = json.loads(resolved.stdout)
    empty_payload = json.loads(empty.stdout)
    assert resolved_payload["participation"][0]["entity_id"] == "john_borthwick"
    assert resolved_payload["participation"][0]["extra"] == "keep"
    assert "participation" in resolved_payload["edits"][0]["fields"]
    assert empty_payload["participation"] == []
    assert "participation" in empty_payload["edits"][0]["fields"]


def test_update_success_errors_and_busy(runner, journal, monkeypatch):
    _write_records(journal, FACET, DAY, [_base_record()])

    success = runner.invoke(
        app,
        ["update", "coding_090000_300", "--day", DAY, "--facet", FACET, "--json"],
        input=json.dumps({"details": "New details", "title": "Focused coding"}),
    )
    disallowed = runner.invoke(
        app,
        ["update", "coding_090000_300", "--day", DAY, "--facet", FACET],
        input=json.dumps({"activity": "meeting"}),
    )
    empty = runner.invoke(
        app,
        ["update", "coding_090000_300", "--day", DAY, "--facet", FACET],
        input=json.dumps({}),
    )
    missing = runner.invoke(
        app,
        ["update", "missing", "--day", DAY, "--facet", FACET],
        input=json.dumps({"title": "Nope"}),
    )
    monkeypatch.setattr(activities_routes, "update_activity_record", _busy)
    busy = runner.invoke(
        app,
        ["update", "coding_090000_300", "--day", DAY, "--facet", FACET],
        input=json.dumps({"title": "Busy"}),
    )

    payload = json.loads(success.stdout)
    assert payload["title"] == "Focused coding"
    assert payload["details"] == "New details"
    assert payload["edits"][-1]["note"] == "updated fields: details, title"
    assert disallowed.stderr == "Error: disallowed update fields: activity\n"
    assert (
        empty.stderr
        == "Error: update payload must include at least one mutable field.\n"
    )
    assert missing.stderr == "activity not found: missing\n"
    assert busy.stderr == f"{ACTIVITIES_BUSY.message}\n"


def test_update_argv_success_and_empty_details(runner, journal):
    _write_records(journal, FACET, DAY, [_base_record(details="Old details")])

    success = runner.invoke(
        app,
        [
            "update",
            "coding_090000_300",
            "--day",
            DAY,
            "--facet",
            FACET,
            "--title",
            "Focused coding",
            "--details",
            "New details",
            "--json",
        ],
    )
    empty_details = runner.invoke(
        app,
        [
            "update",
            "coding_090000_300",
            "--day",
            DAY,
            "--facet",
            FACET,
            "--details",
            "",
            "--json",
        ],
    )

    success_payload = json.loads(success.stdout)
    empty_details_payload = json.loads(empty_details.stdout)
    assert success.exit_code == 0
    assert success_payload["title"] == "Focused coding"
    assert success_payload["details"] == "New details"
    assert empty_details.exit_code == 0
    assert empty_details_payload["details"] == ""


@pytest.mark.parametrize(
    "args",
    [
        ["update", "coding_090000_300", "--day", DAY, "--facet", FACET, "--note", "x"],
        ["update", "coding_090000_300", "--day", DAY, "--facet", FACET, "--json"],
    ],
)
def test_update_no_mutable_field_no_stdin(runner, journal, args):
    _write_records(journal, FACET, DAY, [_base_record()])

    result = runner.invoke(app, args)

    assert result.exit_code == 1
    assert result.stdout == ""
    assert (
        result.stderr
        == "Error: update payload must include at least one mutable field.\n"
    )


def test_mute_unmute_success_not_found_and_busy(runner, journal, monkeypatch):
    _write_records(journal, FACET, DAY, [_base_record()])

    muted = runner.invoke(
        app,
        [
            "mute",
            "coding_090000_300",
            "--day",
            DAY,
            "--facet",
            FACET,
            "--reason",
            "noise",
            "--json",
        ],
    )
    unmuted = runner.invoke(
        app, ["unmute", "coding_090000_300", "--day", DAY, "--facet", FACET]
    )
    missing = runner.invoke(app, ["mute", "missing", "--day", DAY, "--facet", FACET])
    monkeypatch.setattr(activities_routes, "mute_activity_record", _busy)
    busy = runner.invoke(
        app, ["mute", "coding_090000_300", "--day", DAY, "--facet", FACET]
    )

    assert json.loads(muted.stdout)["hidden"] is True
    assert unmuted.exit_code == 0
    assert unmuted.stdout.startswith("### Focused coding\n")
    assert missing.exit_code == 1
    assert missing.stderr == "activity not found: missing\n"
    assert busy.exit_code == 1
    assert busy.stderr == f"{ACTIVITIES_BUSY.message}\n"


def test_convey_down_prints_require_solstone_message(journal, monkeypatch):
    class DownSession:
        def get(self, _url):
            raise requests.exceptions.ConnectionError()

        def post(self, _url, json=None):
            raise requests.exceptions.ConnectionError()

    client = ConveyClient(session=DownSession(), base_url="http://localhost:5015")
    monkeypatch.setattr("solstone.apps.activities.call.get_client", lambda: client)
    monkeypatch.delenv("SOL_SKIP_SUPERVISOR_CHECK", raising=False)
    monkeypatch.delenv("SOL_SUPERVISOR_SPAWNED", raising=False)

    result = CliRunner().invoke(app, ["list", "--day", DAY, "--facet", FACET])

    assert result.exit_code == 1
    assert (
        result.stderr
        == "sol: solstone isn't running. Start it with 'journal up' and retry.\n"
    )
    assert result.stdout == ""

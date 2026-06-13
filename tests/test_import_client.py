# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from solstone.think import import_client
from solstone.think.convey_client import ConveyClientError, ConveyUnreachableError


class FakeClient:
    def __init__(
        self,
        *,
        upload_response: Any = None,
        request_responses: list[Any] | None = None,
        upload_error: Exception | None = None,
        request_errors: list[Exception | None] | None = None,
    ) -> None:
        self.upload_response = upload_response or {
            "path": "/journal/imports/20260101_120000/sample.txt",
            "timestamp": "20260101_120000",
        }
        self.request_responses = request_responses or [
            {"status": "ok", "task_id": "task-1"}
        ]
        self.upload_error = upload_error
        self.request_errors = request_errors or []
        self.uploads: list[dict[str, Any]] = []
        self.requests: list[dict[str, Any]] = []

    def upload(self, path: str, *, files: dict[str, Any], data: Any = None) -> Any:
        self.uploads.append({"path": path, "files": files, "data": data})
        if self.upload_error is not None:
            raise self.upload_error
        return self.upload_response

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Any = None,
        json: Any = None,
    ) -> Any:
        self.requests.append(
            {"method": method, "path": path, "params": params, "json": json}
        )
        index = len(self.requests) - 1
        if index < len(self.request_errors) and self.request_errors[index] is not None:
            raise self.request_errors[index]
        return self.request_responses[index]


def test_mode_disposition_table_covers_d5_modes() -> None:
    assert import_client.MODE_DISPOSITIONS == {
        "positional_media": "http-client",
        "--timestamp": "http-client",
        "--facet": "http-client",
        "--setting": "http-client",
        "--source": "http-client",
        "--force": "http-client",
        "--auto": "http-client",
        "--dry-run": "reject-journal-host",
        "--json": "client-output",
        "-v/--verbose": "client-logging",
        "--backends": "reject-journal-host",
        "--sync": "reject-journal-host",
        "--save": "reject-journal-host",
        "--path": "reject-journal-host",
        "--list-importers": "reject-journal-host",
        "journal-source": "relocate-sol-call-import",
    }


def test_file_save_then_start(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    media = tmp_path / "sample.txt"
    media.write_text("hello", encoding="utf-8")
    client = FakeClient()

    code = import_client.main([str(media)], client=client)  # type: ignore[arg-type]

    assert code == 0
    assert client.uploads == [
        {
            "path": "/app/import/api/save",
            "files": {
                "file": ("sample.txt", media, "application/octet-stream"),
            },
            "data": {},
        }
    ]
    assert client.requests == [
        {
            "method": "POST",
            "path": "/app/import/api/start",
            "params": None,
            "json": {
                "path": "/journal/imports/20260101_120000/sample.txt",
                "timestamp": "20260101_120000",
                "force": False,
            },
        }
    ]
    assert "queued processing task task-1" in capsys.readouterr().out


def test_save_path_then_start(tmp_path: Path) -> None:
    media_dir = tmp_path / "vault"
    media_dir.mkdir()
    client = FakeClient(
        request_responses=[
            {
                "path": str(media_dir),
                "timestamp": "20260101_130000",
            },
            {"status": "ok", "task_id": "task-2"},
        ]
    )

    code = import_client.main([str(media_dir)], client=client)  # type: ignore[arg-type]

    assert code == 0
    assert client.uploads == []
    assert client.requests[0] == {
        "method": "POST",
        "path": "/app/import/api/save-path",
        "params": None,
        "json": {"path": str(media_dir)},
    }
    assert client.requests[1]["path"] == "/app/import/api/start"
    assert client.requests[1]["json"]["timestamp"] == "20260101_130000"


def test_timestamp_override_only_goes_to_start(tmp_path: Path) -> None:
    media = tmp_path / "sample.txt"
    media.write_text("hello", encoding="utf-8")
    client = FakeClient()

    code = import_client.main(
        [str(media), "--timestamp", "20260202_030405"],
        client=client,  # type: ignore[arg-type]
    )

    assert code == 0
    assert "timestamp" not in client.uploads[0]["data"]
    assert client.requests[0]["json"]["timestamp"] == "20260202_030405"


def test_metadata_and_start_options_forward(tmp_path: Path) -> None:
    media = tmp_path / "sample.txt"
    media.write_text("hello", encoding="utf-8")
    client = FakeClient()

    code = import_client.main(
        [
            str(media),
            "--facet",
            "work",
            "--setting",
            "office",
            "--source",
            "ics",
            "--force",
        ],
        client=client,  # type: ignore[arg-type]
    )

    assert code == 0
    assert client.uploads[0]["data"] == {"facet": "work", "setting": "office"}
    assert client.requests[0]["json"] == {
        "path": "/journal/imports/20260101_120000/sample.txt",
        "timestamp": "20260101_120000",
        "force": True,
        "facet": "work",
        "setting": "office",
        "source": "ics",
    }


def test_json_output_shape(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    media = tmp_path / "sample.txt"
    media.write_text("hello", encoding="utf-8")
    client = FakeClient()

    code = import_client.main([str(media), "--json"], client=client)  # type: ignore[arg-type]

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "status": "queued",
        "path": "/journal/imports/20260101_120000/sample.txt",
        "timestamp": "20260101_120000",
        "save": {
            "path": "/journal/imports/20260101_120000/sample.txt",
            "timestamp": "20260101_120000",
        },
        "start": {"status": "ok", "task_id": "task-1"},
    }


def test_unreachable_is_clean(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    media = tmp_path / "sample.txt"
    media.write_text("hello", encoding="utf-8")
    client = FakeClient(upload_error=ConveyUnreachableError("down"))

    code = import_client.main([str(media)], client=client)  # type: ignore[arg-type]

    assert code == 1
    assert "couldn't reach the journal" in capsys.readouterr().err


def test_typed_error_is_clean(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    media = tmp_path / "sample.txt"
    media.write_text("hello", encoding="utf-8")
    client = FakeClient(
        upload_error=ConveyClientError("bad request", detail="invalid file")
    )

    code = import_client.main([str(media)], client=client)  # type: ignore[arg-type]

    captured = capsys.readouterr()
    assert code == 1
    assert "failed to stage import: bad request" in captured.err
    assert "invalid file" in captured.err


def test_malformed_response_is_clean(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    media = tmp_path / "sample.txt"
    media.write_text("hello", encoding="utf-8")
    client = FakeClient(upload_response={"timestamp": "20260101_120000"})

    code = import_client.main([str(media)], client=client)  # type: ignore[arg-type]

    assert code == 1
    assert "couldn't read journal response" in capsys.readouterr().err


def test_partial_save_but_queue_failed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    media = tmp_path / "sample.txt"
    media.write_text("hello", encoding="utf-8")
    client = FakeClient(request_errors=[ConveyClientError("queue failed")])

    code = import_client.main([str(media)], client=client)  # type: ignore[arg-type]

    captured = capsys.readouterr()
    assert code == 1
    assert (
        "staged /journal/imports/20260101_120000/sample.txt "
        "but processing was not queued: queue failed"
    ) in captured.err


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["media.txt", "--dry-run"], "`--dry-run` requires the journal host"),
        (["--backends"], "`--backends` requires the journal host"),
        (["--list-importers"], "`--list-importers` requires the journal host"),
        (["--sync", "plaud"], "`--sync` requires the journal host"),
        (["media.txt", "--save"], "`--save` requires the journal host"),
        (["media.txt", "--path", "/tmp/src"], "`--path` requires the journal host"),
        (
            ["media.txt", "--auto", "timestamps are Pacific"],
            "`--auto <guidance>` requires the journal host",
        ),
        (["journal-source", "list"], "sol call import <verb>"),
    ],
)
def test_reject_modes_exit_cleanly(
    argv: list[str],
    expected: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        import_client.main(argv, client=FakeClient())  # type: ignore[arg-type]

    assert exc_info.value.code == 2
    assert expected in capsys.readouterr().err

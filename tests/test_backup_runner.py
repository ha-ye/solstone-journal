# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import copy
import dataclasses
import inspect
import json
import pickle
import subprocess
import traceback
from pathlib import Path
from typing import Any

import pytest

from solstone.think.backup import runner

SNAPSHOT_ID = "5a1d5a1d5a1d5a1d5a1d5a1d5a1d5a1d5a1d5a1d5a1d5a1d5a1d5a1d5a1d5a1d"
LOGICAL_SOURCE_PATH = "/spb/source.bin"


def _jsonl(*records: object) -> bytes:
    return ("\n".join(json.dumps(record) for record in records) + "\n").encode()


def test_run_restic_builds_safe_argv_and_minimal_env(
    monkeypatch: pytest.MonkeyPatch,
):
    captured: dict[str, Any] = {}

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured["argv"] = argv
        captured["env"] = kwargs["env"]
        captured["pass_fds"] = kwargs["pass_fds"]
        return subprocess.CompletedProcess(argv, 0, stdout="{}", stderr="")

    monkeypatch.setenv("PATH", "/bin")
    monkeypatch.setenv("HOME", "/home/test")
    monkeypatch.setenv("TMPDIR", "/tmp/test")
    monkeypatch.setenv("UNRELATED_SECRET", "do-not-copy")
    monkeypatch.setattr(runner.subprocess, "run", fake_run)

    result = runner.run_restic(
        ["snapshots"],
        repository="s3:safe-bucket/path",
        password="repo-password",
        restic_path=Path("/usr/bin/restic"),
        backend_env={
            "AWS_ACCESS_KEY_ID": "access-key",
            "AWS_SECRET_ACCESS_KEY": "backend-secret",
        },
        json=True,
        max_repack_size="1G",
    )

    assert result.argv == (
        "/usr/bin/restic",
        "snapshots",
        "--json",
        "--max-repack-size",
        "1G",
    )
    assert "--insecure-tls" not in result.argv
    assert all("repo-password" not in token for token in result.argv)
    assert all("backend-secret" not in token for token in result.argv)
    assert captured["env"] == {
        "PATH": "/bin",
        "HOME": "/home/test",
        "TMPDIR": "/tmp/test",
        "RESTIC_REPOSITORY": "s3:safe-bucket/path",
        "RESTIC_PASSWORD": "repo-password",
        "AWS_ACCESS_KEY_ID": "access-key",
        "AWS_SECRET_ACCESS_KEY": "backend-secret",
    }
    assert captured["pass_fds"] == ()


def test_run_restic_default_path_does_not_use_popen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, Any] = {}

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls["argv"] = argv
        calls["kwargs"] = kwargs
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    def fail_popen(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("default path should not use Popen")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    monkeypatch.setattr(runner.subprocess, "Popen", fail_popen)

    result = runner.run_restic(
        ["snapshots"],
        repository="s3:safe-bucket/path",
        password="repo-password",
        restic_path=Path("/usr/bin/restic"),
    )

    assert result.returncode == 0
    assert calls["argv"] == ["/usr/bin/restic", "snapshots"]
    assert calls["kwargs"]["text"] is True
    assert calls["kwargs"]["capture_output"] is True
    assert "input" not in calls["kwargs"]


def test_run_restic_threads_pass_fds(monkeypatch: pytest.MonkeyPatch):
    captured: dict[str, Any] = {}

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured["argv"] = argv
        captured["pass_fds"] = kwargs["pass_fds"]
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)

    result = runner.run_restic(
        ["key", "add", "--new-password-file", "/dev/fd/17"],
        repository="s3:safe-bucket/path",
        password="repo-password",
        restic_path=Path("/usr/bin/restic"),
        pass_fds=(17,),
    )

    assert result.returncode == 0
    assert captured["argv"] == [
        "/usr/bin/restic",
        "key",
        "add",
        "--new-password-file",
        "/dev/fd/17",
    ]
    assert captured["pass_fds"] == (17,)


def test_run_restic_opt_in_process_group_threads_stdin_and_scrubs_argv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class FakePopen:
        returncode = 0
        pid = 12345

        def __init__(self, argv: list[str], **kwargs: Any) -> None:
            captured["argv"] = argv
            captured["kwargs"] = kwargs

        def communicate(
            self,
            input: bytes | None = None,
            timeout: float | None = None,
        ) -> tuple[bytes, bytes]:
            captured["input"] = input
            captured["timeout"] = timeout
            return (
                b'{"message":"repo-url snapshot-id"}\n',
                b"stderr repo-url snapshot-id",
            )

    monkeypatch.setattr(runner.subprocess, "Popen", FakePopen)

    result = runner.run_restic(
        ["ls", "snapshot-id"],
        repository="s3:safe-bucket/path",
        password="repo-password",
        restic_path=Path("/usr/bin/restic"),
        process_group=True,
        stdin_bytes=b"payload",
        timeout=9,
        scrub_values=("repo-url", "snapshot-id"),
    )

    assert captured["kwargs"]["start_new_session"] is True
    assert captured["kwargs"]["close_fds"] is True
    assert captured["input"] == b"payload"
    assert captured["timeout"] == 9
    assert "snapshot-id" not in result.argv
    assert "snapshot-id" not in result.stdout
    assert "repo-url" not in result.stderr
    assert result.returncode == 0


def test_run_restic_json_records_parses_raw_before_scrub(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    raw_stdout = _jsonl(
        {"message_type": "snapshot", "id": SNAPSHOT_ID, "paths": [LOGICAL_SOURCE_PATH]},
        {"message_type": "node", "path": LOGICAL_SOURCE_PATH, "type": "file"},
    )

    class FakePopen:
        returncode = 0
        pid = 12345

        def __init__(self, argv: list[str], **kwargs: Any) -> None:
            captured["argv"] = argv
            captured["kwargs"] = kwargs

        def communicate(
            self,
            input: bytes | None = None,
            timeout: float | None = None,
        ) -> tuple[bytes, bytes]:
            captured["input"] = input
            captured["timeout"] = timeout
            return raw_stdout, b"stderr repo-url " + SNAPSHOT_ID.encode()

    monkeypatch.setattr(runner.subprocess, "Popen", FakePopen)

    result = runner.run_restic_json_records(
        ["ls", SNAPSHOT_ID],
        repository="s3:safe-bucket/path",
        password="repo-password",
        restic_path=Path("/usr/bin/restic"),
        backend_env={"AWS_SECRET_ACCESS_KEY": "backend-secret"},
        timeout=9,
        stdin_bytes=b"payload",
        scrub_values=("repo-url", SNAPSHOT_ID, LOGICAL_SOURCE_PATH),
    )

    assert captured["kwargs"]["start_new_session"] is True
    assert captured["kwargs"]["close_fds"] is True
    assert captured["kwargs"]["pass_fds"] == ()
    assert captured["input"] == b"payload"
    assert captured["timeout"] == 9
    assert captured["argv"][-1] == "--json"
    assert result.stdout == runner._RESTIC_JSON_STDOUT_REDACTED
    assert result.stderr == runner._RESTIC_JSON_STDERR_REDACTED
    assert SNAPSHOT_ID not in " ".join(result.argv)
    assert LOGICAL_SOURCE_PATH not in " ".join(result.argv)
    assert result.has_records is True
    records = result.consume_records()
    assert records[0]["id"] == SNAPSHOT_ID
    assert records[0]["paths"] == [LOGICAL_SOURCE_PATH]
    assert records[1]["path"] == LOGICAL_SOURCE_PATH
    assert result.has_records is False
    with pytest.raises(TypeError, match="unavailable"):
        result.consume_records()


@pytest.mark.parametrize(
    ("raw_stdout", "expected_records"),
    [
        (b'{"a":1}', ({"a": 1},)),
        (b'{"a":1}\n', ({"a": 1},)),
        (b'{"a":1}\n{"b":2}\n', ({"a": 1}, {"b": 2})),
        (b'{"a":1}\r\n{"b":2}\r\n', ({"a": 1}, {"b": 2})),
    ],
    ids=[
        "no_final_lf",
        "one_final_lf",
        "lf_records",
        "crlf_records",
    ],
)
def test_run_restic_json_records_accepts_lf_record_separators(
    monkeypatch: pytest.MonkeyPatch,
    raw_stdout: bytes,
    expected_records: tuple[object, ...],
) -> None:
    class FakePopen:
        returncode = 0
        pid = 12345

        def __init__(self, _argv: list[str], **_kwargs: Any) -> None:
            pass

        def communicate(
            self,
            input: bytes | None = None,
            timeout: float | None = None,
        ) -> tuple[bytes, bytes]:
            return raw_stdout, b""

    monkeypatch.setattr(runner.subprocess, "Popen", FakePopen)

    result = runner.run_restic_json_records(
        ["backup", "--stdin"],
        repository="s3:safe-bucket/path",
        password="repo-password",
        restic_path=Path("/usr/bin/restic"),
    )

    assert result.has_records is True
    assert result.consume_records() == expected_records


@pytest.mark.parametrize(
    "raw_stdout",
    [
        b'{"a":1}\x0b{"b":2}',
        b'{"a":1}\x0c{"b":2}',
        b'{"a":1}\x1c{"b":2}',
        b'{"a":1}\x1d{"b":2}',
        b'{"a":1}\x1e{"b":2}',
        b'{"a":1}\xc2\x85{"b":2}',
        b'{"a":1}\xe2\x80\xa8{"b":2}',
        b'{"a":1}\xe2\x80\xa9{"b":2}',
        b'\n{"a":1}',
        b'{"a":1}\n\n{"b":2}',
        b'{"a":1}\n\n',
        b'{"a":1}\r\n\r\n',
    ],
    ids=[
        "vt_separator",
        "ff_separator",
        "fs_separator",
        "gs_separator",
        "rs_separator",
        "nel_separator",
        "ls_separator",
        "ps_separator",
        "leading_blank",
        "middle_blank",
        "double_terminal_lf",
        "separator_only_trailing_crlf",
    ],
)
def test_run_restic_json_records_rejects_non_lf_record_boundaries(
    monkeypatch: pytest.MonkeyPatch,
    raw_stdout: bytes,
) -> None:
    class FakePopen:
        returncode = 0
        pid = 12345

        def __init__(self, _argv: list[str], **_kwargs: Any) -> None:
            pass

        def communicate(
            self,
            input: bytes | None = None,
            timeout: float | None = None,
        ) -> tuple[bytes, bytes]:
            return raw_stdout, b""

    monkeypatch.setattr(runner.subprocess, "Popen", FakePopen)

    result = runner.run_restic_json_records(
        ["backup", "--stdin"],
        repository="s3:safe-bucket/path",
        password="repo-password",
        restic_path=Path("/usr/bin/restic"),
    )

    assert result.has_records is False


@pytest.mark.parametrize(
    "raw_stdout",
    [
        b"",
        b"\xff",
        b'{"message_type":',
        b'\n{"message_type":"summary"}\n',
        b'{"message_type":"status"}\n\n{"message_type":"summary"}\n',
        b'{"message_type":"summary"}\n\n',
        b"NaN\n",
        b"Infinity\n",
        b"-Infinity\n",
        b'{"message_type":"summary","message_type":"summary"}\n',
        b'{"outer":{"middle":{"key":1,"key":2}}}\n',
    ],
    ids=[
        "empty",
        "invalid_utf8",
        "malformed",
        "blank_leading_record",
        "blank_middle_record",
        "blank_trailing_record",
        "nan",
        "infinity",
        "negative_infinity",
        "duplicate_top_level",
        "duplicate_nested_depth_two",
    ],
)
def test_run_restic_json_records_rejections_are_content_free(
    monkeypatch: pytest.MonkeyPatch,
    raw_stdout: bytes,
) -> None:
    canary = "spb/source.bin"

    class FakePopen:
        returncode = 0
        pid = 12345

        def __init__(self, _argv: list[str], **_kwargs: Any) -> None:
            pass

        def communicate(
            self,
            input: bytes | None = None,
            timeout: float | None = None,
        ) -> tuple[bytes, bytes]:
            return raw_stdout, b""

    monkeypatch.setattr(runner.subprocess, "Popen", FakePopen)

    result = runner.run_restic_json_records(
        ["backup", "--stdin-filename", f"/{canary}"],
        repository="s3:safe-bucket/path",
        password="repo-password",
        restic_path=Path("/usr/bin/restic"),
        scrub_values=(f"/{canary}",),
    )

    assert result.has_records is False
    assert result.stdout in {"", runner._RESTIC_JSON_STDOUT_REDACTED}
    with pytest.raises(TypeError) as excinfo:
        result.consume_records()
    assert canary not in str(excinfo.value)
    rendered = "".join(
        traceback.format_exception(
            type(excinfo.value),
            excinfo.value,
            excinfo.value.__traceback__,
        )
    )
    assert canary not in rendered


def test_run_restic_json_records_preserves_all_json_value_types(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_stdout = b'{"object":true}\n[1,2]\n"text"\n7\ntrue\nfalse\nnull\n'

    class FakePopen:
        returncode = 0
        pid = 12345

        def __init__(self, _argv: list[str], **_kwargs: Any) -> None:
            pass

        def communicate(
            self,
            input: bytes | None = None,
            timeout: float | None = None,
        ) -> tuple[bytes, bytes]:
            return raw_stdout, b""

    monkeypatch.setattr(runner.subprocess, "Popen", FakePopen)

    result = runner.run_restic_json_records(
        ["backup", "--stdin"],
        repository="s3:safe-bucket/path",
        password="repo-password",
        restic_path=Path("/usr/bin/restic"),
    )

    assert result.consume_records() == (
        {"object": True},
        [1, 2],
        "text",
        7,
        True,
        False,
        None,
    )


@pytest.mark.parametrize(
    ("mode", "expected_returncode", "expected_stderr"),
    [
        ("timeout", 124, runner._RESTIC_JSON_STDERR_REDACTED),
        ("nonzero", 7, runner._RESTIC_JSON_STDERR_REDACTED),
        (
            "cleanup_unverified",
            124,
            (
                f"{runner._RESTIC_JSON_STDERR_REDACTED}\n"
                f"{runner._PROCESS_GROUP_CLEANUP_UNVERIFIED}"
            ),
        ),
    ],
)
def test_run_restic_json_records_parse_gate_precedes_parser(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    expected_returncode: int,
    expected_stderr: str,
) -> None:
    parse_calls = 0

    def fake_parse(_raw_stdout: bytes | None) -> tuple[object, ...] | None:
        nonlocal parse_calls
        parse_calls += 1
        return ({"message_type": "summary"},)

    class FakePopen:
        pid = 12345

        def __init__(self, _argv: list[str], **_kwargs: Any) -> None:
            self.returncode = 7 if mode == "nonzero" else 0
            self._calls = 0

        def poll(self) -> None:
            return None

        def communicate(
            self,
            input: bytes | None = None,
            timeout: float | None = None,
        ) -> tuple[bytes, bytes]:
            self._calls += 1
            if mode in {"timeout", "cleanup_unverified"} and self._calls == 1:
                raise subprocess.TimeoutExpired(
                    ["restic"],
                    timeout=1,
                    output=b'{"message_type":"summary"}\n',
                    stderr=b"stderr",
                )
            return b'{"message_type":"summary"}\n', b"stderr"

    monkeypatch.setattr(runner, "_parse_json_records", fake_parse)
    monkeypatch.setattr(runner.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(
        runner,
        "_terminate_process_group",
        lambda *_args, **_kwargs: mode != "cleanup_unverified",
    )

    result = runner.run_restic_json_records(
        ["backup", "--stdin"],
        repository="s3:safe-bucket/path",
        password="repo-password",
        restic_path=Path("/usr/bin/restic"),
    )

    assert result.returncode == expected_returncode
    assert result.stderr == expected_stderr
    assert result.has_records is False
    assert parse_calls == 0


def test_restic_json_records_result_is_opaque_and_one_shot() -> None:
    canaries = ("SECRET-CANARY", SNAPSHOT_ID, LOGICAL_SOURCE_PATH)
    result = runner.ResticJsonRecordsResult(
        returncode=0,
        stdout=runner._RESTIC_JSON_STDOUT_REDACTED,
        stderr=runner._RESTIC_JSON_STDERR_REDACTED,
        argv=("restic", "[redacted]"),
        records=({"message_type": "summary", "secret": canaries[0]},),
    )
    same_shape = runner.ResticJsonRecordsResult(
        returncode=0,
        stdout=runner._RESTIC_JSON_STDOUT_REDACTED,
        stderr=runner._RESTIC_JSON_STDERR_REDACTED,
        argv=("restic", "[redacted]"),
        records=({"message_type": "summary", "secret": canaries[0]},),
    )

    assert not hasattr(result, "__dict__")
    assert repr(result) == "ResticJsonRecordsResult(<redacted>)"
    assert result == result
    assert result != same_shape
    assert hash(result) == id(result)
    for operation in (copy.copy, copy.deepcopy, pickle.dumps):
        with pytest.raises(TypeError) as excinfo:
            operation(result)
        assert excinfo.value.__cause__ is None
        rendered = "".join(
            traceback.format_exception(
                type(excinfo.value),
                excinfo.value,
                excinfo.value.__traceback__,
            )
        )
        for canary in canaries:
            assert canary not in str(excinfo.value)
            assert canary not in rendered

    records = result.consume_records()
    assert records == ({"message_type": "summary", "secret": canaries[0]},)
    assert result.has_records is False
    assert repr(result) == "ResticJsonRecordsResult(<redacted>)"
    refusing_operations = (
        vars,
        dataclasses.asdict,
        json.dumps,
        lambda value: value.consume_records(),
    )
    for operation in refusing_operations:
        with pytest.raises(TypeError) as excinfo:
            operation(result)
        assert excinfo.value.__cause__ is None
        rendered = "".join(
            traceback.format_exception(
                type(excinfo.value),
                excinfo.value,
                excinfo.value.__traceback__,
            )
        )
        for canary in canaries:
            assert canary not in str(excinfo.value)
            assert canary not in rendered
    assert repr(result) == "ResticJsonRecordsResult(<redacted>)"


def test_run_restic_json_records_api_exposes_no_parser_or_raw_escape_hatch() -> None:
    signature = inspect.signature(runner.run_restic_json_records)
    assert tuple(signature.parameters) == (
        "args",
        "repository",
        "password",
        "restic_path",
        "backend_env",
        "timeout",
        "stdin_bytes",
        "scrub_values",
        "terminate_grace_s",
        "kill_grace_s",
    )
    assert not any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )
    result = runner.ResticJsonRecordsResult(
        returncode=0,
        stdout="",
        stderr="",
        argv=("restic",),
        records=None,
    )
    for name in ("json", "raw_stdout", "raw_stderr", "raw_output", "records"):
        assert not hasattr(result, name)


def test_run_restic_scrubs_success_output_and_json(
    monkeypatch: pytest.MonkeyPatch,
):
    secrets = ("repo-password", "backend-secret", "access-key", "SESS-TOKEN")

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        stdout = json.dumps(
            {
                "message": (
                    "repo-password backend-secret access-key "
                    "SESS-TOKEN should be hidden"
                )
            }
        )
        stderr = "stderr has repo-password backend-secret and SESS-TOKEN"
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(runner.subprocess, "run", fake_run)

    result = runner.run_restic(
        ["snapshots"],
        repository="s3:safe-bucket/path",
        password="repo-password",
        restic_path=Path("/usr/bin/restic"),
        backend_env={
            "AWS_ACCESS_KEY_ID": "access-key",
            "AWS_SECRET_ACCESS_KEY": "backend-secret",
            "AWS_SESSION_TOKEN": "SESS-TOKEN",
        },
        json=True,
    )

    json_text = json.dumps(result.json)
    for secret in secrets:
        assert secret not in result.stdout
        assert secret not in result.stderr
        assert secret not in json_text
        assert all(secret not in token for token in result.argv)
    assert result.json == {
        "message": "[redacted] [redacted] [redacted] [redacted] should be hidden"
    }


def test_run_restic_returns_scrubbed_nonzero_result(
    monkeypatch: pytest.MonkeyPatch,
):
    secrets = ("repo-password", "backend-secret")

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            argv,
            42,
            stdout="",
            stderr="failed with repo-password and backend-secret",
        )

    monkeypatch.setattr(runner.subprocess, "run", fake_run)

    result = runner.run_restic(
        ["backup", "/tmp/data"],
        repository="s3:safe-bucket/path",
        password="repo-password",
        restic_path=Path("/usr/bin/restic"),
        backend_env={"AWS_SECRET_ACCESS_KEY": "backend-secret"},
    )

    assert result.returncode == 42
    json_text = json.dumps(result.json)
    for secret in secrets:
        assert secret not in result.stdout
        assert secret not in result.stderr
        assert secret not in json_text
        assert all(secret not in token for token in result.argv)
    assert "[redacted]" in result.stderr


def test_empty_backend_values_are_not_scrubbed(
    monkeypatch: pytest.MonkeyPatch,
):
    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert kwargs["env"]["EMPTY"] == ""
        assert "NONE" not in kwargs["env"]
        return subprocess.CompletedProcess(argv, 0, stdout="abc", stderr="")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)

    result = runner.run_restic(
        ["snapshots"],
        repository="s3:safe-bucket/path",
        password="repo-password",
        restic_path=Path("/usr/bin/restic"),
        backend_env={"EMPTY": "", "NONE": None},
    )

    assert result.stdout == "abc"


def test_run_restic_rejects_insecure_tls(monkeypatch: pytest.MonkeyPatch):
    def fail_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise AssertionError("subprocess should not be called")

    monkeypatch.setattr(runner.subprocess, "run", fail_run)

    with pytest.raises(RuntimeError, match="--insecure-tls"):
        runner.run_restic(
            ["backup", "--insecure-tls", "/tmp/data"],
            repository="s3:safe-bucket/path",
            password="repo-password",
            restic_path=Path("/usr/bin/restic"),
        )


def test_run_restic_rejects_secret_in_argv(monkeypatch: pytest.MonkeyPatch):
    def fail_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise AssertionError("subprocess should not be called")

    monkeypatch.setattr(runner.subprocess, "run", fail_run)

    with pytest.raises(RuntimeError, match="argv contains a secret"):
        runner.run_restic(
            ["backup", "/tmp/repo-password/data"],
            repository="s3:safe-bucket/path",
            password="repo-password",
            restic_path=Path("/usr/bin/restic"),
        )


def test_run_restic_timeout_returns_scrubbed_result(
    monkeypatch: pytest.MonkeyPatch,
):
    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(
            argv,
            timeout=1,
            output=b'{"message":"repo-password backend-secret SESS-TOKEN"}',
            stderr=b"stderr repo-password backend-secret SESS-TOKEN",
        )

    monkeypatch.setattr(runner.subprocess, "run", fake_run)

    result = runner.run_restic(
        ["backup", "/tmp/data"],
        repository="s3:safe-bucket/path",
        password="repo-password",
        restic_path=Path("/usr/bin/restic"),
        backend_env={
            "AWS_SECRET_ACCESS_KEY": "backend-secret",
            "AWS_SESSION_TOKEN": "SESS-TOKEN",
        },
        json=True,
        timeout=1,
    )

    assert result.returncode == 124
    assert "repo-password" not in result.stdout
    assert "backend-secret" not in result.stdout
    assert "SESS-TOKEN" not in result.stdout
    assert "repo-password" not in result.stderr
    assert "backend-secret" not in result.stderr
    assert "SESS-TOKEN" not in result.stderr
    assert result.json is None


@pytest.mark.parametrize("returncode", [0, 7, 124])
def test_run_restic_popen_normal_completion_parses_json_for_returncode(
    monkeypatch: pytest.MonkeyPatch,
    returncode: int,
) -> None:
    class FakePopen:
        pid = 12345

        def __init__(self, _argv: list[str], **_kwargs: Any) -> None:
            self.returncode = returncode

        def communicate(
            self,
            input: bytes | None = None,
            timeout: float | None = None,
        ) -> tuple[bytes, bytes]:
            return b'{"message":"repo-password"}', b"stderr repo-password"

    monkeypatch.setattr(runner.subprocess, "Popen", FakePopen)

    result = runner.run_restic(
        ["backup", "/tmp/data"],
        repository="s3:safe-bucket/path",
        password="repo-password",
        restic_path=Path("/usr/bin/restic"),
        json=True,
        process_group=True,
    )

    assert result.returncode == returncode
    assert "repo-password" not in result.stdout
    assert "repo-password" not in result.stderr
    assert result.json == {"message": "[redacted]"}


def test_run_restic_popen_timeout_json_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakePopen:
        pid = 12345

        def __init__(self, _argv: list[str], **_kwargs: Any) -> None:
            self.returncode = 0
            self._calls = 0

        def poll(self) -> None:
            return None

        def communicate(
            self,
            input: bytes | None = None,
            timeout: float | None = None,
        ) -> tuple[bytes, bytes]:
            self._calls += 1
            if self._calls == 1:
                raise subprocess.TimeoutExpired(
                    ["restic"],
                    timeout=1,
                    output=b'{"message":"repo-password"}',
                    stderr=b"stderr repo-password",
                )
            return b'{"message":"repo-password"}', b"stderr repo-password"

    monkeypatch.setattr(runner.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(
        runner,
        "_terminate_process_group",
        lambda *_args, **_kwargs: True,
    )

    result = runner.run_restic(
        ["backup", "/tmp/data"],
        repository="s3:safe-bucket/path",
        password="repo-password",
        restic_path=Path("/usr/bin/restic"),
        json=True,
        process_group=True,
        timeout=1,
    )

    assert result.returncode == 124
    assert "repo-password" not in result.stdout
    assert "repo-password" not in result.stderr
    assert result.json is None


def test_parse_json_lines_from_scrubbed_stdout(
    monkeypatch: pytest.MonkeyPatch,
):
    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        stdout = '{"message":"backend-secret"}\n{"message":"ok"}\n'
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)

    result = runner.run_restic(
        ["snapshots"],
        repository="s3:safe-bucket/path",
        password="repo-password",
        restic_path=Path("/usr/bin/restic"),
        backend_env={"AWS_SECRET_ACCESS_KEY": "backend-secret"},
        json=True,
    )

    assert result.json == [{"message": "[redacted]"}, {"message": "ok"}]


def test_run_restic_result_shape_and_dataclass_behavior_are_frozen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            argv,
            3,
            stdout='{"message":"repo-password backend-secret","status":"ok"}',
            stderr="stderr repo-password backend-secret",
        )

    monkeypatch.setattr(runner.subprocess, "run", fake_run)

    result = runner.run_restic(
        ["backup", "/tmp/data"],
        repository="s3:safe-bucket/path",
        password="repo-password",
        restic_path=Path("/usr/bin/restic"),
        backend_env={"AWS_SECRET_ACCESS_KEY": "backend-secret"},
        json=True,
    )
    same = runner.ResticResult(
        3,
        result.stdout,
        result.stderr,
        result.json,
        result.argv,
    )

    assert tuple(field.name for field in dataclasses.fields(runner.ResticResult)) == (
        "returncode",
        "stdout",
        "stderr",
        "json",
        "argv",
    )
    assert result.returncode == 3
    assert result.stdout == '{"message":"[redacted] [redacted]","status":"ok"}'
    assert result.stderr == "stderr [redacted] [redacted]"
    assert result.json == {"message": "[redacted] [redacted]", "status": "ok"}
    assert result.argv == ("/usr/bin/restic", "backup", "/tmp/data", "--json")
    assert result == same
    assert repr(result).startswith("ResticResult(")
    assert dataclasses.asdict(result) == {
        "returncode": 3,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "json": result.json,
        "argv": result.argv,
    }
    assert pickle.loads(pickle.dumps(result)) == result


@pytest.mark.parametrize(
    ("returncode", "reason"),
    [
        (3, "incomplete"),
        (10, "repo_missing"),
        (11, "locked"),
        (12, "auth_failed"),
        (124, "timeout"),
        (77, "failed"),
    ],
)
def test_reason_for_returncode(returncode: int, reason: str) -> None:
    assert runner.reason_for_returncode(returncode) == reason


def test_select_summary_from_dict_or_json_lines() -> None:
    assert runner.select_summary({"message_type": "summary", "snapshot_id": "one"}) == {
        "message_type": "summary",
        "snapshot_id": "one",
    }
    assert runner.select_summary(
        [
            {"message_type": "status", "percent_done": 50},
            {"message_type": "summary", "snapshot_id": "two"},
        ]
    ) == {"message_type": "summary", "snapshot_id": "two"}
    assert runner.select_summary({"message_type": "status"}) is None

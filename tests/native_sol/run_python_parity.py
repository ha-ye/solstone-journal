# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import argparse
import copy
import io
import json
import os
import sys
import tempfile
import threading
from collections.abc import Iterable
from contextlib import contextmanager, nullcontext, redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any
from unittest.mock import patch

from typer.testing import CliRunner

from solstone.apps.activities import call as activities_call
from solstone.apps.awareness import call as awareness_call
from solstone.apps.body import call as body_call
from solstone.apps.facets import call as facets_call
from solstone.apps.sol import call as sol_call
from solstone.apps.support import call as support_call
from solstone.think import chat_cli
from solstone.think.call import call_app
from solstone.think.convey_client import (
    ConveyClientError,
    ConveyTimeoutError,
    ConveyUnreachableError,
)
from solstone.think.tools import health as health_call
from solstone.think.tools import ledger as ledger_call
from solstone.think.tools import profile as profile_call
from solstone.think.utils import require_solstone

REPO_ROOT = Path(__file__).resolve().parents[2]
PARITY_DIR = REPO_ROOT / "core/fixtures/native-sol/parity"


class ScriptedConveyClient:
    def __init__(self, vector: dict[str, Any]) -> None:
        self._surface = vector.get("surface", "sol-call")
        self._requests = list(vector.get("transport", {}).get("requests", []))
        self.chat_controller: ChatController | None = None
        self.recorded: list[dict[str, Any]] = []

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: object | None = None,
    ) -> Any:
        actual = {
            "method": method,
            "path": path,
            "query": ordered_query_pairs(params or {}),
            "json": json,
            "headers": [],
            "timeout_policy": self._timeout_policy(path),
        }
        self.recorded.append(actual)
        if not self._requests:
            raise AssertionError(f"unexpected request {actual!r}")
        expected = self._requests.pop(0)
        expected_shape = request_shape(expected)
        if actual != expected_shape:
            raise AssertionError(
                f"request shape mismatch\nactual={actual!r}\nexpected={expected_shape!r}"
            )
        if "fault" in expected:
            raise_fault(expected["fault"])
        return expected["response"]["json"]

    def open_sse(self) -> Iterable[bytes] | None:
        actual = {
            "method": "SSE",
            "path": "/sse/events",
            "headers": [],
            "timeout_policy": "sse-open",
        }
        self.recorded.append(actual)
        if not self._requests:
            raise AssertionError(f"unexpected SSE open {actual!r}")
        expected = self._requests.pop(0)
        expected_shape = sse_shape(expected)
        if actual != expected_shape:
            raise AssertionError(
                f"SSE shape mismatch\nactual={actual!r}\nexpected={expected_shape!r}"
            )
        if "fault" in expected:
            return None
        if self.chat_controller is not None:
            return self.chat_controller.open_stream(expected)
        return [chunk.encode("utf-8") for chunk in expected.get("chunks", [])]

    def upload(
        self,
        path: str,
        *,
        files: dict[str, tuple[str, str, object]],
        data: dict[str, object] | None = None,
    ) -> Any:
        multipart_files = []
        for field_name, (filename, file_path, content_type) in files.items():
            multipart_files.append(
                {
                    "field_name": field_name,
                    "filename": filename,
                    "content_type": content_type,
                    "length": Path(file_path).stat().st_size,
                }
            )
        actual = {
            "method": "UPLOAD",
            "path": path,
            "multipart": {
                "files": multipart_files,
                "data": [[str(key), str(value)] for key, value in (data or {}).items()],
            },
            "headers": [],
            "timeout_policy": "upload",
        }
        self.recorded.append(actual)
        if not self._requests:
            raise AssertionError(f"unexpected upload {actual!r}")
        expected = self._requests.pop(0)
        expected_shape = upload_shape(expected)
        if actual != expected_shape:
            raise AssertionError(
                f"upload shape mismatch\nactual={actual!r}\nexpected={expected_shape!r}"
            )
        if "fault" in expected:
            raise_fault(expected["fault"])
        return expected["response"]["json"]

    def assert_done(self) -> None:
        if self._requests:
            raise AssertionError(f"unused scripted requests: {self._requests!r}")

    def _timeout_policy(self, path: str) -> str:
        if self._surface == "sol-chat" and path in {"/api/chat", "/api/chat/session"}:
            return "chat-post"
        return "api"


def request_shape(request: dict[str, Any]) -> dict[str, Any]:
    return {
        "method": request["method"],
        "path": request["path"],
        "query": request.get("query", []),
        "json": request.get("json"),
        "headers": request.get("headers", []),
        "timeout_policy": request.get("timeout_policy", "api"),
    }


def upload_shape(request: dict[str, Any]) -> dict[str, Any]:
    return {
        "method": "UPLOAD",
        "path": request["path"],
        "multipart": request["multipart"],
        "headers": request.get("headers", []),
        "timeout_policy": request.get("timeout_policy", "upload"),
    }


def sse_shape(request: dict[str, Any]) -> dict[str, Any]:
    return {
        "method": "SSE",
        "path": request["path"],
        "headers": request.get("headers", []),
        "timeout_policy": request.get("timeout_policy", "sse-open"),
    }


def raise_fault(fault: dict[str, Any]) -> None:
    if fault.get("kind") == "service_down":
        # Faithfully reproduce the pre-client require_solstone() gate that the
        # live ConveyClient hits on a transport error when solstone is down.
        # is_solstone_up is patched False in patched_runtime, so this prints the
        # shared service-down line and exits 1 exactly like the real CLI — the
        # generic `unreachable` fault below bypasses this gate and is not
        # evidence for real service-down output.
        require_solstone()
        raise AssertionError("require_solstone() did not exit for service_down fault")
    if fault.get("kind") == "timeout":
        raise ConveyTimeoutError(detail=fault.get("detail"))
    if fault.get("kind") == "unreachable":
        raise ConveyUnreachableError(
            fault.get("error") or "I couldn't reach the journal over HTTP.",
            reason_code=fault.get("reason_code"),
            detail=fault.get("detail"),
            status=fault.get("status"),
            payload=fault.get("payload"),
        )
    if fault.get("kind") == "malformed_success":
        raise ConveyClientError(
            fault.get("error") or "I couldn't read the journal response.",
            reason_code=fault.get("reason_code"),
            detail=fault.get("detail"),
            status=fault.get("status", 200),
            payload=fault.get("payload"),
        )
    raise ConveyClientError(
        fault.get("error") or fault.get("reason_code") or "error",
        reason_code=fault.get("reason_code"),
        detail=fault.get("detail"),
        status=fault.get("status"),
        payload=fault.get("payload"),
    )


def ordered_query_pairs(params: dict[str, Any]) -> list[list[str]]:
    pairs: list[list[str]] = []
    for key, value in params.items():
        if isinstance(value, list | tuple):
            pairs.extend([[str(key), str(item)] for item in value])
        else:
            pairs.append([str(key), str(value)])
    return pairs


def load_vectors(paths: Iterable[Path]) -> list[dict[str, Any]]:
    vectors: list[dict[str, Any]] = []
    for path in paths:
        for line in path.read_text().splitlines():
            if line.strip():
                vectors.append(json.loads(line))
    return vectors


def run_vector(
    vector: dict[str, Any], *, assert_expected: bool = True
) -> dict[str, Any]:
    vector = copy.deepcopy(vector)
    with tempfile.TemporaryDirectory() as temp_dir:
        materialize_files(vector, Path(temp_dir))
        return run_materialized_vector(vector, assert_expected=assert_expected)


def run_materialized_vector(
    vector: dict[str, Any], *, assert_expected: bool = True
) -> dict[str, Any]:
    client = ScriptedConveyClient(vector)
    env = {key: str(value) for key, value in vector.get("env", {}).items()}
    today = vector.get("clock", {}).get("today", "20260723")
    runner = CliRunner()
    with patched_runtime(client, env, today, vector):
        if vector.get("surface") == "sol-chat":
            result = run_chat(vector)
        else:
            result = runner.invoke(
                call_app,
                vector["argv"],
                input=vector.get("stdin", ""),
                env=env,
            )
    client.assert_done()
    actual = {
        "stdout": result.stdout,
        "stderr": result.stderr,
        "exit": result.exit_code,
        "requests": client.recorded,
    }
    if assert_expected:
        expected = vector["expected"]
        normalizations = [str(item) for item in vector.get("normalizations", [])]
        if normalize_result(actual, normalizations) != normalize_result(
            expected, normalizations
        ):
            raise AssertionError(
                f"{vector['id']} parity mismatch\nactual={actual!r}\nexpected={expected!r}"
            )
    return actual


def run_chat(vector: dict[str, Any]) -> Any:
    stdout = io.StringIO()
    stderr = io.StringIO()
    exit_code = 0
    with (
        patch.object(sys, "argv", ["sol chat", *vector["argv"]]),
        nullcontext(),
        redirect_stdout(stdout),
        redirect_stderr(stderr),
    ):
        try:
            chat_cli.main()
        except SystemExit as error:
            exit_code = int(error.code or 0)
    return argparse.Namespace(
        stdout=stdout.getvalue(),
        stderr=stderr.getvalue(),
        exit_code=exit_code,
    )


class FakeMonotonic:
    def __init__(self) -> None:
        self.value = 0.0

    def monotonic(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class ChatController:
    def __init__(self, vector: dict[str, Any], clock: FakeMonotonic) -> None:
        self.inputs = chat_inputs(vector)
        self.clock = clock
        self.stream: ScriptedSseStream | None = None
        self.events: list[ControlledEvent] = []

    def event(self) -> "ControlledEvent":
        event = ControlledEvent(self, len(self.events))
        self.events.append(event)
        return event

    @property
    def done_event(self) -> "ControlledEvent | None":
        return self.events[0] if self.events else None

    def open_stream(self, request: dict[str, Any]) -> "ScriptedSseStream":
        del request
        self.stream = ScriptedSseStream()
        return self.stream

    def step(self, timeout: float | None) -> None:
        timeout = float(timeout or 0)
        while self.inputs:
            item = self.inputs.pop(0)
            kind = item["kind"]
            if kind == "sse-chunk":
                if self.stream is not None:
                    self.stream.push(
                        str(item["value"]).encode("utf-8"), self.done_event
                    )
                if self.done_event is not None and self.done_event.is_set():
                    return
                continue
            if kind == "sse-ended":
                if self.stream is not None:
                    self.stream.close(self.done_event)
                continue
            if kind == "interrupt":
                raise KeyboardInterrupt
            if kind == "poll":
                self.clock.advance(timeout)
                return
            raise AssertionError(f"unsupported chat input kind {kind!r}")
        self.clock.advance(timeout)


class ControlledEvent:
    def __init__(self, controller: ChatController, index: int) -> None:
        self._controller = controller
        self._index = index
        self._set = False

    def set(self) -> None:
        self._set = True

    def is_set(self) -> bool:
        return self._set

    def wait(self, timeout: object = None) -> bool:
        if self._index == 0 and not self._set:
            self._controller.step(float(timeout or 0))
        return self._set


class ScriptedSseStream:
    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._chunks: list[bytes] = []
        self._closed = False
        self._stopped = False
        self._waiting = False
        self._waiting_generation = 0

    def __iter__(self) -> "ScriptedSseStream":
        return self

    def __next__(self) -> bytes:
        with self._condition:
            while not self._chunks and not self._closed:
                self._waiting = True
                self._waiting_generation += 1
                self._condition.notify_all()
                self._condition.wait()
            self._waiting = False
            if self._chunks:
                return self._chunks.pop(0)
            self._stopped = True
            self._condition.notify_all()
            raise StopIteration

    def push(self, chunk: bytes, done_event: ControlledEvent | None) -> None:
        with self._condition:
            start_generation = self._waiting_generation
            self._chunks.append(chunk)
            self._condition.notify_all()
            self._condition.wait_for(
                lambda: (
                    self._waiting_generation > start_generation
                    or self._closed
                    or bool(done_event and done_event.is_set())
                ),
                timeout=1,
            )

    def close(self, done_event: ControlledEvent | None) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify_all()
            self._condition.wait_for(
                lambda: self._stopped or bool(done_event and done_event.is_set()),
                timeout=1,
            )


def chat_inputs(vector: dict[str, Any]) -> list[dict[str, Any]]:
    explicit = vector.get("chat_inputs")
    if isinstance(explicit, list):
        output: list[dict[str, Any]] = []
        for item in explicit:
            count = int(item.get("count", 1))
            for _ in range(count):
                if item.get("kind") == "sse-event":
                    output.append(
                        {"kind": "sse-chunk", "value": sse_frame(item["value"])}
                    )
                else:
                    single = dict(item)
                    single.pop("count", None)
                    output.append(single)
        return output
    for request in vector.get("transport", {}).get("requests", []):
        if request.get("method") == "SSE":
            return [
                {"kind": "sse-chunk", "value": chunk}
                for chunk in request.get("chunks", [])
            ] + [{"kind": "sse-ended"}]
    return []


def sse_frame(value: object) -> str:
    return (
        "data: " + json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n\n"
    )


def materialize_files(vector: dict[str, Any], temp_dir: Path) -> None:
    unreadable = {str(path) for path in vector.get("unreadable_files", [])}
    for relative, body in vector.get("files", {}).items():
        path = temp_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(body))
        if relative in unreadable:
            path.chmod(0)
    vector["argv"] = [expand_file_token(str(arg), temp_dir) for arg in vector["argv"]]


def expand_file_token(value: str, temp_dir: Path) -> str:
    return value.replace("{files}", str(temp_dir))


@contextmanager
def patched_runtime(
    client: ScriptedConveyClient,
    env: dict[str, str],
    today: str,
    vector: dict[str, Any],
):
    real_datetime = activities_call.datetime

    class FixedDateTime(real_datetime):  # type: ignore[misc, valid-type]
        @classmethod
        def now(cls, tz: object = None) -> "FixedDateTime":
            del tz
            return cls.strptime(today, "%Y%m%d")

    original_get_client = activities_call.get_client
    original_datetime = activities_call.datetime
    original_awareness_get_client = awareness_call.get_client
    original_body_get_client = body_call.get_client
    original_facets_get_client = facets_call.get_client
    original_health_get_client = health_call.get_client
    original_health_datetime = health_call.datetime
    original_ledger_get_client = ledger_call.get_client
    original_profile_get_client = profile_call.get_client
    original_sol_get_client = sol_call.get_client
    original_support_get_client = support_call.get_client
    original_local_build_identity = support_call._local_build_identity
    original_build_client = chat_cli._build_client
    original_open_sse = chat_cli._open_sse
    original_resolve_base_url = chat_cli.resolve_base_url
    original_poll_seconds = chat_cli.POLL_SECONDS
    original_idle_ceiling = chat_cli.IDLE_CEILING_SECONDS
    original_thread = chat_cli.threading.Thread
    original_event = chat_cli.threading.Event
    original_monotonic = chat_cli.time.monotonic
    fake_monotonic = FakeMonotonic()
    chat_controller = ChatController(vector, fake_monotonic)
    client.chat_controller = chat_controller

    class ControlledThread:
        def __init__(self, *, target: object, daemon: bool = False) -> None:
            chat_cli.threading.Event = original_event
            try:
                self._thread = original_thread(target=target, daemon=daemon)
            finally:
                chat_cli.threading.Event = chat_controller.event

        def start(self) -> None:
            self._thread.start()

    activities_call.get_client = lambda: client
    activities_call.datetime = FixedDateTime
    awareness_call.get_client = lambda: client
    body_call.get_client = lambda: client
    facets_call.get_client = lambda: client
    health_call.get_client = lambda: client
    health_call.datetime = FixedDateTime
    ledger_call.get_client = lambda: client
    profile_call.get_client = lambda: client
    sol_call.get_client = lambda: client
    support_call.get_client = lambda: client
    support_call._local_build_identity = lambda: build_identity_fixture()
    chat_cli._build_client = lambda _base_url: client
    chat_cli._open_sse = lambda _base_url: client.open_sse()
    chat_cli.resolve_base_url = lambda: "http://localhost:5015"
    chat_cli.threading.Thread = ControlledThread
    chat_cli.threading.Event = chat_controller.event
    chat_cli.time.monotonic = fake_monotonic.monotonic
    with (
        patch.dict(os.environ, env, clear=True),
        patch("solstone.think.identity.ensure_identity_directory", lambda: None),
        patch("solstone.think.utils.is_solstone_up", return_value=False),
        patch("subprocess.Popen", blocked_spawn),
        patch("subprocess.run", blocked_spawn),
        patch("subprocess.check_output", blocked_spawn),
    ):
        try:
            yield
        finally:
            activities_call.get_client = original_get_client
            activities_call.datetime = original_datetime
            awareness_call.get_client = original_awareness_get_client
            body_call.get_client = original_body_get_client
            facets_call.get_client = original_facets_get_client
            health_call.get_client = original_health_get_client
            health_call.datetime = original_health_datetime
            ledger_call.get_client = original_ledger_get_client
            profile_call.get_client = original_profile_get_client
            sol_call.get_client = original_sol_get_client
            support_call.get_client = original_support_get_client
            support_call._local_build_identity = original_local_build_identity
            chat_cli._build_client = original_build_client
            chat_cli._open_sse = original_open_sse
            chat_cli.resolve_base_url = original_resolve_base_url
            chat_cli.POLL_SECONDS = original_poll_seconds
            chat_cli.IDLE_CEILING_SECONDS = original_idle_ceiling
            chat_cli.threading.Thread = original_thread
            chat_cli.threading.Event = original_event
            chat_cli.time.monotonic = original_monotonic
            client.chat_controller = None


def normalize_result(
    result: dict[str, Any], normalizations: list[str]
) -> dict[str, Any]:
    result = copy.deepcopy(result)
    for normalization in normalizations:
        if normalization == "invalid-json-error-tail":
            normalize_invalid_json_error_tail(result)
        elif normalization == "unreadable-file-error":
            normalize_unreadable_file_error(result)
        else:
            raise AssertionError(f"unsupported normalization {normalization!r}")
    return result


def normalize_invalid_json_error_tail(result: dict[str, Any]) -> None:
    prefix = "Error: invalid JSON on stdin:"
    stderr = str(result.get("stderr", ""))
    if stderr.startswith(prefix):
        result["stderr"] = f"{prefix} <parser error>\n"


def normalize_unreadable_file_error(result: dict[str, Any]) -> None:
    stderr = str(result.get("stderr", ""))
    if (
        "not readable" in stderr
        or "file is not readable" in stderr
        or ("Invalid value for 'FILES...'" in stderr and "readable" in stderr)
    ):
        result["stderr"] = "Error: unreadable fixture file\n"


def build_identity_fixture() -> dict[str, object]:
    return {
        "version": "9.9.9",
        "revision": "abc123",
        "platform": {
            "system": "TestOS",
            "release": "1.0",
            "machine": "test64",
            "python": "3.test",
        },
    }


def blocked_spawn(*_args: object, **_kwargs: object) -> None:
    raise AssertionError("process spawning is disabled in native sol parity vectors")


def write_vectors(path: Path, vectors: list[dict[str, Any]]) -> None:
    path.write_text(
        "".join(
            json.dumps(vector, ensure_ascii=False, separators=(",", ":")) + "\n"
            for vector in vectors
        )
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run or refresh native sol Python parity vectors."
    )
    parser.add_argument(
        "paths", nargs="*", type=Path, default=sorted(PARITY_DIR.glob("*.jsonl"))
    )
    parser.add_argument(
        "--bless",
        action="store_true",
        help="Rewrite expected stdout/stderr/exit/request captures from Python.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    for path in args.paths:
        vectors = load_vectors([path])
        for vector in vectors:
            actual = run_vector(vector, assert_expected=not args.bless)
            if args.bless:
                vector["expected"] = actual
        if args.bless:
            write_vectors(path, vectors)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

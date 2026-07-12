# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from scripts import spp_ratls_loopback_e2e as harness

CURRENT_DEFAULT_REQUEST = (
    b"POST /v1/chat/completions HTTP/1.1\r\n"
    b"Host: spp-engine\r\nContent-Length: 2\r\n\r\n{}"
)


def _nvattest_root(tmp_path: Path) -> Path:
    root = tmp_path / "nvattest"
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "nvattest").write_text("#!/bin/sh\n", encoding="utf-8")
    (root / "lib").mkdir()
    return root


def _request_body(tmp_path: Path, body: bytes = b'{"messages":[]}') -> Path:
    path = tmp_path / "request.json"
    path.write_bytes(body)
    return path


def _assert_main_rejects_before_io(
    monkeypatch: pytest.MonkeyPatch,
    args: list[str],
) -> None:
    monkeypatch.setattr(
        harness,
        "_load_gateway_module",
        lambda *_args, **_kwargs: pytest.fail("gateway loaded before validation"),
    )
    with pytest.raises(SystemExit) as exc_info:
        harness.main(args)
    assert exc_info.value.code != 0


def test_build_chat_completions_request_matches_current_default_literal() -> None:
    assert harness.build_chat_completions_request(b"{}") == CURRENT_DEFAULT_REQUEST


def test_build_chat_completions_request_preserves_raw_body_bytes() -> None:
    body = b'{\n  "messages": [{"role": "user", "content": "raw"}]\n}'
    request = harness.build_chat_completions_request(body)

    assert request.endswith(b"\r\n\r\n" + body)
    assert b"Content-Length: 54\r\n" in request


@pytest.mark.parametrize(
    "extra_args",
    [
        ["--nvattest-dir", "nvattest"],
        ["--upstream-port", "9443"],
        ["--request-body", "request.json"],
    ],
)
def test_default_mode_rejects_real_only_args_before_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    extra_args: list[str],
) -> None:
    _assert_main_rejects_before_io(
        monkeypatch,
        [
            "--gateway",
            str(tmp_path / "gateway.py"),
            "--collector",
            str(tmp_path / "collector.py"),
        ]
        + extra_args,
    )


@pytest.mark.parametrize(
    "present_args",
    [
        ["--upstream-port", "9443", "--request-body", "request.json"],
        ["--nvattest-dir", "nvattest", "--request-body", "request.json"],
        ["--nvattest-dir", "nvattest", "--upstream-port", "9443"],
    ],
)
def test_real_mode_rejects_missing_required_args_before_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    present_args: list[str],
) -> None:
    _assert_main_rejects_before_io(
        monkeypatch,
        [
            "--gateway",
            str(tmp_path / "gateway.py"),
            "--collector",
            str(tmp_path / "collector.py"),
            "--real",
        ]
        + present_args,
    )


@pytest.mark.parametrize(
    "request_path_factory",
    [
        lambda tmp_path: tmp_path / "missing.json",
        lambda tmp_path: _request_body(tmp_path, b"{not json"),
    ],
)
def test_real_mode_rejects_unreadable_or_malformed_request_body_before_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request_path_factory,
) -> None:
    _assert_main_rejects_before_io(
        monkeypatch,
        [
            "--gateway",
            str(tmp_path / "gateway.py"),
            "--collector",
            str(tmp_path / "collector.py"),
            "--real",
            "--nvattest-dir",
            str(_nvattest_root(tmp_path)),
            "--upstream-port",
            "9443",
            "--request-body",
            str(request_path_factory(tmp_path)),
        ],
    )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda root: None,
        lambda root: (root / "bin").mkdir(parents=True),
        lambda root: (
            (root / "bin").mkdir(parents=True),
            (root / "bin" / "nvattest").write_text("", encoding="utf-8"),
        ),
    ],
)
def test_real_mode_rejects_invalid_nvattest_root_before_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutate,
) -> None:
    root = tmp_path / "invalid-nvattest"
    mutate(root)
    _assert_main_rejects_before_io(
        monkeypatch,
        [
            "--gateway",
            str(tmp_path / "gateway.py"),
            "--collector",
            str(tmp_path / "collector.py"),
            "--real",
            "--nvattest-dir",
            str(root),
            "--upstream-port",
            "9443",
            "--request-body",
            str(_request_body(tmp_path)),
        ],
    )


def test_real_mode_context_uses_production_verifiers_without_stubbing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = harness.RealModeConfig(
        nvattest_dir=_nvattest_root(tmp_path),
        upstream_port=9443,
        request_body=b"{}",
    )
    production_quote = harness.ratls_verify.verify_quote
    monkeypatch.setattr(
        harness,
        "_stub_verifiers",
        lambda: pytest.fail("real mode installed stubs"),
    )
    captured = {}

    def fake_establish(endpoint, **kwargs):
        captured.update(kwargs)
        captured["endpoint"] = endpoint
        return object()

    monkeypatch.setattr(harness, "establish_attested_channel", fake_establish)

    context = harness.make_run_context(config)
    context.establish(9443)

    assert harness.ratls_verify.verify_quote is production_quote
    assert captured["composite_verifier"] is harness.verify_composite
    assert captured["nvattest_dir"] == config.nvattest_dir


def test_real_mode_random_nonce_differs_across_establish_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = harness.RealModeConfig(
        nvattest_dir=_nvattest_root(tmp_path),
        upstream_port=9443,
        request_body=b"{}",
    )
    nonces = [b"a" * harness.OWNER_NONCE_BYTES, b"b" * harness.OWNER_NONCE_BYTES]
    observed = []
    monkeypatch.setattr(harness.secrets, "token_bytes", lambda _size: nonces.pop(0))

    def fake_establish(_endpoint, **kwargs):
        observed.append(kwargs["owner_nonce"])
        return object()

    monkeypatch.setattr(harness, "establish_attested_channel", fake_establish)

    context = harness.make_run_context(config)
    context.establish(1)
    context.establish(2)

    assert observed == [
        b"a" * harness.OWNER_NONCE_BYTES,
        b"b" * harness.OWNER_NONCE_BYTES,
    ]
    assert observed[0] != observed[1]


def test_real_mode_builds_same_five_cases_with_same_expected_reasons(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = harness.RunContext(
        establish=lambda _port: object(),
        upstream_port=9443,
        request_body=b"{}",
        banner=harness.REAL_BANNER,
    )
    observed = []

    def fake_positive(gateway_path, collector_path, run_context):
        observed.append(("positive", gateway_path, collector_path, run_context))
        return "verified substrate=AMD SEV-SNP + NVIDIA test"

    def fake_adversarial(
        gateway_module,
        collector_path,
        run_context,
        mode,
        expected_reason,
    ):
        observed.append(
            (mode, expected_reason, gateway_module, collector_path, run_context)
        )
        return expected_reason

    def fake_premature(gateway_path, collector_path, run_context):
        observed.append(
            ("premature-inference", gateway_path, collector_path, run_context)
        )
        return "protocol_or_tls_rejected"

    monkeypatch.setattr(harness, "run_positive", fake_positive)
    monkeypatch.setattr(harness, "run_adversarial", fake_adversarial)
    monkeypatch.setattr(harness, "run_premature_inference", fake_premature)
    gateway_path = tmp_path / "gateway.py"
    collector_path = tmp_path / "collector.py"
    gateway_module = ModuleType("gateway")

    cases = harness.build_cases(gateway_path, collector_path, gateway_module, context)
    results = [(name, runner()) for name, runner in cases]

    assert [name for name, _runner in cases] == [
        "positive",
        "relay",
        "splice",
        "stale",
        "premature-inference",
    ]
    assert results == [
        ("positive", "verified substrate=AMD SEV-SNP + NVIDIA test"),
        ("relay", "spki_mismatch"),
        ("splice", "nonce_mismatch"),
        ("stale", "exporter_mismatch"),
        ("premature-inference", "protocol_or_tls_rejected"),
    ]
    assert observed[1][0:2] == ("relay", "spki_mismatch")
    assert observed[2][0:2] == ("splice", "nonce_mismatch")
    assert observed[3][0:2] == ("stale", "exporter_mismatch")


def test_real_positive_does_not_claim_upstream_observation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent = []

    class FakeGateway:
        def __init__(self, _gateway_path, _collector_path, upstream_port: int) -> None:
            self.port = 12345
            self.upstream_port = upstream_port

        def close(self) -> None:
            pass

    class FakeTls:
        def sendall(self, data: bytes) -> None:
            sent.append(data)

    channel = SimpleNamespace(
        tls=FakeTls(),
        verdict=SimpleNamespace(substrate="AMD SEV-SNP + NVIDIA H100"),
        close=lambda: None,
    )
    context = harness.RunContext(
        establish=lambda _port: channel,
        upstream_port=9443,
        request_body=b'{"messages":[]}',
        banner=harness.REAL_BANNER,
    )
    response = (
        b"HTTP/1.1 200 OK\r\nContent-Length: 134\r\n\r\n"
        b'{"id":"cmpl","object":"chat.completion","choices":[{"message":'
        b'{"role":"assistant","content":""},"finish_reason":"stop"}]}'
    )
    monkeypatch.setattr(harness, "GatewayProcess", FakeGateway)
    monkeypatch.setattr(
        harness, "Upstream", lambda: pytest.fail("real mode used Upstream")
    )
    monkeypatch.setattr(harness, "_recv_http_response", lambda _tls: response)

    result = harness.run_positive(
        tmp_path / "gateway.py", tmp_path / "collector.py", context
    )

    assert result == "verified substrate=AMD SEV-SNP + NVIDIA H100"
    assert sent == [harness.build_chat_completions_request(b'{"messages":[]}')]


def _run_default_positive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    forwarded: bytes,
) -> str:
    class FakeGateway:
        def __init__(self, _gateway_path, _collector_path, upstream_port: int) -> None:
            self.port = 12345

        def close(self) -> None:
            pass

    class FakeUpstream:
        def __init__(self) -> None:
            self.port = 9999
            self.request = forwarded
            self.thread = SimpleNamespace(join=lambda timeout=None: None)

        def start(self) -> None:
            pass

        def close(self) -> None:
            pass

    channel = SimpleNamespace(
        tls=SimpleNamespace(sendall=lambda _data: None),
        verdict=SimpleNamespace(substrate=""),
        close=lambda: None,
    )
    context = harness.RunContext(
        establish=lambda _port: channel,
        upstream_port=None,
        request_body=harness.DEFAULT_REQUEST_BODY,
        banner=harness.DEFAULT_BANNER,
    )
    monkeypatch.setattr(harness, "GatewayProcess", FakeGateway)
    monkeypatch.setattr(harness, "Upstream", FakeUpstream)
    monkeypatch.setattr(
        harness, "_recv_http_response", lambda _tls: b"HTTP/1.1 200 OK\r\n\r\n"
    )

    return harness.run_positive(
        tmp_path / "gateway.py", tmp_path / "collector.py", context
    )


# A gateway may legitimately rewrite or add headers while forwarding; the default
# proxy check must not couple to its header handling.
@pytest.mark.parametrize(
    "forwarded",
    [
        CURRENT_DEFAULT_REQUEST,
        b"POST /v1/chat/completions HTTP/1.1\r\n"
        b"Host: upstream.internal\r\nContent-Length: 2\r\n\r\n{}",
        b"POST /v1/chat/completions HTTP/1.1\r\n"
        b"Host: spp-engine\r\nX-Forwarded-For: 127.0.0.1\r\n"
        b"Connection: close\r\nContent-Length: 2\r\n\r\n{}",
    ],
)
def test_default_positive_accepts_gateway_rewritten_headers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    forwarded: bytes,
) -> None:
    assert _run_default_positive(tmp_path, monkeypatch, forwarded) == "verified"


@pytest.mark.parametrize(
    "forwarded",
    [
        b"",
        b"GET /healthz HTTP/1.1\r\nHost: spp-engine\r\n\r\n",
        b"POST /v1/embeddings HTTP/1.1\r\nHost: spp-engine\r\n\r\n",
    ],
)
def test_default_positive_rejects_forward_that_is_not_chat_completions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    forwarded: bytes,
) -> None:
    with pytest.raises(RuntimeError, match="positive_proxy_failed"):
        _run_default_positive(tmp_path, monkeypatch, forwarded)


def test_real_premature_inference_does_not_claim_upstream_observation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent = []

    class FakeGateway:
        def __init__(self, _gateway_path, _collector_path, upstream_port: int) -> None:
            self.port = 12345
            self.upstream_port = upstream_port

        def close(self) -> None:
            pass

    class FakeRaw:
        def sendall(self, data: bytes) -> None:
            sent.append(data)

        def close(self) -> None:
            pass

    class FakeConnection:
        def __init__(self, _context, _raw) -> None:
            pass

        def setblocking(self, _value: int) -> None:
            pass

        def set_connect_state(self) -> None:
            pass

        def set_tlsext_host_name(self, _name: bytes) -> None:
            pass

        def do_handshake(self) -> None:
            pass

        def sendall(self, data: bytes) -> None:
            sent.append(data)

        def recv(self, _size: int) -> bytes:
            return b""

        def close(self) -> None:
            pass

    context = harness.RunContext(
        establish=lambda _port: object(),
        upstream_port=9443,
        request_body=b'{"messages":[]}',
        banner=harness.REAL_BANNER,
    )
    monkeypatch.setattr(harness, "GatewayProcess", FakeGateway)
    monkeypatch.setattr(
        harness, "Upstream", lambda: pytest.fail("real mode used Upstream")
    )
    monkeypatch.setattr(
        harness.socket, "create_connection", lambda *_args, **_kwargs: FakeRaw()
    )
    monkeypatch.setattr(harness.SSL, "Connection", FakeConnection)

    result = harness.run_premature_inference(
        tmp_path / "gateway.py",
        tmp_path / "collector.py",
        context,
    )

    assert result == "protocol_or_tls_rejected"
    assert sent == [
        harness.PREFACE_MAGIC + b"p" * harness.OWNER_NONCE_BYTES,
        harness.build_chat_completions_request(b'{"messages":[]}'),
    ]


def test_output_redacts_sentinel_content_on_success_and_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sentinel = "do-not-print-this-request-body"
    request_path = _request_body(tmp_path, f'{{"sentinel":"{sentinel}"}}'.encode())
    nvattest_dir = _nvattest_root(tmp_path)
    monkeypatch.setattr(
        harness, "_load_gateway_module", lambda _path: ModuleType("gateway")
    )

    def fake_build_cases(_gateway_path, _collector_path, _gateway_module, context):
        assert sentinel.encode() in context.request_body

        def fail():
            raise RuntimeError(f"bad {sentinel}")

        return [
            ("positive", lambda: "verified substrate=AMD SEV-SNP + NVIDIA test"),
            ("relay", fail),
        ]

    monkeypatch.setattr(harness, "build_cases", fake_build_cases)

    result = harness.main(
        [
            "--gateway",
            str(tmp_path / "gateway.py"),
            "--collector",
            str(tmp_path / "collector.py"),
            "--real",
            "--nvattest-dir",
            str(nvattest_dir),
            "--upstream-port",
            "9443",
            "--request-body",
            str(request_path),
        ]
    )
    output = capsys.readouterr()

    assert result == 1
    assert sentinel not in output.out
    assert sentinel not in output.err
    assert "positive: PASS verified substrate=AMD SEV-SNP + NVIDIA test" in output.out
    assert "relay: FAIL RuntimeError" in output.out


def test_real_envelope_validation_requires_openai_compatible_shape() -> None:
    valid = (
        b"HTTP/1.1 200 OK\r\nContent-Length: 134\r\n\r\n"
        b'{"id":"cmpl","object":"chat.completion","choices":[{"message":'
        b'{"role":"assistant","content":""},"finish_reason":"stop"}]}'
    )
    harness.validate_chat_completion_envelope(valid)

    with pytest.raises(RuntimeError, match="response_envelope_invalid"):
        harness.validate_chat_completion_envelope(
            b'HTTP/1.1 200 OK\r\nContent-Length: 24\r\n\r\n{"id":"ok","choices":[]}'
        )

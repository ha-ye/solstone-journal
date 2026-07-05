# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import hashlib
import io
import subprocess
import tarfile
from pathlib import Path

import httpx
import pytest

from solstone.think.providers import oci_image

REPO = "acme/tool"
IMAGE_DIGEST = "a" * 64
MANIFEST_DIGEST = "b" * 64
ARM_MANIFEST_DIGEST = "c" * 64
IMAGE_REF = f"ghcr.io/{REPO}@sha256:{IMAGE_DIGEST}"
ALT_IMAGE_REF = f"ghcr.io/{REPO}@sha256:{'d' * 64}"
TOP_REF = f"sha256:{IMAGE_DIGEST}"
MANIFEST_REF = f"sha256:{MANIFEST_DIGEST}"
ARM_MANIFEST_REF = f"sha256:{ARM_MANIFEST_DIGEST}"


def _policy() -> oci_image.OciSignaturePolicy:
    return oci_image.OciSignaturePolicy(
        certificate_identity_regexp=r"^https://github\.com/acme/tool/.+$",
        oidc_issuer="https://token.actions.githubusercontent.com",
    )


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _digest_ref(data: bytes) -> str:
    return f"sha256:{_sha256_bytes(data)}"


def _layer_bytes(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, data in entries.items():
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    return buffer.getvalue()


def _single_member_layer(name: str, data: bytes = b"bad") -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        member = tarfile.TarInfo(name)
        member.size = len(data)
        archive.addfile(member, io.BytesIO(data))
    return buffer.getvalue()


def _index(manifest_ref: str = MANIFEST_REF) -> dict:
    return {
        "schemaVersion": 2,
        "manifests": [
            {
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "digest": manifest_ref,
                "platform": {"os": "linux", "architecture": "amd64"},
            },
            {
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "digest": ARM_MANIFEST_REF,
                "platform": {"os": "linux", "architecture": "arm64"},
            },
        ],
    }


def _manifest_for_layers(layer_bytes: list[bytes]) -> tuple[dict, dict[str, bytes]]:
    blobs = {_digest_ref(data): data for data in layer_bytes}
    manifest = {
        "schemaVersion": 2,
        "layers": [
            {
                "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                "digest": digest,
            }
            for digest in blobs
        ],
    }
    return manifest, blobs


class _Registry:
    def __init__(
        self,
        manifests: dict[str, dict],
        blobs: dict[str, bytes],
        *,
        token_status: int = 200,
        manifest_statuses: dict[str, int] | None = None,
        blob_statuses: dict[str, int] | None = None,
        garbage_manifests: set[str] | None = None,
    ) -> None:
        self.manifests = manifests
        self.blobs = blobs
        self.token_status = token_status
        self.manifest_statuses = manifest_statuses or {}
        self.blob_statuses = blob_statuses or {}
        self.garbage_manifests = garbage_manifests or set()
        self.requests: list[httpx.Request] = []

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handle))

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/token":
            return httpx.Response(
                self.token_status,
                json={"token": "token-1"},
                request=request,
            )

        manifest_prefix = f"/v2/{REPO}/manifests/"
        if path.startswith(manifest_prefix):
            digest = path.removeprefix(manifest_prefix)
            status = self.manifest_statuses.get(digest)
            if status is not None:
                return httpx.Response(status, request=request)
            if digest in self.garbage_manifests:
                return httpx.Response(200, content=b"not json", request=request)
            payload = self.manifests.get(digest)
            if payload is None:
                return httpx.Response(404, request=request)
            return httpx.Response(200, json=payload, request=request)

        blob_prefix = f"/v2/{REPO}/blobs/"
        if path.startswith(blob_prefix):
            digest = path.removeprefix(blob_prefix)
            status = self.blob_statuses.get(digest)
            if status is not None:
                return httpx.Response(status, request=request)
            payload = self.blobs.get(digest)
            if payload is None:
                return httpx.Response(404, request=request)
            return httpx.Response(200, content=payload, request=request)

        return httpx.Response(404, request=request)


def _registry_for_layers(layer_bytes: list[bytes]) -> _Registry:
    manifest, blobs = _manifest_for_layers(layer_bytes)
    return _Registry(
        {
            TOP_REF: _index(),
            MANIFEST_REF: manifest,
            ARM_MANIFEST_REF: {"schemaVersion": 2, "layers": []},
        },
        blobs,
    )


def _pull_with_registry(
    registry: _Registry,
    target: Path,
    wanted: list[str],
    *,
    image_ref: str = IMAGE_REF,
    policy: oci_image.OciSignaturePolicy | None = None,
    verifier=None,
) -> oci_image.OciInstallResult:
    with registry.client() as client:
        return oci_image.pull_and_install(
            image_ref,
            "amd64",
            wanted,
            target,
            client=client,
            policy=policy,
            verifier=verifier,
        )


def _assert_reason(
    registry: _Registry,
    target: Path,
    reason_code: str,
    *,
    wanted: list[str] | None = None,
) -> None:
    with registry.client() as client:
        with pytest.raises(oci_image.OciImageError) as exc_info:
            oci_image.pull_and_install(
                IMAGE_REF,
                "amd64",
                wanted or ["tool"],
                target,
                client=client,
            )
    assert exc_info.value.reason_code == reason_code


def test_ac1_record_round_trip_and_invalid_inputs(tmp_path: Path) -> None:
    record = oci_image.OciInstallRecord(
        image_ref=IMAGE_REF,
        arch="amd64",
        files={"tool": "1" * 64},
    )

    assert oci_image.OciInstallRecord.from_json(record.to_json()) == record

    for image_ref in (
        f"ghcr.io/{REPO}",
        f"ghcr.io/{REPO}@sha256:{'1' * 63}",
        f"docker.io/{REPO}@sha256:{IMAGE_DIGEST}",
    ):
        with pytest.raises(oci_image.OciImageError) as exc_info:
            oci_image.pull_and_install(image_ref, "amd64", ["tool"], tmp_path / "out")
        assert exc_info.value.reason_code == "invalid_image_ref"

    for wanted in (["dir/tool"], [".."], ["tool..old"], [oci_image.SIDECAR_NAME], [""]):
        with pytest.raises(oci_image.OciImageError) as exc_info:
            oci_image.pull_and_install(IMAGE_REF, "amd64", wanted, tmp_path / "out")
        assert exc_info.value.reason_code == "invalid_wanted_file"


def test_signature_policy_requires_exactly_one_identity() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        oci_image.OciSignaturePolicy(
            oidc_issuer="https://token.actions.githubusercontent.com"
        )
    with pytest.raises(ValueError, match="exactly one"):
        oci_image.OciSignaturePolicy(
            certificate_identity="identity",
            certificate_identity_regexp="identity-regexp",
            oidc_issuer="https://token.actions.githubusercontent.com",
        )


def test_verify_image_signature_invokes_cosign_with_keyless_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []
    policy = _policy()

    def fake_run(command, **kwargs):
        calls.append({"command": command, **kwargs})
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    monkeypatch.setattr(oci_image.subprocess, "run", fake_run)

    oci_image.verify_image_signature(IMAGE_REF, policy)

    assert calls == [
        {
            "command": [
                "cosign",
                "verify",
                IMAGE_REF,
                "--certificate-identity-regexp",
                policy.certificate_identity_regexp,
                "--certificate-oidc-issuer",
                policy.oidc_issuer,
            ],
            "capture_output": True,
            "text": True,
            "timeout": oci_image._COSIGN_TIMEOUT_SECONDS,
            "check": False,
        }
    ]


def test_verify_image_signature_maps_missing_cosign(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_run(_command, **_kwargs):
        raise FileNotFoundError("cosign missing")

    monkeypatch.setattr(oci_image.subprocess, "run", fail_run)

    with pytest.raises(oci_image.OciImageError) as exc_info:
        oci_image.verify_image_signature(IMAGE_REF, _policy())

    assert exc_info.value.reason_code == "cosign_missing"


def test_verify_image_signature_maps_nonzero_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(command, **_kwargs):
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="bad sig")

    monkeypatch.setattr(oci_image.subprocess, "run", fake_run)

    with pytest.raises(oci_image.OciImageError) as exc_info:
        oci_image.verify_image_signature(IMAGE_REF, _policy())

    assert exc_info.value.reason_code == "signature_verify_failed"
    assert "bad sig" in str(exc_info.value)


def test_ac2_happy_path_installs_files_and_sidecar(tmp_path: Path) -> None:
    layer = _layer_bytes(
        {
            "usr/local/bin/llama-server": b"server",
            "usr/lib/libfoo.so": b"library",
        }
    )
    registry = _registry_for_layers([layer])
    target = tmp_path / "target"

    result = _pull_with_registry(registry, target, ["llama-server", "libfoo.so"])

    assert result.already_present is False
    assert (target / "llama-server").read_bytes() == b"server"
    assert (target / "libfoo.so").read_bytes() == b"library"
    record = oci_image.OciInstallRecord.from_json(
        (target / oci_image.SIDECAR_NAME).read_text(encoding="utf-8")
    )
    assert record.image_ref == IMAGE_REF
    assert record.arch == "amd64"
    assert record.files == result.files
    assert record.files == {
        "llama-server": _sha256_file(target / "llama-server"),
        "libfoo.so": _sha256_file(target / "libfoo.so"),
    }

    protected = [
        request for request in registry.requests if request.url.path != "/token"
    ]
    assert protected
    assert all(
        request.headers["authorization"] == "Bearer token-1" for request in protected
    )
    assert all("accept" in request.headers for request in protected)
    manifest_requests = [
        request for request in protected if "/manifests/" in request.url.path
    ]
    assert len(manifest_requests) == 2
    assert all(
        "application/vnd.oci.image.index.v1+json" in request.headers["accept"]
        for request in manifest_requests
    )


def test_signature_verifier_runs_before_first_blob_fetch(tmp_path: Path) -> None:
    layer = _layer_bytes({"bin/tool": b"tool"})
    registry = _registry_for_layers([layer])
    target = tmp_path / "target"
    events: list[str] = []
    original_handle = registry.handle

    def handle(request: httpx.Request) -> httpx.Response:
        if "/blobs/" in request.url.path:
            events.append("blob")
        return original_handle(request)

    def verifier(_image_ref: str, _policy: oci_image.OciSignaturePolicy) -> None:
        events.append("verify")

    registry.handle = handle  # type: ignore[method-assign]

    _pull_with_registry(
        registry,
        target,
        ["tool"],
        policy=_policy(),
        verifier=verifier,
    )

    assert events.index("verify") < events.index("blob")


def test_signature_verifier_failure_leaves_target_unchanged(tmp_path: Path) -> None:
    layer = _layer_bytes({"bin/tool": b"tool"})
    registry = _registry_for_layers([layer])
    target = tmp_path / "target"
    target.mkdir()
    (target / "old").write_text("old\n", encoding="utf-8")

    def verifier(_image_ref: str, _policy: oci_image.OciSignaturePolicy) -> None:
        raise oci_image.OciImageError("cosign_missing", "cosign missing")

    with pytest.raises(oci_image.OciImageError) as exc_info:
        _pull_with_registry(
            registry,
            target,
            ["tool"],
            policy=_policy(),
            verifier=verifier,
        )

    assert exc_info.value.reason_code == "cosign_missing"
    assert (target / "old").read_text(encoding="utf-8") == "old\n"
    assert not (target / "tool").exists()
    assert list(tmp_path.rglob("*.tmp")) == []
    assert {path.name for path in tmp_path.iterdir()} == {"target"}


def test_ac3_fetch_failures_raise_reason_codes(tmp_path: Path) -> None:
    layer = _layer_bytes({"bin/tool": b"tool"})
    manifest, blobs = _manifest_for_layers([layer])

    cases: list[tuple[str, _Registry]] = [
        (
            "token_fetch_failed",
            _Registry(
                {TOP_REF: _index(), MANIFEST_REF: manifest}, blobs, token_status=401
            ),
        ),
        (
            "manifest_fetch_failed",
            _Registry(
                {TOP_REF: _index(), MANIFEST_REF: manifest},
                blobs,
                manifest_statuses={TOP_REF: 500},
            ),
        ),
        (
            "manifest_fetch_failed",
            _Registry(
                {TOP_REF: _index(), MANIFEST_REF: manifest},
                blobs,
                garbage_manifests={TOP_REF},
            ),
        ),
        (
            "arch_unavailable",
            _Registry(
                {
                    TOP_REF: {
                        "schemaVersion": 2,
                        "manifests": [
                            {
                                "digest": ARM_MANIFEST_REF,
                                "platform": {"os": "linux", "architecture": "arm64"},
                            }
                        ],
                    },
                    ARM_MANIFEST_REF: manifest,
                },
                blobs,
            ),
        ),
        (
            "blob_fetch_failed",
            _Registry(
                {TOP_REF: _index(), MANIFEST_REF: manifest},
                blobs,
                blob_statuses={next(iter(blobs)): 500},
            ),
        ),
    ]

    for index, (reason_code, registry) in enumerate(cases):
        target = tmp_path / f"target-{index}"
        _assert_reason(registry, target, reason_code)
        assert not target.exists()


def test_ac4_blob_sha_mismatch_leaves_target_unchanged(tmp_path: Path) -> None:
    layer = _layer_bytes({"bin/tool": b"tool"})
    wrong_digest = f"sha256:{'f' * 64}"
    manifest = {
        "schemaVersion": 2,
        "layers": [
            {
                "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                "digest": wrong_digest,
            }
        ],
    }
    registry = _Registry(
        {TOP_REF: _index(), MANIFEST_REF: manifest}, {wrong_digest: layer}
    )
    target = tmp_path / "target"
    target.mkdir()
    (target / "old").write_text("old\n", encoding="utf-8")

    _assert_reason(registry, target, "sha256_mismatch")

    assert (target / "old").read_text(encoding="utf-8") == "old\n"
    assert not (target / "tool").exists()
    assert list(tmp_path.rglob("*.tmp")) == []


def test_ac5_path_traversal_is_rejected(tmp_path: Path) -> None:
    registry = _registry_for_layers([_single_member_layer("../escape")])
    target = tmp_path / "target"

    _assert_reason(registry, target, "archive_path_traversal")

    assert not (tmp_path / "escape").exists()
    assert not target.exists()


def test_ac5_corrupt_layer_raises_extract_failed(tmp_path: Path) -> None:
    corrupt = b"this is not a tarball"
    registry = _registry_for_layers([corrupt])
    target = tmp_path / "target"

    _assert_reason(registry, target, "extract_failed")

    assert not target.exists()


def test_ac5_whiteout_and_opaque_directory_remove_earlier_files(
    tmp_path: Path,
) -> None:
    layer_one = _layer_bytes({"bin/tool": b"tool", "app/old": b"old"})
    layer_two = _layer_bytes(
        {
            "bin/.wh.tool": b"",
            "app/.wh..wh..opq": b"",
            "app/new": b"new",
        }
    )

    _assert_reason(
        _registry_for_layers([layer_one, layer_two]),
        tmp_path / "whiteout",
        "wanted_file_missing",
        wanted=["tool"],
    )
    _assert_reason(
        _registry_for_layers([layer_one, layer_two]),
        tmp_path / "opaque",
        "wanted_file_missing",
        wanted=["old"],
    )


def test_ac6_shallowest_match_wins(tmp_path: Path) -> None:
    layer_one = _layer_bytes({"very/deep/tool": b"deep"})
    layer_two = _layer_bytes({"bin/tool": b"shallow"})
    registry = _registry_for_layers([layer_one, layer_two])
    target = tmp_path / "target"

    _pull_with_registry(registry, target, ["tool"])

    assert (target / "tool").read_bytes() == b"shallow"


def test_ac6_missing_wanted_file_raises_reason_code(tmp_path: Path) -> None:
    registry = _registry_for_layers([_layer_bytes({"bin/other": b"other"})])

    _assert_reason(registry, tmp_path / "target", "wanted_file_missing")


def test_ac6_publish_failure_restores_preexisting_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layer = _layer_bytes({"bin/one": b"one", "bin/two": b"two"})
    registry = _registry_for_layers([layer])
    target = tmp_path / "target"
    target.mkdir()
    (target / "old").write_text("old\n", encoding="utf-8")
    (target / oci_image.SIDECAR_NAME).write_text("old sidecar\n", encoding="utf-8")
    original_copy2 = oci_image.shutil.copy2
    calls = 0

    def flaky_copy2(src: Path, dest: Path) -> Path:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("copy broke")
        return Path(original_copy2(src, dest))

    monkeypatch.setattr(oci_image.shutil, "copy2", flaky_copy2)

    _assert_reason(registry, target, "install_failed", wanted=["one", "two"])

    assert (target / "old").read_text(encoding="utf-8") == "old\n"
    assert (target / oci_image.SIDECAR_NAME).read_text(
        encoding="utf-8"
    ) == "old sidecar\n"
    assert not (target / "one").exists()
    assert {path.name for path in tmp_path.iterdir()} == {"target"}


def test_ac6_publish_replace_failure_restores_moved_aside_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layer = _layer_bytes({"bin/tool": b"tool"})
    registry = _registry_for_layers([layer])
    target = tmp_path / "target"
    target.mkdir()
    old_file = target / "old"
    old_file.write_text("old\n", encoding="utf-8")
    old_record = oci_image.OciInstallRecord(
        image_ref=ALT_IMAGE_REF,
        arch="amd64",
        files={"old": _sha256_file(old_file)},
    )
    (target / oci_image.SIDECAR_NAME).write_text(
        old_record.to_json(),
        encoding="utf-8",
    )
    before = {path.name: path.read_bytes() for path in target.iterdir()}
    original_replace = oci_image.Path.replace
    aside_path: Path | None = None
    events: list[str] = []

    def flaky_replace(self: Path, target_path: Path) -> Path:
        nonlocal aside_path
        target_path = Path(target_path)
        if self == target:
            aside_path = target_path
            events.append("move-aside")
            return original_replace(self, target_path)
        if target_path == target and aside_path is not None and self == aside_path:
            events.append("restore")
            return original_replace(self, target_path)
        if target_path == target and self.parent == tmp_path:
            events.append("staging-fail")
            raise OSError("replace broke")
        return original_replace(self, target_path)

    monkeypatch.setattr(oci_image.Path, "replace", flaky_replace)

    _assert_reason(registry, target, "install_failed", wanted=["tool"])

    assert events == ["move-aside", "staging-fail", "restore"]
    assert {path.name: path.read_bytes() for path in target.iterdir()} == before
    assert not (target / "tool").exists()
    assert {path.name for path in tmp_path.iterdir()} == {"target"}


def test_ac7_offline_short_circuit_uses_zero_requests(tmp_path: Path) -> None:
    layer = _layer_bytes({"bin/tool": b"tool"})
    target = tmp_path / "target"
    verifier_calls = 0

    def verifier(_image_ref: str, _policy: oci_image.OciSignaturePolicy) -> None:
        nonlocal verifier_calls
        verifier_calls += 1

    policy = _policy()
    _pull_with_registry(
        _registry_for_layers([layer]),
        target,
        ["tool"],
        policy=policy,
        verifier=verifier,
    )
    assert verifier_calls == 1
    request_count = 0

    def counting_handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(500, request=request)

    with httpx.Client(transport=httpx.MockTransport(counting_handler)) as client:
        result = oci_image.pull_and_install(
            IMAGE_REF,
            "amd64",
            ["tool"],
            target,
            client=client,
            policy=policy,
            verifier=verifier,
        )

    assert request_count == 0
    assert verifier_calls == 1
    assert result.already_present is True
    assert result.files["tool"] == _sha256_file(target / "tool")

    with httpx.Client(transport=httpx.MockTransport(counting_handler)) as client:
        with pytest.raises(oci_image.OciImageError) as exc_info:
            oci_image.pull_and_install(
                ALT_IMAGE_REF,
                "amd64",
                ["tool"],
                target,
                client=client,
            )

    assert exc_info.value.reason_code == "token_fetch_failed"
    assert request_count == 1


def test_verify_sidecar_install_reports_cached_integrity(tmp_path: Path) -> None:
    layer = _layer_bytes(
        {
            "bin/tool": b"tool",
            "usr/lib/libfoo.so": b"library",
        }
    )
    target = tmp_path / "target"
    _pull_with_registry(
        _registry_for_layers([layer]),
        target,
        ["tool", "libfoo.so"],
    )

    assert oci_image.verify_sidecar_install(
        IMAGE_REF,
        "amd64",
        ["tool", "libfoo.so"],
        target,
    )
    assert not oci_image.verify_sidecar_install(
        ALT_IMAGE_REF,
        "amd64",
        ["tool", "libfoo.so"],
        target,
    )
    assert not oci_image.verify_sidecar_install(
        IMAGE_REF,
        "arm64",
        ["tool", "libfoo.so"],
        target,
    )

    (target / "libfoo.so").unlink()
    assert not oci_image.verify_sidecar_install(
        IMAGE_REF,
        "amd64",
        ["tool", "libfoo.so"],
        target,
    )

    (target / "libfoo.so").write_bytes(b"wrong")
    assert not oci_image.verify_sidecar_install(
        IMAGE_REF,
        "amd64",
        ["tool", "libfoo.so"],
        target,
    )


def test_ac8_offline_short_circuit_uses_request_raising_transport(
    tmp_path: Path,
) -> None:
    layer = _layer_bytes({"bin/tool": b"tool"})
    target = tmp_path / "target"
    _pull_with_registry(_registry_for_layers([layer]), target, ["tool"])

    def raising_handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected HTTP request: {request.url}")

    with httpx.Client(transport=httpx.MockTransport(raising_handler)) as client:
        result = oci_image.pull_and_install(
            IMAGE_REF,
            "amd64",
            ["tool"],
            target,
            client=client,
        )

    assert result.already_present is True
    assert result.files["tool"] == _sha256_file(target / "tool")

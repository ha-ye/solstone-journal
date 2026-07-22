from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path

import pytest

import scripts.check_transparency_minisign as minisign_gate
import scripts.transparency_publish as publisher
from scripts.release_candidate_driver import DriverError
from scripts.transparency_core import DEFAULT_BASE_URL, PRODUCT
from scripts.transparency_head_log import HeadLogRow, append_head_row
from scripts.transparency_signing import MISSING_MINISIGN_MESSAGE


def _env(tmp_path: Path) -> dict[str, str]:
    return {
        "SOURCE_COMMIT": "a" * 40,
        "TRANSPARENCY_ARCHIVE_CHANNEL": "archive",
        "TRANSPARENCY_BUCKET": "bucket",
        "TRANSPARENCY_MINISIGN_KEY": str(tmp_path / "secret.key"),
        "TRANSPARENCY_MINISIGN_PUB": str(tmp_path / "public.key"),
        "TRANSPARENCY_S3_ACCESS_KEY_ID": "key",
        "TRANSPARENCY_S3_ENDPOINT": "https://r2.example.invalid",
        "TRANSPARENCY_S3_SECRET_ACCESS_KEY": "secret",
    }


def test_config_from_env_defaults_public_base_url(tmp_path: Path) -> None:
    config = publisher.PublishConfig.from_env(
        root=tmp_path,
        version="0.9.1",
        source_commit="a" * 40,
        env=_env(tmp_path),
    )
    assert config.product == PRODUCT
    assert config.base_url == DEFAULT_BASE_URL


def test_config_from_args_derives_source_commit_from_retained_ledger(
    tmp_path: Path,
) -> None:
    version = "0.9.1"
    release_dir = tmp_path / "dist" / "release-candidate" / version
    evidence_dir = tmp_path / "target" / "release-evidence" / version
    release_dir.mkdir(parents=True)
    evidence_dir.mkdir(parents=True)
    (evidence_dir / "ledger.json").write_text(
        json.dumps({"source_commit": "b" * 40}),
        encoding="utf-8",
    )
    env = _env(tmp_path)
    env.pop("SOURCE_COMMIT")
    env["RELEASE_DIR"] = str(release_dir)
    config = publisher._config_from_args(
        Namespace(root=str(tmp_path), version="", source_commit=""),
        env,
    )
    assert config.version == version
    assert config.source_commit == "b" * 40


def test_config_from_args_rejects_mispointed_release_dir(tmp_path: Path) -> None:
    version = "0.9.1"
    supplied = tmp_path / "elsewhere" / version
    supplied.mkdir(parents=True)
    env = _env(tmp_path)
    env["RELEASE_DIR"] = str(supplied)

    with pytest.raises(DriverError) as error:
        publisher._config_from_args(
            Namespace(root=str(tmp_path), version="", source_commit=""),
            env,
        )

    failure = error.value.failures[0]
    assert failure.error == "transparency RELEASE_DIR does not match retained path"
    assert str(supplied.resolve()) in failure.actual
    assert str((tmp_path / "dist" / "release-candidate" / version).resolve()) in (
        failure.expected
    )


def test_check_transparency_minisign_missing_binary_fails_loudly(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail() -> None:
        raise DriverError(
            [
                publisher.failure(
                    MISSING_MINISIGN_MESSAGE,
                    expected="minisign 0.12",
                    actual="missing",
                    repair="sudo dnf install minisign",
                )
            ]
        )

    monkeypatch.setattr(minisign_gate, "check_minisign_binary", fail)
    assert minisign_gate.main([]) == 1
    captured = capsys.readouterr()
    assert MISSING_MINISIGN_MESSAGE in captured.err
    assert "sudo dnf install minisign" in captured.err


def test_check_transparency_minisign_tamper_must_fail(tmp_path: Path) -> None:
    class AcceptingVerifier:
        def verify_file(
            self,
            message_path: Path,
            signature_path: Path,
            *,
            expected_trusted_comment: str,
        ) -> None:
            return None

    message = tmp_path / "message.json"
    signature = tmp_path / "message.json.minisig"
    message.write_bytes(b'{"ok":1}\n')
    signature.write_text("placeholder\n", encoding="utf-8")
    with pytest.raises(DriverError) as error:
        minisign_gate._assert_tampered_verify_fails(
            AcceptingVerifier(),
            message,
            signature,
            expected_trusted_comment="trusted",
        )
    assert (
        error.value.failures[0].error
        == "transparency minisign tampered message verified"
    )


def test_cli_publish_prints_operator_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def publish_transparency(**_kwargs: object) -> publisher.PublishResult:
        return publisher.PublishResult(
            product=PRODUCT,
            version="0.9.1",
            seq=1,
            entry_sha256="a" * 64,
            public_urls=(
                "https://transparency.solstone.app/releases/solstone-journal/latest.json",
            ),
            archive_receipt_sha256="b" * 64,
            witness_status=publisher.WitnessStatus(
                state="witness-unavailable",
                message="git unavailable",
            ),
            elapsed_seconds=0.25,
        )

    monkeypatch.setattr(publisher, "_transport_from_config", lambda _config: object())
    monkeypatch.setattr(publisher, "_signer_from_config", lambda _config: object())
    monkeypatch.setattr(publisher, "publish_transparency", publish_transparency)
    code = publisher.main(
        [
            "publish",
            "--root",
            str(tmp_path),
            "--version",
            "0.9.1",
            "--source-commit",
            "a" * 40,
        ],
        env=_env(tmp_path),
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["product"] == PRODUCT
    assert payload["entry_sha256"] == "a" * 64


def test_head_log_detects_product_seq_fork(tmp_path: Path) -> None:
    append_head_row(
        tmp_path,
        HeadLogRow(
            product=PRODUCT,
            seq=1,
            version="0.9.1",
            entry_sha256="a" * 64,
            published_utc="2026-07-22T00:00:00Z",
        ),
    )
    with pytest.raises(DriverError) as error:
        append_head_row(
            tmp_path,
            HeadLogRow(
                product=PRODUCT,
                seq=1,
                version="0.9.1",
                entry_sha256="b" * 64,
                published_utc="2026-07-22T00:00:01Z",
            ),
        )
    assert error.value.failures[0].error == "transparency head log fork detected"

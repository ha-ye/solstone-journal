# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import copy
import datetime as dt
import json
import pickle
from pathlib import Path

import pytest

from solstone.think.link import auth
from solstone.think.link.auth import (
    AuthorizedClients,
    StrictAuthorizationError,
    StrictAuthorizationReceipt,
)


def test_strict_create_close_preserves_unrelated_raw_entries(tmp_path: Path) -> None:
    path = tmp_path / "authorized_clients.json"
    unrelated = [
        {
            "fingerprint": "sha256:one",
            "device_label": "one",
            "paired_at": "2026-01-01T00:00:00Z",
            "instance_id": "inst",
            "role": "",
            "unknown_outer": {"z": 1, "a": [3, 2, 1]},
        },
        {
            "fingerprint": "sha256:two",
            "device_label": "two",
            "paired_at": "2026-01-01T00:00:00Z",
            "instance_id": "inst",
            "role": "peer",
            "future_key": "must-stay",
        },
    ]
    path.write_text(json.dumps(unrelated, indent=2) + "\n", encoding="utf-8")
    before = _canonical_items(_load(path))
    store = AuthorizedClients(path)

    receipt = store.add_attempt_client_strict(
        fingerprint="sha256:attempt",
        device_label="sandbox-spl-attempt",
        instance_id="inst",
        paired_at="2026-01-01T00:01:00Z",
        network="pl-via-spl",
    )
    after_add = _load(path)
    assert _canonical_items(_without_fp(after_add, "sha256:attempt")) == before
    assert store.is_authorized("sha256:attempt")

    store.remove_attempt_client_strict(receipt)

    after_remove = _load(path)
    assert _canonical_items(after_remove) == before
    assert not store.is_authorized("sha256:attempt")
    store.verify_attempt_client_absent_strict(receipt)


def test_strict_close_succeeds_after_listener_normalizes_unrelated_entries(
    tmp_path: Path,
) -> None:
    path = tmp_path / "authorized_clients.json"
    original = [
        {
            "fingerprint": "sha256:one",
            "device_label": "one",
            "paired_at": "2026-01-01T00:00:00Z",
            "instance_id": "inst",
            "role": "",
            "unmodeled": "listener-will-drop",
        },
        {
            "fingerprint": "sha256:two",
            "device_label": "two",
            "paired_at": "2026-01-01T00:00:00Z",
            "instance_id": "inst",
            "role": "peer",
            "future_key": {"listener": "will-drop"},
        },
    ]
    path.write_text(json.dumps(original, indent=2) + "\n", encoding="utf-8")
    store = AuthorizedClients(path)
    receipt = store.add_attempt_client_strict(
        fingerprint="sha256:attempt",
        device_label="sandbox-spl-attempt",
        instance_id="inst",
    )

    assert store.touch_last_seen(
        receipt.fingerprint,
        now=dt.datetime(2026, 1, 1, 0, 2, tzinfo=dt.UTC),
    )
    normalized_before_close = _without_fp(_load(path), receipt.fingerprint)
    assert all(
        "unmodeled" not in item and "future_key" not in item
        for item in normalized_before_close
    )

    store.remove_attempt_client_strict(receipt)

    after_close = _load(path)
    assert _canonical_items(after_close) == _canonical_items(normalized_before_close)
    assert {item["fingerprint"] for item in after_close} == {"sha256:one", "sha256:two"}
    store.verify_attempt_client_absent_strict(receipt)


def test_strict_add_missing_file_creates_attempt_only(tmp_path: Path) -> None:
    path = tmp_path / "authorized_clients.json"
    store = AuthorizedClients(path)

    receipt = store.add_attempt_client_strict(
        fingerprint="sha256:attempt",
        device_label="sandbox-spl-attempt",
        instance_id="inst",
        paired_at="2026-01-01T00:01:00Z",
    )

    assert _load(path) == [
        {
            "fingerprint": "sha256:attempt",
            "device_label": "sandbox-spl-attempt",
            "paired_at": "2026-01-01T00:01:00Z",
            "instance_id": "inst",
            "role": "",
            "kind": "cert",
        }
    ]
    assert store.is_authorized(receipt.fingerprint)


def test_strict_add_rejects_duplicate_fingerprint_store_without_mutation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "authorized_clients.json"
    payload = [
        {"fingerprint": "sha256:dup", "device_label": "first"},
        {"fingerprint": "sha256:dup", "device_label": "second"},
    ]
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    before = path.read_text("utf-8")

    with pytest.raises(StrictAuthorizationError) as excinfo:
        AuthorizedClients(path).add_attempt_client_strict(
            fingerprint="sha256:attempt",
            device_label="sandbox-spl-attempt",
            instance_id="inst",
        )

    assert excinfo.value.code == "duplicate_fingerprint"
    assert excinfo.value.mutated is False
    assert path.read_text("utf-8") == before
    assert excinfo.value.__cause__ is None


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        ({"fingerprint": "sha256:not-list"}, "non_list_root"),
        ([{"device_label": "missing-fp"}], "non_string_fingerprint"),
        (["not-dict"], "non_dict_item"),
    ],
)
def test_strict_add_rejects_malformed_store_without_mutation(
    tmp_path: Path,
    payload: object,
    code: str,
) -> None:
    path = tmp_path / "authorized_clients.json"
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    before = path.read_text("utf-8")

    with pytest.raises(StrictAuthorizationError) as excinfo:
        AuthorizedClients(path).add_attempt_client_strict(
            fingerprint="sha256:attempt",
            device_label="sandbox-spl-attempt",
            instance_id="inst",
        )

    assert excinfo.value.code == code
    assert excinfo.value.mutated is False
    assert path.read_text("utf-8") == before


@pytest.mark.parametrize(
    ("fingerprint", "device_label", "code"),
    [
        ("sha256:one", "sandbox-spl-new", "fingerprint_collision"),
        ("sha256:new", "one", "label_collision"),
    ],
)
def test_strict_add_rejects_collisions_without_mutation(
    tmp_path: Path,
    fingerprint: str,
    device_label: str,
    code: str,
) -> None:
    path = tmp_path / "authorized_clients.json"
    payload = [
        {
            "fingerprint": "sha256:one",
            "device_label": "one",
            "paired_at": "2026-01-01T00:00:00Z",
            "instance_id": "inst",
            "unmodeled": "preserve",
        }
    ]
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    before = path.read_text("utf-8")

    with pytest.raises(StrictAuthorizationError) as excinfo:
        AuthorizedClients(path).add_attempt_client_strict(
            fingerprint=fingerprint,
            device_label=device_label,
            instance_id="inst",
        )

    assert excinfo.value.code == code
    assert excinfo.value.mutated is False
    assert path.read_text("utf-8") == before


def test_strict_add_rejects_file_disappearing_mid_operation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "authorized_clients.json"
    path.write_text("[]\n", encoding="utf-8")

    def disappearing_write(path_arg: Path, payload: object) -> None:
        assert path_arg == path
        assert payload
        path.unlink()

    monkeypatch.setattr(auth, "write_json", disappearing_write)

    with pytest.raises(StrictAuthorizationError) as excinfo:
        AuthorizedClients(path).add_attempt_client_strict(
            fingerprint="sha256:attempt",
            device_label="sandbox-spl-attempt",
            instance_id="inst",
        )

    assert excinfo.value.code == "missing_file"


def test_strict_add_rejects_concurrent_replacement_between_write_and_verify(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "authorized_clients.json"
    path.write_text("[]\n", encoding="utf-8")

    real_write = auth.write_json

    def replacing_write(path_arg: Path, payload: object) -> None:
        real_write(path_arg, payload)
        path_arg.unlink()
        path_arg.write_text(
            json.dumps(
                [
                    {
                        "fingerprint": "sha256:intruder",
                        "device_label": "intruder",
                    }
                ],
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    monkeypatch.setattr(auth, "write_json", replacing_write)

    with pytest.raises(StrictAuthorizationError) as excinfo:
        AuthorizedClients(path).add_attempt_client_strict(
            fingerprint="sha256:attempt",
            device_label="sandbox-spl-attempt",
            instance_id="inst",
        )

    assert excinfo.value.code == "post_write_verification_failed"
    assert excinfo.value.mutated is True


def test_strict_remove_allows_own_entry_runtime_metadata(tmp_path: Path) -> None:
    path = tmp_path / "authorized_clients.json"
    store = AuthorizedClients(path)
    receipt = store.add_attempt_client_strict(
        fingerprint="sha256:attempt",
        device_label="sandbox-spl-attempt",
        instance_id="inst",
    )
    payload = _load(path)
    payload[0]["network"] = "changed"
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    store.remove_attempt_client_strict(receipt)

    assert not store.is_authorized("sha256:attempt")
    assert _load(path) == []


def test_strict_receipt_repr_and_copy_surfaces_are_redacted(tmp_path: Path) -> None:
    receipt = AuthorizedClients(
        tmp_path / "authorized_clients.json"
    ).add_attempt_client_strict(
        fingerprint="sha256:secret-fingerprint",
        device_label="sandbox-spl-attempt",
        instance_id="inst",
    )

    assert "secret-fingerprint" not in repr(receipt)
    assert "expected_item" not in repr(receipt)
    for operation in (copy.copy, copy.deepcopy, pickle.dumps):
        with pytest.raises(TypeError):
            operation(receipt)


def test_strict_receipt_type_redacts_manual_repr() -> None:
    receipt = StrictAuthorizationReceipt(
        fingerprint="sha256:secret",
        device_label="label",
    )

    assert repr(receipt) == "StrictAuthorizationReceipt(<redacted>)"


def test_strict_verify_absent_rejects_lingering_fingerprint(tmp_path: Path) -> None:
    path = tmp_path / "authorized_clients.json"
    store = AuthorizedClients(path)
    receipt = store.add_attempt_client_strict(
        fingerprint="sha256:attempt",
        device_label="sandbox-spl-attempt",
        instance_id="inst",
    )

    with pytest.raises(StrictAuthorizationError) as excinfo:
        store.verify_attempt_client_absent_strict(receipt)

    assert excinfo.value.code == "attempt_entry_present"


def test_strict_verify_absent_rejects_lingering_label(tmp_path: Path) -> None:
    path = tmp_path / "authorized_clients.json"
    store = AuthorizedClients(path)
    receipt = store.add_attempt_client_strict(
        fingerprint="sha256:attempt",
        device_label="sandbox-spl-attempt",
        instance_id="inst",
    )
    path.write_text(
        json.dumps(
            [
                {
                    "fingerprint": "sha256:other",
                    "device_label": receipt.device_label,
                }
            ],
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(StrictAuthorizationError) as excinfo:
        store.verify_attempt_client_absent_strict(receipt)

    assert excinfo.value.code == "attempt_label_present"


def test_strict_remove_rejects_label_lingering_after_removal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "authorized_clients.json"
    store = AuthorizedClients(path)
    receipt = store.add_attempt_client_strict(
        fingerprint="sha256:attempt",
        device_label="sandbox-spl-attempt",
        instance_id="inst",
    )
    real_write = auth.write_json

    def lingering_label_write(path_arg: Path, payload: object) -> None:
        assert payload == []
        real_write(
            path_arg,
            [
                {
                    "fingerprint": "sha256:other",
                    "device_label": receipt.device_label,
                }
            ],
        )

    monkeypatch.setattr(auth, "write_json", lingering_label_write)

    with pytest.raises(StrictAuthorizationError) as excinfo:
        store.remove_attempt_client_strict(receipt)

    assert excinfo.value.code == "label_absence_unverified"
    assert excinfo.value.mutated is True


def _load(path: Path) -> list[dict[str, object]]:
    payload = json.loads(path.read_text("utf-8"))
    assert isinstance(payload, list)
    return payload


def _without_fp(
    items: list[dict[str, object]],
    fingerprint: str,
) -> list[dict[str, object]]:
    return [item for item in items if item.get("fingerprint") != fingerprint]


def _canonical_items(items: list[dict[str, object]]) -> tuple[str, ...]:
    return tuple(
        json.dumps(item, ensure_ascii=False, separators=(",", ":")) for item in items
    )

# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json

from werkzeug.test import TestResponse

from solstone.think.link.auth import AuthorizedClients
from solstone.think.link.paths import authorized_clients_path

FP_ONE = "sha256:" + "1" * 64
FP_TWO = "sha256:" + "2" * 64
FP_THREE = "sha256:" + "3" * 64


def _write_authorized(entries: list[dict[str, object]]) -> None:
    path = authorized_clients_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries, indent=2) + "\n", encoding="utf-8")


def _post_unpair(env, label: str) -> TestResponse:
    return env.client.post("/app/network/unpair", json={"device_label": label})


def test_unpair_legacy_duplicate_display_label_refuses_with_candidates(
    link_env,
) -> None:
    env = link_env()
    _write_authorized(
        [
            {
                "fingerprint": FP_ONE,
                "device_label": "iPhone",
                "paired_at": "2026-04-19T00:00:01Z",
                "instance_id": "inst-1",
                "role": "",
            },
            {
                "fingerprint": FP_TWO,
                "device_label": "iPhone",
                "paired_at": "2026-04-19T00:00:02Z",
                "instance_id": "inst-1",
                "role": "",
            },
        ]
    )

    response = _post_unpair(env, "iPhone")

    expected_detail = (
        "more than one device is named 'iPhone'. pick the one you mean and run "
        "its command:\n"
        "- paired 2026-04-19T00:00:01Z, fingerprint 1111111111111111\n"
        f"  sol call link unpair {FP_ONE}\n"
        "- paired 2026-04-19T00:00:02Z, fingerprint 2222222222222222\n"
        f"  sol call link unpair {FP_TWO}"
    )
    body = response.get_json()
    assert response.status_code == 400
    assert body["reason_code"] == "invalid_operation_for_state"
    assert body["detail"] == expected_detail
    authorized = AuthorizedClients(authorized_clients_path())
    assert authorized.is_authorized(FP_ONE) is True
    assert authorized.is_authorized(FP_TWO) is True


def test_unpair_display_label_ordinal_match_revokes_selected_entry(link_env) -> None:
    env = link_env()
    _write_authorized(
        [
            {
                "fingerprint": FP_ONE,
                "device_label": "iPhone",
                "paired_at": "2026-04-19T00:00:01Z",
                "instance_id": "inst-1",
                "role": "",
            },
            {
                "fingerprint": FP_TWO,
                "device_label": "iPhone",
                "paired_at": "2026-04-19T00:00:02Z",
                "instance_id": "inst-1",
                "role": "",
                "label_ordinal": 2,
            },
        ]
    )

    response = _post_unpair(env, "iPhone (2)")

    assert response.status_code == 200
    assert response.get_json()["unpaired"] == FP_TWO
    authorized = AuthorizedClients(authorized_clients_path())
    assert authorized.is_authorized(FP_ONE) is True
    assert authorized.is_authorized(FP_TWO) is False


def test_unpair_display_label_reaches_blank_assigned_client_label(link_env) -> None:
    env = link_env()
    _write_authorized(
        [
            {
                "fingerprint": FP_ONE,
                "device_label": "",
                "client_label": "client-host",
                "paired_at": "2026-04-19T00:00:01Z",
                "instance_id": "inst-1",
                "role": "",
            },
        ]
    )

    response = _post_unpair(env, "client-host")

    assert response.status_code == 200
    assert response.get_json()["unpaired"] == FP_ONE
    assert AuthorizedClients(authorized_clients_path()).is_authorized(FP_ONE) is False


def test_unpair_hand_edited_display_label_collision_refuses(link_env) -> None:
    env = link_env()
    _write_authorized(
        [
            {
                "fingerprint": FP_ONE,
                "device_label": "iPhone (2)",
                "paired_at": "2026-04-19T00:00:01Z",
                "instance_id": "inst-1",
                "role": "",
            },
            {
                "fingerprint": FP_TWO,
                "device_label": "iPhone",
                "paired_at": "2026-04-19T00:00:02Z",
                "instance_id": "inst-1",
                "role": "",
                "label_ordinal": 2,
            },
            {
                "fingerprint": FP_THREE,
                "device_label": "iPhone (2)",
                "paired_at": "2026-04-19T00:00:03Z",
                "instance_id": "inst-1",
                "role": "",
            },
        ]
    )

    response = _post_unpair(env, "iPhone (2)")

    body = response.get_json()
    assert response.status_code == 400
    assert body["reason_code"] == "invalid_operation_for_state"
    assert f"  sol call link unpair {FP_ONE}" in body["detail"]
    assert f"  sol call link unpair {FP_TWO}" in body["detail"]
    assert f"  sol call link unpair {FP_THREE}" in body["detail"]
    assert (
        body["detail"].index(FP_ONE)
        < body["detail"].index(FP_TWO)
        < body["detail"].index(FP_THREE)
    )

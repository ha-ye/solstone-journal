# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from typing import Any

from solstone.convey.secure_listener import ConveyIdentity
from solstone.think.link.auth import AuthorizedClients
from solstone.think.link.paths import authorized_clients_path


def pl_identity(fingerprint: str) -> ConveyIdentity:
    return ConveyIdentity(
        mode="pl-via-spl",
        fingerprint=fingerprint,
        device_label="pl-observer",
        paired_at="2026-05-20T00:00:00Z",
        session_id="session-1",
    )


def authorize_cert_observer_device(fingerprint: str) -> AuthorizedClients:
    authorized = AuthorizedClients(authorized_clients_path())
    authorized.add(
        fingerprint,
        "pl-observer",
        "instance-1",
        paired_at="2026-05-20T00:00:00Z",
    )
    return authorized


def observer_register_payload(name: str) -> dict[str, str]:
    if "." in name:
        hostname, stream_type = name.rsplit(".", 1)
    else:
        hostname, stream_type = name, "desktop"
    return {
        "platform": "linux",
        "hostname": hostname,
        "stream_type": stream_type,
        "version": "test",
    }


def register_bound_observer(client: Any, name: str, fingerprint: str):
    authorize_cert_observer_device(fingerprint)
    return client.post(
        "/app/observer/register",
        json=observer_register_payload(name),
        environ_base={"REMOTE_ADDR": "192.168.1.5"},
        environ_overrides={"pl.identity": pl_identity(fingerprint)},
    )


def register_unbound_observer(client: Any, name: str):
    return client.post(
        "/app/observer/register",
        json=observer_register_payload(name),
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
        environ_overrides={"pl.identity": None},
    )

# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
from pathlib import Path

from solstone.think.link.ca import cert_fingerprint
from solstone.think.link.client import ClientIdentity

PL_BUNDLE_FILES = {
    "private.pem",
    "cert.pem",
    "chain.pem",
    "home_attestation.jwt",
    "peer.json",
}


def endpoint_label(endpoint: dict[str, object]) -> str:
    host = str(endpoint.get("ip") or endpoint.get("host") or "?")
    port = endpoint.get("port") or 7657
    return f"lan-direct {host}:{port}"


def load_client_identity(bundle_dir: Path) -> ClientIdentity:
    if not bundle_dir.is_dir():
        raise ValueError(f"PL bundle not found: {bundle_dir}")

    missing = sorted(
        name for name in PL_BUNDLE_FILES if not (bundle_dir / name).exists()
    )
    if missing:
        raise ValueError(
            "missing PL bundle file: "
            + ", ".join(str(bundle_dir / name) for name in missing)
        )

    private_key_pem = (bundle_dir / "private.pem").read_text(encoding="utf-8")
    client_cert_pem = (bundle_dir / "cert.pem").read_text(encoding="utf-8")
    ca_chain_pem = (bundle_dir / "chain.pem").read_text(encoding="utf-8")
    home_attestation = (bundle_dir / "home_attestation.jwt").read_text(encoding="utf-8")
    try:
        peer = json.loads((bundle_dir / "peer.json").read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid peer.json in {bundle_dir}: {exc}") from exc

    local_endpoints = peer.get("local_endpoints", [])
    if local_endpoints is None:
        local_endpoints = []
    if not isinstance(local_endpoints, list):
        raise ValueError("peer.json local_endpoints must be a list")

    return ClientIdentity(
        private_key_pem=private_key_pem,
        client_cert_pem=client_cert_pem,
        ca_chain_pem=ca_chain_pem,
        fingerprint=cert_fingerprint(client_cert_pem),
        home_instance_id=str(peer.get("instance_id") or ""),
        home_label=str(peer.get("home_label") or ""),
        home_attestation=home_attestation,
        local_endpoints=tuple(local_endpoints),
    )

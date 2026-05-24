# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
from pathlib import Path

import requests

from solstone.think.link.ca import cert_fingerprint
from solstone.think.link.client import _build_csr
from tests.link.live_helpers import RELAY_URL


def pair_observer(base_url: str, label: str) -> dict:
    start = requests.post(
        f"{base_url}/app/link/pair-start",
        json={"device_label": label, "role": "observer"},
        timeout=10,
    )
    start.raise_for_status()
    private_key_pem, csr_pem = _build_csr(label)
    paired = requests.post(
        f"{base_url}/app/link/pair",
        json={"nonce": start.json()["nonce"], "csr": csr_pem, "device_label": label},
        timeout=10,
    )
    paired.raise_for_status()
    payload = paired.json()
    payload["private_key"] = private_key_pem
    return payload


def write_bundle(config_home: Path, label: str, identity: dict) -> None:
    bundle = config_home / "solstone-observer" / "spl" / label
    bundle.mkdir(parents=True)
    chain_pem = "".join(identity["ca_chain"])
    peer = {
        "label": label,
        "paired_at": "2026-05-20T00:00:00Z",
        "instance_id": identity["instance_id"],
        "home_label": identity["home_label"],
        "fingerprint": cert_fingerprint(chain_pem),
        "local_endpoints": identity.get("local_endpoints", []),
        "role": "observer",
    }
    (bundle / "private.pem").write_text(identity["private_key"], encoding="utf-8")
    (bundle / "cert.pem").write_text(identity["client_cert"], encoding="utf-8")
    (bundle / "chain.pem").write_text(chain_pem, encoding="utf-8")
    (bundle / "home_attestation.jwt").write_text(
        identity["home_attestation"],
        encoding="utf-8",
    )
    (bundle / "peer.json").write_text(json.dumps(peer, indent=2) + "\n")


def write_observer_config(journal: Path, label: str) -> None:
    path = journal / "config" / "journal.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "observe": {
                    "observer": {
                        "pair_mode": "pl",
                        "spl_label": label,
                        "spl_relay_url": RELAY_URL,
                        "name": "pytest-pl",
                    }
                }
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

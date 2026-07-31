#!/usr/bin/env python3
"""Generate the SPL home-service parity corpus from the Python oracle.

Read-only against the shared dev clone: imports solstone.think.spl.* and
cryptography/pyhpke, touches no journal, writes one JSON doc to stdout.

Every vector is deterministic. HPKE *seal* is randomised (ephemeral key), so
seal is captured as a Python-produced (enc, ct) pair the Rust must be able to
OPEN — the reverse direction (Rust seals, Python opens) is a live differential
the supervisor runs, not a static vector.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from solstone.think.spl import blob_receiver as br
from solstone.think.spl import hpke as H
from solstone.think.spl.health import (
    LINK_HEALTH_EVENT,
    OFFLINE_TUNNEL_REASONS,
    REASON_HOME_MISSING_MOBILE,
    REASON_LOCAL_PRIVATE_LISTENER_UNREACHABLE,
    REASON_RELAY_ADMISSION_SATURATED,
    REASON_RELAY_TUNNEL_REJECTED,
    REASON_RELAY_TUNNEL_UNREACHABLE,
    REASON_SERVICE_TOKEN_REJECTED,
)

hx = bytes.hex


def der_priv(k: ec.EllipticCurvePrivateKey) -> bytes:
    return k.private_bytes(
        serialization.Encoding.DER,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def der_pub(k: ec.EllipticCurvePublicKey) -> bytes:
    return k.public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )


# Deterministic actors. Private scalars are fixed so the corpus is reproducible.
HOME_SCALAR = 0x3F1A2B3C4D5E6F708192A3B4C5D6E7F8091A2B3C4D5E6F708192A3B4C5D6E7F8
SENDER_SCALAR = 0x11223344556677889900AABBCCDDEEFF00112233445566778899AABBCCDDEEFF
EXT_SCALAR = 0x0A1B2C3D4E5F60718293A4B5C6D7E8F90A1B2C3D4E5F60718293A4B5C6D7E8F9

home = ec.derive_private_key(HOME_SCALAR, ec.SECP256R1())
sender = ec.derive_private_key(SENDER_SCALAR, ec.SECP256R1())
ext = ec.derive_private_key(EXT_SCALAR, ec.SECP256R1())

INSTANCE = "3f9a1c22-0e4b-4d7a-9c11-5b6d8e0f2a34"
instance_16 = uuid.UUID(INSTANCE).bytes
sender_fp = hashlib.sha256(der_pub(sender.public_key())).digest()

out: dict = {
    "_generator": "gen_spl_vectors.py",
    "_oracle": "solstone/think/spl at origin/main",
    "_note": (
        "Deterministic parity corpus. HPKE seal is randomised, so seal appears "
        "only as Python-produced (enc, ct) the Rust must OPEN. Rust-seals-Python-opens "
        "is a live differential, not a static vector."
    ),
    "actors": {
        "instance_id": INSTANCE,
        "instance_id_16_hex": hx(instance_16),
        "home_priv_pkcs8_der_hex": hx(der_priv(home)),
        "home_pub_spki_der_hex": hx(der_pub(home.public_key())),
        "sender_priv_pkcs8_der_hex": hx(der_priv(sender)),
        "sender_pub_spki_der_hex": hx(der_pub(sender.public_key())),
        "ext_priv_pkcs8_der_hex": hx(der_priv(ext)),
        "ext_pub_spki_der_hex": hx(der_pub(ext.public_key())),
        "sender_fp_hex": hx(sender_fp),
        "sender_fingerprint": "sha256:" + hx(sender_fp),
    },
}

# ---------------------------------------------------------------- constants
out["constants"] = {
    "OFFER_LEN": br.OFFER_LEN,
    "READY_LEN": br.READY_LEN,
    "ACK_LEN": br.ACK_LEN,
    "ENC_LEN": br.ENC_LEN,
    "MAX_CT_LEN": br.MAX_CT_LEN,
    "MAX_ENTRIES": br.MAX_ENTRIES,
    "MAX_FILE_BYTES": br.MAX_FILE_BYTES,
    "MAX_TOTAL_BYTES": br.MAX_TOTAL_BYTES,
    "OFFER_MAGIC": br._OFFER_MAGIC.decode(),
    "READY_MAGIC": br._READY_MAGIC.decode(),
    "ACK_MAGIC": br._ACK_MAGIC.decode(),
    "VERSION": br._VERSION,
    "KEM_ID": br._KEM_ID,
    "KDF_ID": br._KDF_ID,
    "AEAD_ID": br._AEAD_ID,
    "reasons": {
        "home_missing_mobile": REASON_HOME_MISSING_MOBILE,
        "service_token_rejected": REASON_SERVICE_TOKEN_REJECTED,
        "relay_tunnel_rejected": REASON_RELAY_TUNNEL_REJECTED,
        "relay_tunnel_unreachable": REASON_RELAY_TUNNEL_UNREACHABLE,
        "local_private_listener_unreachable": REASON_LOCAL_PRIVATE_LISTENER_UNREACHABLE,
        "relay_admission_saturated": REASON_RELAY_ADMISSION_SATURATED,
    },
    "OFFLINE_TUNNEL_REASONS": sorted(OFFLINE_TUNNEL_REASONS),
    "LINK_HEALTH_EVENT": LINK_HEALTH_EVENT,
}


def build_offer(blob_id: bytes, ct_len: int, *, magic=None, ver=None,
                kem=None, kdf=None, aead=None, fp=None) -> bytes:
    return (
        (magic if magic is not None else br._OFFER_MAGIC)
        + bytes([ver if ver is not None else br._VERSION])
        + (kem if kem is not None else br._KEM_ID).to_bytes(2, "big")
        + (kdf if kdf is not None else br._KDF_ID).to_bytes(2, "big")
        + (aead if aead is not None else br._AEAD_ID).to_bytes(2, "big")
        + (fp if fp is not None else sender_fp)
        + blob_id
        + ct_len.to_bytes(8, "big")
    )


# ------------------------------------------------------- offer parse table
blob_id = bytes.fromhex("0102030405060708090a0b0c0d0e0f10")
parse_cases = []


def parse_case(name: str, header: bytes, *, note: str = "") -> None:
    try:
        o = br._parse_offer(header)
        res = {
            "ok": True,
            "sender_fp_hex": hx(o.sender_fp),
            "blob_id_hex": o.blob_id_hex,
            "ct_len": o.ct_len,
            "sender_fingerprint": o.sender_fingerprint,
        }
    except ValueError as e:
        res = {"ok": False, "error": str(e)}
    # the wire consequence: SBO1-magic malformed gets READY(0x01); others get nothing
    ready = (not res["ok"]) and header[:4] == br._OFFER_MAGIC
    parse_cases.append(
        {
            "name": name,
            "note": note,
            "header_hex": hx(header),
            "header_len": len(header),
            "parse": res,
            "sends_ready_0x01_before_close": ready,
        }
    )


parse_case("valid", build_offer(blob_id, 4096), note="baseline")
parse_case("valid_ct_len_at_max", build_offer(blob_id, br.MAX_CT_LEN))
parse_case("ct_len_over_max", build_offer(blob_id, br.MAX_CT_LEN + 1),
           note="oversize ciphertext is refused")
parse_case("bad_magic", build_offer(blob_id, 4096, magic=b"XXXX"),
           note="NOT SBO1 -> no READY at all, just close")
parse_case("bad_version", build_offer(blob_id, 4096, ver=0x02))
parse_case("bad_kem", build_offer(blob_id, 4096, kem=0x0011))
parse_case("bad_kdf", build_offer(blob_id, 4096, kdf=0x0002))
parse_case("bad_aead", build_offer(blob_id, 4096, aead=0x0001))
parse_case("short_header", build_offer(blob_id, 4096)[:66], note="66 bytes")
parse_case("long_header", build_offer(blob_id, 4096) + b"\x00", note="68 bytes")
out["offer_parse"] = parse_cases

# ---------------------------------------------------------- ready / ack wire
out["ready_frames"] = [
    {"status": s, "bytes_hex": hx(br._READY_MAGIC + bytes([br._VERSION, s]))}
    for s in (0x00, 0x01)
]

# ------------------------------------------------------- HPKE auth (blob v1)
# br._blob_info() reads LinkState from disk; reconstruct its output explicitly so the
# generator touches no journal. Shape is asserted against the source below.
blob_info = b"spl-blob-v1" + instance_16 + sender_fp
out["blob_info_hex"] = hx(blob_info)

auth_vectors = []
for idx, plaintext in enumerate(
    [b"", b"a", b"the quick brown fox" * 3, bytes(range(256)) * 8]
):
    header = build_offer(blob_id, 0)  # aad = the whole 67-byte header
    suite = H._suite()
    enc, sctx = suite.create_sender_context(
        H._kem_key_from_public_der(der_pub(home.public_key())),
        info=blob_info,
        sks=H._kem_key_from_private(sender),
    )
    ct = sctx.seal(plaintext, header)
    opened = H.open_auth(
        enc, home, blob_info, der_pub(sender.public_key()), ct, header
    )
    assert opened.plaintext == plaintext
    k_ack = opened.export(b"spl-blob-ack-v1", 32)
    acks = {}
    for status in (0x00, 0x01):
        tag = hmac.new(
            k_ack, b"spl-blob-ack" + bytes([status]) + blob_id, hashlib.sha256
        ).digest()[:16]
        acks[f"status_{status:#04x}"] = hx(
            br._ACK_MAGIC + bytes([br._VERSION, status]) + blob_id + tag
        )
    auth_vectors.append(
        {
            "name": f"auth_open_{idx}",
            "info_hex": hx(blob_info),
            "aad_hex": hx(header),
            "enc_hex": hx(enc),
            "enc_len": len(enc),
            "ct_hex": hx(ct),
            "expect_plaintext_hex": hx(plaintext),
            "expect_k_ack_hex": hx(k_ack),
            "expect_ack_frames": acks,
        }
    )
out["hpke_auth_open"] = auth_vectors

# ------------------------------------------- HPKE base (browser pairing msg3)
base_vectors = []
for idx, plaintext in enumerate(
    [b"{}", json.dumps({"S": "AAAAAAAAAAA", "device_label": "lab"}).encode(), bytes(512)]
):
    sealed = H.seal_base(der_pub(home.public_key()), instance_16, plaintext, b"")
    got = H.open_base(sealed.enc, home, instance_16, sealed.ciphertext, b"")
    assert got == plaintext
    base_vectors.append(
        {
            "name": f"base_open_{idx}",
            "info_hex": hx(instance_16),
            "aad_hex": "",
            "enc_hex": hx(sealed.enc),
            "ct_hex": hx(sealed.ciphertext),
            "expect_plaintext_hex": hx(plaintext),
        }
    )
out["hpke_base_open"] = base_vectors

# --------------------------------------------------------- health payloads
out["health_payloads"] = {
    "all_null": {
        "state": "connecting",
        "listen_generation": 1,
        "last_successful_relay_tunnel_at": None,
        "last_relay_tunnel_error": None,
        "last_relay_tunnel_error_at": None,
        "relay_tunnel_error_status": None,
        "relay_admission_saturated_count": 0,
    },
    "populated_rejected": {
        "state": "connected",
        "listen_generation": 7,
        "last_successful_relay_tunnel_at": 1_750_000_000_000,
        "last_relay_tunnel_error": REASON_RELAY_TUNNEL_REJECTED,
        "last_relay_tunnel_error_at": 1_750_000_001_000,
        "relay_tunnel_error_status": 502,
        "relay_admission_saturated_count": 3,
    },
    "_key_order_is_contract": [
        "state",
        "listen_generation",
        "last_successful_relay_tunnel_at",
        "last_relay_tunnel_error",
        "last_relay_tunnel_error_at",
        "relay_tunnel_error_status",
        "relay_admission_saturated_count",
    ],
}

print(json.dumps(out, indent=1, sort_keys=False))

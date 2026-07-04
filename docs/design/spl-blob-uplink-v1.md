# SPL Blob Uplink v1

Home-side browser blob uplink rides the existing SPL relay tunnel. TLS tunnels
remain byte-for-byte unchanged; blob and browser-pairing tunnels are selected by
the first four bytes.

## Blob Transport

Transport: existing relay tunnel. Extension holds an ordinary device token
(`device_fp = SHA-256` of its P-256 pubkey SPKI DER, in place of a cert
fingerprint), dials `/session/dial` like a native client. These bytes ride the
opaque tunnel in place of TLS records. One blob per tunnel.

Crypto suite (HPKE, RFC 9180):

- mode=2 (auth) — sender authenticated by its static P-256 key
- kem=0x0010 DHKEM(P-256,HKDF-SHA256)
- kdf=0x0001 HKDF-SHA256
- aead=0x0002 AES-256-GCM

HPKE info = `"spl-blob-v1" || instance_id_16 || sender_fp_32`

- `instance_id_16` = home instance UUID as 16 raw bytes
- `sender_fp_32` = SHA-256(ext pubkey SPKI DER), raw 32 bytes

HPKE AAD = the Offer header bytes, exactly as received (`magic .. ct_len`).
Multi-byte integers are big-endian.

### ext->home Offer

Offset table, total 67 bytes:

| Offset | Size | Field |
|---:|---:|---|
| 0 | 4 | magic `"SBO1"` |
| 4 | 1 | version `0x01` |
| 5 | 2 | kem_id `0x0010` |
| 7 | 2 | kdf_id `0x0001` |
| 9 | 2 | aead_id `0x0002` |
| 11 | 32 | sender_fp SHA-256(ext SPKI DER) |
| 43 | 16 | blob_id UUIDv7 |
| 59 | 8 | ct_len u64 |

The AAD is exactly these 67 bytes.

### home->ext Ready

Offset table, total 6 bytes:

| Offset | Size | Field |
|---:|---:|---|
| 0 | 4 | magic `"SBR1"` |
| 4 | 1 | version `0x01` |
| 5 | 1 | status, `0x00` proceed; nonzero reject |

### ext->home Sealed

| Offset | Size | Field |
|---:|---:|---|
| 0 | 65 | enc, HPKE encapsulated key, uncompressed P-256 point |
| 65 | ct_len | ct, HPKE single-shot seal of gzip(tar(files)), AAD=Offer bytes |

Plaintext inside seal is `gzip(tar(...))` containing:

- `blob.json` -> `{ "v":1, "day":"YYYYMMDD", "segment":"HHMMSS_LEN", "host":"<host>", "meta": {...} }`
- one or more observer segment files, for example `browser_<host>.jsonl`

Tar entries must be plain files only. Absolute paths, `..`, path separators,
symlinks, hardlinks, devices, and FIFOs are rejected. Home caps entries and
uncompressed size.

### home->ext Ack

Offset table, total 38 bytes:

| Offset | Size | Field |
|---:|---:|---|
| 0 | 4 | magic `"SBA1"` |
| 4 | 1 | version `0x01` |
| 5 | 1 | status, `0x00` ok/stored; `0x01` duplicate |
| 6 | 16 | blob_id echo |
| 22 | 16 | tag |

`tag = first 16 bytes of HMAC-SHA256(K_ack, "spl-blob-ack" || status_byte || blob_id)`

`K_ack = HPKE context.export("spl-blob-ack-v1", 32)`

Idempotency is the existing observer per-handle content-SHA dedupe. Re-sending
content already held by the bound observer handle returns Ack status `0x01`.

## Browser Pairing

Browser registration reuses the 0x06 home-opened pair-window. The home relay-open
path remains unchanged and continues to carry RK in the `Sec-Pair-Key` header.

Message framing:

- msg1 = fixed 5 bytes: `"SBP1" || 0x01`
- msgs 2/3/4 = u32 big-endian length prefix + payload
- msg3/msg4 payload = `enc(65) || ct`

Inner pairing JSON is compact UTF-8 with no whitespace. Binary fields are
base64url unpadded strings.

1. ext->home PairHello: magic `"SBP1"` | version `0x01`.
2. home->ext signed identity: `{ "pkH_spki", "ca_spki", "instance_id", "sig" }`.
   `sig` is raw IEEE-P1363 `R || S`, 64 bytes. It signs raw bytes:
   `"spl-pair-browser-v1" || pkH_spki_DER || instance_id_16`.
   `ca_spki` is the CA public-key SPKI DER (base64url); the browser holds only
   the 16-byte `ca_fp_spki` pin from the 0x06 link, so the home must supply the
   full CA key here. The browser checks `SHA-256(ca_spki)[:16] == ca_fp_spki`,
   then verifies `sig` with it.
3. ext->home HPKE base-mode seal to pkH, info=`instance_id_16`, AAD=`b""`.
   Plaintext is `{ "S", "ext_pub_spki", "device_label" }`.
4. home verifies `S` through the existing single-use nonce store, registers the
   browser pubkey and bound observer handle, mints home attestation with
   `device_fp = "sha256:" + sha256(ext_SPKI_DER).hexdigest()`, and replies with
   HPKE BASE mode to `ext_pub`, info=`instance_id_16`, AAD=`b""`. Plaintext is
   `{ "instance_id", "home_attestation" }`.
5. ext POSTs `/enroll/device` over plain HTTPS to receive its device token.

The HPKE blob `sender_fp` is raw 32 bytes. The attestation/enroll `device_fp` is
the string `"sha256:" + lowercase hex`.

## RFC 9180 AUTH Fixture

Committed in `tests/spl/test_hpke_fixture.py`. The vector is immutable and pins
the `pyhpke==0.6.4` call shape:

- suite: `DHKEM(P-256,HKDF-SHA256) / HKDF-SHA256 / AES-256-GCM`
- recipient AUTH context from `(enc, skRm, info, pks=pkSm)`
- `ctx.open(ct, aad)` equals the fixture plaintext
- `ctx.export(b"", 32)` equals the fixture export value

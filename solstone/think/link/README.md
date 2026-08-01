# link helpers

Shared private-link pairing/runtime helpers. Public caller-side `sol link`
commands are native; this Python package remains the home-side/shared helper
package used by pairing, direct dialing, SPL, Convey, observe, and dashboards.

**Forked from [`github.com/solpbc/spl`](https://github.com/solpbc/spl) `home/` on 2026-04-20.**
The two copies are now fully independent: no pip dep, no submodule, no sync scripts.
The `spl` repo's `home/` continues as the open-source reference implementation of the protocol; this package keeps the shared implementation used by pairing, direct dialing, and the link dashboard. The supervised home-side rendezvous daemon is native `solstone-core spl service`, reached through `journal spl`.

## layout

| File | Purpose |
|------|---------|
| `ca.py` | Local CA lifecycle + CSR signing + home-attestation minting. |
| `auth.py` | `authorized_clients.json` reader/writer with mtime-reload and last-seen tracking. |
| `bundle.py` | Observer bundle loading and client identity conversion. |
| `client.py` | PL/SPL tunnel client protocol helpers. |
| `dialer.py` | Paired-device dialer used by observe and transfer flows. |
| `establish.py` | Home-side private-link setup and pairing bootstrap helpers. |
| `interface_watcher.py` | LAN/VPN interface polling and local endpoint advertising for pair links. |
| `local_endpoints.py` | Frozen wire values and serializers for LAN-direct endpoint responses. |
| `mark.py` | Journal mark derivation and display assets. |
| `nonces.py` | Pair-ceremony nonce store shared with Convey pair routes. |
| `paths.py` | Journal-path helpers + `SOL_LINK_RELAY_URL` resolution. |
| `pair_window.py` | TLS-only relay pair-window bridge for native link clients. |
| `runtime.py` | Convey startup integration for link runtime state. |
| `tls.py` | Client-side TLS helper wrappers used by PL/SPL helpers. |
| `ws_buffer.py` | Unbounded byte buffering for the TLS pair-window bridge. |
| `window.py` | Pairing-window state helpers. |

TLS termination, multiplexing, and inline WSGI dispatch now live in
`solstone/convey/secure_listener/`, because Convey owns both listening ports:
the DL web port and the PL secure-listener port 7657.
Secure-listener capacity is configured with `link.secure_listener_capacity`; `link.secure_listener_streaming_capacity = 0` disables the streaming lane split; `link.secure_listener_queue_timeout_seconds` bounds queued admission waits, accepts 1.0-600.0 seconds, and `0` disables queue-timeout refusal.

## naming

- **link** — user-facing and architecturally-visible names: `sol link`, `sol call link`, `journal/link/`.
- **network** — Convey app identity: blueprint `app:network`, route prefix `/app/network`; the legacy `/app/link` alias remains for shipped pairing clients.
- **spl** — the home-side relay daemon (`journal spl`) and protocol-level constructs such as wire-format frames, JWT claim schemas, and reset reason codes. These reference the external stable spl protocol and keep that name.

The home-side daemon emits Callosum relay-status events on the internal `link` tract, and Convey caches the structured `link_health` snapshot for dashboard status.

## native join admission

Native `sol link join` owns direct pair-link decoding and admission. Direct
0x04 and 0x05 pair-links are structurally decoded before any key material is
generated or any socket is opened. The intent is to dial only local,
self-owned, or operator-controlled local-network addresses from a pasted direct
pair-link. If `--home` is supplied, it is applied only after the embedded set
passes that policy. The override is operator-supplied and may be a hostname, but
it still must include an explicit port.

## privacy

No payload bytes are ever logged. The CA private key never leaves `journal/link/ca/private.pem`; service tokens live in `journal/link/tokens/` and device tokens live on paired devices.

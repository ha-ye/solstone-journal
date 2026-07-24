# Native Sol Raw-Body Parity Vector Design

This note extends the parity corpus response shape documented in
`02-design.md`.

## Why

`decode_response` parsed response bodies with serde_json's default best-effort
float precision. Server-emitted entity scores `22.184388937230562` and
`9.785157957944147` therefore rendered as `22.18438893723056` and
`9.785157957944149`, while the Python oracle emitted the original bytes.

The fix is to enable serde_json's `float_roundtrip` feature. That feature is an
empty feature list: it adds no dependency edge, leaves `core/Cargo.lock`
unchanged, and affects parsing only. Serialization behavior is unchanged.

## `response.raw_body`

The `response` shape inside each transport request now supports exactly one body
source:

| Field | Meaning |
|---|---|
| `json` | Parsed JSON value. The harness serializes it back to response body bytes. |
| `raw_body` | String carrying the exact UTF-8 response body bytes. |

A vector response must set exactly one of `json` or `raw_body`. Both-present,
neither-present, and non-string `raw_body` cases panic or raise with a message
naming the vector id.

Use `raw_body` for any vector asserting float byte-parity. It exists so the
numeric literal is never round-tripped through serde_json at vector-load time.

## Harness Symmetry

The rule is enforced in both
`core/crates/solstone-core-sol-client-cli/tests/parity.rs` and
`tests/native_sol/run_python_parity.py`. Both harnesses read the same committed
corpus, so any future schema extension must land in both places.

## Coverage Classification

`scripts/check_native_sol_coverage.py` classifies success and failure from
`response.status`, not from `response.json`. Vectors using `raw_body` therefore
classify the same way as existing success responses.

## Reference Vector

`entities-overview-raw-body-numeric-roundtrip` in
`core/fixtures/native-sol/parity/entities.jsonl` carries both observed score
literals as raw response bytes. Its expected stdout is the exact
`json.dumps(body, indent=2, ensure_ascii=False)` output for that overview body.

## Shared-Client Regression

`core/crates/solstone-core-sol-client/tests/json_numeric_roundtrip.rs` starts
from raw `Vec<u8>` bodies and crosses `decode_response` plus all five
`json_format` modes.

| Assertion class | Values |
|---|---|
| Python-parity | `22.184388937230562`, `9.785157957944147`, `-9.785157957944147` |
| Renderer policy | `5e-324`, `1.7976931348623157e+308`, `1e+300`, `2.2250738585072014e-308` |

The Python-parity assertions pin byte-exact output against the oracle. The
exponent and subnormal cases assert accurate finite-`f64` decoding plus existing
renderer policy; they do not assert preservation of the server's original token
spelling.

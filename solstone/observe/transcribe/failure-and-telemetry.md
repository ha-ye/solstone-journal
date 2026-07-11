# Transcribe: failure semantics & telemetry

How `journal transcribe` reports what happened, and what the `observe.transcribed`
event carries. Two rules govern everything here:

1. **A failure is never reported as a success.** If the transcript was not produced,
   the process says so with an exit code and a reasoned event.
2. **Telemetry is content-free.** Timings and labels only — no transcript text, words,
   topics, setting, or emotion ever rides on an event.

## Exit-code contract

`journal transcribe <file>` exits with exactly one of:

| Exit | Meaning | Input file | Output |
|------|---------|-----------|--------|
| `0` | Work is done. Either a transcript was written, or the clip was silence-filtered / preserved by policy. | Consumed or preserved per policy | `.jsonl` (+ `.npz`) written, or deliberately not written for filtered silence |
| `69` (`EXIT_PROVIDER_BLOCKED`) | **Honest deferral.** The STT provider could not do the work. Nothing was attempted downstream, nothing was written. | **Preserved on disk** | None |
| `1` | Hard failure. Something broke that a retry will not fix on its own. | Preserved on disk | None |

`sense.py` treats each distinctly. On `69` it records **neither** a success nor a
failure — it calls `_check_segment_observed()` with no error and does not record a
successful contact. On `1` it records a handler failure and raises a notification.

The deferral path was previously a bare `return`, which exited `0` and made
`sense.py` log "Handler completed successfully" for a segment that was never
transcribed. That is the bug this contract closes.

## How the retry actually happens

Retry after a deferral is always **cross-process**. `FileSensor.start()` has no rescan
loop; there is no in-process retry, no backoff timer, and no attempt counter.

The re-attempt comes from the daily think run's sense-repair pre-phase, which shells
out to `journal sense --day <day>`. That builds a *fresh* `FileSensor` whose
`scan_unprocessed` re-picks any input that still lacks a `.jsonl`. Because the deferral
path writes nothing, the audio is still there and still lacks its output, so it is
picked up again on the next pass.

This is why a deferral must not write a placeholder or an empty output — doing so would
mark the segment done and the audio would never be retried.

## Defer vs. fail

| Condition | Classified as | Why |
|-----------|--------------|-----|
| Parakeet server unreachable, warming, or **dead mid-request** | Defer (`69`) | The server is a supervised process. It comes back. |
| Confidential lane refuses cloud egress | Defer (`69`) | The lane may permit a local backend later; the audio must not be lost. |
| HTTP 5xx from a live server, malformed JSON, contract violation | Fail (`1`) | The server answered — it is broken, not absent. Retrying the same request reproduces it. |
| Anything else unexpected | Fail (`1`) | Surface it. |

Server-death-mid-request is the subtle one. When the parakeet.cpp server is OOM-killed
partway through a request (measured: a clip longer than roughly 320–340 s exhausts the
6 GiB Vulkan backend, and the server exits 139 with no HTTP body), the connection drops
without a response and `httpx` raises `RemoteProtocolError`. That is a `TransportError`
but **not** a `NetworkError` — so the old explicit catch tuple missed it and the crash
surfaced as a hard failure. `_parakeet_cpp.transcribe()` now catches `httpx.TransportError`,
which covers connect, timeout, network *and* protocol failures in one class, while
deliberately leaving `DecodingError` / `TooManyRedirects` / `HTTPStatusError` uncaught.

Note that this makes the failure *honest*, not *rare*. A long clip will still OOM the
server; it will now defer, be re-picked the next day, and OOM again. Chunking long audio
so it stops OOMing is separate work.

## Reason strings

Every deferred and failed event carries a machine-readable `reason`.

| Reason | Raised by | Means |
|--------|-----------|-------|
| `no_port` | `parakeet_server.connect()` | The supervisor has published no port for the service. |
| `server_not_ready` | `parakeet_server.connect()` | Port exists; the health probe did not report ready (usually still loading the model). |
| `read_timeout` | transport classifier | Any `httpx.TimeoutException`, including `ConnectTimeout` (which is a timeout, not a connect error, in the httpx hierarchy). |
| `server_disconnected` | transport classifier | `httpx.ProtocolError` / `RemoteProtocolError` — **the server died mid-response.** |
| `connect_error` | transport classifier | `httpx.ConnectError` — nothing listening. |
| `network_error` | transport classifier | Other `httpx.NetworkError` (read/write errors on an established connection). |
| `transport_error` | transport classifier | Any other `TransportError` (proxy, unsupported protocol). |
| `confidential_egress_blocked` | `process_audio` | The confidential lane refused to send audio to a cloud backend. |
| *(provider reason code)* | failed path | On a hard failure from a provider error — e.g. `transcription_http_error`, `invalid_json`, `contract_violation`. |
| *(exception type name)* | failed path | On any other hard failure. |

The transport classifier (`_transport_retry_reason` in `_parakeet_cpp.py`) is the single
source of truth for the five transport reasons. Its checks run subclass-before-base
because the httpx exception tree overlaps.

## The `observe.transcribed` event

One event name, five outcomes. Every attempt emits exactly one event.

| Field | Type | Present on |
|-------|------|-----------|
| `outcome` | `transcribed` \| `deferred` \| `failed` \| `filtered` \| `preserved` | always |
| `input` | journal-relative path of the audio | always |
| `output` | journal-relative path of the `.jsonl` | success |
| `reason` | machine reason (table above) | deferred, failed |
| `error` | human-readable exception message | failed |
| `backend` | STT backend name (`parakeet-cpp`, `gemini`, …) | whenever resolved |
| `device` | resolved device (`auto` / `cpu`) | whenever known (see below) |
| `model` | model filename | success, and failures after the backend reported it |
| `audio_seconds` | original decoded length, 1 dp | whenever decoded |
| `reduced_seconds` | length after silence-trimming, 1 dp | when reduction ran |
| `rtfx` | `audio_seconds / (asr_ms / 1000)`, 2 dp | success, when ASR took ≥ 1 ms |
| `peak_rss_mib` | peak RSS of this process, MiB | always |
| `timings` | nested `{<stage>_ms: int}` | always (only the stages that ran) |
| `vad_duration`, `vad_speech`, `noisy`, RMS/loud stats | VAD summary | always |
| `duration_ms` | total wall-clock of `process_audio` | success |
| `day`, `segment`, `observer` | provenance | when derivable |

### Timing stages

Only stages that actually ran appear. A stage split across several calls (`write` covers
the jsonl and the npz) reports its total.

| Key | Stage |
|-----|-------|
| `queue_wait_ms` | Time the file sat in the sense queue. Measured by `sense.py` and passed in via `SOL_QUEUE_WAIT_MS`; the handler cannot compute it itself. Absent when not supplied. |
| `decode_ms` | `load_audio` |
| `vad_ms` | `run_vad` |
| `reduce_ms` | `reduce_audio` (absent when reduction was skipped) |
| `asr_ms` | `stt_transcribe` — the STT call itself |
| `enrich_ms` | `enrich_transcript` (absent when enrichment is disabled) |
| `embed_ms` | sentence-embedding generation |
| `overlap_ms` | overlap + log-prob computation |
| `diarize_ms` | local diarization (absent when skipped — the common case) |
| `write_ms` | jsonl + npz writes |

Deferred and failed events carry whatever completed before the failure — typically
`queue_wait`, `decode`, `vad`, `reduce`, and (on a mid-ASR death) `asr`.

### The content-free guarantee

No transcript text, word list, topic, setting, or emotion appears in any event field.
The event carries numbers, paths, and labels only. `tests/test_transcribe_telemetry.py`
enforces this by seeding the mocked STT with a sentinel string and asserting it appears
nowhere in the serialized event payload.

`error` on the failed path carries an exception message, which is provider-generated
diagnostic text, not transcript content.

## Intentionally not measured

These were considered and deliberately left out. Each would cost more than it is worth
right now.

- **Retry count.** Not stored, because it is **derivable**: every attempt emits one
  reason-tagged `deferred` event, so counting those per input across the daily retry
  cadence *is* the count. An in-memory counter would always read 1 (each retry is a
  fresh process), and a durable attempt ledger is a metrics service — a much larger
  thing than this problem justifies.
- **Cold vs. warm start.** STT runs one process per file with a fresh HTTP client. The
  persistent server's warmth is a property of the supervisor, not reliably observable
  from the client. A `cold` flag here would be a guess.
- **VRAM / GPU memory.** Needs a resource sampler (an `nvidia-smi` or Vulkan polling
  loop) — a standing subsystem, not a field. The single `resource.getrusage` read behind
  `peak_rss_mib` is deliberately the whole budget.
- **`model` on deferred events.** `get_model_info()` is cheap for the parakeet-cpp and
  cloud backends, but on Apple Silicon it shells out to the CoreML helper (`--version`,
  10 s timeout). Rather than hoist a subprocess probe onto a path whose whole point is
  *not* to do expensive work, deferred events omit `model`. `device` is still reported
  when the config names one.

## Rollback

Revert the lode. The change is behaviour-local to the transcribe handler
(`transcribe/main.py`, `transcribe/_parakeet_cpp.py`), the `ParakeetServerNotReady`
constructor in `think/providers/parakeet_server.py`, and the dead-fallback removal in
`observe/sense.py`. It adds only additive fields to an event that has no consumers, and
introduces one new `outcome` value. There is no schema migration, no config change, and
no on-disk format change to undo. Reverting restores the old (silently-successful)
behaviour and nothing else.

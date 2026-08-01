# SPL Rust divergences

## A8 — priority terminal Callosum delivery

- Classification: `expected-differs`
- Python behaviour: the Callosum writer treats every event as best-effort and
  drops any of them when its bounded output path is unavailable or full.
- Rust behaviour: `disconnect → health` and each `tunnel_close → health` pair
  are queued as ordered priority batches. When the bounded writer queue is
  full of ordinary telemetry, a terminal batch evicts the oldest ordinary
  message rather than being dropped. Distinct `tunnel_close` IDs are never
  coalesced.
- Rationale: dropping a terminal tunnel event leaves the dashboard showing a
  live tunnel indefinitely. The priority path remains nonblocking so a wedged
  Callosum consumer cannot hold a tunnel finally or the shutdown path open.
  If no ordinary telemetry remains, Rust retains the newest terminal event by
  evicting the oldest retained terminal event or unfinished terminal notice;
  it emits one class-only error line for that bounded-loss condition. This is
  intentionally newest-wins under a wedged consumer, never a blocking wait.

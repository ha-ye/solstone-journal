# Synthetic Oura Fixture Bundle

Synthetic Oura API v2 usercollection page documents for
`solstone/think/importers/oura.py`. Field names mirror the real API shape
(`{"data": [...], "next_token": ...}` pages; `id`, `day`, `score`,
`contributors`, stage durations in seconds) but every value is invented.
This directory intentionally contains no real account, device, or health
data, and never may.

Files: one `<endpoint>.json` per supported endpoint — `daily_sleep`,
`daily_readiness`, `daily_resilience`, `daily_stress`, `daily_spo2`,
`sleep`.

# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Reproducible synthetic mixed-load benchmark for bundled-local admission."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import statistics
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import httpx

from solstone.think.providers.local_admission import acquire_local_slot

WORKLOADS = (
    {
        "name": "short_json",
        "prompt": "Return JSON with keys summary and confidence about a synthetic note.",
        "max_tokens": 48,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "short",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {
                        "summary": {"type": "string"},
                        "confidence": {"type": "number"},
                    },
                    "required": ["summary", "confidence"],
                    "additionalProperties": False,
                },
            },
        },
    },
    {
        "name": "entity_json",
        "prompt": (
            "From this synthetic project update, return JSON containing people and "
            "projects arrays. Alex discussed Orion with Sam; no real data is present."
        ),
        "max_tokens": 96,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "entities",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {
                        "people": {"type": "array", "items": {"type": "string"}},
                        "projects": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["people", "projects"],
                    "additionalProperties": False,
                },
            },
        },
    },
    {
        "name": "brief_text",
        "prompt": "Summarize a synthetic meeting in three concise bullet points.",
        "max_tokens": 72,
    },
)


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = (len(ordered) - 1) * percentile
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = rank - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _distribution(values: list[float]) -> dict[str, float]:
    return {
        "p50": round(_percentile(values, 0.50), 3),
        "p95": round(_percentile(values, 0.95), 3),
        "p99": round(_percentile(values, 0.99), 3),
        "mean": round(statistics.fmean(values), 3) if values else 0.0,
        "max": round(max(values), 3) if values else 0.0,
    }


def _server_ms(data: dict[str, Any]) -> float:
    timings = data.get("timings")
    if not isinstance(timings, dict):
        return 0.0
    return float(timings.get("prompt_ms") or 0) + float(
        timings.get("predicted_ms") or 0
    )


def _run_one(
    *,
    endpoint: str,
    model: str,
    workload: dict[str, Any],
    slots: int,
    admitted: bool,
    timeout_s: float,
) -> dict[str, Any]:
    started = time.monotonic()
    permit = acquire_local_slot(slots, timeout_s) if admitted else None
    queue_wait_ms = permit.queue_wait_ms if permit is not None else 0.0
    try:
        body = {
            "model": model,
            "messages": [{"role": "user", "content": workload["prompt"]}],
            "max_tokens": workload["max_tokens"],
            "temperature": 0.2,
            "stream": False,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        if "response_format" in workload:
            body["response_format"] = workload["response_format"]
        response = httpx.post(
            f"{endpoint.rstrip('/')}/v1/chat/completions",
            json=body,
            timeout=timeout_s,
        )
        response.raise_for_status()
        data = response.json()
        latency_ms = (time.monotonic() - started) * 1000.0
        server_ms = _server_ms(data)
        return {
            "workload": workload["name"],
            "ok": True,
            "latency_ms": latency_ms,
            "queue_wait_ms": queue_wait_ms,
            "server_ms": server_ms,
            "opaque_wait_ms": max(0.0, latency_ms - queue_wait_ms - server_ms),
        }
    except Exception as exc:
        return {
            "workload": workload["name"],
            "ok": False,
            "latency_ms": (time.monotonic() - started) * 1000.0,
            "queue_wait_ms": queue_wait_ms,
            "server_ms": 0.0,
            "opaque_wait_ms": 0.0,
            "error_type": type(exc).__name__,
        }
    finally:
        if permit is not None:
            permit.release()


def _sample_gpu(stop: threading.Event, samples: list[dict[str, float]]) -> None:
    while not stop.wait(0.1):
        try:
            output = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=memory.used,utilization.gpu",
                    "--format=csv,noheader,nounits",
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=2,
            ).stdout.splitlines()[0]
            memory_mib, utilization = (
                float(item.strip()) for item in output.split(",")
            )
            samples.append(
                {"gpu_memory_mib": memory_mib, "gpu_utilization_pct": utilization}
            )
        except (FileNotFoundError, IndexError, ValueError, subprocess.SubprocessError):
            return


def _summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    successful = [row for row in rows if row["ok"]]
    return {
        "requests": len(rows),
        "succeeded": len(successful),
        "failed": len(rows) - len(successful),
        "latency_ms": _distribution([row["latency_ms"] for row in successful]),
        "queue_wait_ms": _distribution([row["queue_wait_ms"] for row in successful]),
        "opaque_wait_ms": _distribution([row["opaque_wait_ms"] for row in successful]),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--model", default="local/qwen3.5-4b")
    parser.add_argument("--slots", type=int, required=True)
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--requests", type=int, default=30)
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--mode", choices=("baseline", "admitted"), required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.slots < 1 or args.concurrency < 1 or args.requests < 1:
        parser.error("slots, concurrency, and requests must be positive")

    with tempfile.TemporaryDirectory(prefix="solstone-admission-bench-") as state_dir:
        os.environ["SOLSTONE_JOURNAL"] = state_dir
        started = time.monotonic()
        gpu_samples: list[dict[str, float]] = []
        stop = threading.Event()
        sampler = threading.Thread(
            target=_sample_gpu, args=(stop, gpu_samples), daemon=True
        )
        sampler.start()
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=args.concurrency
        ) as executor:
            futures = [
                executor.submit(
                    _run_one,
                    endpoint=args.endpoint,
                    model=args.model,
                    workload=WORKLOADS[index % len(WORKLOADS)],
                    slots=args.slots,
                    admitted=args.mode == "admitted",
                    timeout_s=args.timeout,
                )
                for index in range(args.requests)
            ]
            rows = [future.result() for future in futures]
        elapsed_s = time.monotonic() - started
        stop.set()
        sampler.join(timeout=1)

    result = {
        "mode": args.mode,
        "endpoint": args.endpoint,
        "model": args.model,
        "configured_slots": args.slots,
        "producer_concurrency": args.concurrency,
        "elapsed_s": round(elapsed_s, 3),
        "throughput_requests_per_s": round(args.requests / elapsed_s, 4),
        "all": _summarize(rows),
        "by_workload": {
            workload["name"]: _summarize(
                [row for row in rows if row["workload"] == workload["name"]]
            )
            for workload in WORKLOADS
        },
        "resources": {
            "gpu_samples": len(gpu_samples),
            "peak_gpu_memory_mib": max(
                (sample["gpu_memory_mib"] for sample in gpu_samples), default=None
            ),
            "peak_gpu_utilization_pct": max(
                (sample["gpu_utilization_pct"] for sample in gpu_samples),
                default=None,
            ),
        },
    }
    rendered = json.dumps(result, indent=2, sort_keys=True)
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()

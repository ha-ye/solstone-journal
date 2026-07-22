#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc
"""External install-proof host adapter for the release rail."""

from __future__ import annotations

import json
import os
import shlex
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.channel_adapters.adapter_common import (  # noqa: E402
    LaneConfig,
    die,
    load_config,
    read_json,
    require_success_token,
    run,
    scp_from,
    scp_to,
    ssh_run,
    verify_retrieved_file,
    write_json,
)
from scripts.release_proof_host import TARGET_ENV_KEYS, TARGET_POLICY  # noqa: E402

PROOF_TOKEN = "PROOF_OK"


def _remote_work(lane: LaneConfig, cohort_id: str) -> str:
    return f"{lane.remote_work_prefix}-proof-{cohort_id}"


def _harness(clone_root: str, reqdir: str) -> str:
    return f"""
import json, sys
from pathlib import Path
if sys.version_info < (3, 11):
    sys.stderr.write("host python too old (%s); need >= 3.11 for the rail\\n" % sys.version.split()[0])
    raise SystemExit(7)
sys.path.insert(0, {clone_root!r})
from scripts.release_digest import file_sha256_size
from scripts.release_install_smoke import run_install_proof
reqdir = Path({reqdir!r})
req = json.loads((reqdir / "request.json").read_text())
ledger = json.loads((reqdir / "ledger.json").read_text())
candidate_dir = reqdir / "candidate"
candidate_paths = [candidate_dir / f["basename"] for f in req["candidate_files"]]
proof_path = reqdir / "output" / "proof.json"
run_install_proof(
    target=req["target"],
    version=req["version"],
    source_commit=req["source_commit"],
    core_lock_sha256=req["core_lock_sha256"],
    candidate_digest=req["candidate_digest"],
    ledger_sha256=req["ledger_sha256"],
    candidate_dir=candidate_dir,
    candidate_paths=candidate_paths,
    ledger_payload=ledger,
    output_path=proof_path,
)
proof_sha256, proof_bytes = file_sha256_size(proof_path)
print("PROOF_OK " + json.dumps({{"sha256": proof_sha256, "bytes": proof_bytes}}, sort_keys=True))
"""


def _proof_status(stdout: str) -> tuple[str, int]:
    for line in stdout.splitlines():
        if not line.startswith(f"{PROOF_TOKEN} "):
            continue
        try:
            payload = json.loads(line[len(PROOF_TOKEN) + 1 :])
        except json.JSONDecodeError:
            break
        if isinstance(payload, dict):
            sha256 = payload.get("sha256")
            byte_count = payload.get("bytes")
            if isinstance(sha256, str) and isinstance(byte_count, int):
                return sha256, byte_count
    die("proof run did not report proof digest/size", detail=stdout)


def _verify_host(target: str, lane: LaneConfig) -> None:
    exp_os, exp_arch = TARGET_POLICY[target]
    if lane.is_local:
        got = run(["uname", "-s"]).stdout.strip(), run(["uname", "-m"]).stdout.strip()
    else:
        result = ssh_run(lane, "uname -s; uname -m", check=False)
        lines = (result.stdout or "").split()
        got = (lines[0] if lines else "?", lines[1] if len(lines) > 1 else "?")
    if got[0] != exp_os or got[1] != exp_arch:
        die(f"proof host os/arch {got} != expected ({exp_os},{exp_arch}) for {target}")


def prove(target: str, lane: LaneConfig, request_path: Path) -> None:
    if target not in TARGET_ENV_KEYS:
        die(f"unknown target: {target}")
    req = read_json(request_path)
    cohort = req["cohort_id"]
    request_dir = request_path.resolve().parent
    ledger_path = request_dir / req["paths"]["ledger"]
    candidate_dir = request_dir / req["paths"]["candidate_dir"]
    out_proof = request_dir / req["paths"]["proof"]
    out_proof.parent.mkdir(parents=True, exist_ok=True)

    _verify_host(target, lane)

    if lane.is_local:
        harness = _harness(str(ROOT), str(request_dir))
        clean_env = {
            key: value for key, value in os.environ.items() if key != "PYTHONPATH"
        }
        result = run([sys.executable, "-c", harness], check=False, env=clean_env)
        require_success_token(result, PROOF_TOKEN, "local proof run")
        expected_sha256, expected_bytes = _proof_status(result.stdout or "")
    else:
        work = _remote_work(lane, cohort)
        rreq = f"{work}/reqdir"
        quoted_work = shlex.quote(work)
        quoted_rreq = shlex.quote(rreq)
        ssh_run(
            lane,
            f"set -e; rm -rf {quoted_work}; "
            f"mkdir -p {quoted_rreq}/candidate {quoted_rreq}/output {quoted_work}/clone",
        )
        fd, bundle_name = tempfile.mkstemp(
            prefix=f"proof-src-{cohort}-",
            suffix=".bundle",
        )
        os.close(fd)
        bundle = Path(bundle_name)
        try:
            run(["git", "-C", str(ROOT), "bundle", "create", str(bundle), "HEAD"])
            scp_to(lane, bundle, f"{work}/src.bundle")
        finally:
            bundle.unlink(missing_ok=True)
        ssh_run(lane, f"git clone --quiet {quoted_work}/src.bundle {quoted_work}/clone")
        scp_to(lane, request_path, f"{rreq}/request.json")
        scp_to(lane, ledger_path, f"{rreq}/ledger.json")
        for candidate in req["candidate_files"]:
            name = candidate["basename"]
            scp_to(lane, candidate_dir / name, f"{rreq}/candidate/{name}")
        harness = _harness(f"{work}/clone", rreq)
        hpath = f"{work}/harness.py"
        ssh_run(
            lane, f"cat > {shlex.quote(hpath)} <<'HARNESS_EOF'\n{harness}\nHARNESS_EOF"
        )
        result = ssh_run(
            lane, f"{shlex.quote(lane.remote_python)} {shlex.quote(hpath)}", check=False
        )
        require_success_token(result, PROOF_TOKEN, f"remote proof run on {target}")
        expected_sha256, expected_bytes = _proof_status(result.stdout or "")
        scp_from(lane, f"{rreq}/output/proof.json", out_proof)

    verify_retrieved_file(
        out_proof,
        expected_sha256=expected_sha256,
        expected_bytes=expected_bytes,
        label="proof.json",
    )
    exp_os, exp_arch = TARGET_POLICY[target]
    response = {
        "schema_version": 1,
        "cohort_id": cohort,
        "attestation": {
            "os": exp_os,
            "arch": exp_arch,
            "candidate_digest": req["candidate_digest"],
            "ledger_sha256": req["ledger_sha256"],
        },
        "proof": {
            "path": req["paths"]["proof"],
            "sha256": expected_sha256,
            "bytes": expected_bytes,
        },
    }
    write_json(request_dir / req["paths"]["response"], response)


def cleanup(target: str, lane: LaneConfig, cohort_id: str, _ledger_sha256: str) -> None:
    if lane.is_local:
        return
    ssh_run(
        lane,
        f"rm -rf {shlex.quote(_remote_work(lane, cohort_id))}",
        check=False,
    )


def main(argv: list[str]) -> int:
    _build_lane, proof_lanes = load_config(proof_targets=tuple(TARGET_ENV_KEYS))
    if len(argv) < 3 or argv[0] != "--target":
        die(
            "usage: proof_host.py --target <t> {prove <req>|cleanup <cohort> <ledger_sha256>}"
        )
    target = argv[1]
    sub = argv[2]
    rest = argv[3:]
    if target not in proof_lanes:
        die(f"unknown target: {target}")
    lane = proof_lanes[target]
    if sub == "prove":
        if len(rest) != 1:
            die("prove requires a request file path")
        prove(target, lane, Path(rest[0]))
        return 0
    if sub == "cleanup":
        if len(rest) != 2:
            die("cleanup requires <cohort_id> <ledger_sha256>")
        cleanup(target, lane, rest[0], rest[1])
        return 0
    die(f"unknown subcommand: {sub}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc
#
# solstone release-candidate rail.
#
# This script is only the candidate rail dispatcher. It can finalize a
# provider-neutral release candidate, revalidate retained candidate evidence, or
# run the narrowed Linux structural dry-run. Publication lives in the aggregate
# publisher: make publish-release RELEASE_DIR=dist/release-candidate/<version>/
# or make publish-release-test RELEASE_DIR=dist/release-candidate/<version>/.
#
# DESTRUCTIVE: --candidate is fresh construction; before policy or build work it
# deletes prior raw build/dist outputs and that version's stale payload/evidence.
#
# Candidate flow:
#   1. Verify the expected source commit, clean source tree, and core lock.
#   2. Delete prior raw outputs and stale retained payload/evidence for the
#      version being constructed.
#   3. Build local artifacts and receive macOS artifacts through the externally
#      configured build-host channel.
#   4. Generate a fresh proof challenge, extract the nvattest authority from the
#      candidate's own root wheels and require every target to agree, and
#      materialize the locked support-wheel set, verifying it against uv.lock.
#      All of this happens before the ledger is written and before any proof
#      host is contacted.
#   5. Collect a per-target receipt pair through configured proof-host channels
#      -- the install/smoke proof and a challenge-bound nvattest proof -- then
#      pair-promote payload and evidence.
#   6. Revalidate payload, manifests, ledger, retained support wheels, and both
#      receipt classes, then print canonical local readiness JSON. This is not
#      publication authorization.
#
# Recovery flow:
#   `--recover <version> <source-commit>` is retained-byte-only, read-only
#   validation. It preserves retained payload, ledger, retained support wheels,
#   and both receipt classes, and never rebuilds, refreshes advisories, contacts
#   hosts, installs wheels, reads authentication, or uses the network. It
#   re-derives the nvattest authority from the retained candidate wheel bytes
#   rather than trusting the ledger, so a ledger and receipt set forged to agree
#   with itself is still rejected. It reads no release lock: lock agreement is
#   enforced when the candidate is cut.
#
# Dry-run flow:
#   `--dry-run-linux` is structural only. It emits no ready payload, manifest,
#   ledger, proof, or clean-source claim.

set -euo pipefail

PUBLISH_LOCKOUT_MESSAGE="publishing is handled by scripts/release_publish.py; run make publish-release RELEASE_DIR=dist/release-candidate/<version>/ or make publish-release-test RELEASE_DIR=dist/release-candidate/<version>/"

usage() {
    cat <<'EOF'
Usage: scripts/release.sh [--candidate|--recover <version> <source-commit>|--dry-run-linux]

Modes:
  --candidate       Finalize a non-publishing all-host release candidate, write
                    retained local evidence, and report readiness digests. Each
                    attempt generates a fresh proof challenge and requires a
                    per-target receipt pair: the install/smoke proof and a
                    challenge-bound nvattest proof.
                    DESTRUCTIVE: fresh construction deletes prior raw
                    build/dist outputs and that version's stale payload/evidence
                    before policy or build work begins.
  --recover VERSION SOURCE_COMMIT
                    Retained-byte-only, read-only validation of payload, ledger,
                    retained support wheels, and both per-target receipt classes
                    (install/smoke and nvattest). Re-derives the nvattest
                    authority from the retained wheel bytes. Preserves retained
                    bytes and never rebuilds, refreshes, or reads the network.
  --dry-run-linux   Structural Linux-only dry-run. Emits no ready payload,
                    manifest, ledger, proof, or clean-source claim.
  -h, --help        Show this help.

Required environment:
  --candidate:
    EXPECTED_RELEASE_COMMIT       expected lowercase source commit
    RELEASE_MODEL_PACKAGES        include or exclude
    RELEASE_ADVISORY_SOURCE_NAME  public advisory source id
    RELEASE_ADVISORY_DB_URL       explicit non-GitHub advisory DB source
    RELEASE_ADVISORY_DB_ROOT      cargo-deny advisory db parent; see
                                  scripts/release_advisory_policy.py
    RELEASE_BUILD_HOST_CHANNEL    external build-host adapter command
    RELEASE_PROOF_HOST_LINUX_X86_64_MUSL_CHANNEL
                                  external proof-host adapter command
    RELEASE_PROOF_HOST_LINUX_AARCH64_MUSL_CHANNEL
                                  external proof-host adapter command
    RELEASE_PROOF_HOST_MACOS_ARM64_CHANNEL
                                  external proof-host adapter command

  --recover:
    VERSION and SOURCE_COMMIT are required positional selectors. Recovery does
    not read current source metadata to decide what retained bytes to validate.

  --dry-run-linux:
    RELEASE_MODEL_PACKAGES        include or exclude

Publication entry points:
  Running with no args, --test, make release, or make release-test fails closed
  before any external seam. Publish a retained candidate with:
    make publish-release RELEASE_DIR=dist/release-candidate/<version>/
  or rehearse against TestPyPI only with:
    make publish-release-test RELEASE_DIR=dist/release-candidate/<version>/
EOF
}

fail_closed_publish() {
    echo "$PUBLISH_LOCKOUT_MESSAGE" >&2
    exit 1
}

MODE=""
RECOVER_VERSION=""
RECOVER_SOURCE_COMMIT=""

if [[ $# -eq 0 ]]; then
    fail_closed_publish
fi

set_mode() {
    local next_mode="$1"
    if [[ -n "$MODE" ]]; then
        echo "only one release mode may be selected" >&2
        usage >&2
        exit 2
    fi
    MODE="$next_mode"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --candidate)
            set_mode "candidate"
            shift
            ;;
        --recover)
            set_mode "recover"
            if [[ $# -lt 3 ]]; then
                echo "--recover requires VERSION and SOURCE_COMMIT" >&2
                usage >&2
                exit 2
            fi
            RECOVER_VERSION="$2"
            RECOVER_SOURCE_COMMIT="$3"
            shift 3
            ;;
        --dry-run-linux)
            set_mode "dry-run-linux"
            shift
            ;;
        --test)
            fail_closed_publish
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "unknown argument: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

if [[ -z "$MODE" ]]; then
    fail_closed_publish
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

if [[ "$MODE" == "recover" ]]; then
    exec python3 scripts/release_candidate_driver.py recover \
        --version "$RECOVER_VERSION" \
        --source-commit "$RECOVER_SOURCE_COMMIT"
fi

exec python3 scripts/release_candidate_driver.py "$MODE"

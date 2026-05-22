#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc
#
# Multi-wheel solstone release.
#
# Builds and uploads three artifacts to PyPI:
#   - solstone-${VERSION}.tar.gz                                (sdist, Linux)
#   - solstone-${VERSION}-py3-none-any.whl                      (Linux + Intel Mac + macOS<14)
#   - solstone-${VERSION}-py3-none-macosx_14_0_arm64.whl        (Apple Silicon macOS 14+)
#
# The Linux artifacts are built locally. The macOS arm64 wheel is built on
# pro5e.local with a Developer-ID-signed + notarized parakeet-helper bundled
# at solstone/observe/transcribe/parakeet_helper/_bin/parakeet-helper.
#
# Preconditions for the pro5e leg:
#   - pro5e.local SSH-reachable
#   - sol-signing.keychain-db unlocked (run `make unlock-signing` from
#     ~/projects/solstone-macos on pro5e once per launchd session — all build
#     windows in the hopper tmux session share keychain state)
#   - notarytool keychain-profile available; defaults to `sol-pbc-notary` per
#     cto/playbooks/apple-remote-dev.md § sol-signing keychain. Override with
#     NOTARY_KEYCHAIN_PROFILE if needed.
#
# Tokens (set in the env before running):
#   PYPI_TOKEN      __token__ password for production PyPI
#   TESTPYPI_TOKEN  same shape, for --test runs

set -euo pipefail

usage() {
    cat <<'EOF'
Usage: scripts/release.sh [--test]

Options:
  --test                       Publish to TestPyPI.
  -h, --help                   Show this help.

Env overrides:
  NOTARY_KEYCHAIN_PROFILE      notarytool keychain profile on pro5e
                               (default: sol-pbc-notary)
  PRO5E_HOST                   SSH alias for the macOS build host
                               (default: pro5e.local)
EOF
}

TARGET="pypi"
TOKEN_VAR="PYPI_TOKEN"
REPOSITORY_ARGS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --test)
            TARGET="testpypi"
            TOKEN_VAR="TESTPYPI_TOKEN"
            REPOSITORY_ARGS=(--repository-url https://test.pypi.org/legacy/)
            shift
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

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

if [[ -z "${!TOKEN_VAR:-}" ]]; then
    echo "set \$${TOKEN_VAR} before re-running" >&2
    exit 1
fi

TOKEN="${!TOKEN_VAR}"
PRO5E_HOST="${PRO5E_HOST:-pro5e.local}"
NOTARY_PROFILE="${NOTARY_KEYCHAIN_PROFILE:-sol-pbc-notary}"

# Capture the git ref we're publishing from. pro5e checks out the same ref
# so the macOS wheel's source matches the local sdist.
if ! git diff --quiet HEAD; then
    echo "working tree dirty; commit before releasing" >&2
    exit 1
fi
GIT_REF=$(git rev-parse HEAD)

# 1. Linux artifacts: sdist + py3-none-any.whl
echo "==> [1/5] building Linux artifacts (sdist + py3-none-any.whl)"
rm -rf dist/
uv build

# Pre-flight the CHANGELOG block now — before the expensive pro5e leg and the
# irreversible PyPI upload. extract_changelog.sh exits non-zero if the
# `## [VERSION]` block is missing, so a forgotten changelog fails fast instead
# of after publish.
VERSION=$(ls dist/solstone-*-py3-none-any.whl | head -1 | sed -E 's/.*solstone-([^-]+)-.*/\1/')
bash scripts/extract_changelog.sh "$VERSION" >/dev/null

# 2. macOS arm64 wheel: build helper + sign + notarize + bundle on pro5e
echo "==> [2/5] pro5e: building macosx_14_0_arm64 wheel from $GIT_REF"
if ! ssh -o ConnectTimeout=5 "$PRO5E_HOST" true 2>/dev/null; then
    echo "error: $PRO5E_HOST not reachable; skip with --no-macos to publish only Linux artifacts (not implemented)" >&2
    exit 1
fi

# tmux-run is required: codesign + notarytool need the sol-signing keychain
# unlocked, and that unlock state lives in the hopper tmux session's launchd
# session — fresh raw SSH connections don't inherit it. ensure-build-windows
# is idempotent and re-applies the unlock.
ssh "$PRO5E_HOST" "ensure-build-windows >/dev/null"
ssh "$PRO5E_HOST" "tmux-run hopper ~/projects/solstone 'set -e; \
    git fetch origin && \
    git checkout $GIT_REF && \
    rm -rf dist/ solstone/observe/transcribe/parakeet_helper/_bin && \
    NOTARY_KEYCHAIN_PROFILE=$NOTARY_PROFILE make wheel-macos'"

# 3. Pull the macOS wheel back into local dist/
echo "==> [3/5] rsyncing macOS wheel back"
rsync -av --include='*macosx_14_0_arm64.whl' --exclude='*' \
    "$PRO5E_HOST:projects/solstone/dist/" ./dist/

echo
echo "release artifacts:"
ls -la dist/

# 4. twine check + upload
echo
echo "==> [4/5] twine check + upload to $TARGET"
uvx twine check dist/*
TWINE_USERNAME=__token__ TWINE_PASSWORD="$TOKEN" \
    uvx twine upload "${REPOSITORY_ARGS[@]}" dist/*

echo
echo "published solstone ${VERSION} to ${TARGET}:"
echo "  sdist: dist/solstone-${VERSION}.tar.gz"
echo "  any:   dist/solstone-${VERSION}-py3-none-any.whl"
echo "  macos: dist/solstone-${VERSION}-py3-none-macosx_14_0_arm64.whl"

# 5. tag the commit + cut a GitHub Release. Production only — a TestPyPI dry-run
#    should not leave a git tag or a public release behind. Mirrors the
#    solstone-linux release.sh tail so all product repos share one shape, with
#    release notes pulled from the shared scripts/extract_changelog.sh.
if [[ "$TARGET" != "pypi" ]]; then
    echo
    echo "skipping git tag + GitHub release (TestPyPI run)"
    exit 0
fi

TAG="v${VERSION}"
echo
echo "==> [5/5] tagging ${TAG} + creating GitHub release"
git tag -a "$TAG" -m "solstone ${VERSION}"
if ! git push origin "$TAG"; then
    echo "error: git push origin ${TAG} failed; the tag was created locally but not pushed." >&2
    echo "       PyPI is published and immutable. Resolve the push and create the release manually:" >&2
    echo "       gh release create ${TAG} dist/solstone-${VERSION}.tar.gz dist/solstone-${VERSION}-*.whl --title 'solstone ${VERSION}' --notes-file <(scripts/extract_changelog.sh ${VERSION})" >&2
    exit 1
fi

NOTES_FILE=$(mktemp)
trap 'rm -f "$NOTES_FILE"' EXIT
scripts/extract_changelog.sh "$VERSION" > "$NOTES_FILE"

if ! gh release create "$TAG" \
    "dist/solstone-${VERSION}.tar.gz" \
    dist/solstone-${VERSION}-*.whl \
    --title "solstone ${VERSION}" \
    --notes-file "$NOTES_FILE"; then
    echo "error: gh release create failed." >&2
    echo "       PyPI is published and immutable; the git tag ${TAG} is pushed." >&2
    echo "       Re-run manually:" >&2
    echo "       gh release create ${TAG} dist/solstone-${VERSION}.tar.gz dist/solstone-${VERSION}-*.whl --title 'solstone ${VERSION}' --notes-file <(scripts/extract_changelog.sh ${VERSION})" >&2
    exit 1
fi

echo
echo "✓ tagged ${TAG} and created GitHub release with sdist + wheels attached"

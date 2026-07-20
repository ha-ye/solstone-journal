#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc
#
# Multi-wheel solstone release.
#
# Builds and uploads the eleven lockstep artifacts to PyPI:
#   - solstone-${VERSION}.tar.gz                                (root sdist)
#   - solstone-${VERSION}-py3-none-any.whl                      (root any wheel)
#   - solstone-${VERSION}-py3-none-macosx_14_0_arm64.whl        (Apple Silicon macOS 14+)
#   - solstone_core-${VERSION}.tar.gz                           (core helper sdist)
#   - solstone_core-${VERSION}-py3-none-manylinux_2_17_x86_64.manylinux2014_x86_64.whl
#   - solstone_core-${VERSION}-py3-none-manylinux_2_17_aarch64.manylinux2014_aarch64.whl
#   - solstone_core-${VERSION}-py3-none-macosx_14_0_arm64.whl
#   - solstone_journal-${VERSION}.tar.gz                        (journal CPU sdist)
#   - solstone_journal-${VERSION}-py3-none-any.whl              (journal CPU wheel)
#   - solstone_journal_cuda-${VERSION}.tar.gz                   (journal CUDA sdist)
#   - solstone_journal_cuda-${VERSION}-py3-none-any.whl         (journal CUDA wheel)
# The independently-versioned solstone_journal_models sdist + wheel are included
# only when their version is absent from the target package index.
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
Usage: scripts/release.sh [--test] [--dry-run-linux|--dry-run-all-hosts]

Modes:
  default                      Build all-host artifacts, publish to PyPI, tag,
                               and create a GitHub release.
  --test                       Build all-host artifacts and publish to TestPyPI;
                               skip git tag and GitHub release.
  --dry-run-linux              Build and check local Linux artifacts only; skip
                               macOS, upload, git tag, and GitHub release. No
                               token required.
  --dry-run-all-hosts          Build and check local Linux plus macOS artifacts,
                               run twine check, then stop before upload, git tag,
                               and GitHub release. No token required.
  -h, --help                   Show this help.

Env overrides:
  NOTARY_KEYCHAIN_PROFILE      notarytool keychain profile on the macOS build host
                               (default: sol-pbc-notary)
  PRO5E_HOST                   SSH alias for the macOS build host
                               (default: pro5e.local)
EOF
}

TARGET="pypi"
TOKEN_VAR="PYPI_TOKEN"
REPOSITORY_ARGS=()
MODE="publish"
TMP_FILES=()

cleanup_tmp_files() {
    ((${#TMP_FILES[@]} == 0)) || rm -f "${TMP_FILES[@]}"
}
trap cleanup_tmp_files EXIT

set_mode() {
    local next_mode="$1"
    if [[ "$MODE" != "publish" ]]; then
        echo "only one release mode may be selected" >&2
        usage >&2
        exit 2
    fi
    MODE="$next_mode"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --test)
            set_mode "test"
            TARGET="testpypi"
            TOKEN_VAR="TESTPYPI_TOKEN"
            REPOSITORY_ARGS=(--repository-url https://test.pypi.org/legacy/)
            shift
            ;;
        --dry-run-linux)
            set_mode "dry-run-linux"
            shift
            ;;
        --dry-run-all-hosts)
            set_mode "dry-run-all-hosts"
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

LOCAL_PREFLIGHT_ARGS=(local --root .)
if [[ "$MODE" != "dry-run-linux" ]]; then
    LOCAL_PREFLIGHT_ARGS+=(--require-clean)
fi
python3 scripts/check_release_preflight.py "${LOCAL_PREFLIGHT_ARGS[@]}"

if [[ "$MODE" == "publish" || "$MODE" == "test" ]]; then
    if [[ -z "${!TOKEN_VAR:-}" ]]; then
        echo "set \$${TOKEN_VAR} before re-running" >&2
        exit 1
    fi
    TOKEN="${!TOKEN_VAR}"
fi
PRO5E_HOST="${PRO5E_HOST:-pro5e.local}"
NOTARY_PROFILE="${NOTARY_KEYCHAIN_PROFILE:-sol-pbc-notary}"
CORE_X86_64_MATURIN_ARGS="--locked --zig --compatibility manylinux2014 --target x86_64-unknown-linux-musl"
CORE_AARCH64_MATURIN_ARGS="--locked --zig --compatibility manylinux2014 --target aarch64-unknown-linux-musl"

# Capture the git ref we're publishing from. pro5e checks out the same ref
# so the macOS wheel's source matches the local sdist. Reject ANY tracked or
# untracked (non-ignored) change — untracked files would otherwise be built
# locally but absent from the ref pro5e checks out.
if [[ "$MODE" != "dry-run-linux" ]]; then
    GIT_REF=$(git rev-parse HEAD)
fi

echo "==> running Rust advisory audit"
make audit
echo

# 1. Local lockstep artifacts: root + journal leaves + models
echo "==> [1/5] building local lockstep artifacts"
python3 scripts/render_packaging.py --check
rm -rf build/ dist/ *.egg-info/
MATURIN_PEP517_ARGS="$CORE_X86_64_MATURIN_ARGS" uv build --all-packages
MATURIN_PEP517_ARGS="$CORE_AARCH64_MATURIN_ARGS" uv build --package solstone-core --wheel
python3 scripts/check_wheel_contents.py dist/

# Pre-flight the CHANGELOG block now — before the expensive pro5e leg and the
# irreversible PyPI upload. extract_changelog.sh exits non-zero if the
# `## [VERSION]` block is missing, so a forgotten changelog fails fast instead
# of after publish.
VERSION=$(ls dist/solstone-[0-9]*-py3-none-any.whl | head -1 | sed -E 's/.*solstone-([^-]+)-.*/\1/')
bash scripts/extract_changelog.sh "$VERSION" >/dev/null
MODELS_VERSION=$(ls dist/solstone_journal_models-[0-9]*-py3-none-any.whl | head -1 | sed -E 's/.*solstone_journal_models-([^-]+)-.*/\1/')
TEST_FLAG=""
[[ "$TARGET" == "testpypi" ]] && TEST_FLAG="--test"
MODELS_DECISION="$(python3 scripts/release_models_gate.py --version "$MODELS_VERSION" $TEST_FLAG)"
echo "models gate: solstone-journal-models ${MODELS_VERSION} -> ${MODELS_DECISION}"

RELEASE_SCOPE="linux"

if [[ "$MODE" == "dry-run-linux" ]]; then
    ARTIFACT_LIST=$(mktemp)
    TMP_FILES+=("$ARTIFACT_LIST")
    python3 scripts/check_wheel_contents.py \
        --release-scope "$RELEASE_SCOPE" \
        --models-decision "$MODELS_DECISION" \
        --print-artifacts \
        dist/ > "$ARTIFACT_LIST"
    mapfile -t ARTIFACTS < "$ARTIFACT_LIST"

    echo
    echo "release artifacts:"
    ls -la dist/

    echo
    echo "==> twine check (--dry-run-linux)"
    uvx twine check "${ARTIFACTS[@]}"

    echo
    echo "build/check complete (--dry-run-linux); skipped macOS, upload, git tag, git push, and GitHub release"
    echo "skipped macOS artifacts (--dry-run-linux)"
    printf '  %s\n' "${ARTIFACTS[@]}"
    echo "  models gate: ${MODELS_DECISION}"
    exit 0
fi

LOCAL_STATUS_FILE=$(mktemp)
TMP_FILES+=("$LOCAL_STATUS_FILE")
LOCAL_REF=$(git rev-parse HEAD)
git status --porcelain --untracked-files=normal > "$LOCAL_STATUS_FILE"
python3 scripts/check_release_preflight.py remote-state \
    --label "local release tree" \
    --expected-ref "$GIT_REF" \
    --actual-ref "$LOCAL_REF" \
    --status-file "$LOCAL_STATUS_FILE"

# 2. macOS arm64 wheel: build helper + sign + notarize + bundle on pro5e
echo "==> [2/5] pro5e: building macosx_14_0_arm64 wheel from $GIT_REF"
if ! ssh -o ConnectTimeout=5 "$PRO5E_HOST" true 2>/dev/null; then
    echo "error: $PRO5E_HOST not reachable; use --dry-run-linux to build only Linux artifacts" >&2
    exit 1
fi

REMOTE_STATUS_FILE=$(mktemp)
TMP_FILES+=("$REMOTE_STATUS_FILE")
REMOTE_REF=$(ssh "$PRO5E_HOST" "cd ~/projects/solstone && git fetch origin >/dev/null && git checkout $GIT_REF >/dev/null && git rev-parse HEAD")
ssh "$PRO5E_HOST" "cd ~/projects/solstone && git status --porcelain --untracked-files=normal" > "$REMOTE_STATUS_FILE"
python3 scripts/check_release_preflight.py remote-state \
    --label "$PRO5E_HOST" \
    --expected-ref "$GIT_REF" \
    --actual-ref "$REMOTE_REF" \
    --status-file "$REMOTE_STATUS_FILE"

# tmux-run is required: codesign + notarytool need the sol-signing keychain
# unlocked, and that unlock state lives in the hopper tmux session's launchd
# session — fresh raw SSH connections don't inherit it. ensure-build-windows
# is idempotent and re-applies the unlock.
ssh "$PRO5E_HOST" "ensure-build-windows >/dev/null"
ssh "$PRO5E_HOST" "tmux-run hopper ~/projects/solstone 'set -e; \
    python3 scripts/check_release_preflight.py local --root . && \
    rm -rf build/ dist/ *.egg-info/ solstone/observe/transcribe/parakeet_helper/_bin && \
    NOTARY_KEYCHAIN_PROFILE=$NOTARY_PROFILE make wheel-macos'"

# 3. Pull the macOS wheel back into local dist/
echo "==> [3/5] rsyncing macOS wheel back"
rsync -av --include='*macosx_14_0_arm64.whl' --exclude='*' \
    "$PRO5E_HOST:projects/solstone/dist/" ./dist/
RELEASE_SCOPE="all-hosts"

echo
echo "release artifacts:"
ls -la dist/

ARTIFACT_LIST=$(mktemp)
TMP_FILES+=("$ARTIFACT_LIST")
python3 scripts/check_wheel_contents.py \
    --release-scope "$RELEASE_SCOPE" \
    --models-decision "$MODELS_DECISION" \
    --print-artifacts \
    dist/ > "$ARTIFACT_LIST"
mapfile -t ARTIFACTS < "$ARTIFACT_LIST"
GH_RELEASE_ARTIFACT_HINT="${ARTIFACTS[*]}"

if [[ "$MODE" == "dry-run-all-hosts" ]]; then
    echo
    echo "==> twine check (--dry-run-all-hosts)"
    uvx twine check "${ARTIFACTS[@]}"

    echo
    echo "build/check complete (--dry-run-all-hosts); skipped upload, git tag, git push, and GitHub release"
    printf '  %s\n' "${ARTIFACTS[@]}"
    echo "  models gate: ${MODELS_DECISION}"
    exit 0
fi

# 4. twine check + upload
echo
echo "==> [4/5] twine check + upload to $TARGET"
uvx twine check "${ARTIFACTS[@]}"
TWINE_USERNAME=__token__ TWINE_PASSWORD="$TOKEN" \
    uvx twine upload "${REPOSITORY_ARGS[@]}" "${ARTIFACTS[@]}"

echo
echo "published solstone ${VERSION} to ${TARGET}:"
echo "  root sdist: dist/solstone-${VERSION}.tar.gz"
echo "  root any:   dist/solstone-${VERSION}-py3-none-any.whl"
echo "  core sdist: dist/solstone_core-${VERSION}.tar.gz"
echo "  core wheels: dist/solstone_core-${VERSION}-*.whl"
echo "  journal sdist: dist/solstone_journal-${VERSION}.tar.gz"
echo "  journal any:   dist/solstone_journal-${VERSION}-py3-none-any.whl"
echo "  cuda sdist:    dist/solstone_journal_cuda-${VERSION}.tar.gz"
echo "  cuda any:      dist/solstone_journal_cuda-${VERSION}-py3-none-any.whl"
echo "  macos:         dist/solstone-${VERSION}-py3-none-macosx_14_0_arm64.whl"
if [[ "$MODELS_DECISION" == "publish" ]]; then
    echo "  models sdist:  dist/solstone_journal_models-${MODELS_VERSION}.tar.gz"
    echo "  models any:    dist/solstone_journal_models-${MODELS_VERSION}-py3-none-any.whl"
else
    echo "  models:        skipped; solstone-journal-models ${MODELS_VERSION} already exists on ${TARGET}"
fi

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
    echo "       gh release create ${TAG} ${GH_RELEASE_ARTIFACT_HINT} --title 'solstone ${VERSION}' --notes-file <(scripts/extract_changelog.sh ${VERSION})" >&2
    exit 1
fi

NOTES_FILE=$(mktemp)
TMP_FILES+=("$NOTES_FILE")
scripts/extract_changelog.sh "$VERSION" > "$NOTES_FILE"

if ! gh release create "$TAG" \
    "${ARTIFACTS[@]}" \
    --title "solstone ${VERSION}" \
    --notes-file "$NOTES_FILE"; then
    echo "error: gh release create failed." >&2
    echo "       PyPI is published and immutable; the git tag ${TAG} is pushed." >&2
    echo "       Re-run manually:" >&2
    echo "       gh release create ${TAG} ${GH_RELEASE_ARTIFACT_HINT} --title 'solstone ${VERSION}' --notes-file <(scripts/extract_changelog.sh ${VERSION})" >&2
    exit 1
fi

echo
echo "✓ tagged ${TAG} and created GitHub release with sdist + wheels attached"

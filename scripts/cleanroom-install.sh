#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc
set -euo pipefail

usage() {
    cat <<'EOF'
Usage: scripts/cleanroom-install.sh [--source wheel|testpypi|pypi] [--image python:3.12-slim|python:3.12] [--version X.Y.Z] [--host] [--cuda]
  --source    wheel, testpypi, or pypi. Default: wheel.
  --image     One image only. Default: python:3.12-slim then python:3.12.
  --version   Default: dist wheel version for wheel, else pyproject.toml version.
  --host      Also run the host-surface matrix for solstone[journal].
  --cuda      With --host, also run solstone[journal-cuda] variants.
  -h, --help  Show help.
EOF
}

SOURCE="wheel"
IMAGE=""
VERSION=""
RUN_HOST="no"
RUN_CUDA="no"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --source|--image|--version)
            [[ $# -ge 2 ]] || { echo "$1 requires a value" >&2; exit 2; }
            case "$1" in
                --source) SOURCE="$2" ;;
                --image) IMAGE="$2" ;;
                --version) VERSION="$2" ;;
            esac
            shift 2
            ;;
        --host)
            RUN_HOST="yes"
            shift
            ;;
        --cuda)
            RUN_CUDA="yes"
            shift
            ;;
        -h|--help) usage; exit 0 ;;
        *)
            echo "unknown argument: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

case "$SOURCE" in wheel|testpypi|pypi) ;; *) echo "invalid --source: $SOURCE" >&2; exit 2 ;; esac
case "$IMAGE" in ""|python:3.12-slim|python:3.12) ;; *) echo "invalid --image: $IMAGE" >&2; exit 2 ;; esac
if [[ "$RUN_CUDA" == "yes" && "$RUN_HOST" != "yes" ]]; then
    echo "--cuda requires --host" >&2
    exit 2
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

if [[ -z "$VERSION" && "$SOURCE" == "wheel" ]]; then
    VERSION=$(ls dist/solstone-[0-9]*-py3-none-any.whl 2>/dev/null | head -1 | sed -E 's/.*solstone-([^-]+)-.*/\1/')
    if [[ -z "$VERSION" ]]; then
        echo "no root wheel found in dist/, run uv build --all-packages first" >&2
        exit 1
    fi
elif [[ -z "$VERSION" ]]; then
    VERSION="$(grep -E '^version[[:space:]]*=' "$REPO_ROOT/pyproject.toml" | head -1 | sed -E 's/.*"(.+)".*/\1/')"
fi

BASE_SPEC=""
JOURNAL_SPEC=""
CUDA_SPEC=""
PIP_INDEX_ARGS=""
UV_INDEX_ARGS=""
FIND_LINKS_ARGS=""
PIPX_PIP_ARGS=""
mount_args=()

case "$SOURCE" in
    wheel)
        ROOT_WHEEL="dist/solstone-${VERSION}-py3-none-any.whl"
        if [[ ! -f "$ROOT_WHEEL" ]]; then
            echo "missing root wheel: $ROOT_WHEEL" >&2
            exit 1
        fi
        if [[ "$RUN_HOST" == "yes" ]] && ! compgen -G "dist/solstone_journal_host-${VERSION}-*" >/dev/null; then
            echo "missing solstone-journal-host dist in dist/, run uv build --all-packages first" >&2
            exit 1
        fi
        BASE_SPEC="/work/dist/solstone-${VERSION}-py3-none-any.whl"
        JOURNAL_SPEC="/work/dist/solstone-${VERSION}-py3-none-any.whl[journal]"
        CUDA_SPEC="/work/dist/solstone-${VERSION}-py3-none-any.whl[journal-cuda]"
        FIND_LINKS_ARGS="--find-links /work/dist"
        PIPX_PIP_ARGS="--find-links /work/dist"
        mount_args=(-v "$REPO_ROOT/dist:/work/dist:ro")
        ;;
    testpypi)
        BASE_SPEC="solstone==${VERSION}"
        JOURNAL_SPEC="solstone[journal]==${VERSION}"
        CUDA_SPEC="solstone[journal-cuda]==${VERSION}"
        PIP_INDEX_ARGS="--index-url https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple/"
        UV_INDEX_ARGS="--index-url https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple/"
        PIPX_PIP_ARGS="$PIP_INDEX_ARGS"
        ;;
    pypi)
        BASE_SPEC="solstone==${VERSION}"
        JOURNAL_SPEC="solstone[journal]==${VERSION}"
        CUDA_SPEC="solstone[journal-cuda]==${VERSION}"
        ;;
esac

summaries=()
full_status=0

CONTAINER_SCRIPT=$(cat <<'EOF'
set -euo pipefail

if command -v apt-get >/dev/null 2>&1; then
    apt-get update -qq >/dev/null
    apt-get install -y --no-install-recommends ca-certificates >/dev/null
fi

python -m ensurepip --upgrade >/dev/null 2>&1 || true
python -m pip install -q --upgrade pip
python -m pip install -q uv pipx

fail() {
    echo "FAIL: $*" >&2
    exit 1
}

assert_present() {
    local bin_dir="$1"
    shift
    local name
    for name in "$@"; do
        if [[ ! -x "${bin_dir}/${name}" ]]; then
            fail "expected script '${name}' in ${bin_dir}"
        fi
        echo "present: ${bin_dir}/${name}"
    done
}

assert_absent() {
    local bin_dir="$1"
    shift
    local name
    for name in "$@"; do
        if [[ -e "${bin_dir}/${name}" ]]; then
            fail "denied script '${name}' present in ${bin_dir}"
        fi
        echo "absent: ${bin_dir}/${name}"
    done
}

run_pip_bare() {
    echo "== bare/pip =="
    python -m venv /tmp/solstone-clean-pip-bare
    /tmp/solstone-clean-pip-bare/bin/python -m pip install -q --upgrade pip
    /tmp/solstone-clean-pip-bare/bin/python -m pip install -q ${PIP_INDEX_ARGS} ${FIND_LINKS_ARGS} "$BASE_SPEC"
    assert_present /tmp/solstone-clean-pip-bare/bin sol solstone
    assert_absent /tmp/solstone-clean-pip-bare/bin journal mlx-vlm-server
}

run_uv_tool_bare() {
    echo "== bare/uv-tool =="
    rm -rf /tmp/uv-data-bare /tmp/uv-bin-bare
    XDG_DATA_HOME=/tmp/uv-data-bare XDG_BIN_HOME=/tmp/uv-bin-bare \
        uv tool install -q ${UV_INDEX_ARGS} "$BASE_SPEC"
    assert_present /tmp/uv-bin-bare sol solstone
    assert_absent /tmp/uv-bin-bare journal mlx-vlm-server
}

run_uvx_thin() {
    echo "== bare/uvx =="
    XDG_CACHE_HOME=/tmp/uv-cache-uvx uvx -q ${UV_INDEX_ARGS} --from "$BASE_SPEC" solstone --version
    if XDG_CACHE_HOME=/tmp/uv-cache-uvx uvx -q ${UV_INDEX_ARGS} --from "$BASE_SPEC" journal --version >/tmp/uvx-journal.out 2>&1; then
        fail "denied uvx command 'journal' unexpectedly ran"
    fi
    echo "absent: uvx journal"
}

run_pip_host() {
    local extra="$1"
    local spec="$2"
    local venv="/tmp/solstone-clean-pip-${extra}"
    echo "== host/pip ${extra} =="
    python -m venv "$venv"
    "$venv/bin/python" -m pip install -q --upgrade pip
    "$venv/bin/python" -m pip install -q ${PIP_INDEX_ARGS} ${FIND_LINKS_ARGS} "$spec"
    assert_present "$venv/bin" sol solstone journal mlx-vlm-server
}

run_uv_tool_host() {
    local extra="$1"
    local spec="$2"
    local data="/tmp/uv-data-${extra}"
    local bin="/tmp/uv-bin-${extra}"
    echo "== host/uv-tool ${extra} =="
    rm -rf "$data" "$bin"
    XDG_DATA_HOME="$data" XDG_BIN_HOME="$bin" \
        uv tool install -q ${UV_INDEX_ARGS} ${FIND_LINKS_ARGS} \
            --with-executables-from solstone-journal-host "$spec"
    assert_present "$bin" sol solstone journal mlx-vlm-server
}

run_pipx_host() {
    local extra="$1"
    local spec="$2"
    local home="/tmp/pipx-home-${extra}"
    local bin="/tmp/pipx-bin-${extra}"
    local man="/tmp/pipx-man-${extra}"
    echo "== host/pipx ${extra} =="
    rm -rf "$home" "$bin" "$man"
    mkdir -p "$bin"
    if [[ -n "${PIPX_PIP_ARGS:-}" ]]; then
        PIPX_HOME="$home" PIPX_BIN_DIR="$bin" PIPX_MAN_DIR="$man" \
            pipx install --quiet --include-deps --pip-args "$PIPX_PIP_ARGS" "$spec"
    else
        PIPX_HOME="$home" PIPX_BIN_DIR="$bin" PIPX_MAN_DIR="$man" \
            pipx install --quiet --include-deps "$spec"
    fi
    assert_present "$bin" sol solstone journal mlx-vlm-server
}

run_host_extra() {
    local extra="$1"
    local spec="$2"
    run_pip_host "$extra" "$spec"
    run_uv_tool_host "$extra" "$spec"
    run_pipx_host "$extra" "$spec"
}

run_pip_bare
run_uv_tool_bare
run_uvx_thin

if [[ "$RUN_HOST" == "yes" ]]; then
    run_host_extra journal "$JOURNAL_SPEC"
fi

if [[ "$RUN_CUDA" == "yes" ]]; then
    run_host_extra journal-cuda "$CUDA_SPEC"
fi
EOF
)

run_image() {
    local image="$1"
    local propagate_failure="$2"
    local label="full"
    local output status

    [[ "$image" == "python:3.12-slim" ]] && label="slim"

    set +e
    output=$(docker run --rm \
        "${mount_args[@]}" \
        -e BASE_SPEC="$BASE_SPEC" \
        -e JOURNAL_SPEC="$JOURNAL_SPEC" \
        -e CUDA_SPEC="$CUDA_SPEC" \
        -e PIP_INDEX_ARGS="$PIP_INDEX_ARGS" \
        -e UV_INDEX_ARGS="$UV_INDEX_ARGS" \
        -e FIND_LINKS_ARGS="$FIND_LINKS_ARGS" \
        -e PIPX_PIP_ARGS="$PIPX_PIP_ARGS" \
        -e RUN_HOST="$RUN_HOST" \
        -e RUN_CUDA="$RUN_CUDA" \
        "$image" bash -c "$CONTAINER_SCRIPT" 2>&1)
    status=$?
    set -e

    printf '%s\n' "$output"
    if [[ "$status" -eq 0 ]]; then
        summaries+=("${label}: PASS - cleanroom ${SOURCE} ${VERSION}")
    elif [[ "$label" == "slim" ]]; then
        summaries+=("slim: FAIL (documentation only)")
    else
        summaries+=("full: FAIL")
    fi

    if [[ "$propagate_failure" == "yes" && "$status" -ne 0 ]]; then
        full_status="$status"
    fi
    return 0
}

if [[ -n "$IMAGE" ]]; then
    if [[ "$IMAGE" == "python:3.12" ]]; then
        run_image "$IMAGE" yes
    else
        run_image "$IMAGE" no
    fi
else
    run_image "python:3.12-slim" no
    run_image "python:3.12" yes
fi

printf '%s\n' "${summaries[@]}"
exit "$full_status"

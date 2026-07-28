#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc
#
# Sign and notarize a bare Mach-O native binary for the macOS platform wheel.
# Stapling is intentionally skipped — bare Mach-O binaries are not stapleable;
# Gatekeeper performs an online check on first run instead.

set -euo pipefail

if [ "$#" -ne 1 ]; then
    echo "usage: $0 <binary-path>" >&2
    exit 2
fi

BINARY="$1"
PROFILE="${NOTARY_KEYCHAIN_PROFILE:-sol-pbc-notary}"
NOTARY_KEYCHAIN="${NOTARY_KEYCHAIN:-$HOME/Library/Keychains/sol-signing.keychain-db}"

eval "$(python3 - <<'PY'
import shlex

from scripts.release_tool_pins import (
    MACOS_CODESIGN_PATH,
    MACOS_CODESIGN_PUBLIC_PIN,
    MACOS_NOTARYTOOL_PIN,
    MACOS_SIGNER_IDENTITY,
    MACOS_TEAM_IDENTIFIER,
    MACOS_XCODE_BUILD,
    MACOS_XCODE_PIN,
    MACOS_XCODE_VERSION,
)

values = {
    "CODESIGN_BIN": MACOS_CODESIGN_PATH,
    "CODESIGN_PUBLIC_PIN": MACOS_CODESIGN_PUBLIC_PIN,
    "IDENTITY": MACOS_SIGNER_IDENTITY,
    "NOTARYTOOL_PIN": MACOS_NOTARYTOOL_PIN,
    "TEAM_ID": MACOS_TEAM_IDENTIFIER,
    "XCODE_BUILD": MACOS_XCODE_BUILD,
    "XCODE_PIN": MACOS_XCODE_PIN,
    "XCODE_VERSION": MACOS_XCODE_VERSION,
}
for key, value in values.items():
    print(f"{key}={shlex.quote(value)}")
PY
)"

if [ ! -f "$BINARY" ]; then
    echo "error: not a regular file: $BINARY" >&2
    exit 1
fi

if [ ! -x "$CODESIGN_BIN" ]; then
    echo "error: pinned codesign path is not executable" >&2
    exit 1
fi

XCODE_OUTPUT="$(xcodebuild -version 2>&1)" || {
    echo "error: xcodebuild version check failed" >&2
    exit 1
}
printf '%s\n' "$XCODE_OUTPUT" | grep -qx "Xcode $XCODE_VERSION" || {
    echo "error: Xcode version does not match release policy" >&2
    exit 1
}
printf '%s\n' "$XCODE_OUTPUT" | grep -qx "Build version $XCODE_BUILD" || {
    echo "error: Xcode build does not match release policy" >&2
    exit 1
}

SWIFT_OUTPUT="$(swift --version 2>&1)" || {
    echo "error: swift version check failed" >&2
    exit 1
}
SWIFT_FIRST_LINE="$(printf '%s\n' "$SWIFT_OUTPUT" | sed -n '1p')"
SWIFT_ACTUAL="$SWIFT_FIRST_LINE" python3 - <<'PY'
import os
import sys

from scripts.release_tool_pins import MACOS_SWIFT_PIN, check_host_variant_tool_pin

actual = os.environ["SWIFT_ACTUAL"]
if check_host_variant_tool_pin("swift", MACOS_SWIFT_PIN, actual):
    raise SystemExit(0)
print(
    "error: Swift version does not match release policy "
    f"(MACOS_SWIFT_PIN expected {MACOS_SWIFT_PIN!r}; actual {actual!r})",
    file=sys.stderr,
)
raise SystemExit(1)
PY

NOTARYTOOL_OUTPUT="$(xcrun notarytool --version 2>&1)" || {
    echo "error: notarytool version check failed" >&2
    exit 1
}
if [ "$NOTARYTOOL_OUTPUT" != "$NOTARYTOOL_PIN" ]; then
    echo "error: notarytool version does not match release policy" >&2
    exit 1
fi

UNSIGNED_BINARY_SHA256="$(shasum -a 256 "$BINARY" | awk '{print $1}')"

echo "==> codesigning $BINARY with repository-pinned identity" >&2
"$CODESIGN_BIN" --force --options runtime --timestamp \
    --keychain "$NOTARY_KEYCHAIN" \
    --sign "$IDENTITY" \
    "$BINARY" >/dev/null

echo "==> verifying signature" >&2
"$CODESIGN_BIN" --verify --strict --verbose=2 "$BINARY" >/dev/null
DISPLAY_OUTPUT="$("$CODESIGN_BIN" -dv --verbose=4 "$BINARY" 2>&1)"

SIGNER_PINNED=false
TEAM_PINNED=false
HARDENED_RUNTIME=false
TRUSTED_TIMESTAMP=false
case "$DISPLAY_OUTPUT" in
    *"Authority=$IDENTITY"*) SIGNER_PINNED=true ;;
esac
case "$DISPLAY_OUTPUT" in
    *"TeamIdentifier=$TEAM_ID"*) TEAM_PINNED=true ;;
esac
case "$DISPLAY_OUTPUT" in
    *"runtime"*) HARDENED_RUNTIME=true ;;
esac
case "$DISPLAY_OUTPUT" in
    *"Timestamp="*) TRUSTED_TIMESTAMP=true ;;
esac

ZIPDIR="$(mktemp -d)"
trap 'rm -rf "$ZIPDIR"' EXIT
ZIPPATH="$ZIPDIR/$(basename "$BINARY").zip"

echo "==> packaging $BINARY for notarytool" >&2
ditto -c -k --keepParent "$BINARY" "$ZIPPATH"

echo "==> submitting to notarytool" >&2
NOTARY_SUBMIT_OUTPUT="$(xcrun notarytool submit "$ZIPPATH" \
    --keychain-profile "$PROFILE" \
    --keychain "$NOTARY_KEYCHAIN" \
    --wait 2>&1)" || {
    echo "error: notarytool submission failed" >&2
    exit 1
}
NOTARIZATION_STATUS="rejected"
case "$NOTARY_SUBMIT_OUTPUT" in
    *"status: Accepted"*|*"status: accepted"*) NOTARIZATION_STATUS="accepted" ;;
esac

SIGNED_BINARY_SHA256="$(shasum -a 256 "$BINARY" | awk '{print $1}')"

SIGNED_BINARY_SHA256="$SIGNED_BINARY_SHA256" \
SIGNER_PINNED="$SIGNER_PINNED" \
TEAM_PINNED="$TEAM_PINNED" \
HARDENED_RUNTIME="$HARDENED_RUNTIME" \
TRUSTED_TIMESTAMP="$TRUSTED_TIMESTAMP" \
UNSIGNED_BINARY_SHA256="$UNSIGNED_BINARY_SHA256" \
NOTARIZATION_STATUS="$NOTARIZATION_STATUS" \
XCODE_PIN="$XCODE_PIN" \
SWIFT_FIRST_LINE="$SWIFT_FIRST_LINE" \
CODESIGN_PUBLIC_PIN="$CODESIGN_PUBLIC_PIN" \
NOTARYTOOL_OUTPUT="$NOTARYTOOL_OUTPUT" \
python3 - <<'PY'
import json
import os

payload = {
    "signed_binary_sha256": os.environ["SIGNED_BINARY_SHA256"],
    "unsigned_binary_sha256": os.environ["UNSIGNED_BINARY_SHA256"],
    "signer_pinned": os.environ["SIGNER_PINNED"] == "true",
    "team_pinned": os.environ["TEAM_PINNED"] == "true",
    "hardened_runtime": os.environ["HARDENED_RUNTIME"] == "true",
    "trusted_timestamp": os.environ["TRUSTED_TIMESTAMP"] == "true",
    "notarization_status": os.environ["NOTARIZATION_STATUS"],
    "tools": {
        "xcode": os.environ["XCODE_PIN"],
        "swift": os.environ["SWIFT_FIRST_LINE"],
        "codesign": os.environ["CODESIGN_PUBLIC_PIN"],
        "notarytool": os.environ["NOTARYTOOL_OUTPUT"],
    },
}
print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
PY

echo "==> sign-and-notarize complete: $BINARY" >&2
echo "note: bare Mach-O binaries cannot be stapled; Gatekeeper performs an online check on first run." >&2

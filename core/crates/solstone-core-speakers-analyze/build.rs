// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 sol pbc

fn main() {
    // GNU ld defaults to new dtags, so this emits DT_RUNPATH rather than old
    // DT_RPATH. The dynamic loader searches RUNPATH after LD_LIBRARY_PATH,
    // keeping scripts/resolve_onnxruntime_capi.py's dev resolver in control.
    println!(
        "cargo:rustc-link-arg-bin=solstone-core-speakers-analyze=-Wl,-rpath,$ORIGIN/../lib/solstone-core-speakers-analyze"
    );
}

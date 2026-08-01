// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 sol pbc

//! The retained synchronous event seam shared by relay and supervision code.

use serde_json::Value;

/// Emits one owner-visible Callosum event with its JSON-object payload.
///
/// This belongs to the relay/service boundary: relay code supplies health and
/// tunnel events, while the service owns the enclosing lifecycle.
pub trait CallosumEmit: Send + Sync {
    /// Emits one named event with a JSON object payload.
    fn emit(&self, event: &'static str, payload: Value);
}

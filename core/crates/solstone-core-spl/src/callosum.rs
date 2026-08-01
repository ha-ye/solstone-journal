// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 sol pbc

//! The retained synchronous event seam shared by relay and supervision code.

use serde_json::Value;

/// Emits one owner-visible Callosum event with its JSON-object payload.
///
/// This belongs to the retained relay/service boundary rather than blob
/// receive: U4 supplies actual relay-health and tunnel events, while U5 owns
/// the enclosing service lifecycle. The temporarily present blob receiver
/// uses the same generic seam only for its admission notification.
pub trait CallosumEmit: Send + Sync {
    /// Emits one named event with a JSON object payload.
    fn emit(&self, event: &'static str, payload: Value);
}

// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 sol pbc

use std::sync::{Mutex, MutexGuard};

const DEFAULT_GLOBAL_CEILING: usize = 32;

#[derive(Debug, Default)]
struct AdmissionState {
    count: usize,
    saturated_count: u64,
}

/// Synchronous, in-memory admission limit for concurrent relay tunnels.
///
/// Each method holds the mutex only while it updates or reads a counter.
/// Callers acquire before tunnel setup and release before long-lived tunnel
/// forwarding, so the gate never holds a lock across I/O.
#[derive(Debug)]
pub struct RelayAdmissionGate {
    ceiling: usize,
    state: Mutex<AdmissionState>,
}

impl RelayAdmissionGate {
    /// Creates a gate with one global tunnel ceiling.
    pub fn new(ceiling: usize) -> Self {
        Self {
            ceiling,
            state: Mutex::new(AdmissionState::default()),
        }
    }

    /// Acquires one tunnel-setup slot without waiting, or records a refusal.
    pub fn try_acquire(&self) -> bool {
        let mut state = self.lock_state();
        if state.count < self.ceiling {
            state.count += 1;
            true
        } else {
            state.saturated_count = state.saturated_count.saturating_add(1);
            false
        }
    }

    /// Releases one tunnel-setup slot. Releasing an empty gate leaves it empty.
    pub fn release(&self) {
        let mut state = self.lock_state();
        state.count = state.count.saturating_sub(1);
    }

    /// Returns the number of held tunnel-setup slots.
    pub fn count(&self) -> usize {
        self.lock_state().count
    }

    /// Returns the cumulative number of admission refusals.
    pub fn saturated_count(&self) -> u64 {
        self.lock_state().saturated_count
    }

    fn lock_state(&self) -> MutexGuard<'_, AdmissionState> {
        match self.state.lock() {
            Ok(state) => state,
            Err(poisoned) => poisoned.into_inner(),
        }
    }
}

impl Default for RelayAdmissionGate {
    fn default() -> Self {
        Self::new(DEFAULT_GLOBAL_CEILING)
    }
}

#[cfg(test)]
mod tests {
    use super::RelayAdmissionGate;

    #[test]
    fn default_ceiling_is_32_concurrent_tunnel_setups() {
        let gate = RelayAdmissionGate::default();

        for _ in 0..32 {
            assert!(gate.try_acquire());
        }
        assert!(!gate.try_acquire());
        assert_eq!(gate.saturated_count(), 1);
    }

    #[test]
    fn release_does_not_underflow_and_refusal_count_is_monotonic() {
        let gate = RelayAdmissionGate::new(1);

        gate.release();
        assert_eq!(gate.count(), 0);
        assert!(gate.try_acquire());
        assert!(!gate.try_acquire());
        assert_eq!(gate.saturated_count(), 1);
        gate.release();
        assert_eq!(gate.count(), 0);
        assert_eq!(gate.saturated_count(), 1);
    }
}

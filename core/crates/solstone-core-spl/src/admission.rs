// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 sol pbc

use std::collections::HashMap;
use std::sync::{Mutex, MutexGuard};

const DEFAULT_GLOBAL_CEILING: usize = 32;
const DEFAULT_SENDER_CEILING: usize = 4;

#[derive(Debug, Default)]
struct AdmissionState {
    global_count: usize,
    sender_counts: HashMap<String, usize>,
    saturated_count: u64,
}

/// Synchronous, in-memory admission limits for concurrent blob transfers.
///
/// Each method holds the mutex only while it updates or reads a counter. Callers
/// acquire before transfer work and release afterward, so the gate can be shared
/// across tunnel tasks without a lock spanning the transfer itself.
#[derive(Debug)]
pub struct BlobAdmissionGate {
    global_ceiling: usize,
    sender_ceiling: usize,
    state: Mutex<AdmissionState>,
}

impl BlobAdmissionGate {
    /// Creates a gate with independent global and per-sender ceilings.
    pub fn new(global_ceiling: usize, sender_ceiling: usize) -> Self {
        Self {
            global_ceiling,
            sender_ceiling,
            state: Mutex::new(AdmissionState::default()),
        }
    }

    /// Acquires one global slot without waiting, or records a refusal.
    pub fn try_acquire_global(&self) -> bool {
        let mut state = self.lock_state();
        if state.global_count < self.global_ceiling {
            state.global_count += 1;
            true
        } else {
            state.saturated_count = state.saturated_count.saturating_add(1);
            false
        }
    }

    /// Releases one global slot. Releasing an empty gate leaves it empty.
    pub fn release_global(&self) {
        let mut state = self.lock_state();
        state.global_count = state.global_count.saturating_sub(1);
    }

    /// Acquires one sender slot without waiting, or records a refusal.
    pub fn try_acquire_sender(&self, fp: &str) -> bool {
        let mut state = self.lock_state();
        let count = sender_count(&state.sender_counts, fp);
        if count < self.sender_ceiling {
            state.sender_counts.insert(fp.to_owned(), count + 1);
            true
        } else {
            state.saturated_count = state.saturated_count.saturating_add(1);
            false
        }
    }

    /// Releases one sender slot and removes the sender entry when it reaches zero.
    pub fn release_sender(&self, fp: &str) {
        let mut state = self.lock_state();
        match state.sender_counts.get(fp).copied() {
            Some(0) | None => {}
            Some(1) => {
                state.sender_counts.remove(fp);
            }
            Some(count) => {
                state.sender_counts.insert(fp.to_owned(), count - 1);
            }
        }
    }

    /// Returns the number of held global slots.
    pub fn global_count(&self) -> usize {
        self.lock_state().global_count
    }

    /// Returns the number of held slots for one sender.
    pub fn sender_count(&self, fp: &str) -> usize {
        sender_count(&self.lock_state().sender_counts, fp)
    }

    /// Returns the number of senders currently holding at least one slot.
    pub fn active_senders(&self) -> usize {
        self.lock_state().sender_counts.len()
    }

    /// Returns the cumulative number of global or per-sender refusals.
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

impl Default for BlobAdmissionGate {
    fn default() -> Self {
        Self::new(DEFAULT_GLOBAL_CEILING, DEFAULT_SENDER_CEILING)
    }
}

fn sender_count(sender_counts: &HashMap<String, usize>, fp: &str) -> usize {
    match sender_counts.get(fp) {
        Some(count) => *count,
        None => 0,
    }
}

#[cfg(test)]
mod tests {
    use super::BlobAdmissionGate;

    #[test]
    fn default_ceilings_are_32_global_and_4_per_sender() {
        let gate = BlobAdmissionGate::default();

        for _ in 0..32 {
            assert!(gate.try_acquire_global());
        }
        assert!(!gate.try_acquire_global());

        for _ in 0..4 {
            assert!(gate.try_acquire_sender("sender"));
        }
        assert!(!gate.try_acquire_sender("sender"));
    }

    #[test]
    fn global_and_sender_ceilings_refuse_independently() {
        let gate = BlobAdmissionGate::new(1, 1);

        assert!(gate.try_acquire_global());
        assert!(!gate.try_acquire_global());
        assert_eq!(gate.saturated_count(), 1);

        assert!(gate.try_acquire_sender("first"));
        assert!(!gate.try_acquire_sender("first"));
        assert!(gate.try_acquire_sender("second"));
        assert_eq!(gate.saturated_count(), 2);

        gate.release_global();
        assert!(gate.try_acquire_global());
        assert_eq!(gate.saturated_count(), 2);
    }

    #[test]
    fn refusal_count_is_monotonic_and_only_refusals_change_it() {
        let gate = BlobAdmissionGate::new(1, 1);

        assert!(gate.try_acquire_global());
        assert_eq!(gate.saturated_count(), 0);
        assert!(!gate.try_acquire_global());
        assert_eq!(gate.saturated_count(), 1);
        gate.release_global();
        assert_eq!(gate.saturated_count(), 1);
        assert!(gate.try_acquire_global());
        assert_eq!(gate.saturated_count(), 1);

        assert!(gate.try_acquire_sender("sender"));
        assert!(!gate.try_acquire_sender("sender"));
        assert_eq!(gate.saturated_count(), 2);
        gate.release_sender("sender");
        assert_eq!(gate.saturated_count(), 2);
    }

    #[test]
    fn releases_do_not_underflow_or_leak_sender_entries() {
        let gate = BlobAdmissionGate::new(2, 2);

        gate.release_global();
        gate.release_sender("missing");
        assert_eq!(gate.global_count(), 0);
        assert_eq!(gate.sender_count("missing"), 0);
        assert_eq!(gate.active_senders(), 0);

        assert!(gate.try_acquire_sender("sender"));
        assert!(gate.try_acquire_sender("sender"));
        assert_eq!(gate.active_senders(), 1);
        gate.release_sender("sender");
        assert_eq!(gate.sender_count("sender"), 1);
        assert_eq!(gate.active_senders(), 1);
        gate.release_sender("sender");
        gate.release_sender("sender");
        assert_eq!(gate.sender_count("sender"), 0);
        assert_eq!(gate.active_senders(), 0);
    }

    #[test]
    fn refused_new_sender_does_not_create_an_active_entry() {
        let gate = BlobAdmissionGate::new(1, 0);

        assert!(!gate.try_acquire_sender("sender"));
        assert_eq!(gate.sender_count("sender"), 0);
        assert_eq!(gate.active_senders(), 0);
    }
}

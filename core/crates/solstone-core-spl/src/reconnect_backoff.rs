// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 sol pbc

use std::time::Duration;

/// The initial unjittered delay after a failed relay connection loop.
pub const INITIAL_RECONNECT_BASE: Duration = Duration::from_secs(1);

/// The largest unjittered reconnect base.
pub const MAX_RECONNECT_BASE: Duration = Duration::from_secs(60);

/// A deterministic reconnect schedule for one failed connection loop.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ReconnectSchedule {
    /// The capped base to use after this retry has been scheduled.
    pub next_base: Duration,
    /// The jittered, nonnegative delay to wait before the current retry.
    pub delay: Duration,
}

/// A caller supplied an invalid normalized jitter sample.
#[derive(Debug, Clone, Copy, PartialEq, Eq, thiserror::Error)]
pub enum ReconnectBackoffError {
    /// The sample must be finite and in the inclusive 0.0 through 1.0 range.
    #[error("normalized jitter sample must be finite and within 0.0..=1.0")]
    InvalidJitterSample,
}

/// Schedules a deterministic reconnect delay without reading a clock or RNG.
///
/// Pass `Duration::ZERO` before the first failure; it schedules the contract's
/// one-second base and returns a two-second base for the following failure.
/// Subsequent callers pass the preceding [`ReconnectSchedule::next_base`].
/// `normalized_jitter` maps 0.0 to -25%, 0.5 to no change, and 1.0 to +25%.
pub fn schedule_reconnect(
    current_base: Duration,
    normalized_jitter: f64,
) -> Result<ReconnectSchedule, ReconnectBackoffError> {
    if !normalized_jitter.is_finite() || !(0.0..=1.0).contains(&normalized_jitter) {
        return Err(ReconnectBackoffError::InvalidJitterSample);
    }

    let base = current_base.clamp(INITIAL_RECONNECT_BASE, MAX_RECONNECT_BASE);
    let multiplier = 0.75 + (normalized_jitter * 0.5);
    let delay_nanos = ((base.as_nanos() as f64) * multiplier).round() as u64;

    Ok(ReconnectSchedule {
        next_base: base.saturating_mul(2).min(MAX_RECONNECT_BASE),
        delay: Duration::from_nanos(delay_nanos),
    })
}

#[cfg(test)]
mod tests {
    use std::time::Duration;

    use super::{
        INITIAL_RECONNECT_BASE, MAX_RECONNECT_BASE, ReconnectBackoffError, ReconnectSchedule,
        schedule_reconnect,
    };

    #[test]
    fn first_retry_uses_a_one_second_base_and_advances_to_two_seconds() {
        assert_eq!(
            schedule_reconnect(Duration::ZERO, 0.5),
            Ok(ReconnectSchedule {
                next_base: Duration::from_secs(2),
                delay: INITIAL_RECONNECT_BASE,
            })
        );
    }

    #[test]
    fn retries_double_the_base_until_the_cap() {
        let second = schedule_reconnect(Duration::from_secs(2), 0.5);
        let fourth = schedule_reconnect(Duration::from_secs(4), 0.5);

        assert_eq!(
            second,
            Ok(ReconnectSchedule {
                next_base: Duration::from_secs(4),
                delay: Duration::from_secs(2),
            })
        );
        assert_eq!(
            fourth,
            Ok(ReconnectSchedule {
                next_base: Duration::from_secs(8),
                delay: Duration::from_secs(4),
            })
        );
    }

    #[test]
    fn base_saturates_at_sixty_seconds() {
        assert_eq!(
            schedule_reconnect(MAX_RECONNECT_BASE, 0.5),
            Ok(ReconnectSchedule {
                next_base: MAX_RECONNECT_BASE,
                delay: MAX_RECONNECT_BASE,
            })
        );
        assert_eq!(
            schedule_reconnect(Duration::from_secs(600), 0.5),
            Ok(ReconnectSchedule {
                next_base: MAX_RECONNECT_BASE,
                delay: MAX_RECONNECT_BASE,
            })
        );
    }

    #[test]
    fn jitter_bounds_are_minus_and_plus_twenty_five_percent() {
        assert_eq!(
            schedule_reconnect(MAX_RECONNECT_BASE, 0.0),
            Ok(ReconnectSchedule {
                next_base: MAX_RECONNECT_BASE,
                delay: Duration::from_secs(45),
            })
        );
        assert_eq!(
            schedule_reconnect(MAX_RECONNECT_BASE, 1.0),
            Ok(ReconnectSchedule {
                next_base: MAX_RECONNECT_BASE,
                delay: Duration::from_secs(75),
            })
        );
    }

    #[test]
    fn rejects_out_of_range_and_non_finite_jitter_samples() {
        assert_eq!(
            schedule_reconnect(Duration::ZERO, -0.01),
            Err(ReconnectBackoffError::InvalidJitterSample)
        );
        assert_eq!(
            schedule_reconnect(Duration::ZERO, 1.01),
            Err(ReconnectBackoffError::InvalidJitterSample)
        );
        assert_eq!(
            schedule_reconnect(Duration::ZERO, f64::NAN),
            Err(ReconnectBackoffError::InvalidJitterSample)
        );
        assert_eq!(
            schedule_reconnect(Duration::ZERO, f64::INFINITY),
            Err(ReconnectBackoffError::InvalidJitterSample)
        );
    }

    #[test]
    fn the_same_inputs_always_produce_the_same_schedule() {
        let first = schedule_reconnect(Duration::from_secs(16), 0.37);
        let second = schedule_reconnect(Duration::from_secs(16), 0.37);

        assert_eq!(first, second);
    }
}

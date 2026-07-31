// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 sol pbc

/// Whether the supervised SPL client is currently absent or running.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ServiceLifecycle {
    /// No client is running.
    Idle,
    /// A client is running and may be kept alive through a posture read error.
    Parked,
}

/// The current observation of the local SPL posture.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum PostureObservation {
    /// The posture source contained exactly `spl`.
    Spl,
    /// The posture source was read but did not contain exactly `spl`.
    NotSpl,
    /// The posture source could not be read.
    ReadFailed,
}

impl PostureObservation {
    /// Converts an observed posture value without normalizing it.
    pub fn from_value(value: &str) -> Self {
        if value == "spl" {
            Self::Spl
        } else {
            Self::NotSpl
        }
    }
}

/// The current observation of the SPL service-token source.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum TokenObservation {
    /// A non-empty service token is available to a new client.
    Present,
    /// No service token is available.
    Missing,
    /// The service-token source could not be read.
    ReadFailed,
}

/// The supervisor action for one posture/token observation cycle.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ServiceAction {
    /// Keep the supervisor idle.
    StayIdle,
    /// Start a new SPL client.
    Start,
    /// Keep the existing SPL client running.
    StayParked,
    /// Stop the existing SPL client and clear its state.
    Stop,
}

/// Decides a lifecycle action without starting, stopping, or inspecting a client.
///
/// A token gates only a new start. Once parked, the client stays available while
/// the posture remains `spl` or cannot be read; an explicit non-SPL posture is
/// the only observation that stops it.
pub const fn transition(
    lifecycle: ServiceLifecycle,
    posture: PostureObservation,
    token: TokenObservation,
) -> ServiceAction {
    match lifecycle {
        ServiceLifecycle::Idle => match (posture, token) {
            (PostureObservation::Spl, TokenObservation::Present) => ServiceAction::Start,
            _ => ServiceAction::StayIdle,
        },
        ServiceLifecycle::Parked => match posture {
            PostureObservation::NotSpl => ServiceAction::Stop,
            PostureObservation::Spl | PostureObservation::ReadFailed => ServiceAction::StayParked,
        },
    }
}

#[cfg(test)]
mod tests {
    use super::{
        PostureObservation, ServiceAction, ServiceLifecycle, TokenObservation, transition,
    };

    #[test]
    fn only_exact_spl_is_an_eligible_posture() {
        assert_eq!(
            PostureObservation::from_value("spl"),
            PostureObservation::Spl
        );

        for value in ["SPL", "spl ", " spl", "Spl", "home", ""] {
            assert_eq!(
                PostureObservation::from_value(value),
                PostureObservation::NotSpl
            );
        }
    }

    #[test]
    fn idle_transition_table_fails_closed() {
        let cases = [
            (
                PostureObservation::Spl,
                TokenObservation::Present,
                ServiceAction::Start,
            ),
            (
                PostureObservation::Spl,
                TokenObservation::Missing,
                ServiceAction::StayIdle,
            ),
            (
                PostureObservation::Spl,
                TokenObservation::ReadFailed,
                ServiceAction::StayIdle,
            ),
            (
                PostureObservation::NotSpl,
                TokenObservation::Present,
                ServiceAction::StayIdle,
            ),
            (
                PostureObservation::ReadFailed,
                TokenObservation::Present,
                ServiceAction::StayIdle,
            ),
        ];

        for (posture, token, expected) in cases {
            assert_eq!(transition(ServiceLifecycle::Idle, posture, token), expected);
        }
    }

    #[test]
    fn parked_transition_table_only_stops_after_an_explicit_non_spl_posture() {
        let cases = [
            (
                PostureObservation::Spl,
                TokenObservation::Present,
                ServiceAction::StayParked,
            ),
            (
                PostureObservation::Spl,
                TokenObservation::Missing,
                ServiceAction::StayParked,
            ),
            (
                PostureObservation::Spl,
                TokenObservation::ReadFailed,
                ServiceAction::StayParked,
            ),
            (
                PostureObservation::ReadFailed,
                TokenObservation::Present,
                ServiceAction::StayParked,
            ),
            (
                PostureObservation::NotSpl,
                TokenObservation::Present,
                ServiceAction::Stop,
            ),
            (
                PostureObservation::NotSpl,
                TokenObservation::Missing,
                ServiceAction::Stop,
            ),
            (
                PostureObservation::NotSpl,
                TokenObservation::ReadFailed,
                ServiceAction::Stop,
            ),
        ];

        for (posture, token, expected) in cases {
            assert_eq!(
                transition(ServiceLifecycle::Parked, posture, token),
                expected
            );
        }
    }
}

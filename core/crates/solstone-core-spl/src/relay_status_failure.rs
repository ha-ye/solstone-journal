// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 sol pbc

//! Pure classification of relay-tunnel connection failures.

use crate::relay_health::RelayTunnelFailure;

/// The externally observed category of a failed relay-tunnel connection.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RelayTunnelFailureSignal {
    /// The relay accepted the HTTP connection attempt but returned this status.
    HttpStatus(u16),
    /// The connection failed before the relay could return an HTTP status.
    TransportFailure,
}

/// Maps a relay connection failure signal to its owner-visible failure reason.
pub fn classify_relay_tunnel_failure(signal: RelayTunnelFailureSignal) -> RelayTunnelFailure {
    match signal {
        RelayTunnelFailureSignal::HttpStatus(404) => RelayTunnelFailure::HomeMissingMobile,
        RelayTunnelFailureSignal::HttpStatus(401 | 403) => RelayTunnelFailure::ServiceTokenRejected,
        RelayTunnelFailureSignal::HttpStatus(status) => {
            RelayTunnelFailure::RelayTunnelRejected { status }
        }
        RelayTunnelFailureSignal::TransportFailure => RelayTunnelFailure::RelayTunnelUnreachable,
    }
}

#[cfg(test)]
mod tests {
    use super::{RelayTunnelFailureSignal, classify_relay_tunnel_failure};
    use crate::relay_health::RelayTunnelFailure;

    #[test]
    fn maps_not_found_to_missing_mobile_home() {
        assert_eq!(
            classify_relay_tunnel_failure(RelayTunnelFailureSignal::HttpStatus(404)),
            RelayTunnelFailure::HomeMissingMobile,
        );
    }

    #[test]
    fn maps_authentication_and_authorization_rejections_to_token_rejected() {
        for status in [401, 403] {
            assert_eq!(
                classify_relay_tunnel_failure(RelayTunnelFailureSignal::HttpStatus(status)),
                RelayTunnelFailure::ServiceTokenRejected,
            );
        }
    }

    #[test]
    fn retains_every_other_relay_rejection_status() {
        for status in [400, 402, 500, 503] {
            assert_eq!(
                classify_relay_tunnel_failure(RelayTunnelFailureSignal::HttpStatus(status)),
                RelayTunnelFailure::RelayTunnelRejected { status },
            );
        }
    }

    #[test]
    fn maps_connection_failures_without_http_status_to_unreachable() {
        assert_eq!(
            classify_relay_tunnel_failure(RelayTunnelFailureSignal::TransportFailure),
            RelayTunnelFailure::RelayTunnelUnreachable,
        );
    }
}

// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 sol pbc

//! Routing decision for the first four bytes of an inbound relay tunnel.

/// The action selected from a bounded four-byte tunnel prefix peek.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum TunnelRoute {
    /// Fewer than four bytes are available, so the caller must continue peeking.
    NeedMorePrefix,
    /// A TLS ClientHello prefix that belongs on the local loopback connection.
    TlsLoopback,
    /// A complete four-byte prefix that this service does not support.
    Unsupported,
}

/// Routes an inbound tunnel from its bounded four-byte prefix.
///
/// The caller supplies the bytes already obtained from its peek operation. Only
/// the first four bytes participate in the decision, so a caller that has read
/// farther than its bounded peek still gets the same route.
pub fn route_tunnel_prefix(prefix: &[u8]) -> TunnelRoute {
    if prefix.len() < 4 {
        return TunnelRoute::NeedMorePrefix;
    }

    if prefix.first() == Some(&0x16) {
        return TunnelRoute::TlsLoopback;
    }

    TunnelRoute::Unsupported
}

#[cfg(test)]
mod tests {
    use super::{TunnelRoute, route_tunnel_prefix};

    #[test]
    fn routes_a_tls_client_hello_prefix_to_loopback() {
        assert_eq!(
            route_tunnel_prefix(&[0x16, 0x03, 0x01, 0x00]),
            TunnelRoute::TlsLoopback
        );
    }

    #[test]
    fn treats_non_tls_prefixes_as_unsupported() {
        assert_eq!(route_tunnel_prefix(b"RETI"), TunnelRoute::Unsupported);
    }

    #[test]
    fn rejects_an_unsupported_complete_prefix() {
        assert_eq!(route_tunnel_prefix(b"NOPE"), TunnelRoute::Unsupported);
    }

    #[test]
    fn waits_until_all_four_prefix_bytes_are_available() {
        for prefix in [
            b"".as_slice(),
            b"S".as_slice(),
            b"SB".as_slice(),
            b"SBO".as_slice(),
        ] {
            assert_eq!(route_tunnel_prefix(prefix), TunnelRoute::NeedMorePrefix);
        }
    }
}

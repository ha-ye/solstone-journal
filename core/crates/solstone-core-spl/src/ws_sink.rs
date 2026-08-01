// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 sol pbc

//! The write half of the frozen split WebSocket transport contract.
//!
//! The TLS loopback pipe owns a [`crate::BufferedWsReader`] over the read half
//! and a separate `WsByteSink` over the write half. The split is required
//! because the relay tunnel concurrently carries WebSocket-to-TCP and
//! TCP-to-WebSocket traffic; it is not a reader escape hatch.

use std::future::Future;

use bytes::Bytes;

use crate::WsClosed;

/// The write half of a split WebSocket transport.
pub trait WsByteSink {
    fn send(&mut self, bytes: Bytes) -> impl Future<Output = Result<(), WsClosed>> + Send;

    fn close(&mut self) -> impl Future<Output = Result<(), WsClosed>> + Send;
}

#[cfg(test)]
mod tests {
    use super::*;

    #[derive(Default)]
    struct RecordingSink {
        sent: Vec<Bytes>,
        closed: bool,
    }

    impl WsByteSink for RecordingSink {
        fn send(&mut self, bytes: Bytes) -> impl Future<Output = Result<(), WsClosed>> + Send {
            self.sent.push(bytes);
            std::future::ready(Ok(()))
        }

        fn close(&mut self) -> impl Future<Output = Result<(), WsClosed>> + Send {
            self.closed = true;
            std::future::ready(Ok(()))
        }
    }

    #[tokio::test]
    async fn split_write_half_sends_then_closes() {
        let mut sink = RecordingSink::default();

        assert!(sink.send(Bytes::from_static(b"SBR1\x00\x01")).await.is_ok());
        assert!(sink.close().await.is_ok());

        assert_eq!(sink.sent, [Bytes::from_static(b"SBR1\x00\x01")]);
        assert!(sink.closed);
    }
}

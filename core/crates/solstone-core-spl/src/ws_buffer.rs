// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 sol pbc

use std::future::Future;
use std::time::Duration;

use bytes::{Bytes, BytesMut};
use thiserror::Error;
use tokio::time::Instant;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct WsClosed;

/// The narrow WebSocket seam used by relay tunnel forwarding.
pub trait WsByteSource {
    fn next_message(&mut self) -> impl Future<Output = Result<Option<Bytes>, WsClosed>> + Send;
}

#[derive(Debug, Error, Clone, Copy, PartialEq, Eq)]
pub enum WsBufferError {
    #[error("websocket read exceeded its absolute deadline")]
    ReadTimeout,
    #[error("websocket read did not make the required progress")]
    ProgressTimeout,
    #[error("websocket closed before the requested bytes arrived")]
    Closed,
}

pub struct BufferedWsReader<S: WsByteSource> {
    source: S,
    buffer: BytesMut,
}

impl<S: WsByteSource> BufferedWsReader<S> {
    pub fn new(source: S) -> Self {
        Self {
            source,
            buffer: BytesMut::new(),
        }
    }

    /// Return the next `n` bytes without consuming them.
    ///
    /// WebSocket message boundaries are not byte boundaries, so this may read
    /// across several binary frames before returning.
    pub async fn peek(&mut self, n: usize) -> Result<Bytes, WsBufferError> {
        self.fill(n).await?;
        let bytes = self.buffer.get(..n).ok_or(WsBufferError::Closed)?;
        Ok(Bytes::copy_from_slice(bytes))
    }

    /// Consume and return exactly `n` bytes, spanning binary frames as needed.
    pub async fn read_exactly(&mut self, n: usize) -> Result<Bytes, WsBufferError> {
        self.fill(n).await?;
        Ok(self.buffer.split_to(n).freeze())
    }

    pub async fn peek_bounded(
        &mut self,
        n: usize,
        deadline: Duration,
    ) -> Result<Bytes, WsBufferError> {
        self.fill_bounded(n, deadline, None).await?;
        let bytes = self.buffer.get(..n).ok_or(WsBufferError::Closed)?;
        Ok(Bytes::copy_from_slice(bytes))
    }

    pub async fn read_exactly_bounded(
        &mut self,
        n: usize,
        deadline: Duration,
    ) -> Result<Bytes, WsBufferError> {
        self.fill_bounded(n, deadline, None).await?;
        Ok(self.buffer.split_to(n).freeze())
    }

    pub async fn read_exactly_progress(
        &mut self,
        n: usize,
        deadline: Duration,
        window: Duration,
        min_bytes_per_window: usize,
    ) -> Result<Bytes, WsBufferError> {
        self.fill_bounded(n, deadline, Some((window, min_bytes_per_window)))
            .await?;
        Ok(self.buffer.split_to(n).freeze())
    }

    /// Return all residue already received from the WebSocket and clear it.
    ///
    /// This preserves bytes exactly for loopback replay; it does not wait for a
    /// further frame.
    pub fn drain_buffer(&mut self) -> Bytes {
        self.buffer.split().freeze()
    }

    async fn fill(&mut self, n: usize) -> Result<(), WsBufferError> {
        while self.buffer.len() < n {
            self.receive().await?;
        }
        Ok(())
    }

    async fn fill_bounded(
        &mut self,
        n: usize,
        deadline: Duration,
        progress: Option<(Duration, usize)>,
    ) -> Result<(), WsBufferError> {
        let progress = progress.filter(|(_, minimum)| *minimum > 0);
        let started = Instant::now();
        let absolute_deadline = started.checked_add(deadline);
        let mut window_started = started;
        let mut window_bytes = 0;

        while self.buffer.len() < n {
            let now = Instant::now();
            if deadline_has_elapsed(absolute_deadline, now) {
                return Err(WsBufferError::ReadTimeout);
            }
            let timeout_at = match progress {
                Some((window, minimum)) => {
                    if let Some(window_ended) = elapsed_window_end(window_started, window, now) {
                        if window_bytes < minimum {
                            return Err(WsBufferError::ProgressTimeout);
                        }
                        window_started = window_ended;
                        window_bytes = 0;
                    }
                    earliest_deadline(absolute_deadline, window_started.checked_add(window))
                }
                None => absolute_deadline,
            };

            let frame = match wait_for_frame(&mut self.source, timeout_at).await {
                Ok(Ok(Some(frame))) => frame,
                Ok(Ok(None)) | Ok(Err(_)) => return Err(WsBufferError::Closed),
                Err(_) => {
                    let now = Instant::now();
                    if deadline_has_elapsed(absolute_deadline, now) {
                        return Err(WsBufferError::ReadTimeout);
                    }
                    if let Some((window, minimum)) = progress
                        && let Some(window_ended) = elapsed_window_end(window_started, window, now)
                    {
                        if window_bytes < minimum {
                            return Err(WsBufferError::ProgressTimeout);
                        }
                        window_started = window_ended;
                        window_bytes = 0;
                        continue;
                    }
                    return Err(WsBufferError::ReadTimeout);
                }
            };
            window_bytes += frame.len();
            self.buffer.extend_from_slice(&frame);
        }
        Ok(())
    }

    async fn receive(&mut self) -> Result<(), WsBufferError> {
        match self.source.next_message().await {
            Ok(Some(frame)) => {
                let is_empty = frame.is_empty();
                self.buffer.extend_from_slice(&frame);
                if is_empty {
                    tokio::task::yield_now().await;
                }
                Ok(())
            }
            Ok(None) | Err(_) => Err(WsBufferError::Closed),
        }
    }
}

fn deadline_has_elapsed(deadline: Option<Instant>, now: Instant) -> bool {
    deadline.is_some_and(|deadline| now >= deadline)
}

fn elapsed_window_end(started: Instant, window: Duration, now: Instant) -> Option<Instant> {
    started.checked_add(window).filter(|ends| now >= *ends)
}

fn earliest_deadline(left: Option<Instant>, right: Option<Instant>) -> Option<Instant> {
    match (left, right) {
        (Some(left), Some(right)) => Some(left.min(right)),
        (Some(deadline), None) | (None, Some(deadline)) => Some(deadline),
        (None, None) => None,
    }
}

async fn wait_for_frame<S: WsByteSource>(
    source: &mut S,
    deadline: Option<Instant>,
) -> Result<Result<Option<Bytes>, WsClosed>, tokio::time::error::Elapsed> {
    match deadline {
        Some(deadline) => tokio::time::timeout_at(deadline, source.next_message()).await,
        None => Ok(source.next_message().await),
    }
}

#[cfg(test)]
mod tests {
    use std::collections::VecDeque;

    use super::*;

    struct Frames(VecDeque<Bytes>);

    impl Frames {
        fn new(frames: &[&[u8]]) -> Self {
            Self(
                frames
                    .iter()
                    .map(|frame| Bytes::copy_from_slice(frame))
                    .collect(),
            )
        }
    }

    impl WsByteSource for Frames {
        fn next_message(&mut self) -> impl Future<Output = Result<Option<Bytes>, WsClosed>> + Send {
            std::future::ready(Ok(self.0.pop_front()))
        }
    }

    #[tokio::test]
    async fn split_sbo1_header_peeks_then_reads_the_same_wire_bytes() {
        const HEADER: &[u8] = b"SBO1\x00\x00\x00\x10";

        let mut reader =
            BufferedWsReader::new(Frames::new(&[b"SB", b"O1\x00", b"\x00\x00\x10body"]));

        assert_eq!(
            reader.peek(HEADER.len()).await,
            Ok(Bytes::from_static(HEADER))
        );
        assert_eq!(
            reader.read_exactly(HEADER.len()).await,
            Ok(Bytes::from_static(HEADER))
        );
        assert_eq!(
            reader.read_exactly(4).await,
            Ok(Bytes::from_static(b"body"))
        );
    }

    #[tokio::test]
    async fn consumed_tls_prefix_and_residue_replay_the_exact_client_hello() {
        const CLIENT_HELLO: &[u8] = &[
            0x16, 0x03, 0x01, 0x00, 0x14, 0x01, 0x00, 0x00, 0x10, 0x03, 0x03, 0x42, 0x42, 0x42,
            0x42, 0x42, 0x42, 0x42, 0x42, 0x42, 0x42, 0x42, 0x42, 0x42, 0x42,
        ];
        let mut reader =
            BufferedWsReader::new(Frames::new(&[&CLIENT_HELLO[..5], &CLIENT_HELLO[5..]]));

        let consumed = reader.read_exactly(8).await;
        let residue = reader.drain_buffer();
        assert_eq!(consumed, Ok(Bytes::copy_from_slice(&CLIENT_HELLO[..8])));
        let mut replay = consumed.map_or_else(|_| Vec::new(), |bytes| bytes.to_vec());
        replay.extend_from_slice(&residue);

        assert_eq!(replay, CLIENT_HELLO);
        assert_eq!(reader.drain_buffer(), Bytes::new());
    }

    #[tokio::test]
    async fn closed_stream_is_not_a_short_success() {
        let mut reader = BufferedWsReader::new(Frames::new(&[b"ab"]));
        assert_eq!(reader.read_exactly(3).await, Err(WsBufferError::Closed));
    }

    #[tokio::test]
    async fn absolute_deadline_times_out_when_no_frame_arrives() {
        struct Stalled;
        impl WsByteSource for Stalled {
            fn next_message(
                &mut self,
            ) -> impl Future<Output = Result<Option<Bytes>, WsClosed>> + Send {
                std::future::pending()
            }
        }
        let mut reader = BufferedWsReader::new(Stalled);
        assert_eq!(
            reader.read_exactly_bounded(1, Duration::ZERO).await,
            Err(WsBufferError::ReadTimeout)
        );
    }

    #[tokio::test]
    async fn progress_read_spans_frames_when_each_window_has_enough_bytes() {
        let mut reader = BufferedWsReader::new(Frames::new(&[b"hel", b"lo"]));

        assert_eq!(
            reader
                .read_exactly_progress(5, Duration::from_secs(1), Duration::from_secs(1), 3,)
                .await,
            Ok(Bytes::from_static(b"hello"))
        );
    }

    #[tokio::test]
    async fn elapsed_progress_window_reports_progress_timeout_without_sleeping() {
        let mut reader = BufferedWsReader::new(Frames::new(&[]));

        assert_eq!(
            reader
                .read_exactly_progress(1, Duration::from_secs(1), Duration::ZERO, 1)
                .await,
            Err(WsBufferError::ProgressTimeout)
        );
    }

    #[tokio::test]
    async fn zero_progress_threshold_keeps_the_bounded_read_usable() {
        let mut reader = BufferedWsReader::new(Frames::new(&[b"x"]));

        assert_eq!(
            reader
                .read_exactly_progress(1, Duration::from_secs(1), Duration::ZERO, 0)
                .await,
            Ok(Bytes::from_static(b"x"))
        );
    }
}

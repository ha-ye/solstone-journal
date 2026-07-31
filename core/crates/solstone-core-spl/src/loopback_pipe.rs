// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 sol pbc

//! Full-duplex forwarding between a relay tunnel and the local SPL listener.

use std::io;

use bytes::Bytes;
use thiserror::Error;
use tokio::io::{AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt};

use crate::{BufferedWsReader, WsBufferError, WsByteSink, WsByteSource};

/// The largest local TCP read sent in one WebSocket message.
pub const TCP_TO_WS_READ_MAX: usize = 64 * 1024;

/// Class-only failures at the relay-tunnel-to-loopback seam.
///
/// The variants deliberately contain no transport details: a relay URL can
/// carry a service token, so callers must surface only this taxonomy rather
/// than formatting an underlying WebSocket or I/O error.
#[derive(Debug, Error, Clone, Copy, PartialEq, Eq)]
pub enum TunnelPipeError {
    #[error("websocket read failed")]
    WebSocketRead,
    #[error("websocket write failed")]
    WebSocketWrite,
    #[error("loopback read failed")]
    LoopbackRead,
    #[error("loopback write failed")]
    LoopbackWrite,
    #[error("forwarded byte count overflowed")]
    ByteCountOverflow,
}

/// Directional byte counts observed before either task completed.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct TunnelPipeProgress {
    /// Bytes copied from the WebSocket reader to the loopback TCP writer.
    pub websocket_to_tcp: u64,
    /// Bytes copied from the loopback TCP reader to the WebSocket sink.
    pub tcp_to_websocket: u64,
}

/// Replay buffered TLS bytes and forward a split WebSocket tunnel to TCP.
///
/// The relay dispatcher may have peeked its TLS prefix already.  That prefix
/// remains in `reader`, so draining the buffer before either forwarding task
/// starts is what preserves the local byte stream.  The two directions then
/// race exactly as Python's `_pipe_tunnel` does: the first clean end or
/// genuine failure cancels the other direction.  A clean WebSocket end writes
/// EOF to the local TCP writer before returning.
///
/// # Errors
///
/// Returns the class-only transport failure that ended the tunnel first.
pub async fn pipe_tunnel<R, W, Loopback>(
    reader: &mut BufferedWsReader<R>,
    sink: &mut W,
    loopback: Loopback,
) -> Result<TunnelPipeProgress, TunnelPipeError>
where
    R: WsByteSource,
    W: WsByteSink,
    Loopback: AsyncRead + AsyncWrite + Unpin,
{
    let initial = reader.drain_buffer();
    let (mut tcp_reader, mut tcp_writer) = tokio::io::split(loopback);

    write_loopback(&mut tcp_writer, &initial).await?;

    tokio::select! {
        result = forward_websocket_to_tcp(reader, &mut tcp_writer) => {
            let websocket_to_tcp = result?;
            Ok(TunnelPipeProgress {
                websocket_to_tcp,
                tcp_to_websocket: 0,
            })
        }
        result = forward_tcp_to_websocket(&mut tcp_reader, sink) => {
            let tcp_to_websocket = result?;
            Ok(TunnelPipeProgress {
                websocket_to_tcp: 0,
                tcp_to_websocket,
            })
        }
    }
}

async fn forward_websocket_to_tcp<R, Writer>(
    reader: &mut BufferedWsReader<R>,
    tcp_writer: &mut Writer,
) -> Result<u64, TunnelPipeError>
where
    R: WsByteSource,
    Writer: AsyncWrite + Unpin,
{
    let mut transferred = 0_u64;

    loop {
        let first = match reader.read_exactly(1).await {
            Ok(bytes) => bytes,
            Err(WsBufferError::Closed) => {
                tcp_writer
                    .shutdown()
                    .await
                    .map_err(|_| TunnelPipeError::LoopbackWrite)?;
                return Ok(transferred);
            }
            Err(WsBufferError::ReadTimeout | WsBufferError::ProgressTimeout) => {
                return Err(TunnelPipeError::WebSocketRead);
            }
        };
        let buffered = reader.drain_buffer();

        write_loopback(tcp_writer, &first).await?;
        write_loopback(tcp_writer, &buffered).await?;
        transferred = add_bytes(transferred, first.len())?;
        transferred = add_bytes(transferred, buffered.len())?;
    }
}

async fn forward_tcp_to_websocket<Reader, W>(
    tcp_reader: &mut Reader,
    sink: &mut W,
) -> Result<u64, TunnelPipeError>
where
    Reader: AsyncRead + Unpin,
    W: WsByteSink,
{
    let mut buffer = vec![0_u8; TCP_TO_WS_READ_MAX].into_boxed_slice();
    let mut transferred = 0_u64;

    loop {
        let count = tcp_reader
            .read(&mut buffer)
            .await
            .map_err(|_| TunnelPipeError::LoopbackRead)?;
        if count == 0 {
            return Ok(transferred);
        }
        let data = buffer.get(..count).ok_or(TunnelPipeError::LoopbackRead)?;
        sink.send(Bytes::copy_from_slice(data))
            .await
            .map_err(|_| TunnelPipeError::WebSocketWrite)?;
        transferred = add_bytes(transferred, count)?;
    }
}

async fn write_loopback<Writer>(writer: &mut Writer, bytes: &[u8]) -> Result<(), TunnelPipeError>
where
    Writer: AsyncWrite + Unpin,
{
    if bytes.is_empty() {
        return Ok(());
    }
    writer
        .write_all(bytes)
        .await
        .map_err(|_| TunnelPipeError::LoopbackWrite)?;
    writer
        .flush()
        .await
        .map_err(|_| TunnelPipeError::LoopbackWrite)
}

fn add_bytes(total: u64, count: usize) -> Result<u64, TunnelPipeError> {
    let count = u64::try_from(count).map_err(|_| TunnelPipeError::ByteCountOverflow)?;
    total
        .checked_add(count)
        .ok_or(TunnelPipeError::ByteCountOverflow)
}

/// Compatibility helper for unframed duplex streams.
///
/// This remains useful for callers whose tunnel is not a WebSocket split. It
/// has the same prefix-before-forwarding guarantee as [`pipe_tunnel`].
///
/// # Errors
///
/// Returns the first underlying local or tunnel stream I/O failure.
pub async fn pipe_loopback<Tunnel, Loopback>(
    mut tunnel: Tunnel,
    mut loopback: Loopback,
    initial_prefix: &[u8],
) -> io::Result<(u64, u64)>
where
    Tunnel: AsyncRead + AsyncWrite + Unpin,
    Loopback: AsyncRead + AsyncWrite + Unpin,
{
    loopback.write_all(initial_prefix).await?;
    loopback.flush().await?;
    tokio::io::copy_bidirectional(&mut tunnel, &mut loopback).await
}

#[cfg(test)]
mod tests {
    use std::future::Future;
    use std::io;

    use bytes::Bytes;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    use tokio::sync::mpsc;

    use crate::{BufferedWsReader, WsByteSink, WsByteSource, WsClosed};

    use super::{TunnelPipeProgress, pipe_loopback, pipe_tunnel};

    struct Frames {
        frames: mpsc::UnboundedReceiver<Bytes>,
    }

    impl Frames {
        fn from_receiver(frames: mpsc::UnboundedReceiver<Bytes>) -> Self {
            Self { frames }
        }

        fn fixed(frames: &[&[u8]]) -> Self {
            let (sender, receiver) = mpsc::unbounded_channel();
            for frame in frames {
                let _ = sender.send(Bytes::copy_from_slice(frame));
            }
            drop(sender);
            Self::from_receiver(receiver)
        }
    }

    impl WsByteSource for Frames {
        async fn next_message(&mut self) -> Result<Option<Bytes>, WsClosed> {
            Ok(self.frames.recv().await)
        }
    }

    struct RecordingSink {
        sent: mpsc::UnboundedSender<Bytes>,
    }

    impl WsByteSink for RecordingSink {
        fn send(&mut self, bytes: Bytes) -> impl Future<Output = Result<(), WsClosed>> + Send {
            let result = self.sent.send(bytes).map_err(|_| WsClosed);
            std::future::ready(result)
        }

        fn close(&mut self) -> impl Future<Output = Result<(), WsClosed>> + Send {
            std::future::ready(Ok(()))
        }
    }

    #[tokio::test]
    async fn replays_buffered_tls_bytes_first_and_forwards_both_directions() -> io::Result<()> {
        let (source_send, source_receive) = mpsc::unbounded_channel();
        let mut reader = BufferedWsReader::new(Frames::from_receiver(source_receive));
        source_send
            .send(Bytes::from_static(b"TLS-client-hello"))
            .map_err(|_| io::Error::other("test source receiver missing"))?;
        assert_eq!(reader.peek(1).await, Ok(Bytes::from_static(b"T")));

        let (sink_send, mut sink_receive) = mpsc::unbounded_channel();
        let mut sink = RecordingSink { sent: sink_send };
        let (loopback, mut loopback_peer) = tokio::io::duplex(128);

        let pipe = tokio::spawn(async move { pipe_tunnel(&mut reader, &mut sink, loopback).await });

        let mut initial = [0_u8; 16];
        loopback_peer.read_exact(&mut initial).await?;
        assert_eq!(&initial, b"TLS-client-hello");

        source_send
            .send(Bytes::from_static(b"-continued"))
            .map_err(|_| io::Error::other("test source receiver missing"))?;
        let mut continued = [0_u8; 10];
        loopback_peer.read_exact(&mut continued).await?;
        assert_eq!(&continued, b"-continued");

        loopback_peer.write_all(b"server-response").await?;
        let sent = sink_receive
            .recv()
            .await
            .ok_or_else(|| io::Error::other("test sink did not receive response"))?;
        assert_eq!(sent, Bytes::from_static(b"server-response"));

        drop(source_send);
        let progress = pipe
            .await
            .map_err(|error| io::Error::other(error.to_string()))?
            .map_err(|error| io::Error::other(error.to_string()))?;
        assert_eq!(
            progress,
            TunnelPipeProgress {
                websocket_to_tcp: 10,
                tcp_to_websocket: 0,
            }
        );
        Ok(())
    }

    #[tokio::test]
    async fn websocket_eof_writes_tcp_eof_and_cancels_reverse_read() -> io::Result<()> {
        let mut reader = BufferedWsReader::new(Frames::fixed(&[b"TLS"]));
        assert_eq!(reader.peek(1).await, Ok(Bytes::from_static(b"T")));

        let (sink_send, _sink_receive) = mpsc::unbounded_channel();
        let mut sink = RecordingSink { sent: sink_send };
        let (loopback, mut loopback_peer) = tokio::io::duplex(32);

        let pipe = tokio::spawn(async move { pipe_tunnel(&mut reader, &mut sink, loopback).await });

        let mut prefix = [0_u8; 3];
        loopback_peer.read_exact(&mut prefix).await?;
        assert_eq!(&prefix, b"TLS");
        let mut eof = [0_u8; 1];
        assert_eq!(loopback_peer.read(&mut eof).await?, 0);

        let progress = pipe
            .await
            .map_err(|error| io::Error::other(error.to_string()))?
            .map_err(|error| io::Error::other(error.to_string()))?;
        assert_eq!(progress, TunnelPipeProgress::default());
        Ok(())
    }

    #[tokio::test]
    async fn replays_initial_prefix_once_before_tunnel_payload() -> io::Result<()> {
        let (mut tunnel_peer, tunnel) = tokio::io::duplex(64);
        let (loopback, mut loopback_peer) = tokio::io::duplex(64);

        let pipe = tokio::spawn(async move { pipe_loopback(tunnel, loopback, b"TLS").await });

        tunnel_peer.write_all(b"-payload").await?;
        tunnel_peer.shutdown().await?;

        let mut received = [0_u8; 11];
        loopback_peer.read_exact(&mut received).await?;
        assert_eq!(&received, b"TLS-payload");
        loopback_peer.shutdown().await?;

        let forwarded = pipe
            .await
            .map_err(|error| io::Error::other(error.to_string()))??;
        assert_eq!(forwarded, (8, 0));
        Ok(())
    }

    #[tokio::test]
    async fn forwards_loopback_responses_to_the_tunnel() -> io::Result<()> {
        let (mut tunnel_peer, tunnel) = tokio::io::duplex(64);
        let (loopback, mut loopback_peer) = tokio::io::duplex(64);

        let pipe = tokio::spawn(async move { pipe_loopback(tunnel, loopback, &[]).await });

        loopback_peer.write_all(b"response").await?;
        loopback_peer.shutdown().await?;

        let mut received = [0_u8; 8];
        tunnel_peer.read_exact(&mut received).await?;
        assert_eq!(&received, b"response");
        tunnel_peer.shutdown().await?;

        let forwarded = pipe
            .await
            .map_err(|error| io::Error::other(error.to_string()))??;
        assert_eq!(forwarded, (0, 8));
        Ok(())
    }
}

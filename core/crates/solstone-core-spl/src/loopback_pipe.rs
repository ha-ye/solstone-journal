// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 sol pbc

//! Full-duplex forwarding between a relay tunnel and the local SPL listener.

use std::io;

use tokio::io::{AsyncRead, AsyncWrite, AsyncWriteExt};

/// Replay `initial_prefix` to the loopback listener, then forward both streams.
///
/// The prefix has already been consumed by the relay-side protocol classifier.
/// Replaying it before either forwarding direction starts preserves the original
/// local byte stream exactly. `copy_bidirectional` shuts down the opposite
/// writer after an EOF and returns the underlying I/O error unchanged.
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
    use std::io;

    use tokio::io::{AsyncReadExt, AsyncWriteExt};

    use super::pipe_loopback;

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

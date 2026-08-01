// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 sol pbc

//! The relay listen client and its TLS-only tunnel dispatcher.

use std::{
    future::Future,
    io,
    pin::Pin,
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
    },
    time::{Duration, SystemTime, UNIX_EPOCH},
};

use bytes::Bytes;
use thiserror::Error;
use tokio::{task::JoinSet, time::sleep};

use crate::{
    BlobAdmissionGate, BufferedWsReader, CallosumEmit, ListenControl, RelayHealth,
    RelayHealthState, RelayTunnelFailure, RelayTunnelFailureSignal, RelayWebSocket,
    RelayWebSocketError, ServiceToken, TunnelRoute, WsByteSink, WsByteSource,
    classify_relay_tunnel_failure, pipe_tunnel, relay_tunnel_url, route_tunnel_prefix,
    schedule_reconnect,
};

/// A stream accepted by the local private listener.
pub trait LoopbackStream: tokio::io::AsyncRead + tokio::io::AsyncWrite + Send + Unpin {}

impl<T> LoopbackStream for T where T: tokio::io::AsyncRead + tokio::io::AsyncWrite + Send + Unpin {}

/// The object-safe future returned by a local-loopback dialer.
pub type LoopbackConnect =
    Pin<Box<dyn Future<Output = io::Result<Box<dyn LoopbackStream>>> + Send>>;

/// The concrete seam for the local private listener.
pub trait LoopbackDialer: Send + Sync {
    /// Opens a loopback stream for one TLS tunnel.
    fn connect(&self) -> LoopbackConnect;
}

/// Configuration that is fixed for one relay-client lifetime.
pub struct RelayClientConfig {
    /// The persisted home instance identifier used in relay URL query strings.
    pub instance_id: String,
    /// The configured HTTP(S) or WebSocket relay endpoint.
    pub relay_endpoint: String,
    /// The service credential sent in both the query and bearer header.
    pub service_token: ServiceToken,
    /// The absolute bound for collecting the four-byte dispatch prefix.
    pub dispatch_read_deadline: Duration,
    /// Maximum concurrently-prefix-peeking relay tunnels.
    pub global_admission_ceiling: usize,
}

/// Class-only relay-client failures.
///
/// These errors deliberately retain neither URLs nor upstream error strings:
/// every relay URL contains the service credential in its query string.
#[derive(Clone, Copy, Debug, Error, Eq, PartialEq)]
pub enum RelayError {
    /// The relay refused or could not establish the listen WebSocket.
    #[error("relay listen connection failed")]
    ListenConnection,
    /// The active listen WebSocket ended and the client will reconnect.
    #[error("relay listen connection closed")]
    ListenClosed,
}

#[derive(Clone)]
pub struct RelayClient {
    inner: Arc<RelayClientInner>,
}

struct RelayClientInner {
    config: RelayClientConfig,
    emit: Arc<dyn CallosumEmit>,
    dialer: Arc<dyn LoopbackDialer>,
    admission: Arc<BlobAdmissionGate>,
    health: Mutex<RelayHealth>,
    accepting_tunnels: AtomicBool,
    disconnect_announced: AtomicBool,
    tunnels: tokio::sync::Mutex<JoinSet<()>>,
}

impl RelayClient {
    /// Constructs a relay listener around the frozen U4 seams.
    #[must_use]
    pub fn new(
        config: RelayClientConfig,
        emit: Arc<dyn CallosumEmit>,
        dialer: Arc<dyn LoopbackDialer>,
    ) -> Self {
        let admission = Arc::new(BlobAdmissionGate::new(config.global_admission_ceiling, 0));
        Self {
            inner: Arc::new(RelayClientInner {
                config,
                emit,
                dialer,
                admission,
                health: Mutex::new(RelayHealth::new()),
                accepting_tunnels: AtomicBool::new(true),
                disconnect_announced: AtomicBool::new(false),
                tunnels: tokio::sync::Mutex::new(JoinSet::new()),
            }),
        }
    }

    /// Runs the listen WebSocket and reconnects after every disconnected attempt.
    ///
    /// `stop` intentionally only stops paired tunnels. The supervisor owns this
    /// task and aborts it after calling `stop`, which is what closes the listen
    /// WebSocket on a posture transition.
    pub async fn run(&self) -> Result<(), RelayError> {
        let mut reconnect_base = Duration::ZERO;
        loop {
            let result = self.run_once().await;
            if result.is_ok() {
                reconnect_base = Duration::ZERO;
            }
            self.announce_disconnect();
            let schedule = schedule_reconnect(reconnect_base, jitter_sample())
                .map_err(|_| RelayError::ListenConnection)?;
            reconnect_base = schedule.next_base;
            sleep(schedule.delay).await;
        }
    }

    /// Cancels and awaits all tunnel work without closing the listen WebSocket.
    pub async fn stop(&self) {
        self.inner.accepting_tunnels.store(false, Ordering::Release);
        let mut tunnels = self.inner.tunnels.lock().await;
        tunnels.shutdown().await;
        drop(tunnels);
        self.announce_disconnect();
    }

    async fn run_once(&self) -> Result<(), RelayError> {
        self.begin_listen_attempt();
        let listen_url = relay_tunnel_url(
            &self.inner.config.relay_endpoint,
            "/session/listen",
            &self.inner.config.instance_id,
            self.inner.config.service_token.as_str(),
        );
        let websocket = RelayWebSocket::connect(&listen_url, &self.inner.config.service_token)
            .await
            .map_err(|_| RelayError::ListenConnection)?;
        let (mut reader, _writer) = websocket.split();
        self.set_state(RelayHealthState::Connected, "connected");

        loop {
            let message = reader
                .next_message()
                .await
                .map_err(|_| RelayError::ListenClosed)?;
            let Some(message) = message else {
                return Err(RelayError::ListenClosed);
            };
            if let ListenControl::Incoming { tunnel_id } = crate::parse_listen_control(message) {
                self.emit_tunnel_pair(&tunnel_id);
                self.start_tunnel(tunnel_id).await;
            }
        }
    }

    async fn start_tunnel(&self, tunnel_id: String) {
        if !self.inner.accepting_tunnels.load(Ordering::Acquire) {
            self.emit_tunnel_close(&tunnel_id);
            return;
        }
        self.start_tunnel_after_admission_check(tunnel_id).await;
    }

    async fn start_tunnel_after_admission_check(&self, tunnel_id: String) {
        let mut tunnels = self.inner.tunnels.lock().await;
        while tunnels.try_join_next().is_some() {}
        if !self.inner.accepting_tunnels.load(Ordering::Acquire) {
            self.emit_tunnel_close(&tunnel_id);
            return;
        }
        let client = self.clone();
        let lifecycle = TunnelLifecycle::new(client.clone(), tunnel_id.clone());
        tunnels.spawn(async move {
            client.handle_tunnel(tunnel_id, lifecycle).await;
        });
    }

    async fn handle_tunnel(&self, tunnel_id: String, _lifecycle: TunnelLifecycle) {
        let url = relay_tunnel_url(
            &self.inner.config.relay_endpoint,
            &format!("/tunnel/{tunnel_id}"),
            &self.inner.config.instance_id,
            self.inner.config.service_token.as_str(),
        );
        let websocket = RelayWebSocket::connect(&url, &self.inner.config.service_token).await;
        let websocket = match websocket {
            Ok(websocket) => websocket,
            Err(error) => {
                self.record_connect_failure(error);
                return;
            }
        };
        self.record_tunnel_success();
        let (reader, mut writer) = websocket.split();
        let mut buffered = BufferedWsReader::new(reader);

        let Some(mut admission) = GlobalAdmission::acquire(Arc::clone(&self.inner.admission))
        else {
            self.record_admission_saturated();
            let _ = writer.close().await;
            return;
        };
        let prefix = buffered
            .peek_bounded(4, self.inner.config.dispatch_read_deadline)
            .await;
        let prefix = match prefix {
            Ok(prefix) => prefix,
            Err(_) => {
                self.record_failure(RelayTunnelFailure::RelayTunnelUnreachable);
                let _ = writer.close().await;
                return;
            }
        };

        match route_tunnel_prefix(&prefix) {
            TunnelRoute::TlsLoopback => {
                admission.release();
                match self.inner.dialer.connect().await {
                    Ok(loopback) => {
                        let _ = pipe_tunnel(&mut buffered, &mut writer, loopback).await;
                    }
                    Err(_) => {
                        self.record_failure(RelayTunnelFailure::LocalPrivateListenerUnreachable)
                    }
                }
            }
            TunnelRoute::Unsupported | TunnelRoute::NeedMorePrefix => {
                self.emit_unknown_prefix(&prefix);
            }
        }

        let _ = writer.close().await;
    }

    fn begin_listen_attempt(&self) {
        self.inner
            .disconnect_announced
            .store(false, Ordering::Release);
        {
            let mut health = lock_unpoisoned(&self.inner.health);
            health.begin_listen_attempt();
            health.set_state(RelayHealthState::Connecting);
        }
        self.inner.emit.emit("connecting", serde_json::json!({}));
        self.emit_health();
    }

    fn set_state(&self, state: RelayHealthState, event: &'static str) {
        lock_unpoisoned(&self.inner.health).set_state(state);
        self.inner.emit.emit(event, serde_json::json!({}));
        self.emit_health();
    }

    fn announce_disconnect(&self) {
        if !self.inner.disconnect_announced.swap(true, Ordering::AcqRel) {
            self.set_state(RelayHealthState::Reconnecting, "disconnect");
        }
    }

    fn record_tunnel_success(&self) {
        lock_unpoisoned(&self.inner.health).record_tunnel_success(now_ms());
        self.emit_health();
    }

    fn record_connect_failure(&self, error: RelayWebSocketError) {
        let failure = match error {
            RelayWebSocketError::Status(status) => {
                classify_relay_tunnel_failure(RelayTunnelFailureSignal::HttpStatus(status))
            }
            RelayWebSocketError::Request | RelayWebSocketError::Connection => {
                classify_relay_tunnel_failure(RelayTunnelFailureSignal::TransportFailure)
            }
        };
        self.record_failure(failure);
    }

    fn record_failure(&self, failure: RelayTunnelFailure) {
        lock_unpoisoned(&self.inner.health).record_tunnel_failure(failure, now_ms());
        self.emit_health();
    }

    fn record_admission_saturated(&self) {
        let count = self.inner.admission.saturated_count();
        {
            let mut health = lock_unpoisoned(&self.inner.health);
            health.set_relay_admission_saturated_count(count);
        }
        self.inner.emit.emit(
            "admission_saturated",
            serde_json::json!({"reason": "relay_admission_saturated", "count": count}),
        );
        self.emit_health();
    }

    fn emit_unknown_prefix(&self, prefix: &Bytes) {
        self.inner.emit.emit(
            "tunnel_unknown_prefix",
            serde_json::json!({"prefix": prefix_hex(prefix)}),
        );
    }

    fn emit_tunnel_pair(&self, tunnel_id: &str) {
        self.inner
            .emit
            .emit("tunnel_pair", serde_json::json!({"tunnel_id": tunnel_id}));
    }

    fn emit_tunnel_close(&self, tunnel_id: &str) {
        self.inner
            .emit
            .emit("tunnel_close", serde_json::json!({"tunnel_id": tunnel_id}));
        self.emit_health();
    }

    fn emit_health(&self) {
        let payload = {
            let mut health = lock_unpoisoned(&self.inner.health);
            health.set_relay_admission_saturated_count(self.inner.admission.saturated_count());
            health.payload()
        };
        self.inner.emit.emit("health", payload);
    }
}

impl crate::RelayStop for RelayClient {
    type Error = std::convert::Infallible;

    async fn stop(&mut self) -> Result<(), Self::Error> {
        RelayClient::stop(self).await;
        Ok(())
    }
}

fn lock_unpoisoned<T>(mutex: &Mutex<T>) -> std::sync::MutexGuard<'_, T> {
    match mutex.lock() {
        Ok(value) => value,
        Err(poisoned) => poisoned.into_inner(),
    }
}

fn now_ms() -> u64 {
    let duration = match SystemTime::now().duration_since(UNIX_EPOCH) {
        Ok(duration) => duration,
        Err(_) => Duration::ZERO,
    };
    u64::try_from(duration.as_millis()).map_or(u64::MAX, std::convert::identity)
}

fn jitter_sample() -> f64 {
    let duration = match SystemTime::now().duration_since(UNIX_EPOCH) {
        Ok(duration) => duration,
        Err(_) => Duration::ZERO,
    };
    f64::from(duration.subsec_nanos()) / 1_000_000_000.0
}

fn prefix_hex(prefix: &Bytes) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut output = String::with_capacity(prefix.len().saturating_mul(2));
    for byte in prefix {
        output.push(char::from(HEX[usize::from(byte >> 4)]));
        output.push(char::from(HEX[usize::from(byte & 0x0f)]));
    }
    output
}

struct GlobalAdmission {
    gate: Arc<BlobAdmissionGate>,
    held: bool,
}

struct TunnelLifecycle {
    client: RelayClient,
    tunnel_id: String,
}

impl TunnelLifecycle {
    fn new(client: RelayClient, tunnel_id: String) -> Self {
        Self { client, tunnel_id }
    }
}

impl Drop for TunnelLifecycle {
    fn drop(&mut self) {
        self.client.emit_tunnel_close(&self.tunnel_id);
    }
}

impl GlobalAdmission {
    fn acquire(gate: Arc<BlobAdmissionGate>) -> Option<Self> {
        gate.try_acquire_global()
            .then_some(Self { gate, held: true })
    }

    fn release(&mut self) {
        if self.held {
            self.gate.release_global();
            self.held = false;
        }
    }
}

impl Drop for GlobalAdmission {
    fn drop(&mut self) {
        self.release();
    }
}

#[cfg(test)]
mod tests {
    use std::time::Duration;
    use std::{
        future,
        sync::{Arc, Mutex},
    };

    use super::{
        GlobalAdmission, LoopbackConnect, LoopbackDialer, RelayClient, RelayClientConfig,
        prefix_hex,
    };
    use bytes::Bytes;
    use futures_util::{SinkExt, StreamExt};
    use tokio::{
        io::{AsyncReadExt, DuplexStream},
        net::TcpListener,
        sync::{Notify, oneshot},
        time::timeout,
    };
    use tokio_tungstenite::{accept_async, tungstenite::Message};

    use crate::{CallosumEmit, ServiceToken};

    struct Emitter {
        events: Mutex<Vec<(String, serde_json::Value)>>,
        changed: Notify,
    }

    impl Default for Emitter {
        fn default() -> Self {
            Self {
                events: Mutex::new(Vec::new()),
                changed: Notify::new(),
            }
        }
    }

    impl Emitter {
        fn snapshot(&self) -> Vec<(String, serde_json::Value)> {
            match self.events.lock() {
                Ok(events) => events.clone(),
                Err(poisoned) => poisoned.into_inner().clone(),
            }
        }

        async fn wait_for_event(&self, expected: &str) {
            loop {
                let notified = self.changed.notified();
                if self
                    .events
                    .lock()
                    .is_ok_and(|events| events.iter().any(|(event, _)| event == expected))
                {
                    return;
                }
                notified.await;
            }
        }
    }

    impl CallosumEmit for Emitter {
        fn emit(&self, event: &'static str, fields: serde_json::Value) {
            match self.events.lock() {
                Ok(mut events) => events.push((event.to_owned(), fields)),
                Err(poisoned) => poisoned.into_inner().push((event.to_owned(), fields)),
            }
            self.changed.notify_waiters();
        }
    }

    struct Dialer {
        peer: Mutex<Option<oneshot::Sender<DuplexStream>>>,
    }

    impl Dialer {
        fn new(peer: oneshot::Sender<DuplexStream>) -> Self {
            Self {
                peer: Mutex::new(Some(peer)),
            }
        }
    }

    impl LoopbackDialer for Dialer {
        fn connect(&self) -> LoopbackConnect {
            let (client, peer) = tokio::io::duplex(1024);
            let sender = match self.peer.lock() {
                Ok(mut peer_slot) => peer_slot.take(),
                Err(poisoned) => poisoned.into_inner().take(),
            };
            Box::pin(async move {
                if let Some(sender) = sender {
                    let _ = sender.send(peer);
                }
                Ok(Box::new(client) as Box<dyn super::LoopbackStream>)
            })
        }
    }

    fn client_config(address: std::net::SocketAddr, token: &str) -> RelayClientConfig {
        RelayClientConfig {
            instance_id: "home-instance".to_owned(),
            relay_endpoint: format!("http://{address}"),
            service_token: ServiceToken::new(token.to_owned()),
            dispatch_read_deadline: Duration::from_secs(1),
            global_admission_ceiling: 1,
        }
    }

    #[test]
    fn prefix_logging_is_bounded_hex_not_utf8_or_transport_text() {
        assert_eq!(
            prefix_hex(&Bytes::from_static(&[0, 0xff, 0x16, 0x03])),
            "00ff1603"
        );
    }

    #[test]
    fn global_admission_releases_on_drop_and_explicit_early_release() {
        let gate = Arc::new(crate::BlobAdmissionGate::new(1, 0));
        let mut guard = GlobalAdmission::acquire(Arc::clone(&gate));
        assert!(guard.is_some());
        assert_eq!(gate.global_count(), 1);
        if let Some(guard) = guard.as_mut() {
            guard.release();
        }
        assert_eq!(gate.global_count(), 0);
        drop(guard);
        assert_eq!(gate.global_count(), 0);

        let guard = GlobalAdmission::acquire(Arc::clone(&gate));
        assert!(guard.is_some());
        drop(guard);
        assert_eq!(gate.global_count(), 0);
    }

    #[tokio::test]
    async fn stop_race_after_pair_emits_one_terminal_close_and_health() -> Result<(), String> {
        let (peer_sender, _peer_receiver) = oneshot::channel();
        let emitter = Arc::new(Emitter::default());
        let emission: Arc<dyn CallosumEmit> = emitter.clone();
        let client = RelayClient::new(
            client_config(
                "127.0.0.1:9".parse().map_err(|_| "test address invalid")?,
                "token",
            ),
            emission,
            Arc::new(Dialer::new(peer_sender)),
        );

        client.emit_tunnel_pair("race");
        client
            .inner
            .accepting_tunnels
            .store(false, std::sync::atomic::Ordering::Release);
        client
            .start_tunnel_after_admission_check("race".to_owned())
            .await;

        assert_eq!(
            emitter.snapshot(),
            vec![
                (
                    "tunnel_pair".to_owned(),
                    serde_json::json!({"tunnel_id": "race"})
                ),
                (
                    "tunnel_close".to_owned(),
                    serde_json::json!({"tunnel_id": "race"})
                ),
                (
                    "health".to_owned(),
                    serde_json::json!({
                        "state": "connecting",
                        "listen_generation": 0,
                        "last_successful_relay_tunnel_at": null,
                        "last_relay_tunnel_error": null,
                        "last_relay_tunnel_error_at": null,
                        "relay_tunnel_error_status": null,
                        "relay_admission_saturated_count": 0,
                    }),
                ),
            ]
        );
        Ok(())
    }

    #[tokio::test]
    async fn listener_dispatches_tls_to_loopback_and_replays_the_peeked_prefix()
    -> Result<(), String> {
        let listener = TcpListener::bind("127.0.0.1:0")
            .await
            .map_err(|_| "relay bind failed".to_owned())?;
        let address = listener
            .local_addr()
            .map_err(|_| "relay address failed".to_owned())?;
        let server = tokio::spawn(async move {
            let (stream, _) = listener.accept().await.map_err(|_| ())?;
            let mut listen = accept_async(stream).await.map_err(|_| ())?;
            listen
                .send(Message::Text(
                    "{\"type\":\"incoming\",\"tunnel_id\":\"tls\"}".into(),
                ))
                .await
                .map_err(|_| ())?;
            let (stream, _) = listener.accept().await.map_err(|_| ())?;
            let mut tunnel = accept_async(stream).await.map_err(|_| ())?;
            tunnel
                .send(Message::Binary(Bytes::from_static(&[0x16, 0x03])))
                .await
                .map_err(|_| ())?;
            tunnel
                .send(Message::Binary(Bytes::from_static(&[0x01, 0x00])))
                .await
                .map_err(|_| ())?;
            let _ = tunnel.next().await;
            Ok::<(), ()>(())
        });
        let (peer_sender, mut peer_receiver) = oneshot::channel();
        let emitter = Arc::new(Emitter::default());
        let emission: Arc<dyn CallosumEmit> = emitter.clone();
        let client = RelayClient::new(
            client_config(address, "known-service-token"),
            emission,
            Arc::new(Dialer::new(peer_sender)),
        );
        let running = {
            let client = client.clone();
            tokio::spawn(async move { client.run().await })
        };

        let mut peer = timeout(Duration::from_secs(2), &mut peer_receiver)
            .await
            .map_err(|_| "loopback dial timed out".to_owned())?
            .map_err(|_| "loopback dial was dropped".to_owned())?;
        let mut received = [0_u8; 4];
        peer.read_exact(&mut received)
            .await
            .map_err(|_| "loopback read failed".to_owned())?;
        assert_eq!(received, [0x16, 0x03, 0x01, 0x00]);
        assert_eq!(client.inner.admission.global_count(), 0);

        client.stop().await;
        let events_after_stop = emitter.snapshot();
        let tail = events_after_stop
            .get(events_after_stop.len().saturating_sub(4)..)
            .ok_or_else(|| "missing cancelled tunnel event tail".to_owned())?;
        if tail.len() != 4 {
            return Err("cancelled tunnel event tail had an unexpected length".to_owned());
        }
        assert_eq!(
            tail[0],
            (
                "tunnel_close".to_owned(),
                serde_json::json!({"tunnel_id": "tls"})
            )
        );
        assert_eq!(tail[1].0, "health");
        assert_eq!(tail[2], ("disconnect".to_owned(), serde_json::json!({})));
        assert_eq!(tail[3].0, "health");
        running.abort();
        let _ = running.await;
        server.abort();
        let _ = server.await;
        Ok(())
    }

    #[tokio::test]
    async fn retired_blob_and_arbitrary_prefixes_close_as_the_same_unknown_route()
    -> Result<(), String> {
        let listener = TcpListener::bind("127.0.0.1:0")
            .await
            .map_err(|_| "relay bind failed".to_owned())?;
        let address = listener
            .local_addr()
            .map_err(|_| "relay address failed".to_owned())?;
        let server = tokio::spawn(async move {
            let (stream, _) = listener.accept().await.map_err(|_| ())?;
            let mut listen = accept_async(stream).await.map_err(|_| ())?;
            listen
                .send(Message::Text(
                    "{\"type\":\"incoming\",\"tunnel_id\":\"unknown\"}".into(),
                ))
                .await
                .map_err(|_| ())?;
            let (stream, _) = listener.accept().await.map_err(|_| ())?;
            let mut tunnel = accept_async(stream).await.map_err(|_| ())?;
            tunnel
                .send(Message::Binary(Bytes::from_static(b"SBO1")))
                .await
                .map_err(|_| ())?;
            match timeout(Duration::from_secs(2), tunnel.next()).await {
                Ok(Some(Ok(Message::Close(_)))) | Ok(None) => Ok::<(), ()>(()),
                _ => Err(()),
            }
        });
        let (peer_sender, _peer_receiver) = oneshot::channel();
        let emitter = Arc::new(Emitter::default());
        let emission: Arc<dyn CallosumEmit> = emitter.clone();
        let client = RelayClient::new(
            client_config(address, "known-service-token"),
            emission,
            Arc::new(Dialer::new(peer_sender)),
        );
        let running = {
            let client = client.clone();
            tokio::spawn(async move { client.run().await })
        };

        server
            .await
            .map_err(|_| "relay server panicked".to_owned())?
            .map_err(|_| "unknown prefix did not close".to_owned())?;
        assert_eq!(client.inner.admission.global_count(), 0);
        let formatted = {
            let events = match emitter.events.lock() {
                Ok(events) => events,
                Err(poisoned) => poisoned.into_inner(),
            };
            format!("{events:?}")
        };
        assert!(formatted.contains("53424f31"));
        assert!(!formatted.contains("known-service-token"));

        client.stop().await;
        running.abort();
        let _ = running.await;
        Ok(())
    }

    #[tokio::test]
    async fn short_prefix_error_releases_the_global_admission_slot() -> Result<(), String> {
        let listener = TcpListener::bind("127.0.0.1:0")
            .await
            .map_err(|_| "relay bind failed".to_owned())?;
        let address = listener
            .local_addr()
            .map_err(|_| "relay address failed".to_owned())?;
        let server = tokio::spawn(async move {
            let (stream, _) = listener.accept().await.map_err(|_| ())?;
            let mut listen = accept_async(stream).await.map_err(|_| ())?;
            listen
                .send(Message::Text(
                    "{\"type\":\"incoming\",\"tunnel_id\":\"short\"}".into(),
                ))
                .await
                .map_err(|_| ())?;
            let (stream, _) = listener.accept().await.map_err(|_| ())?;
            let mut tunnel = accept_async(stream).await.map_err(|_| ())?;
            tunnel
                .send(Message::Binary(Bytes::from_static(b"no")))
                .await
                .map_err(|_| ())?;
            tunnel.close(None).await.map_err(|_| ())?;
            Ok::<(), ()>(())
        });
        let (peer_sender, _peer_receiver) = oneshot::channel();
        let emitter = Arc::new(Emitter::default());
        let emission: Arc<dyn CallosumEmit> = emitter.clone();
        let client = RelayClient::new(
            client_config(address, "known-service-token"),
            emission,
            Arc::new(Dialer::new(peer_sender)),
        );
        let running = {
            let client = client.clone();
            tokio::spawn(async move { client.run().await })
        };

        server
            .await
            .map_err(|_| "relay server panicked".to_owned())?
            .map_err(|_| "relay server failed".to_owned())?;
        {
            let mut tunnels = client.inner.tunnels.lock().await;
            let joined = timeout(Duration::from_secs(2), tunnels.join_next())
                .await
                .map_err(|_| "short-prefix tunnel did not finish".to_owned())?;
            assert!(joined.is_some());
        }
        assert_eq!(client.inner.admission.global_count(), 0);
        let events = emitter.snapshot();
        assert!(events.contains(&(
            "tunnel_pair".to_owned(),
            serde_json::json!({"tunnel_id": "short"}),
        )));
        assert!(events.windows(2).any(|events| {
            events[0]
                == (
                    "tunnel_close".to_owned(),
                    serde_json::json!({"tunnel_id": "short"}),
                )
                && events[1].0 == "health"
        }));

        client.stop().await;
        running.abort();
        let _ = running.await;
        Ok(())
    }

    #[tokio::test]
    async fn stop_emits_final_disconnect_and_health_before_run_task_cancellation()
    -> Result<(), String> {
        let listener = TcpListener::bind("127.0.0.1:0")
            .await
            .map_err(|_| "relay bind failed".to_owned())?;
        let address = listener
            .local_addr()
            .map_err(|_| "relay address failed".to_owned())?;
        let (connected_sender, connected_receiver) = oneshot::channel();
        let (check_sender, check_receiver) = oneshot::channel();
        let (open_sender, open_receiver) = oneshot::channel();
        let server = tokio::spawn(async move {
            let (stream, _) = listener.accept().await.map_err(|_| ())?;
            let mut listen = accept_async(stream).await.map_err(|_| ())?;
            let _ = connected_sender.send(());
            check_receiver.await.map_err(|_| ())?;
            let remains_open = timeout(Duration::from_millis(50), listen.next())
                .await
                .is_err();
            let _ = open_sender.send(remains_open);
            future::pending::<()>().await;
            Ok::<(), ()>(())
        });
        let (peer_sender, _peer_receiver) = oneshot::channel();
        let emitter = Arc::new(Emitter::default());
        let emission: Arc<dyn CallosumEmit> = emitter.clone();
        let client = RelayClient::new(
            client_config(address, "known-service-token"),
            emission,
            Arc::new(Dialer::new(peer_sender)),
        );
        let running = {
            let client = client.clone();
            tokio::spawn(async move { client.run().await })
        };

        connected_receiver
            .await
            .map_err(|_| "listen websocket did not connect".to_owned())?;
        timeout(Duration::from_secs(2), emitter.wait_for_event("connected"))
            .await
            .map_err(|_| "connected event did not arrive".to_owned())?;

        client.stop().await;
        let events_after_stop = emitter.snapshot();
        let tail = events_after_stop
            .get(events_after_stop.len().saturating_sub(2)..)
            .ok_or_else(|| "missing disconnect event tail".to_owned())?;
        if tail.len() != 2 {
            return Err("disconnect event tail had an unexpected length".to_owned());
        }
        assert_eq!(tail[0], ("disconnect".to_owned(), serde_json::json!({})));
        assert_eq!(
            tail[1],
            (
                "health".to_owned(),
                serde_json::json!({
                    "state": "reconnecting",
                    "listen_generation": 1,
                    "last_successful_relay_tunnel_at": null,
                    "last_relay_tunnel_error": null,
                    "last_relay_tunnel_error_at": null,
                    "relay_tunnel_error_status": null,
                    "relay_admission_saturated_count": 0,
                }),
            )
        );
        check_sender
            .send(())
            .map_err(|_| "listen server stopped early".to_owned())?;
        assert!(
            open_receiver
                .await
                .map_err(|_| "listen-open check did not finish".to_owned())?
        );

        running.abort();
        let _ = running.await;
        assert_eq!(emitter.snapshot(), events_after_stop);
        server.abort();
        let _ = server.await;
        Ok(())
    }
}

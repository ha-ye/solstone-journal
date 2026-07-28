// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 sol pbc

use std::future::Future;
use std::io::ErrorKind;
use std::pin::Pin;
use std::sync::{Arc, Mutex};
use std::time::{SystemTime, UNIX_EPOCH};

use serde_json::{Map, Value};
use solstone_core_sol_client::resident::ShutdownSignal;
use solstone_core_sol_client::seam::{
    LinkServeBundle, LinkServeError, LinkServeErrorKind, LinkServeFailure,
    LinkServeRelayControlEndpoint, LinkServeRelayErrorKind, LinkServeRequest, LinkServeRunner,
    LinkServeSession, LinkServeStatusSnapshot, LinkServeTransportErrorKind,
};
use spl_core::bridge::{BridgeNames, RequestHeaderPolicy};
use spl_transport::client::{DialedCarrier, TransportClient};
use spl_transport::credential::{Credential, EndpointAddr};
use spl_transport::journal_bridge::{
    self, BridgePolicy, BridgeStartError, CapabilityGate, CarrierOpener, JournalBridgeConfig,
    JournalBridgeHandle, JournalBridgeStatus, LocalResponse,
};
use spl_transport::relay_pairing::enroll_device;
use spl_transport::{RelayControlEndpoint, RelayError, TransportError, tls};

const STATUS_PATH: &str = "/_solstone/link/status";
const OBSERVER_HEADER: &str = "X-Solstone-Observer";
const PROTOCOL_HEADER: &str = "X-Solstone-Protocol-Version";
const OBSERVER_PROTOCOL_VERSION: &str = "2";
const MAX_REQUEST_BODY_BYTES: usize = 8 * 1024 * 1024;

#[derive(Debug, Clone, Copy, Default)]
pub struct SplLinkServeRunner;

impl LinkServeRunner for SplLinkServeRunner {
    fn start(
        &self,
        request: LinkServeRequest,
    ) -> Result<Box<dyn LinkServeSession>, LinkServeError> {
        ServeStarter::default().start(request)
    }
}

struct ServeStarter {
    enrollment: Arc<dyn RelayEnrollment>,
    clock: Arc<dyn StatusClock>,
}

impl Default for ServeStarter {
    fn default() -> Self {
        Self {
            enrollment: Arc::new(SplRelayEnrollment),
            clock: Arc::new(SystemStatusClock),
        }
    }
}

impl ServeStarter {
    fn start(
        &self,
        request: LinkServeRequest,
    ) -> Result<Box<dyn LinkServeSession>, LinkServeError> {
        // Must be multi-threaded: `LinkServeSession::serve` parks the calling
        // thread in a blocking `ShutdownSignal::wait()` for the process's whole
        // lifetime, and never re-enters the runtime until shutdown. A
        // current-thread runtime only polls spawned tasks while some thread is
        // inside `block_on`, so the bridge's accept loop — spawned by
        // `journal_bridge::start` below — would never run. The listener would
        // still bind (the kernel completes handshakes from the backlog), so the
        // port looks healthy while every request hangs and returns zero bytes.
        //
        // The worker count is pinned rather than left to default: this proxy
        // carries one person's loopback traffic over a single carrier, and the
        // default spawns one worker per core (33 threads on a large host). The
        // work is entirely async I/O, so two workers is ample — one can block
        // briefly on a task without stalling the accept loop.
        let runtime = tokio::runtime::Builder::new_multi_thread()
            .worker_threads(2)
            .enable_all()
            .build()
            .map_err(|_| LinkServeError::new(LinkServeErrorKind::RuntimeUnavailable))?;
        let enrollment = self.enrollment.clone();
        let credential = runtime.block_on(credential_from_request(&request, enrollment))?;
        let relay_only = credential.relay_origin.is_some() && credential.endpoints.is_empty();
        let client = Arc::new(
            if relay_only {
                TransportClient::new_relay_only(credential, None)
            } else {
                TransportClient::new(credential, None)
            }
            .map_err(|error| {
                LinkServeError::new(LinkServeErrorKind::Transport(map_transport_error(error)))
            })?,
        );
        let tracker = Arc::new(StatusTracker::new(self.clock.clone()));
        let opener = Arc::new(SolstoneCarrierOpener {
            client,
            label: request.label.clone(),
            tracker: tracker.clone(),
        });
        let policy = bridge_policy_for_port(request.port, tracker);
        let endpoint_hosts = request
            .bundle
            .endpoints
            .iter()
            .map(|endpoint| endpoint.host.clone())
            .collect::<Vec<_>>();
        let config = JournalBridgeConfig {
            opener,
            bridge_names: bridge_names(),
            endpoint_hosts,
            policy,
        };
        let handle = runtime
            .block_on(journal_bridge::start(config))
            .map_err(|error| map_bridge_start_error(error, request.port))?;
        Ok(Box::new(SplLinkServeSession {
            port: handle.port(),
            runtime,
            handle: Some(handle),
        }))
    }
}

struct SplLinkServeSession {
    port: u16,
    runtime: tokio::runtime::Runtime,
    handle: Option<JournalBridgeHandle>,
}

impl LinkServeSession for SplLinkServeSession {
    fn bound_port(&self) -> u16 {
        self.port
    }

    fn serve(mut self: Box<Self>, shutdown: &dyn ShutdownSignal) -> Result<(), LinkServeError> {
        shutdown.wait();
        if let Some(handle) = self.handle.take() {
            self.runtime.block_on(handle.shutdown_and_wait());
        }
        Ok(())
    }
}

struct SolstoneCarrierOpener {
    client: Arc<TransportClient>,
    label: String,
    tracker: Arc<StatusTracker>,
}

impl CarrierOpener for SolstoneCarrierOpener {
    fn proxy_headers(
        &self,
        upstream_headers: &[(String, String)],
    ) -> Result<Vec<(String, String)>, TransportError> {
        let mut headers = upstream_headers.to_vec();
        headers.push((OBSERVER_HEADER.to_string(), self.label.clone()));
        headers.push((
            PROTOCOL_HEADER.to_string(),
            OBSERVER_PROTOCOL_VERSION.to_string(),
        ));
        Ok(headers)
    }

    fn dial_carrier(
        &self,
    ) -> Pin<Box<dyn Future<Output = Result<DialedCarrier, TransportError>> + Send + '_>> {
        Box::pin(async move {
            let result = self.client.dial_carrier().await;
            match &result {
                Ok(_) => self.tracker.carrier_open_succeeded(),
                Err(error) => self.tracker.carrier_open_failed(error),
            }
            result
        })
    }
}

async fn credential_from_request(
    request: &LinkServeRequest,
    enrollment: Arc<dyn RelayEnrollment>,
) -> Result<Credential, LinkServeError> {
    let token = if request.direct {
        None
    } else if let Some(origin) = request.relay_origin.as_deref() {
        Some(
            enrollment
                .enroll(
                    origin,
                    &request.bundle.instance_id,
                    &request.bundle.home_attestation,
                )
                .await
                .map_err(|error| {
                    LinkServeError::new(LinkServeErrorKind::Transport(map_transport_error(error)))
                })?,
        )
    } else {
        None
    };
    Ok(Credential {
        client_key_pem: request.bundle.private_key_pem.clone(),
        client_cert_pem: request.bundle.client_cert_pem.clone(),
        ca_chain_pem: request.bundle.ca_chain_pem.clone(),
        ca_fp_prefix: ca_fp_prefix(&request.bundle)?,
        instance_id: request.bundle.instance_id.clone(),
        home_label: request.bundle.home_label.clone(),
        endpoints: request
            .bundle
            .endpoints
            .iter()
            .map(|endpoint| EndpointAddr {
                host: endpoint.host.clone(),
                port: endpoint.port,
            })
            .collect(),
        home_attestation: Some(request.bundle.home_attestation.clone()),
        local_endpoints: Some(request.bundle.local_endpoints.clone()),
        relay_origin: if request.direct {
            None
        } else {
            request.relay_origin.clone()
        },
        device_token: token,
        device_token_expires_at: None,
    })
}

fn ca_fp_prefix(bundle: &LinkServeBundle) -> Result<Vec<u8>, LinkServeError> {
    let chain_pem = bundle
        .ca_chain_pem
        .iter()
        .map(|cert| {
            if cert.ends_with('\n') {
                cert.clone()
            } else {
                format!("{cert}\n")
            }
        })
        .collect::<String>();
    let certs = tls::parse_certs(&chain_pem).map_err(|error| {
        LinkServeError::new(LinkServeErrorKind::Transport(map_transport_error(error)))
    })?;
    let Some(first) = certs.first() else {
        return Err(LinkServeError::new(LinkServeErrorKind::InvalidBundle));
    };
    Ok(spl_core::ca::sha256(first.as_ref())[..16].to_vec())
}

fn bridge_names() -> BridgeNames {
    BridgeNames {
        capability_cookie_name: "__solstone_link_cap".to_string(),
        upstream_cookie_prefix: String::new(),
        observer_header_name: "x-solstone-observer".to_string(),
        protocol_version_header_name: "x-solstone-protocol-version".to_string(),
    }
}

fn bridge_policy(tracker: Arc<StatusTracker>) -> BridgePolicy {
    BridgePolicy {
        port: 0,
        capability_gate: CapabilityGate::Disabled,
        stream_response: Arc::new(|_| true),
        local_response: Arc::new(move |head, status| {
            if head.path() != STATUS_PATH {
                return None;
            }
            let body = status_body(&tracker.snapshot(*status));
            Some(LocalResponse {
                status: 200,
                content_type: "application/json".to_string(),
                body,
            })
        }),
        attribution_headers: Arc::new(|_| Vec::new()),
        request_headers: RequestHeaderPolicy::ForwardAll,
        max_request_body_bytes: MAX_REQUEST_BODY_BYTES,
    }
}

fn bridge_policy_for_port(port: u16, tracker: Arc<StatusTracker>) -> BridgePolicy {
    BridgePolicy {
        port,
        ..bridge_policy(tracker)
    }
}

fn status_body(snapshot: &LinkServeStatusSnapshot) -> Vec<u8> {
    let mut root = Map::new();
    root.insert(
        "active_requests".to_string(),
        Value::Number(snapshot.active_requests.into()),
    );
    root.insert(
        "connected_age_seconds".to_string(),
        option_f64(snapshot.connected_age_seconds),
    );
    root.insert("health".to_string(), Value::String(snapshot.health.clone()));
    root.insert(
        "last_connected_at".to_string(),
        option_f64(snapshot.last_connected_at),
    );
    root.insert(
        "last_failure".to_string(),
        snapshot
            .last_failure
            .as_ref()
            .map_or(Value::Null, |failure| {
                let mut item = Map::new();
                item.insert("at".to_string(), number_or_null(failure.at));
                item.insert("detail".to_string(), Value::String(failure.detail.clone()));
                item.insert("reason".to_string(), Value::String(failure.reason.clone()));
                Value::Object(item)
            }),
    );
    root.insert(
        "manager_alive".to_string(),
        Value::Bool(snapshot.manager_alive),
    );
    root.insert("next_retry_at".to_string(), Value::Null);
    root.insert(
        "reconnect_count".to_string(),
        Value::Number(snapshot.reconnect_count.into()),
    );
    root.insert("state".to_string(), Value::String(snapshot.state.clone()));
    serde_json::to_vec(&Value::Object(root)).expect("status snapshot must serialize")
}

fn option_f64(value: Option<f64>) -> Value {
    value.map_or(Value::Null, number_or_null)
}

fn number_or_null(value: f64) -> Value {
    serde_json::Number::from_f64(value).map_or(Value::Null, Value::Number)
}

struct StatusTracker {
    inner: Mutex<StatusTrackerState>,
    clock: Arc<dyn StatusClock>,
}

#[derive(Debug, Default)]
struct StatusTrackerState {
    last_connected_at: Option<f64>,
    last_failure: Option<LinkServeFailure>,
    reconnect_count: u64,
}

impl StatusTracker {
    fn new(clock: Arc<dyn StatusClock>) -> Self {
        Self {
            inner: Mutex::new(StatusTrackerState::default()),
            clock,
        }
    }

    fn carrier_open_succeeded(&self) {
        let mut state = self.inner.lock().expect("status tracker lock");
        state.last_connected_at = Some(self.clock.now_unix_seconds());
    }

    fn carrier_open_failed(&self, error: &TransportError) {
        let mut state = self.inner.lock().expect("status tracker lock");
        state.reconnect_count = state.reconnect_count.saturating_add(1);
        state.last_failure = Some(failure_from_transport(error, self.clock.now_unix_seconds()));
    }

    fn snapshot(&self, bridge: JournalBridgeStatus) -> LinkServeStatusSnapshot {
        let state = self.inner.lock().expect("status tracker lock");
        let now = self.clock.now_unix_seconds();
        let connected_age_seconds = if bridge.carrier_live {
            state
                .last_connected_at
                .map(|connected| (now - connected).max(0.0))
        } else {
            None
        };
        LinkServeStatusSnapshot {
            health: if bridge.listener_active && bridge.carrier_live {
                "healthy".to_string()
            } else {
                "unhealthy".to_string()
            },
            state: if bridge.carrier_live {
                "connected".to_string()
            } else if bridge.listener_active {
                "disconnected".to_string()
            } else {
                "closed".to_string()
            },
            manager_alive: bridge.listener_active,
            connected_age_seconds,
            last_connected_at: state.last_connected_at,
            last_failure: state.last_failure.clone(),
            next_retry_at: None,
            reconnect_count: state.reconnect_count,
            active_requests: bridge.active_requests,
        }
    }
}

fn failure_from_transport(error: &TransportError, at: f64) -> LinkServeFailure {
    let kind = map_transport_error_ref(error);
    LinkServeFailure {
        reason: serve_reason_code(&kind).to_string(),
        detail: serve_failure_detail(&kind).to_string(),
        at,
    }
}

trait StatusClock: Send + Sync {
    fn now_unix_seconds(&self) -> f64;
}

#[derive(Debug)]
struct SystemStatusClock;

impl StatusClock for SystemStatusClock {
    fn now_unix_seconds(&self) -> f64 {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_or(0.0, |duration| duration.as_secs_f64())
    }
}

trait RelayEnrollment: Send + Sync {
    fn enroll<'a>(
        &'a self,
        relay_origin: &'a str,
        instance_id: &'a str,
        home_attestation: &'a str,
    ) -> Pin<Box<dyn Future<Output = Result<String, TransportError>> + Send + 'a>>;
}

#[derive(Debug)]
struct SplRelayEnrollment;

impl RelayEnrollment for SplRelayEnrollment {
    fn enroll<'a>(
        &'a self,
        relay_origin: &'a str,
        instance_id: &'a str,
        home_attestation: &'a str,
    ) -> Pin<Box<dyn Future<Output = Result<String, TransportError>> + Send + 'a>> {
        Box::pin(enroll_device(relay_origin, instance_id, home_attestation))
    }
}

fn map_bridge_start_error(error: BridgeStartError, port: u16) -> LinkServeError {
    match error {
        BridgeStartError::Capability(error) => {
            drop(error);
            LinkServeError::new(LinkServeErrorKind::BridgeCapability)
        }
        BridgeStartError::Bind(error) => LinkServeError::new(LinkServeErrorKind::Bind {
            port,
            addr_in_use: error.kind() == ErrorKind::AddrInUse,
        }),
    }
}

fn map_transport_error(error: TransportError) -> LinkServeTransportErrorKind {
    map_transport_error_ref(&error)
}

fn map_transport_error_ref(error: &TransportError) -> LinkServeTransportErrorKind {
    match error {
        TransportError::Io(_) => LinkServeTransportErrorKind::Io,
        TransportError::Tls(_) => LinkServeTransportErrorKind::Tls,
        TransportError::Crypto(_) => LinkServeTransportErrorKind::Crypto,
        TransportError::Mux(_) => LinkServeTransportErrorKind::Mux,
        TransportError::Http(_) => LinkServeTransportErrorKind::Http,
        TransportError::Json(_) => LinkServeTransportErrorKind::Json,
        TransportError::PairLink(_) => LinkServeTransportErrorKind::PairLink,
        TransportError::Pairing(_) => LinkServeTransportErrorKind::Pairing,
        TransportError::Rejected { status, body: _ } => {
            LinkServeTransportErrorKind::Rejected { status: *status }
        }
        TransportError::Relay(error) => LinkServeTransportErrorKind::Relay(map_relay_error(*error)),
        TransportError::RelayControlRejected { endpoint, status } => {
            LinkServeTransportErrorKind::RelayControlRejected {
                endpoint: map_relay_control_endpoint(*endpoint),
                status: *status,
            }
        }
        TransportError::NoEndpoint => LinkServeTransportErrorKind::NoEndpoint,
        TransportError::NotPaired => LinkServeTransportErrorKind::NotPaired,
        TransportError::LocalOffset => LinkServeTransportErrorKind::LocalOffset,
    }
}

fn map_relay_error(error: RelayError) -> LinkServeRelayErrorKind {
    match error {
        RelayError::HomeOffline => LinkServeRelayErrorKind::HomeOffline,
        RelayError::Unauthorized => LinkServeRelayErrorKind::Unauthorized,
        RelayError::Unpaid => LinkServeRelayErrorKind::Unpaid,
        RelayError::UnknownInstance => LinkServeRelayErrorKind::UnknownInstance,
        RelayError::PairWindowClosed => LinkServeRelayErrorKind::PairWindowClosed,
        RelayError::Overflow => LinkServeRelayErrorKind::Overflow,
        RelayError::Abnormal => LinkServeRelayErrorKind::Abnormal,
        RelayError::UpgradeRejected => LinkServeRelayErrorKind::UpgradeRejected,
        RelayError::Stalled => LinkServeRelayErrorKind::Stalled,
    }
}

fn map_relay_control_endpoint(endpoint: RelayControlEndpoint) -> LinkServeRelayControlEndpoint {
    match endpoint {
        RelayControlEndpoint::EnrollDevice => LinkServeRelayControlEndpoint::EnrollDevice,
        RelayControlEndpoint::TokenRefresh => LinkServeRelayControlEndpoint::TokenRefresh,
    }
}

fn serve_reason_code(kind: &LinkServeTransportErrorKind) -> &'static str {
    match kind {
        LinkServeTransportErrorKind::Io => "io",
        LinkServeTransportErrorKind::Tls => "tls",
        LinkServeTransportErrorKind::Crypto => "crypto",
        LinkServeTransportErrorKind::Mux => "mux",
        LinkServeTransportErrorKind::Http => "http",
        LinkServeTransportErrorKind::Json => "json",
        LinkServeTransportErrorKind::PairLink => "pair-link",
        LinkServeTransportErrorKind::Pairing => "pairing",
        LinkServeTransportErrorKind::Rejected { status: _ } => "rejected",
        LinkServeTransportErrorKind::Relay(LinkServeRelayErrorKind::HomeOffline) => {
            "relay-home-offline"
        }
        LinkServeTransportErrorKind::Relay(LinkServeRelayErrorKind::Unauthorized) => {
            "relay-unauthorized"
        }
        LinkServeTransportErrorKind::Relay(LinkServeRelayErrorKind::Unpaid) => "relay-unpaid",
        LinkServeTransportErrorKind::Relay(LinkServeRelayErrorKind::UnknownInstance) => {
            "relay-unknown-instance"
        }
        LinkServeTransportErrorKind::Relay(LinkServeRelayErrorKind::PairWindowClosed) => {
            "relay-pair-window-closed"
        }
        LinkServeTransportErrorKind::Relay(LinkServeRelayErrorKind::Overflow) => "relay-overflow",
        LinkServeTransportErrorKind::Relay(LinkServeRelayErrorKind::Abnormal) => "relay-abnormal",
        LinkServeTransportErrorKind::Relay(LinkServeRelayErrorKind::UpgradeRejected) => {
            "relay-upgrade-rejected"
        }
        LinkServeTransportErrorKind::Relay(LinkServeRelayErrorKind::Stalled) => "relay-stalled",
        LinkServeTransportErrorKind::RelayControlRejected {
            endpoint,
            status: _,
        } => match endpoint {
            LinkServeRelayControlEndpoint::EnrollDevice => "relay-control-enroll-device",
            LinkServeRelayControlEndpoint::TokenRefresh => "relay-control-token-refresh",
        },
        LinkServeTransportErrorKind::NoEndpoint => "no-endpoint",
        LinkServeTransportErrorKind::NotPaired => "not-paired",
        LinkServeTransportErrorKind::LocalOffset => "local-offset",
    }
}

fn serve_failure_detail(kind: &LinkServeTransportErrorKind) -> &'static str {
    match kind {
        LinkServeTransportErrorKind::Relay(LinkServeRelayErrorKind::HomeOffline) => {
            "relay reports home offline"
        }
        LinkServeTransportErrorKind::Relay(LinkServeRelayErrorKind::Unauthorized)
        | LinkServeTransportErrorKind::RelayControlRejected { .. } => {
            "relay rejected link credentials"
        }
        LinkServeTransportErrorKind::NoEndpoint => "no journal endpoint is available",
        LinkServeTransportErrorKind::NotPaired => "link credentials are missing",
        _ => "link carrier failed",
    }
}

#[cfg(test)]
mod tests {
    use std::net::{IpAddr, Ipv4Addr, SocketAddr, TcpListener, TcpStream};
    use std::sync::Condvar;
    use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
    use std::time::Duration;

    use rcgen::{CertificateParams, KeyPair, PKCS_ECDSA_P256_SHA256};
    use rustls::ServerConfig;
    use rustls::pki_types::{CertificateDer, PrivateKeyDer, PrivatePkcs8KeyDer};
    use serde_json::json;
    use solstone_core_sol_client::seam::LinkServeEndpoint;
    use spl_core::bridge::RequestHead;
    use spl_core::frame::{FLAG_CLOSE, FLAG_DATA, Frame, FrameDecoder, RECOMMENDED_CHUNK};
    use spl_transport::credential::{Credential, EndpointAddr};
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    use tokio::net::TcpListener as TokioTcpListener;
    use tokio::sync::oneshot;
    use tokio_rustls::TlsAcceptor;

    use super::*;

    #[derive(Debug, Clone, PartialEq, Eq)]
    struct EnrollmentCall {
        relay_origin: String,
        instance_id: String,
        home_attestation: String,
    }

    #[derive(Debug, Default)]
    struct FakeEnrollment {
        calls: Arc<Mutex<Vec<EnrollmentCall>>>,
    }

    impl FakeEnrollment {
        fn calls(&self) -> Vec<EnrollmentCall> {
            self.calls.lock().expect("enrollment calls lock").clone()
        }
    }

    impl RelayEnrollment for FakeEnrollment {
        fn enroll<'a>(
            &'a self,
            relay_origin: &'a str,
            instance_id: &'a str,
            home_attestation: &'a str,
        ) -> Pin<Box<dyn Future<Output = Result<String, TransportError>> + Send + 'a>> {
            let calls = self.calls.clone();
            let call = EnrollmentCall {
                relay_origin: relay_origin.to_string(),
                instance_id: instance_id.to_string(),
                home_attestation: home_attestation.to_string(),
            };
            Box::pin(async move {
                calls.lock().expect("enrollment calls lock").push(call);
                Ok("device-token".to_string())
            })
        }
    }

    #[derive(Debug)]
    struct FixedStatusClock(Mutex<f64>);

    impl FixedStatusClock {
        fn new(now: f64) -> Self {
            Self(Mutex::new(now))
        }

        fn set(&self, now: f64) {
            *self.0.lock().expect("clock lock") = now;
        }
    }

    impl StatusClock for FixedStatusClock {
        fn now_unix_seconds(&self) -> f64 {
            *self.0.lock().expect("clock lock")
        }
    }

    #[derive(Debug, Default)]
    struct CountingOpener {
        dials: AtomicUsize,
    }

    impl CountingOpener {
        fn dials(&self) -> usize {
            self.dials.load(Ordering::SeqCst)
        }
    }

    impl CarrierOpener for CountingOpener {
        fn proxy_headers(
            &self,
            upstream_headers: &[(String, String)],
        ) -> Result<Vec<(String, String)>, TransportError> {
            Ok(upstream_headers.to_vec())
        }

        fn dial_carrier(
            &self,
        ) -> Pin<Box<dyn Future<Output = Result<DialedCarrier, TransportError>> + Send + '_>>
        {
            Box::pin(async move {
                self.dials.fetch_add(1, Ordering::SeqCst);
                Err(TransportError::NoEndpoint)
            })
        }
    }

    struct TransportClientOpener {
        client: Arc<TransportClient>,
    }

    impl CarrierOpener for TransportClientOpener {
        fn proxy_headers(
            &self,
            upstream_headers: &[(String, String)],
        ) -> Result<Vec<(String, String)>, TransportError> {
            Ok(upstream_headers.to_vec())
        }

        fn dial_carrier(
            &self,
        ) -> Pin<Box<dyn Future<Output = Result<DialedCarrier, TransportError>> + Send + '_>>
        {
            Box::pin(self.client.dial_carrier())
        }
    }

    fn ca_pem() -> String {
        let key = KeyPair::generate_for(&PKCS_ECDSA_P256_SHA256).expect("test key");
        let params = CertificateParams::new(Vec::<String>::new()).expect("test params");
        params.self_signed(&key).expect("test ca").pem()
    }

    fn serve_request(direct: bool, relay_origin: Option<&str>) -> LinkServeRequest {
        let ca = ca_pem();
        LinkServeRequest {
            label: "laptop".to_string(),
            port: 5015,
            direct,
            relay_origin: relay_origin.map(str::to_string),
            bundle: LinkServeBundle {
                private_key_pem: "PRIVATE\n".to_string(),
                client_cert_pem: "CERT\n".to_string(),
                ca_chain_pem: vec![ca],
                home_attestation: "attestation.jwt".to_string(),
                instance_id: "home-instance".to_string(),
                home_label: "Home".to_string(),
                endpoints: vec![LinkServeEndpoint {
                    host: "192.168.1.10".to_string(),
                    port: 7657,
                }],
                local_endpoints: json!([{"ip": "192.168.1.10", "port": 7657}]),
            },
        }
    }

    fn bridge_status(listener_active: bool, carrier_live: bool) -> JournalBridgeStatus {
        JournalBridgeStatus {
            listener_active,
            contacted: false,
            carrier_live,
            active_requests: 0,
        }
    }

    fn request_head(target: &str) -> RequestHead {
        RequestHead {
            method: "GET".to_string(),
            target: target.to_string(),
            headers: vec![("host".to_string(), "127.0.0.1:5015".to_string())],
        }
    }

    fn unused_loopback_port() -> u16 {
        TcpListener::bind(("127.0.0.1", 0))
            .expect("bind probe")
            .local_addr()
            .expect("probe addr")
            .port()
    }

    fn self_signed_server() -> (CertificateDer<'static>, PrivateKeyDer<'static>) {
        let key = KeyPair::generate_for(&PKCS_ECDSA_P256_SHA256).expect("server key");
        let params = CertificateParams::new(vec!["spl.local".to_string()]).expect("server params");
        let cert = params.self_signed(&key).expect("server cert");
        let cert_der = CertificateDer::from(cert.der().to_vec());
        let key_der = PrivateKeyDer::Pkcs8(PrivatePkcs8KeyDer::from(key.serialize_der()));
        (cert_der, key_der)
    }

    fn server_config(cert: CertificateDer<'static>, key: PrivateKeyDer<'static>) -> ServerConfig {
        ServerConfig::builder_with_provider(Arc::new(rustls::crypto::ring::default_provider()))
            .with_safe_default_protocol_versions()
            .expect("server protocol versions")
            .with_no_client_auth()
            .with_single_cert(vec![cert], key)
            .expect("server config")
    }

    fn transport_credential(pin: Vec<u8>, port: u16) -> Credential {
        let key = KeyPair::generate_for(&PKCS_ECDSA_P256_SHA256).expect("client key");
        let params =
            CertificateParams::new(vec!["transport.test".to_string()]).expect("client cert params");
        let cert = params.self_signed(&key).expect("client cert");
        Credential {
            client_key_pem: key.serialize_pem(),
            client_cert_pem: cert.pem(),
            ca_chain_pem: vec![cert.pem()],
            ca_fp_prefix: pin,
            instance_id: "test-instance".to_string(),
            home_label: "Home".to_string(),
            endpoints: vec![EndpointAddr {
                host: "127.0.0.1".to_string(),
                port,
            }],
            home_attestation: None,
            local_endpoints: None,
            relay_origin: None,
            device_token: None,
            device_token_expires_at: None,
        }
    }

    async fn read_framed_request(
        tls: &mut tokio_rustls::server::TlsStream<tokio::net::TcpStream>,
    ) -> u32 {
        let mut decoder = FrameDecoder::new();
        let mut stream_id = 1u32;
        let mut closed = false;
        let mut buf = [0u8; 4096];
        while !closed {
            let n = tls.read(&mut buf).await.expect("read framed request");
            if n == 0 {
                break;
            }
            decoder.feed(&buf[..n]);
            for frame in decoder.drain().expect("decode request frame") {
                stream_id = frame.stream_id;
                if frame.flags & FLAG_CLOSE != 0 {
                    closed = true;
                }
            }
        }
        stream_id
    }

    async fn write_response_frame(
        tls: &mut tokio_rustls::server::TlsStream<tokio::net::TcpStream>,
        stream_id: u32,
        flags: u8,
        payload: Vec<u8>,
    ) {
        let frame = Frame::new(stream_id, flags, payload);
        tls.write_all(&frame.encode().expect("encode response frame"))
            .await
            .expect("write response frame");
        tls.flush().await.expect("flush response frame");
    }

    async fn serve_latched_stream(
        listener: TokioTcpListener,
        acceptor: TlsAcceptor,
        release: oneshot::Receiver<()>,
        released: Arc<AtomicBool>,
    ) {
        let (tcp, _) = listener.accept().await.expect("accept transport peer");
        let mut tls = acceptor.accept(tcp).await.expect("accept tls");
        let stream_id = read_framed_request(&mut tls).await;
        let mut first_chunk = vec![b'x'; RECOMMENDED_CHUNK * 2 + 1];
        first_chunk[0] = b'A';
        let content_length = first_chunk.len() + 1;
        let head = format!(
            "HTTP/1.1 200 OK\r\nContent-Type: application/octet-stream\r\nContent-Length: {content_length}\r\n\r\n"
        );
        write_response_frame(&mut tls, stream_id, FLAG_DATA, head.into_bytes()).await;
        write_response_frame(&mut tls, stream_id, FLAG_DATA, first_chunk).await;
        release.await.expect("release latch");
        released.store(true, Ordering::SeqCst);
        write_response_frame(&mut tls, stream_id, FLAG_DATA | FLAG_CLOSE, vec![b'B']).await;
        let _ = tls.shutdown().await;
    }

    async fn http_get(port: u16, target: &str) -> (SocketAddr, String) {
        let mut stream = tokio::net::TcpStream::connect(("127.0.0.1", port))
            .await
            .expect("connect bridge");
        let peer = stream.peer_addr().expect("bridge peer addr");
        let request =
            format!("GET {target} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n");
        stream
            .write_all(request.as_bytes())
            .await
            .expect("write request");
        let mut response = Vec::new();
        tokio::time::timeout(Duration::from_secs(2), stream.read_to_end(&mut response))
            .await
            .expect("response timeout")
            .expect("read response");
        (peer, String::from_utf8_lossy(&response).into_owned())
    }

    #[test]
    fn direct_credentials_have_no_relay_fields_and_do_not_enroll() {
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .expect("runtime");
        let enrollment = Arc::new(FakeEnrollment::default());
        let request = serve_request(true, Some("https://poisoned.invalid"));

        let credential = runtime
            .block_on(credential_from_request(&request, enrollment.clone()))
            .expect("direct credential");

        assert!(credential.relay_origin.is_none());
        assert!(credential.device_token.is_none());
        assert!(credential.device_token_expires_at.is_none());
        assert!(enrollment.calls().is_empty());
    }

    #[test]
    fn relay_credentials_enroll_at_serve_time_in_memory() {
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .expect("runtime");
        let enrollment = Arc::new(FakeEnrollment::default());
        let request = serve_request(false, Some("https://relay.example"));

        let credential = runtime
            .block_on(credential_from_request(&request, enrollment.clone()))
            .expect("relay credential");

        assert_eq!(
            credential.relay_origin.as_deref(),
            Some("https://relay.example")
        );
        assert_eq!(credential.device_token.as_deref(), Some("device-token"));
        assert!(credential.device_token_expires_at.is_none());
        assert_eq!(
            enrollment.calls(),
            vec![EnrollmentCall {
                relay_origin: "https://relay.example".to_string(),
                instance_id: "home-instance".to_string(),
                home_attestation: "attestation.jwt".to_string(),
            }]
        );
    }

    #[test]
    fn status_tracker_uses_one_shared_update_point_for_times_and_failures() {
        let clock = Arc::new(FixedStatusClock::new(100.0));
        let tracker = StatusTracker::new(clock.clone());
        tracker.carrier_open_failed(&TransportError::NoEndpoint);
        let failed = tracker.snapshot(bridge_status(true, false));
        assert_eq!(failed.reconnect_count, 1);
        assert_eq!(
            failed.last_failure.as_ref().map(|failure| failure.at),
            Some(100.0)
        );

        clock.set(110.0);
        tracker.carrier_open_succeeded();
        clock.set(115.5);
        let connected = tracker.snapshot(bridge_status(true, true));
        assert_eq!(connected.last_connected_at, Some(110.0));
        assert_eq!(connected.connected_age_seconds, Some(5.5));
        assert_eq!(connected.reconnect_count, 1);
    }

    #[test]
    fn bridge_policy_status_is_local_and_attribution_hook_is_empty() {
        let tracker = Arc::new(StatusTracker::new(Arc::new(FixedStatusClock::new(10.0))));
        let policy = bridge_policy_for_port(5015, tracker);
        let status = bridge_status(true, false);
        assert_eq!(policy.port, 5015);
        assert!((policy.stream_response)(&request_head("/ordinary")));
        let local = (policy.local_response)(&request_head(STATUS_PATH), &status)
            .expect("status local response");
        assert_eq!(local.status, 200);
        assert_eq!(local.content_type, "application/json");
        let body: serde_json::Value =
            serde_json::from_slice(&local.body).expect("status json body");
        assert_eq!(
            body.as_object()
                .expect("status object")
                .keys()
                .cloned()
                .collect::<Vec<_>>(),
            vec![
                "active_requests",
                "connected_age_seconds",
                "health",
                "last_connected_at",
                "last_failure",
                "manager_alive",
                "next_retry_at",
                "reconnect_count",
                "state",
            ]
        );
        assert!((policy.local_response)(&request_head("/not-status"), &status).is_none());
        assert!((policy.attribution_headers)(&request_head(STATUS_PATH)).is_empty());
    }

    #[test]
    fn solstone_adapter_adds_no_wildcard_bind_host_literal() {
        let source = include_str!("serve.rs");
        let wildcard_v4 = ["0", "0", "0", "0"].join(".");
        let wildcard_v6 = ":".repeat(2);
        let named_loopback = format!("{}{}", "local", "host");
        for host in [wildcard_v4, wildcard_v6, named_loopback] {
            assert!(!source.contains(&format!("{host:?}")));
        }
    }

    #[test]
    fn status_request_does_not_open_carrier_but_ordinary_request_does() {
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .expect("runtime");
        runtime.block_on(async {
            let port = unused_loopback_port();
            let opener = Arc::new(CountingOpener::default());
            let tracker = Arc::new(StatusTracker::new(Arc::new(FixedStatusClock::new(0.0))));
            let handle = journal_bridge::start(JournalBridgeConfig {
                opener: opener.clone(),
                bridge_names: bridge_names(),
                endpoint_hosts: vec!["192.168.1.10".to_string()],
                policy: bridge_policy_for_port(port, tracker),
            })
            .await
            .expect("bridge start");
            let bound = handle.port();

            let (peer, status_response) = http_get(bound, STATUS_PATH).await;
            assert_eq!(
                peer,
                SocketAddr::new(IpAddr::V4(Ipv4Addr::LOCALHOST), bound)
            );
            assert!(status_response.starts_with("HTTP/1.1 200"));
            assert!(status_response.contains("Content-Type: application/json\r\n"));
            assert!(status_response.contains("Content-Length: "));
            assert_eq!(opener.dials(), 0);

            let (_peer, ordinary_response) = http_get(bound, "/ordinary").await;
            assert!(ordinary_response.starts_with("HTTP/1.1 502"));
            assert!(opener.dials() >= 1);

            handle.shutdown_and_wait().await;
        });
    }

    #[test]
    fn proxied_response_streams_before_upstream_completion() {
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .expect("runtime");
        runtime.block_on(async {
            let (server_cert, server_key) = self_signed_server();
            let pin = spl_core::ca::sha256(server_cert.as_ref())[..16].to_vec();
            let listener = TokioTcpListener::bind(("127.0.0.1", 0))
                .await
                .expect("bind transport peer");
            let transport_port = listener.local_addr().expect("transport addr").port();
            let client = Arc::new(
                TransportClient::new(transport_credential(pin, transport_port), None)
                    .expect("transport client"),
            );
            let acceptor = TlsAcceptor::from(Arc::new(server_config(server_cert, server_key)));
            let (release_tx, release_rx) = oneshot::channel();
            let released = Arc::new(AtomicBool::new(false));
            let server = tokio::spawn(serve_latched_stream(
                listener,
                acceptor,
                release_rx,
                released.clone(),
            ));
            let bridge_port = unused_loopback_port();
            let tracker = Arc::new(StatusTracker::new(Arc::new(FixedStatusClock::new(0.0))));
            let handle = journal_bridge::start(JournalBridgeConfig {
                opener: Arc::new(TransportClientOpener { client }),
                bridge_names: bridge_names(),
                endpoint_hosts: vec!["127.0.0.1".to_string()],
                policy: bridge_policy_for_port(bridge_port, tracker),
            })
            .await
            .expect("bridge start");
            let bound = handle.port();

            let mut stream = tokio::net::TcpStream::connect(("127.0.0.1", bound))
                .await
                .expect("connect bridge");
            let request = format!(
                "GET /ordinary HTTP/1.1\r\nHost: 127.0.0.1:{bound}\r\nConnection: close\r\n\r\n"
            );
            stream
                .write_all(request.as_bytes())
                .await
                .expect("write request");

            let mut response = Vec::new();
            let header_end = loop {
                let mut buf = [0u8; 1024];
                let n = tokio::time::timeout(Duration::from_secs(2), stream.read(&mut buf))
                    .await
                    .expect("response head timeout")
                    .expect("read response head");
                assert_ne!(n, 0, "bridge closed before response head");
                response.extend_from_slice(&buf[..n]);
                if let Some(index) = response.windows(4).position(|window| window == b"\r\n\r\n") {
                    break index + 4;
                }
            };
            assert!(String::from_utf8_lossy(&response[..header_end]).starts_with("HTTP/1.1 200"));
            let mut body = response.split_off(header_end);

            // A buffering implementation cannot satisfy this read until the
            // upstream producer emits `B` or closes the response after the latch.
            if body.is_empty() {
                body.resize(1, 0);
                tokio::time::timeout(Duration::from_secs(2), stream.read_exact(&mut body[..1]))
                    .await
                    .expect("first streamed body byte timeout")
                    .expect("read first streamed body byte");
            }
            assert_eq!(body[0], b'A');
            assert!(!released.load(Ordering::SeqCst));

            release_tx.send(()).expect("release upstream stream");
            tokio::time::timeout(Duration::from_secs(2), stream.read_to_end(&mut body))
                .await
                .expect("response completion timeout")
                .expect("read response completion");
            assert_eq!(body.last().copied(), Some(b'B'));
            assert!(released.load(Ordering::SeqCst));

            server.await.expect("server task");
            handle.shutdown_and_wait().await;
        });
    }

    struct GateShutdown {
        released: Mutex<bool>,
        gate: Condvar,
    }

    impl GateShutdown {
        fn new() -> Self {
            Self {
                released: Mutex::new(false),
                gate: Condvar::new(),
            }
        }

        fn release(&self) {
            *self.released.lock().expect("shutdown lock") = true;
            self.gate.notify_all();
        }
    }

    impl ShutdownSignal for GateShutdown {
        fn wait(&self) {
            let mut released = self.released.lock().expect("shutdown lock");
            while !*released {
                released = self.gate.wait(released).expect("shutdown wait");
            }
        }
    }

    /// Issue one HTTP/1.1 GET over loopback and read the whole response.
    ///
    /// Returns `None` when the peer accepts the connection but never answers —
    /// the exact shape of the regression below, which must not be reported as a
    /// hang or a panic.
    fn loopback_get(port: u16, target: &str) -> Option<String> {
        use std::io::{Read as _, Write as _};

        let addr = SocketAddr::new(IpAddr::V4(Ipv4Addr::LOCALHOST), port);
        let mut stream = TcpStream::connect_timeout(&addr, Duration::from_secs(5)).ok()?;
        stream
            .set_read_timeout(Some(Duration::from_secs(5)))
            .expect("read timeout");
        stream
            .write_all(
                format!(
                    "GET {target} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n"
                )
                .as_bytes(),
            )
            .ok()?;

        let mut raw = Vec::new();
        let mut chunk = [0_u8; 4096];
        loop {
            match stream.read(&mut chunk) {
                Ok(0) => break,
                Ok(read) => {
                    raw.extend_from_slice(&chunk[..read]);
                    if body_is_complete(&raw) {
                        break;
                    }
                }
                Err(_) => break,
            }
        }
        if raw.is_empty() {
            return None;
        }
        Some(String::from_utf8_lossy(&raw).into_owned())
    }

    /// True once `raw` holds a full header block plus its declared body.
    fn body_is_complete(raw: &[u8]) -> bool {
        let text = String::from_utf8_lossy(raw);
        let Some(header_end) = text.find("\r\n\r\n") else {
            return false;
        };
        let declared = text[..header_end].lines().find_map(|line| {
            let (name, value) = line.split_once(':')?;
            name.trim()
                .eq_ignore_ascii_case("content-length")
                .then(|| value.trim().parse::<usize>().ok())?
        });
        declared.is_some_and(|length| raw.len() >= header_end + 4 + length)
    }

    fn resident_serve_request(port: u16) -> LinkServeRequest {
        let key = KeyPair::generate_for(&PKCS_ECDSA_P256_SHA256).expect("client key");
        let params =
            CertificateParams::new(vec!["client.test".to_string()]).expect("client params");
        let cert = params.self_signed(&key).expect("client cert");
        LinkServeRequest {
            label: "laptop".to_string(),
            port,
            direct: true,
            relay_origin: None,
            bundle: LinkServeBundle {
                private_key_pem: key.serialize_pem(),
                client_cert_pem: cert.pem(),
                ca_chain_pem: vec![ca_pem()],
                home_attestation: "attestation.jwt".to_string(),
                instance_id: "home-instance".to_string(),
                home_label: "Home".to_string(),
                endpoints: vec![LinkServeEndpoint {
                    host: "127.0.0.1".to_string(),
                    port: unused_loopback_port(),
                }],
                local_endpoints: json!([{"ip": "127.0.0.1", "port": 7657}]),
            },
        }
    }

    #[test]
    fn resident_serve_answers_the_local_status_route_while_on_duty() {
        // Regression: `start` spawns the bridge accept loop onto its runtime and
        // `serve` then parks the calling thread in a blocking `ShutdownSignal::wait`
        // for the process's whole lifetime. On a current-thread runtime nothing
        // polls that accept loop until shutdown, so the listener binds — the kernel
        // completes handshakes from the backlog, so the port looks healthy — while
        // every request hangs and returns zero bytes.
        //
        // This must drive the real session lifecycle: `serve` on its own thread and
        // a genuine loopback request. Every other test here drives the bridge under
        // its own `block_on`, which keeps the runtime driven and hides this
        // entirely. No journal, relay, or peer is involved: the status route is
        // answered locally and never forwarded upstream.
        let port = unused_loopback_port();
        let session = SplLinkServeRunner
            .start(resident_serve_request(port))
            .expect("serve session starts");
        assert_eq!(session.bound_port(), port);

        let shutdown = Arc::new(GateShutdown::new());
        let serve_shutdown = Arc::clone(&shutdown);
        let resident = std::thread::spawn(move || session.serve(serve_shutdown.as_ref()));

        let response = loopback_get(port, STATUS_PATH);
        shutdown.release();
        resident
            .join()
            .expect("resident thread")
            .expect("clean shutdown");

        let response = response.expect(
            "status route returned no bytes — the bridge accept loop is not being polled while \
             the resident command is on duty",
        );
        assert!(
            response.starts_with("HTTP/1.1 200"),
            "unexpected status response: {response}"
        );
        // `manager_alive` is how `status_body` surfaces the bridge's
        // `listener_active`; true here proves the listener is genuinely on duty
        // and not merely bound.
        assert!(
            response.contains("\"manager_alive\":true"),
            "status payload should report an active listener: {response}"
        );
    }
}

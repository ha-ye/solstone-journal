// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 sol pbc

//! The standalone SPL home-service runtime.
//!
//! The service-facing I/O adapters are intentionally kept out of this first
//! layer. Buffering, admission, and health vocabulary are pure so every
//! transport implementation shares the same owner-visible behaviour.

mod admission;
mod authenticated_blob;
mod authorized_client_ledger;
mod authorized_clients;
mod blob_archive;
mod blob_content_type;
mod blob_receive;
mod health;
mod link_state_files;
mod loopback_pipe;
mod posture_gate;
mod reconnect_backoff;
mod relay_control;
mod relay_health;
mod relay_status_failure;
mod relay_websocket;
mod service;
mod service_shutdown;
mod service_transition;
mod tunnel_route;
mod ws_buffer;
mod ws_sink;

pub use admission::BlobAdmissionGate;
pub use authenticated_blob::{
    AuthenticatedBlobError, PreparedAuthenticatedBlob, prepare_authenticated_blob,
};
pub use authorized_client_ledger::{AuthorizedClientLedger, BrowserLedgerLookup};
pub use authorized_clients::{
    AuthorizedClients, BrowserUploadAuthorization, ClientEntry, LedgerStatus,
    parse_authorized_clients,
};
pub use blob_archive::{
    BlobArchiveEntry, BlobArchiveError, BlobArchiveMetadata, ValidatedBlobArchive,
    parse_blob_archive,
};
pub use blob_content_type::blob_content_type;
pub use blob_receive::{
    BlobDeps, BlobError, BlobIngest, BlobIngestError, BlobIngestFuture, BlobIngestStatus,
    BlobReceiveTiming, CallosumEmit, receive_blob,
};
pub use health::{
    LINK_HEALTH_EVENT, OFFLINE_TUNNEL_REASONS, REASON_HOME_MISSING_MOBILE,
    REASON_LOCAL_PRIVATE_LISTENER_UNREACHABLE, REASON_RELAY_ADMISSION_SATURATED,
    REASON_RELAY_TUNNEL_REJECTED, REASON_RELAY_TUNNEL_UNREACHABLE, REASON_SERVICE_TOKEN_REJECTED,
};
pub use link_state_files::{
    LinkServiceToken, LinkServiceTokenRead, LinkState, LinkStateRead, load_link_service_token,
    load_link_state,
};
pub use loopback_pipe::{
    TCP_TO_WS_READ_MAX, TunnelPipeError, TunnelPipeProgress, pipe_loopback, pipe_tunnel,
};
pub use posture_gate::{
    PostureGate, PostureInput, RelayBlocked, RelayDecision, RelayPermit, ServiceToken, TokenInput,
};
pub use reconnect_backoff::{
    INITIAL_RECONNECT_BASE, MAX_RECONNECT_BASE, ReconnectBackoffError, ReconnectSchedule,
    schedule_reconnect,
};
pub use relay_control::{
    ListenControl, bearer_authorization_value, parse_listen_control, relay_tunnel_url,
    websocket_endpoint,
};
pub use relay_health::{RelayHealth, RelayHealthState, RelayTunnelFailure};
pub use relay_status_failure::{RelayTunnelFailureSignal, classify_relay_tunnel_failure};
pub use relay_websocket::{
    RelayWebSocket, RelayWebSocketError, RelayWebSocketReader, RelayWebSocketWriter,
};
pub use service::{
    POSTURE_POLL_INTERVAL, RelayRunTask, RelayServiceToken, ServiceDeps, ServiceError, ServicePoll,
    StartedRelay, run_service,
};
pub use service_shutdown::{RelayStop, ServiceShutdownError, stop_relay_run};
pub use service_transition::{
    PostureObservation, ServiceAction, ServiceLifecycle, TokenObservation, transition,
};
pub use tunnel_route::{TunnelRoute, route_tunnel_prefix};
pub use ws_buffer::{BufferedWsReader, WsBufferError, WsByteSource, WsClosed};
pub use ws_sink::WsByteSink;

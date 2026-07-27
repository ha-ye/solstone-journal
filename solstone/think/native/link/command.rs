// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 sol pbc

use std::collections::BTreeMap;
use std::fs::{self, File, OpenOptions};
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use chrono::{DateTime, Utc};
use serde_json::{Map, Value};
use spl_core::pairlink::{PairLinkError, ParsedPairLink};

use crate::command::{CommandContext, CommandOutput};
use crate::json_format::{json_compact_ascii, json_pretty_ascii};
use crate::seam::{
    LinkJoinCredential, LinkJoinDirectRequest, LinkJoinPairTarget, LinkJoinPairingError,
    LinkJoinPairingErrorKind, LinkJoinRelayControlEndpoint, LinkJoinRelayErrorKind,
    LinkJoinRelayRequest,
};

const HELP: &str = "usage: sol link join [-h] [--home HOME] --code CODE [--as AS_ROLE]\n                     [--label LABEL]\n\noptions:\n  -h, --help     show this help message and exit\n  --home HOME    Receiver base URL\n  --code CODE    pair-link URL\n  --as AS_ROLE   Optional tag to join as\n  --label LABEL  Local credentials label (defaults to this machine's hostname)\n";
const USAGE: &str = "usage: sol link join [-h] [--home HOME] --code CODE [--as AS_ROLE]\n                     [--label LABEL]\n";
const DEFAULT_CLIENT_LABEL: &str = "linked-system";
const PAIR_LINK_PREFIX: &str = "https://go.solstone.app/p#";
const LOCAL_ENDPOINTS_MAX_BYTES: usize = 16 * 1024;
const PEER_STATE_GUIDANCE: &str = "Peer join requires an initialized link identity. Run 'sol call link pair' on this journal first, then retry.\n";
const BUNDLE_FILES: &[&str] = &[
    "private.pem",
    "cert.pem",
    "chain.pem",
    "home_attestation.jwt",
    "peer.json",
];

#[must_use]
pub fn link_join(ctx: CommandContext<'_>) -> CommandOutput {
    let parsed = match parse_args(ctx.args) {
        Ok(parsed) => parsed,
        Err(error) => return argparse_error(error),
    };
    if parsed.help {
        return CommandOutput::success(HELP);
    }
    if parsed.code.is_none() {
        return argparse_error("the following arguments are required: --code".to_string());
    }
    if let Some(unknown) = parsed.unknown {
        return argparse_error(format!("unrecognized arguments: {unknown}"));
    }

    let as_role = parsed.as_role.unwrap_or_default();
    if !matches!(as_role.as_str(), "" | "phone" | "observer" | "peer") {
        return CommandOutput::failure("invalid role; expected one of: phone, observer, peer\n", 2);
    }

    let label = match parsed.label {
        Some(label) => {
            if let Some(error) = label_error(&label) {
                return CommandOutput::failure(format!("{error}\n"), 2);
            }
            label
        }
        None => hostname_client_label(ctx.env),
    };

    let Some(seam) = ctx.link_pairing else {
        return CommandOutput::failure("Link pairing seam is unavailable.\n", 1);
    };

    let pair_request = match parse_pair_request(
        parsed
            .code
            .as_deref()
            .expect("code presence checked")
            .trim(),
        parsed.home.as_deref(),
    ) {
        Ok(pair_request) => pair_request,
        Err(error) => return CommandOutput::failure(format!("{error}\n"), 1),
    };

    let mut additional_fields = Map::new();
    let is_peer = as_role == "peer";
    if is_peer {
        let Some(journal_root) = ctx.journal_root else {
            return CommandOutput::failure(PEER_STATE_GUIDANCE, 1);
        };
        let Some(instance_id) = load_peer_sender_instance_id(journal_root) else {
            return CommandOutput::failure(PEER_STATE_GUIDANCE, 1);
        };
        additional_fields.insert("sender_instance_id".to_string(), Value::String(instance_id));
    }

    let prechecked_bundle_dir = if is_peer {
        None
    } else {
        match observer_bundle_dir(&label, ctx.env) {
            Ok(bundle_dir) => {
                if path_lexists(&bundle_dir) {
                    return CommandOutput::failure(
                        format!("{}\n", existing_path_message(&bundle_dir)),
                        1,
                    );
                }
                Some(bundle_dir)
            }
            Err(error) => return CommandOutput::failure(format!("{error}\n"), 1),
        }
    };

    let credential = match pair_request {
        PairRequest::Direct(request) => {
            let request = LinkJoinDirectRequest {
                targets: request.targets,
                nonce_hex: request.nonce_hex,
                ca_fp_prefix: request.ca_fp_prefix,
                device_label: label.clone(),
                additional_fields,
            };
            seam.pair_direct(request)
        }
        PairRequest::Relay(request) => {
            let request = LinkJoinRelayRequest {
                relay_origin: request.relay_origin,
                secret: request.secret,
                ca_fp_spki: request.ca_fp_spki,
                device_label: label.clone(),
                additional_fields,
            };
            seam.pair_relay(request)
        }
    };
    let credential = match credential {
        Ok(credential) => credential,
        Err(error) => return CommandOutput::failure(format!("{}\n", pairing_error_text(error)), 1),
    };

    if let Err(error) = validate_credential(&credential) {
        return CommandOutput::failure(format!("{error}\n"), 1);
    }
    let local_endpoints = match normalized_local_endpoints(&credential.local_endpoints) {
        Ok(value) => value,
        Err(error) => return CommandOutput::failure(format!("{error}\n"), 1),
    };
    if json_compact_ascii(&local_endpoints).len() > LOCAL_ENDPOINTS_MAX_BYTES {
        return CommandOutput::failure("Pair response local_endpoints is too large.\n", 1);
    }

    let bundle_dir = if is_peer {
        let Some(journal_root) = ctx.journal_root else {
            return CommandOutput::failure(PEER_STATE_GUIDANCE, 1);
        };
        if let Some(error) = validate_instance_id(&credential.instance_id) {
            return CommandOutput::failure(format!("{error}\n"), 1);
        }
        journal_root.join("peers").join(&credential.instance_id)
    } else {
        prechecked_bundle_dir.expect("observer bundle dir was resolved before pairing")
    };

    let chain_pem = join_chain(&credential.ca_chain_pem);
    let peer_json = peer_json(
        &label,
        now_utc(ctx.clock),
        &credential,
        local_endpoints,
        is_peer,
    );
    let mut files = BTreeMap::new();
    files.insert(
        "private.pem".to_string(),
        credential.client_key_pem.as_bytes().to_vec(),
    );
    files.insert(
        "cert.pem".to_string(),
        credential.client_cert_pem.as_bytes().to_vec(),
    );
    files.insert("chain.pem".to_string(), chain_pem.into_bytes());
    files.insert(
        "home_attestation.jwt".to_string(),
        credential
            .home_attestation
            .as_ref()
            .expect("home_attestation checked")
            .as_bytes()
            .to_vec(),
    );
    files.insert("peer.json".to_string(), peer_json.into_bytes());

    if let Err(error) = publish_bundle_atomic(&bundle_dir, &files) {
        let message = if error.kind() == io::ErrorKind::AlreadyExists {
            spent_existing_path_message(&bundle_dir)
        } else {
            error.to_string()
        };
        return CommandOutput::failure(format!("{message}\n"), 1);
    }

    let suffix = if is_peer { " as peer" } else { "" };
    CommandOutput::success(format!(
        "Linked {label}{suffix}.\nCredentials: {}\n",
        bundle_dir.display()
    ))
}

#[derive(Debug, Default, Clone, PartialEq, Eq)]
struct ParsedArgs {
    home: Option<String>,
    code: Option<String>,
    as_role: Option<String>,
    label: Option<String>,
    help: bool,
    unknown: Option<String>,
}

fn parse_args(args: &[String]) -> Result<ParsedArgs, String> {
    let mut parsed = ParsedArgs::default();
    let mut index = 0;
    while index < args.len() {
        let token = &args[index];
        if token == "-h" || token == "--help" {
            parsed.help = true;
        } else if let Some(value) = token.strip_prefix("--home=") {
            parsed.home = Some(value.to_string());
        } else if token == "--home" {
            index += 1;
            parsed.home = Some(take_value(args, index, "--home")?.to_string());
        } else if let Some(value) = token.strip_prefix("--code=") {
            parsed.code = Some(value.to_string());
        } else if token == "--code" {
            index += 1;
            parsed.code = Some(take_value(args, index, "--code")?.to_string());
        } else if let Some(value) = token.strip_prefix("--as=") {
            parsed.as_role = Some(value.to_string());
        } else if token == "--as" {
            index += 1;
            parsed.as_role = Some(take_value(args, index, "--as")?.to_string());
        } else if let Some(value) = token.strip_prefix("--label=") {
            parsed.label = Some(value.to_string());
        } else if token == "--label" {
            index += 1;
            parsed.label = Some(take_value(args, index, "--label")?.to_string());
        } else if parsed.unknown.is_none() {
            parsed.unknown = Some(token.clone());
        }
        index += 1;
    }
    Ok(parsed)
}

fn take_value<'a>(args: &'a [String], index: usize, option: &str) -> Result<&'a str, String> {
    let Some(value) = args.get(index).map(String::as_str) else {
        return Err(format!("argument {option}: expected one argument"));
    };
    if value.starts_with('-') {
        return Err(format!("argument {option}: expected one argument"));
    }
    Ok(value)
}

fn argparse_error(error: String) -> CommandOutput {
    CommandOutput::failure(format!("{USAGE}sol link join: error: {error}\n"), 2)
}

#[derive(Debug, Clone, PartialEq, Eq)]
enum PairRequest {
    Direct(DirectPairRequest),
    Relay(RelayPairRequest),
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct DirectPairRequest {
    targets: Vec<LinkJoinPairTarget>,
    nonce_hex: String,
    ca_fp_prefix: Vec<u8>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct RelayPairRequest {
    relay_origin: String,
    secret: Vec<u8>,
    ca_fp_spki: Vec<u8>,
}

fn parse_pair_request(code: &str, home: Option<&str>) -> Result<PairRequest, String> {
    if !code.starts_with(PAIR_LINK_PREFIX) {
        return Err(format!(
            "Pair code did not match an accepted form. Use a pair-link like {PAIR_LINK_PREFIX}... from 'sol call link pair'."
        ));
    }
    match spl_core::pairlink::parse(code) {
        Ok(ParsedPairLink::Direct(link)) => {
            let targets = if let Some(home) = home {
                vec![parse_home_target(home)?]
            } else {
                link.candidates
                    .into_iter()
                    .map(|endpoint| LinkJoinPairTarget {
                        host: endpoint.host,
                        port: endpoint.port,
                    })
                    .collect()
            };
            Ok(PairRequest::Direct(DirectPairRequest {
                targets,
                nonce_hex: link.nonce_hex,
                ca_fp_prefix: link.ca_fp_prefix,
            }))
        }
        Ok(ParsedPairLink::Relay(link)) => Ok(PairRequest::Relay(RelayPairRequest {
            relay_origin: link.relay_origin,
            secret: link.s.to_vec(),
            ca_fp_spki: link.ca_fp_spki,
        })),
        Err(PairLinkError::DisallowedDirectIpv4 { address: _ }) => Err(
            "Pair-link points at an address outside the local network this joiner will dial."
                .to_string(),
        ),
        Err(
            PairLinkError::MissingFragment
            | PairLinkError::Crockford(_)
            | PairLinkError::UnsupportedVersion(_)
            | PairLinkError::UnsupportedAddressType(_)
            | PairLinkError::UnknownCaFpTag(_)
            | PairLinkError::BadRelayOrigin
            | PairLinkError::Truncated { .. }
            | PairLinkError::LengthMismatch { .. }
            | PairLinkError::InvalidCandidateCount { .. },
        ) => Err(malformed_pair_link_message()),
    }
}

fn malformed_pair_link_message() -> String {
    format!(
        "Malformed pair-link. Use the full {PAIR_LINK_PREFIX}... value from the pairing output."
    )
}

fn parse_home_target(home: &str) -> Result<LinkJoinPairTarget, String> {
    let Some((_, rest)) = home.split_once("://") else {
        return Err("Pair-link target missing host.".to_string());
    };
    let authority = rest
        .split(['/', '?', '#'])
        .next()
        .unwrap_or_default()
        .rsplit('@')
        .next()
        .unwrap_or_default();
    if authority.is_empty() {
        return Err("Pair-link target missing host.".to_string());
    }
    if let Some(after_bracket) = authority
        .strip_prefix('[')
        .and_then(|value| value.split_once(']'))
    {
        let host = after_bracket.0;
        if host.is_empty() {
            return Err("Pair-link target missing host.".to_string());
        }
        let port_text = after_bracket
            .1
            .strip_prefix(':')
            .ok_or_else(|| "Pair-link target missing explicit port.".to_string())?;
        let port = parse_explicit_port(port_text)?;
        return Ok(LinkJoinPairTarget {
            host: host.to_string(),
            port,
        });
    }
    let Some((host, port_text)) = authority.rsplit_once(':') else {
        return Err("Pair-link target missing explicit port.".to_string());
    };
    if host.is_empty() {
        return Err("Pair-link target missing host.".to_string());
    }
    let port = parse_explicit_port(port_text)?;
    Ok(LinkJoinPairTarget {
        host: host.to_string(),
        port,
    })
}

fn parse_explicit_port(value: &str) -> Result<u16, String> {
    value
        .parse::<u16>()
        .map_err(|_| "Pair-link target missing explicit port.".to_string())
}

fn label_error(label: &str) -> Option<&'static str> {
    if label.is_empty() {
        return Some("--label must not be empty");
    }
    if label.chars().count() > 80 {
        return Some("--label must be 80 characters or fewer");
    }
    if label.contains('/') || label.contains('\\') {
        return Some("--label must not contain path separators");
    }
    if label.contains("..") {
        return Some("--label must not contain '..'");
    }
    if label.starts_with('.') {
        return Some("--label must not start with '.'");
    }
    if !label
        .chars()
        .all(|ch| ch.is_ascii_alphanumeric() || matches!(ch, '_' | '.' | '-'))
    {
        return Some("--label may contain only letters, numbers, '-', '_', and '.'");
    }
    None
}

fn sanitize_client_label(raw: &str) -> String {
    if !raw
        .chars()
        .any(|ch| ch.is_ascii_alphanumeric() || matches!(ch, '_' | '.' | '-'))
    {
        return String::new();
    }
    let mut label = String::new();
    let mut dot_run = 0usize;
    for ch in raw.chars() {
        let mapped = if ch.is_ascii_alphanumeric() || matches!(ch, '_' | '.' | '-') {
            ch
        } else {
            '-'
        };
        if mapped == '.' {
            dot_run += 1;
            if dot_run == 2 {
                label.pop();
                label.push('-');
            } else if dot_run < 2 {
                label.push(mapped);
            }
        } else {
            dot_run = 0;
            label.push(mapped);
        }
    }
    let trimmed = label
        .trim_start_matches('.')
        .chars()
        .take(80)
        .collect::<String>();
    if trimmed.is_empty() || label_error(&trimmed).is_some() {
        String::new()
    } else {
        trimmed
    }
}

fn hostname_client_label(env: &BTreeMap<String, String>) -> String {
    env.get("HOSTNAME")
        .or_else(|| env.get("COMPUTERNAME"))
        .map_or_else(String::new, |value| sanitize_client_label(value))
        .if_empty(DEFAULT_CLIENT_LABEL)
}

trait IfEmpty {
    fn if_empty(self, fallback: &str) -> String;
}

impl IfEmpty for String {
    fn if_empty(self, fallback: &str) -> String {
        if self.is_empty() {
            fallback.to_string()
        } else {
            self
        }
    }
}

fn load_peer_sender_instance_id(journal_root: &Path) -> Option<String> {
    let text = fs::read_to_string(journal_root.join("link").join("state.json")).ok()?;
    let value: Value = serde_json::from_str(&text).ok()?;
    value
        .get("instance_id")
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
        .map(str::to_string)
}

fn observer_bundle_dir(label: &str, env: &BTreeMap<String, String>) -> Result<PathBuf, String> {
    let base = env
        .get("XDG_CONFIG_HOME")
        .filter(|value| !value.is_empty())
        .map_or_else(
            || {
                env.get("HOME")
                    .filter(|value| !value.is_empty())
                    .map(|home| PathBuf::from(home).join(".config"))
            },
            |xdg| Some(PathBuf::from(xdg)),
        );
    let Some(base) = base else {
        return Err("Could not resolve home directory for observer credentials.".to_string());
    };
    Ok(base.join("solstone-observer").join("spl").join(label))
}

fn validate_instance_id(value: &str) -> Option<String> {
    let valid = !value.is_empty()
        && value.len() <= 256
        && value
            .chars()
            .all(|ch| ch.is_ascii_alphanumeric() || ch == '-');
    if valid {
        None
    } else {
        Some(format!(
            "bad instance_id from receiver: '{}'",
            value.replace('\\', "\\\\").replace('\'', "\\'")
        ))
    }
}

fn validate_credential(credential: &LinkJoinCredential) -> Result<(), &'static str> {
    if credential.client_key_pem.is_empty() {
        return Err("Pair response missing generated client key");
    }
    if credential.client_cert_pem.is_empty() {
        return Err("Pair response missing client_cert");
    }
    if credential.ca_chain_pem.is_empty() || credential.ca_chain_pem.iter().any(String::is_empty) {
        return Err("Pair response missing ca_chain");
    }
    if credential.instance_id.is_empty() {
        return Err("Pair response missing instance_id");
    }
    if credential
        .home_attestation
        .as_deref()
        .unwrap_or_default()
        .is_empty()
    {
        return Err("Pair response missing home_attestation");
    }
    Ok(())
}

fn normalized_local_endpoints(value: &Value) -> Result<Value, &'static str> {
    match value {
        Value::Null => Ok(Value::Array(Vec::new())),
        Value::Array(_) => Ok(value.clone()),
        Value::Bool(_) | Value::Number(_) | Value::String(_) | Value::Object(_) => {
            Err("Pair response local_endpoints must be an array.")
        }
    }
}

fn join_chain(ca_chain_pem: &[String]) -> String {
    ca_chain_pem
        .iter()
        .map(|cert| {
            if cert.ends_with('\n') {
                cert.clone()
            } else {
                format!("{cert}\n")
            }
        })
        .collect()
}

fn peer_json(
    label: &str,
    paired_at: String,
    credential: &LinkJoinCredential,
    local_endpoints: Value,
    is_peer: bool,
) -> String {
    let mut peer = Map::new();
    peer.insert("label".to_string(), Value::String(label.to_string()));
    peer.insert("paired_at".to_string(), Value::String(paired_at));
    peer.insert(
        "instance_id".to_string(),
        Value::String(credential.instance_id.clone()),
    );
    peer.insert(
        "home_label".to_string(),
        Value::String(credential.home_label.clone()),
    );
    peer.insert(
        "fingerprint".to_string(),
        Value::String(credential.ca_fingerprint.clone()),
    );
    peer.insert("local_endpoints".to_string(), local_endpoints);
    peer.insert(
        "role".to_string(),
        Value::String(if is_peer { "peer" } else { "" }.to_string()),
    );
    format!("{}\n", json_pretty_ascii(&Value::Object(peer)))
}

fn now_utc(clock: Option<&dyn crate::seam::Clock>) -> String {
    let now = clock.map_or_else(SystemTime::now, |clock| clock.now());
    let datetime: DateTime<Utc> = now.into();
    datetime.format("%Y-%m-%dT%H:%M:%SZ").to_string()
}

fn pairing_error_text(error: LinkJoinPairingError) -> String {
    let kind = error.kind;
    match kind {
        LinkJoinPairingErrorKind::Rejected { status } => format!(
            "Pairing failed (HTTP {status}): the pairing window is closed or the code was already used."
        ),
        LinkJoinPairingErrorKind::Relay(LinkJoinRelayErrorKind::PairWindowClosed) => {
            "Pairing failed: the pairing window is closed or expired. Generate a new pair code and retry."
                .to_string()
        }
        LinkJoinPairingErrorKind::PairResponseMissingHomeAttestation => {
            "Pair response missing home_attestation".to_string()
        }
        LinkJoinPairingErrorKind::NoEndpoint => {
            "Could not connect to the pairing listener.".to_string()
        }
        LinkJoinPairingErrorKind::RelayControlRejected { endpoint, status } => {
            format!(
                "Relay rejected device enrollment ({} HTTP {status}).",
                relay_control_endpoint_code(endpoint)
            )
        }
        kind @ (LinkJoinPairingErrorKind::Io
        | LinkJoinPairingErrorKind::Tls
        | LinkJoinPairingErrorKind::Crypto
        | LinkJoinPairingErrorKind::Mux
        | LinkJoinPairingErrorKind::Http
        | LinkJoinPairingErrorKind::Json
        | LinkJoinPairingErrorKind::PairLink
        | LinkJoinPairingErrorKind::Pairing
        | LinkJoinPairingErrorKind::Relay(
            LinkJoinRelayErrorKind::HomeOffline
            | LinkJoinRelayErrorKind::Unauthorized
            | LinkJoinRelayErrorKind::Unpaid
            | LinkJoinRelayErrorKind::UnknownInstance
            | LinkJoinRelayErrorKind::Overflow
            | LinkJoinRelayErrorKind::Abnormal
            | LinkJoinRelayErrorKind::UpgradeRejected
            | LinkJoinRelayErrorKind::Stalled,
        )
        | LinkJoinPairingErrorKind::NotPaired
        | LinkJoinPairingErrorKind::LocalOffset
        | LinkJoinPairingErrorKind::RuntimeUnavailable) => format!(
            "Pairing failed ({}). Generate a new pair code and retry.",
            transport_error_code(kind)
        ),
    }
}

fn relay_control_endpoint_code(endpoint: LinkJoinRelayControlEndpoint) -> &'static str {
    match endpoint {
        LinkJoinRelayControlEndpoint::EnrollDevice => "relay-control-enroll-device",
        LinkJoinRelayControlEndpoint::TokenRefresh => "relay-control-token-refresh",
    }
}

fn transport_error_code(kind: LinkJoinPairingErrorKind) -> &'static str {
    match kind {
        LinkJoinPairingErrorKind::Io => "io",
        LinkJoinPairingErrorKind::Tls => "tls",
        LinkJoinPairingErrorKind::Crypto => "crypto",
        LinkJoinPairingErrorKind::Mux => "mux",
        LinkJoinPairingErrorKind::Http => "http",
        LinkJoinPairingErrorKind::Json => "json",
        LinkJoinPairingErrorKind::PairLink => "pair-link",
        LinkJoinPairingErrorKind::Pairing => "pairing",
        LinkJoinPairingErrorKind::PairResponseMissingHomeAttestation => {
            "pair-response-missing-home-attestation"
        }
        LinkJoinPairingErrorKind::Rejected { status: _ } => "rejected",
        LinkJoinPairingErrorKind::Relay(LinkJoinRelayErrorKind::HomeOffline) => {
            "relay-home-offline"
        }
        LinkJoinPairingErrorKind::Relay(LinkJoinRelayErrorKind::Unauthorized) => {
            "relay-unauthorized"
        }
        LinkJoinPairingErrorKind::Relay(LinkJoinRelayErrorKind::Unpaid) => "relay-unpaid",
        LinkJoinPairingErrorKind::Relay(LinkJoinRelayErrorKind::UnknownInstance) => {
            "relay-unknown-instance"
        }
        LinkJoinPairingErrorKind::Relay(LinkJoinRelayErrorKind::PairWindowClosed) => {
            "relay-pair-window-closed"
        }
        LinkJoinPairingErrorKind::Relay(LinkJoinRelayErrorKind::Overflow) => "relay-overflow",
        LinkJoinPairingErrorKind::Relay(LinkJoinRelayErrorKind::Abnormal) => "relay-abnormal",
        LinkJoinPairingErrorKind::Relay(LinkJoinRelayErrorKind::UpgradeRejected) => {
            "relay-upgrade-rejected"
        }
        LinkJoinPairingErrorKind::Relay(LinkJoinRelayErrorKind::Stalled) => "relay-stalled",
        LinkJoinPairingErrorKind::RelayControlRejected {
            endpoint,
            status: _,
        } => relay_control_endpoint_code(endpoint),
        LinkJoinPairingErrorKind::NoEndpoint => "no-endpoint",
        LinkJoinPairingErrorKind::NotPaired => "not-paired",
        LinkJoinPairingErrorKind::LocalOffset => "local-offset",
        LinkJoinPairingErrorKind::RuntimeUnavailable => "runtime-unavailable",
    }
}

fn path_lexists(path: &Path) -> bool {
    fs::symlink_metadata(path).is_ok()
}

fn existing_path_message(path: &Path) -> String {
    format!(
        "Credentials path already exists: {}. Remove it and rerun if re-pairing.",
        path.display()
    )
}

fn spent_existing_path_message(path: &Path) -> String {
    format!(
        "Credentials path already exists: {}. The pairing code is now spent; generate a new one and rerun after removing it.",
        path.display()
    )
}

fn publish_bundle_atomic(bundle_dir: &Path, files: &BTreeMap<String, Vec<u8>>) -> io::Result<()> {
    let parent = bundle_dir
        .parent()
        .ok_or_else(|| io::Error::other("credential path has no parent"))?;
    fs::create_dir_all(parent)?;
    if path_lexists(bundle_dir) {
        return Err(io::Error::new(
            io::ErrorKind::AlreadyExists,
            existing_path_message(bundle_dir),
        ));
    }
    let staging = create_staging_dir(parent, bundle_dir)?;
    let result = write_bundle_to_staging(&staging, files).and_then(|()| {
        fsync_directory(&staging);
        fs::rename(&staging, bundle_dir)?;
        fsync_directory(parent);
        Ok(())
    });
    if result.is_err() && path_lexists(&staging) {
        let _ = fs::remove_dir_all(&staging);
    }
    result
}

fn create_staging_dir(parent: &Path, bundle_dir: &Path) -> io::Result<PathBuf> {
    let bundle_name = bundle_dir
        .file_name()
        .and_then(|name| name.to_str())
        .unwrap_or("bundle");
    let pid = std::process::id();
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos();
    for attempt in 0..100u32 {
        let candidate = parent.join(format!(".{bundle_name}.{pid}.{nanos}.{attempt}"));
        match fs::create_dir(&candidate) {
            Ok(()) => {
                chmod_dir(&candidate)?;
                return Ok(candidate);
            }
            Err(error) if error.kind() == io::ErrorKind::AlreadyExists => {}
            Err(error) => return Err(error),
        }
    }
    Err(io::Error::new(
        io::ErrorKind::AlreadyExists,
        "could not create credential staging directory",
    ))
}

fn write_bundle_to_staging(staging: &Path, files: &BTreeMap<String, Vec<u8>>) -> io::Result<()> {
    if files.len() != BUNDLE_FILES.len()
        || BUNDLE_FILES.iter().any(|name| !files.contains_key(*name))
    {
        return Err(io::Error::other("credential bundle file set is incomplete"));
    }
    for (name, content) in files {
        write_bundle_file(&staging.join(name), content)?;
    }
    Ok(())
}

fn write_bundle_file(path: &Path, content: &[u8]) -> io::Result<()> {
    let mut file = OpenOptions::new().write(true).create_new(true).open(path)?;
    file.write_all(content)?;
    file.sync_all()?;
    chmod_file(path)
}

fn fsync_directory(path: &Path) {
    if let Ok(file) = File::open(path) {
        let _ = file.sync_all();
    }
}

#[cfg(unix)]
fn chmod_dir(path: &Path) -> io::Result<()> {
    use std::os::unix::fs::PermissionsExt;

    fs::set_permissions(path, fs::Permissions::from_mode(0o700))
}

#[cfg(not(unix))]
fn chmod_dir(_path: &Path) -> io::Result<()> {
    Ok(())
}

#[cfg(unix)]
fn chmod_file(path: &Path) -> io::Result<()> {
    use std::os::unix::fs::PermissionsExt;

    fs::set_permissions(path, fs::Permissions::from_mode(0o600))
}

#[cfg(not(unix))]
fn chmod_file(_path: &Path) -> io::Result<()> {
    Ok(())
}

#[cfg(test)]
mod tests {
    use std::collections::BTreeMap;
    use std::fs;
    use std::path::{Path, PathBuf};
    use std::sync::atomic::{AtomicU64, Ordering};

    use crate::seam::{
        ExpectedLinkJoinPairingCall, FakeClock, ScriptedHttpTransport, ScriptedLinkJoinPairingSeam,
    };
    use serde_json::json;
    use spl_core::crockford;

    use super::*;

    const OBSERVER_PEER_JSON: &str =
        include_str!("../../../../core/fixtures/native-sol/link-join/observer_ascii_peer.json");
    const PEER_NON_ASCII_JSON: &str =
        include_str!("../../../../core/fixtures/native-sol/link-join/peer_non_ascii_peer.json");
    const NESTED_ENDPOINTS_JSON: &str =
        include_str!("../../../../core/fixtures/native-sol/link-join/nested_endpoints_peer.json");

    static TEMP_COUNTER: AtomicU64 = AtomicU64::new(0);

    fn string_args(values: &[&str]) -> Vec<String> {
        values.iter().map(|value| (*value).to_string()).collect()
    }

    fn direct_pair_link() -> String {
        let mut blob = vec![0x04, 0x01, 192, 168, 1, 10];
        blob.extend_from_slice(&7657u16.to_be_bytes());
        blob.extend_from_slice(&[0x11; 16]);
        blob.extend_from_slice(&[0x22; 16]);
        format!("{PAIR_LINK_PREFIX}{}", crockford::encode(&blob))
    }

    fn relay_pair_link() -> String {
        let mut blob = vec![0x06];
        blob.extend_from_slice(&[0x33; 8]);
        blob.push(0x01);
        blob.extend_from_slice(&[0x44; 16]);
        blob.push(0);
        format!("{PAIR_LINK_PREFIX}{}", crockford::encode(&blob))
    }

    fn temp_dir(name: &str) -> PathBuf {
        let id = TEMP_COUNTER.fetch_add(1, Ordering::Relaxed);
        let path = std::env::temp_dir().join(format!(
            "solstone-link-join-test-{}-{id}-{name}",
            std::process::id()
        ));
        let _ = fs::remove_dir_all(&path);
        fs::create_dir_all(&path).expect("temp dir");
        path
    }

    fn base_env(config: &Path, home: &Path) -> BTreeMap<String, String> {
        BTreeMap::from([
            ("XDG_CONFIG_HOME".to_string(), config.display().to_string()),
            ("HOME".to_string(), home.display().to_string()),
            ("HOSTNAME".to_string(), "Test Host".to_string()),
        ])
    }

    fn run(
        args: &[&str],
        env: &BTreeMap<String, String>,
        journal_root: &Path,
        seam: &ScriptedLinkJoinPairingSeam,
        clock: &FakeClock,
    ) -> CommandOutput {
        let argv = string_args(args);
        let transport = ScriptedHttpTransport::new(vec![]);
        link_join(CommandContext {
            args: &argv,
            env,
            stdin: "",
            today: "20260726",
            transport: &transport,
            clock: Some(clock),
            chat_events: None,
            files: None,
            build_identity: None,
            client_item_ids: None,
            notification_sink: None,
            link_pairing: Some(seam),
            journal_root: Some(journal_root),
        })
    }

    fn credential(local_endpoints: Value) -> LinkJoinCredential {
        LinkJoinCredential {
            client_key_pem: "PRIVATE\n".to_string(),
            client_cert_pem: "CERT\n".to_string(),
            ca_chain_pem: vec!["CA".to_string()],
            ca_fingerprint: "sha256:abc".to_string(),
            instance_id: "receiver-instance".to_string(),
            home_label: "Home".to_string(),
            home_attestation: Some("header.payload.signature".to_string()),
            local_endpoints,
            relay_device_token: None,
            relay_device_token_expires_at: None,
        }
    }

    fn expected_direct_request(label: &str) -> LinkJoinDirectRequest {
        LinkJoinDirectRequest {
            targets: vec![LinkJoinPairTarget {
                host: "192.168.1.10".to_string(),
                port: 7657,
            }],
            nonce_hex: "11111111111111111111111111111111".to_string(),
            ca_fp_prefix: vec![0x22; 16],
            device_label: label.to_string(),
            additional_fields: Map::new(),
        }
    }

    #[test]
    fn help_is_python_byte_exact() {
        let env = BTreeMap::new();
        let root = temp_dir("help-root");
        let seam = ScriptedLinkJoinPairingSeam::new(vec![]);
        let clock = FakeClock::at_unix(0);
        let output = run(&["--help"], &env, &root, &seam, &clock);
        assert_eq!(output, CommandOutput::success(HELP));
        assert_eq!(HELP.len(), 349);
        seam.assert_done();
    }

    #[test]
    fn missing_code_exits_like_argparse() {
        let env = BTreeMap::new();
        let root = temp_dir("missing-code-root");
        let seam = ScriptedLinkJoinPairingSeam::new(vec![]);
        let clock = FakeClock::at_unix(0);
        let output = run(&[], &env, &root, &seam, &clock);
        assert_eq!(
            output.stderr,
            format!("{USAGE}sol link join: error: the following arguments are required: --code\n")
        );
        assert_eq!(output.exit, 2);
        seam.assert_done();
    }

    #[test]
    fn invalid_role_and_label_fail_before_pairing() {
        let env = BTreeMap::new();
        let root = temp_dir("invalid-root");
        let seam = ScriptedLinkJoinPairingSeam::new(vec![]);
        let clock = FakeClock::at_unix(0);
        let role = run(
            &["--code", &direct_pair_link(), "--as", "bad"],
            &env,
            &root,
            &seam,
            &clock,
        );
        assert_eq!(
            role.stderr,
            "invalid role; expected one of: phone, observer, peer\n"
        );
        assert_eq!(role.exit, 2);
        let label = run(
            &["--code", &direct_pair_link(), "--label", "bad..name"],
            &env,
            &root,
            &seam,
            &clock,
        );
        assert_eq!(label.stderr, "--label must not contain '..'\n");
        assert_eq!(label.exit, 2);
        seam.assert_done();
    }

    #[test]
    fn observer_existing_path_is_checked_before_direct_pairing() {
        let temp = temp_dir("observer-precheck");
        let config = temp.join("config");
        let env = base_env(&config, &temp.join("home"));
        let root = temp.join("journal");
        let existing = config.join("solstone-observer").join("spl").join("laptop");
        fs::create_dir_all(&existing).expect("existing bundle");
        let seam = ScriptedLinkJoinPairingSeam::new(vec![]);
        let clock = FakeClock::at_unix(0);

        let output = run(
            &["--code", &direct_pair_link(), "--label", "laptop"],
            &env,
            &root,
            &seam,
            &clock,
        );

        assert_eq!(
            output.stderr,
            format!("{}\n", existing_path_message(&existing))
        );
        assert_eq!(output.exit, 1);
        seam.assert_done();
    }

    #[test]
    fn observer_existing_path_is_checked_before_relay_pairing() {
        let temp = temp_dir("observer-relay-precheck");
        let config = temp.join("config");
        let env = base_env(&config, &temp.join("home"));
        let root = temp.join("journal");
        let existing = config.join("solstone-observer").join("spl").join("laptop");
        fs::create_dir_all(&existing).expect("existing bundle");
        let seam = ScriptedLinkJoinPairingSeam::new(vec![]);
        let clock = FakeClock::at_unix(0);

        let output = run(
            &["--code", &relay_pair_link(), "--label", "laptop"],
            &env,
            &root,
            &seam,
            &clock,
        );

        assert_eq!(
            output.stderr,
            format!("{}\n", existing_path_message(&existing))
        );
        assert_eq!(output.exit, 1);
        seam.assert_done();
    }

    #[test]
    fn observer_success_writes_one_bundle_with_python_peer_json_bytes() {
        let temp = temp_dir("observer-success");
        let config = temp.join("config");
        let env = base_env(&config, &temp.join("home"));
        let root = temp.join("journal");
        let expected = expected_direct_request("laptop");
        let seam = ScriptedLinkJoinPairingSeam::new(vec![ExpectedLinkJoinPairingCall::Direct {
            expected,
            result: Ok(credential(Value::Null)),
        }]);
        let clock = FakeClock::at_unix(0);

        let output = run(
            &["--code", &direct_pair_link(), "--label", "laptop"],
            &env,
            &root,
            &seam,
            &clock,
        );

        let bundle = config.join("solstone-observer").join("spl").join("laptop");
        assert_eq!(
            output.stdout,
            format!("Linked laptop.\nCredentials: {}\n", bundle.display())
        );
        assert_eq!(output.exit, 0);
        assert_eq!(
            fs::read_to_string(bundle.join("peer.json")).expect("peer json"),
            OBSERVER_PEER_JSON
        );
        let entries = fs::read_dir(bundle.parent().expect("bundle parent"))
            .expect("bundle parent")
            .collect::<Result<Vec<_>, _>>()
            .expect("bundle entries");
        assert_eq!(entries.len(), 1);
        for name in BUNDLE_FILES {
            assert!(bundle.join(name).is_file(), "{name}");
        }
        seam.assert_done();
    }

    #[test]
    fn peer_missing_state_fails_without_pairing() {
        let temp = temp_dir("peer-state");
        let config = temp.join("config");
        let env = base_env(&config, &temp.join("home"));
        let root = temp.join("journal");
        let seam = ScriptedLinkJoinPairingSeam::new(vec![]);
        let clock = FakeClock::at_unix(0);

        let output = run(
            &["--code", &direct_pair_link(), "--as", "peer"],
            &env,
            &root,
            &seam,
            &clock,
        );

        assert_eq!(output.stderr, PEER_STATE_GUIDANCE);
        assert_eq!(output.exit, 1);
        seam.assert_done();
    }

    #[test]
    fn peer_existing_path_is_checked_after_pairing_with_spent_message() {
        let temp = temp_dir("peer-existing");
        let config = temp.join("config");
        let env = base_env(&config, &temp.join("home"));
        let root = temp.join("journal");
        fs::create_dir_all(root.join("link")).expect("link dir");
        fs::write(
            root.join("link").join("state.json"),
            "{\"instance_id\": \"sender-instance\"}",
        )
        .expect("state");
        let bundle = root.join("peers").join("receiver-instance");
        fs::create_dir_all(&bundle).expect("existing peer");
        let mut additional_fields = Map::new();
        additional_fields.insert(
            "sender_instance_id".to_string(),
            Value::String("sender-instance".to_string()),
        );
        let expected = LinkJoinDirectRequest {
            additional_fields,
            ..expected_direct_request("Test-Host")
        };
        let seam = ScriptedLinkJoinPairingSeam::new(vec![ExpectedLinkJoinPairingCall::Direct {
            expected,
            result: Ok(credential(Value::Array(Vec::new()))),
        }]);
        let clock = FakeClock::at_unix(0);

        let output = run(
            &["--code", &direct_pair_link(), "--as", "peer"],
            &env,
            &root,
            &seam,
            &clock,
        );

        assert_eq!(
            output.stderr,
            format!("{}\n", spent_existing_path_message(&bundle))
        );
        assert_eq!(output.exit, 1);
        seam.assert_done();
    }

    #[test]
    fn local_endpoints_shape_errors_fail_before_writing() {
        let temp = temp_dir("endpoint-shape");
        let config = temp.join("config");
        let env = base_env(&config, &temp.join("home"));
        let root = temp.join("journal");
        let bundle = config.join("solstone-observer").join("spl").join("laptop");
        let seam = ScriptedLinkJoinPairingSeam::new(vec![ExpectedLinkJoinPairingCall::Direct {
            expected: expected_direct_request("laptop"),
            result: Ok(credential(json!({"ip": "10.0.0.2"}))),
        }]);
        let clock = FakeClock::at_unix(0);

        let output = run(
            &["--code", &direct_pair_link(), "--label", "laptop"],
            &env,
            &root,
            &seam,
            &clock,
        );

        assert_eq!(
            output.stderr,
            "Pair response local_endpoints must be an array.\n"
        );
        assert_eq!(output.exit, 1);
        assert!(!path_lexists(&bundle));
        seam.assert_done();
    }

    #[test]
    fn local_endpoints_size_errors_fail_before_writing() {
        let temp = temp_dir("endpoint-size");
        let config = temp.join("config");
        let env = base_env(&config, &temp.join("home"));
        let root = temp.join("journal");
        let bundle = config.join("solstone-observer").join("spl").join("laptop");
        let endpoints = Value::Array(vec![Value::String("x".repeat(LOCAL_ENDPOINTS_MAX_BYTES))]);
        let seam = ScriptedLinkJoinPairingSeam::new(vec![ExpectedLinkJoinPairingCall::Direct {
            expected: expected_direct_request("laptop"),
            result: Ok(credential(endpoints)),
        }]);
        let clock = FakeClock::at_unix(0);

        let output = run(
            &["--code", &direct_pair_link(), "--label", "laptop"],
            &env,
            &root,
            &seam,
            &clock,
        );

        assert_eq!(
            output.stderr,
            "Pair response local_endpoints is too large.\n"
        );
        assert_eq!(output.exit, 1);
        assert!(!path_lexists(&bundle));
        seam.assert_done();
    }

    #[test]
    fn missing_home_attestation_fails_before_writing() {
        let temp = temp_dir("attestation");
        let config = temp.join("config");
        let env = base_env(&config, &temp.join("home"));
        let root = temp.join("journal");
        let bundle = config.join("solstone-observer").join("spl").join("laptop");
        let mut returned = credential(Value::Array(Vec::new()));
        returned.home_attestation = None;
        let seam = ScriptedLinkJoinPairingSeam::new(vec![ExpectedLinkJoinPairingCall::Direct {
            expected: expected_direct_request("laptop"),
            result: Ok(returned),
        }]);
        let clock = FakeClock::at_unix(0);

        let output = run(
            &["--code", &direct_pair_link(), "--label", "laptop"],
            &env,
            &root,
            &seam,
            &clock,
        );

        assert_eq!(output.stderr, "Pair response missing home_attestation\n");
        assert_eq!(output.exit, 1);
        assert!(!path_lexists(&bundle));
        seam.assert_done();
    }

    #[test]
    fn peer_json_byte_oracles_cover_non_ascii_and_nested_endpoint_order() {
        let mut non_ascii = credential(json!([
            {"endpoint": "réseau-local", "port": 7657, "scope": "lan"}
        ]));
        non_ascii.home_label = "Hôme".to_string();
        assert_eq!(
            peer_json(
                "café",
                "1970-01-01T00:00:00Z".to_string(),
                &non_ascii,
                non_ascii.local_endpoints.clone(),
                true
            ),
            PEER_NON_ASCII_JSON
        );

        let nested = credential(json!([
            {
                "ip": "10.0.0.2",
                "port": 7657,
                "scope": "lan",
                "meta": {"first": "one", "second": ["two", {"third": "three"}]}
            }
        ]));
        assert_eq!(
            peer_json(
                "laptop",
                "1970-01-01T00:00:00Z".to_string(),
                &nested,
                nested.local_endpoints.clone(),
                false
            ),
            NESTED_ENDPOINTS_JSON
        );
    }

    #[test]
    fn home_override_requires_host_and_explicit_port() {
        assert_eq!(
            parse_home_target("https://").expect_err("missing host"),
            "Pair-link target missing host."
        );
        assert_eq!(
            parse_home_target("https://home.local").expect_err("missing port"),
            "Pair-link target missing explicit port."
        );
        assert_eq!(
            parse_home_target("https://home.local:7657/path?ignored=true").expect("target"),
            LinkJoinPairTarget {
                host: "home.local".to_string(),
                port: 7657,
            }
        );
    }
}

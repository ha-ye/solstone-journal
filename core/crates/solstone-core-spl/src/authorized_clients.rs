// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 sol pbc

//! Pure parsing and lookup for the paired-client authorization ledger.

use std::collections::BTreeMap;

use serde_json::Value;

/// A parsed entry from `link/authorized_clients.json`.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ClientEntry {
    /// The persisted client fingerprint, such as `sha256:abcd`.
    pub fingerprint: String,
    /// The home-assigned device label.
    pub device_label: String,
    /// The persisted pairing timestamp.
    pub paired_at: String,
    /// The home instance that paired the client.
    pub instance_id: String,
    /// The optional pairing provenance role.
    pub role: String,
    /// The last-seen timestamp, when present as a string.
    pub last_seen_at: Option<String>,
    /// The local network display name, when present as a string.
    pub network: Option<String>,
    /// The client-provided display label.
    pub client_label: String,
    /// The client kind, defaulting to `cert`.
    pub kind: String,
    /// The browser sender public key SPKI encoding, when present as a string.
    pub pubkey_spki: Option<String>,
    /// The observer upload handle, when present as a string.
    pub observer_handle: Option<String>,
}

/// Whether source bytes represented a usable ledger list.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum LedgerStatus {
    /// The source was a JSON list, including an empty list.
    Available,
    /// The source was missing to the caller or did not decode to a JSON list.
    Unavailable,
}

/// Parsed paired-client entries plus source availability.
///
/// An unavailable ledger intentionally has no entries. This preserves Python's
/// fail-closed behavior while allowing a caller to distinguish it from an
/// available ledger that simply has no matching client.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct AuthorizedClients {
    status: LedgerStatus,
    entries: BTreeMap<String, ClientEntry>,
}

/// Browser upload authorization resolved from a raw sender fingerprint.
#[derive(Debug, Eq, PartialEq)]
pub enum BrowserUploadAuthorization<'a> {
    /// The caller had no usable authorization ledger.
    LedgerUnavailable,
    /// The fingerprint has no browser entry in the ledger.
    NotAuthorized,
    /// A browser entry matched but lacks a non-empty upload key or observer handle.
    Incomplete,
    /// A browser entry contains the materials needed for an upload request.
    Authorized {
        /// Hex-encoded browser sender SPKI.
        pubkey_spki: &'a str,
        /// The observer upload handle.
        observer_handle: &'a str,
    },
}

impl AuthorizedClients {
    /// Represents a missing ledger without introducing file I/O into this module.
    #[must_use]
    pub fn unavailable() -> Self {
        Self {
            status: LedgerStatus::Unavailable,
            entries: BTreeMap::new(),
        }
    }

    /// Returns whether the source was a JSON ledger list.
    #[must_use]
    pub fn status(&self) -> LedgerStatus {
        self.status
    }

    /// Returns the parsed entries, which are empty for an unavailable ledger.
    #[must_use]
    pub fn entries(&self) -> &BTreeMap<String, ClientEntry> {
        &self.entries
    }

    /// Resolves browser upload authorization for a raw SHA-256 sender fingerprint.
    #[must_use]
    pub fn browser_upload_authorization(
        &self,
        sender_fingerprint: &[u8; 32],
    ) -> BrowserUploadAuthorization<'_> {
        if self.status == LedgerStatus::Unavailable {
            return BrowserUploadAuthorization::LedgerUnavailable;
        }

        let fingerprint = fingerprint_key(sender_fingerprint);
        let Some(entry) = self.entries.get(&fingerprint) else {
            return BrowserUploadAuthorization::NotAuthorized;
        };
        if entry.kind != "browser" {
            return BrowserUploadAuthorization::NotAuthorized;
        }
        let (Some(pubkey_spki), Some(observer_handle)) = (
            entry
                .pubkey_spki
                .as_deref()
                .filter(|value| !value.is_empty()),
            entry
                .observer_handle
                .as_deref()
                .filter(|value| !value.is_empty()),
        ) else {
            return BrowserUploadAuthorization::Incomplete;
        };

        BrowserUploadAuthorization::Authorized {
            pubkey_spki,
            observer_handle,
        }
    }
}

/// Parses JSON ledger bytes using the Python reader's fail-closed rules.
#[must_use]
pub fn parse_authorized_clients(input: &[u8]) -> AuthorizedClients {
    let value: Value = match serde_json::from_slice(input) {
        Ok(value) => value,
        Err(_) => return AuthorizedClients::unavailable(),
    };
    let Some(items) = value.as_array() else {
        return AuthorizedClients::unavailable();
    };

    let mut entries = BTreeMap::new();
    for item in items {
        let Some(object) = item.as_object() else {
            continue;
        };
        let Some(fingerprint) = object.get("fingerprint").and_then(Value::as_str) else {
            continue;
        };

        let entry = ClientEntry {
            fingerprint: fingerprint.to_owned(),
            device_label: python_str(object.get("device_label")),
            paired_at: python_str(object.get("paired_at")),
            instance_id: python_str(object.get("instance_id")),
            role: string_or_empty(object.get("role")),
            last_seen_at: string_or_none(object.get("last_seen_at")),
            network: string_or_none(object.get("network")),
            client_label: string_or_empty(object.get("client_label")),
            kind: nonempty_string_or_cert(object.get("kind")),
            pubkey_spki: string_or_none(object.get("pubkey_spki")),
            observer_handle: string_or_none(object.get("observer_handle")),
        };
        entries.insert(fingerprint.to_owned(), entry);
    }

    AuthorizedClients {
        status: LedgerStatus::Available,
        entries,
    }
}

fn string_or_empty(value: Option<&Value>) -> String {
    value
        .and_then(Value::as_str)
        .map_or_else(String::new, ToOwned::to_owned)
}

fn string_or_none(value: Option<&Value>) -> Option<String> {
    value.and_then(Value::as_str).map(ToOwned::to_owned)
}

fn nonempty_string_or_cert(value: Option<&Value>) -> String {
    value
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
        .map_or_else(|| "cert".to_owned(), ToOwned::to_owned)
}

/// Reproduces Python's `str(item.get(field, ""))` for JSON values.
fn python_str(value: Option<&Value>) -> String {
    value.map_or_else(String::new, python_repr_or_string)
}

fn python_repr_or_string(value: &Value) -> String {
    match value {
        Value::String(value) => value.clone(),
        _ => python_repr(value),
    }
}

fn python_repr(value: &Value) -> String {
    match value {
        Value::Null => "None".to_owned(),
        Value::Bool(true) => "True".to_owned(),
        Value::Bool(false) => "False".to_owned(),
        Value::Number(number) => number.to_string(),
        Value::String(value) => python_quoted_string(value),
        Value::Array(values) => {
            let values = values
                .iter()
                .map(python_repr)
                .collect::<Vec<_>>()
                .join(", ");
            format!("[{values}]")
        }
        Value::Object(values) => {
            let values = values
                .iter()
                .map(|(key, value)| {
                    format!("{}: {}", python_quoted_string(key), python_repr(value))
                })
                .collect::<Vec<_>>()
                .join(", ");
            format!("{{{values}}}")
        }
    }
}

fn python_quoted_string(value: &str) -> String {
    let quote = if value.contains('\'') && !value.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut quoted = String::with_capacity(value.len() + 2);
    quoted.push(quote);
    for character in value.chars() {
        match character {
            '\\' => quoted.push_str("\\\\"),
            '\n' => quoted.push_str("\\n"),
            '\r' => quoted.push_str("\\r"),
            '\t' => quoted.push_str("\\t"),
            '\u{08}' => quoted.push_str("\\x08"),
            '\u{0c}' => quoted.push_str("\\x0c"),
            character if character == quote => {
                quoted.push('\\');
                quoted.push(character);
            }
            character if character.is_control() => {
                let code_point = character as u32;
                if code_point <= 0xff {
                    format_hex_escape(&mut quoted, code_point as u8);
                } else {
                    quoted.push_str(&format!("\\u{code_point:04x}"));
                }
            }
            character => quoted.push(character),
        }
    }
    quoted.push(quote);
    quoted
}

fn format_hex_escape(output: &mut String, value: u8) {
    const HEX: &[u8; 16] = b"0123456789abcdef";

    output.push_str("\\x");
    output.push(char::from(HEX[usize::from(value >> 4)]));
    output.push(char::from(HEX[usize::from(value & 0x0f)]));
}

fn fingerprint_key(sender_fingerprint: &[u8; 32]) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";

    let mut fingerprint = String::with_capacity("sha256:".len() + sender_fingerprint.len() * 2);
    fingerprint.push_str("sha256:");
    for &byte in sender_fingerprint {
        fingerprint.push(char::from(HEX[usize::from(byte >> 4)]));
        fingerprint.push(char::from(HEX[usize::from(byte & 0x0f)]));
    }
    fingerprint
}

#[cfg(test)]
mod tests {
    use super::{
        AuthorizedClients, BrowserUploadAuthorization, LedgerStatus, parse_authorized_clients,
    };

    #[test]
    fn malformed_and_non_list_ledgers_fail_closed_with_no_entries() {
        for source in [b"{".as_slice(), b"{}".as_slice(), b"42".as_slice()] {
            let ledger = parse_authorized_clients(source);
            assert_eq!(ledger.status(), LedgerStatus::Unavailable);
            assert!(ledger.entries().is_empty());
        }
    }

    #[test]
    fn non_objects_and_non_string_fingerprints_are_skipped() {
        let ledger = parse_authorized_clients(
            br#"["entry", 3, null, {}, {"fingerprint": 3}, {"fingerprint":"ok"}]"#,
        );

        assert_eq!(ledger.status(), LedgerStatus::Available);
        assert_eq!(ledger.entries().len(), 1);
        assert!(ledger.entries().contains_key("ok"));
    }

    #[test]
    fn fields_follow_python_coercions_and_defaults() {
        let ledger = parse_authorized_clients(
            br#"[{"fingerprint":"client","device_label":false,"paired_at":null,"instance_id":["x",true],"role":false,"last_seen_at":1,"network":false,"client_label":[],"kind":"","pubkey_spki":3,"observer_handle":null}]"#,
        );
        let entry = ledger.entries().get("client");

        assert!(entry.is_some());
        let entry = match entry {
            Some(entry) => entry,
            None => return,
        };
        assert_eq!(entry.device_label, "False");
        assert_eq!(entry.paired_at, "None");
        assert_eq!(entry.instance_id, "['x', True]");
        assert_eq!(entry.role, "");
        assert_eq!(entry.last_seen_at, None);
        assert_eq!(entry.network, None);
        assert_eq!(entry.client_label, "");
        assert_eq!(entry.kind, "cert");
        assert_eq!(entry.pubkey_spki, None);
        assert_eq!(entry.observer_handle, None);
    }

    #[test]
    fn duplicate_fingerprints_keep_the_later_entry() {
        let ledger = parse_authorized_clients(
            br#"[{"fingerprint":"same","device_label":"first"},{"fingerprint":"same","device_label":"later"}]"#,
        );
        let entry = ledger.entries().get("same");

        assert_eq!(ledger.entries().len(), 1);
        assert_eq!(
            entry.map(|entry| entry.device_label.as_str()),
            Some("later")
        );
    }

    #[test]
    fn raw_sender_fingerprint_resolves_browser_upload_authorization() {
        let sender_fingerprint = [0xab; 32];
        let fingerprint = "ab".repeat(32);
        let source = format!(
            "[{{\"fingerprint\":\"sha256:{fingerprint}\",\"kind\":\"browser\",\"pubkey_spki\":\"30aa\",\"observer_handle\":\"observer-a\"}}]"
        );
        let ledger = parse_authorized_clients(source.as_bytes());

        assert_eq!(
            ledger.browser_upload_authorization(&sender_fingerprint),
            BrowserUploadAuthorization::Authorized {
                pubkey_spki: "30aa",
                observer_handle: "observer-a",
            }
        );
    }

    #[test]
    fn incomplete_browser_entries_are_rejected_separately() {
        let sender_fingerprint = [0xcd; 32];
        let fingerprint = "cd".repeat(32);
        let source = format!(
            "[{{\"fingerprint\":\"sha256:{fingerprint}\",\"kind\":\"browser\",\"pubkey_spki\":\"\",\"observer_handle\":\"observer-a\"}}]"
        );
        let ledger = parse_authorized_clients(source.as_bytes());

        assert_eq!(
            ledger.browser_upload_authorization(&sender_fingerprint),
            BrowserUploadAuthorization::Incomplete
        );
    }

    #[test]
    fn unavailable_ledger_is_not_conflated_with_no_matching_client() {
        let unavailable = AuthorizedClients::unavailable();
        let available = parse_authorized_clients(br#"[]"#);
        let sender_fingerprint = [0; 32];

        assert_eq!(
            unavailable.browser_upload_authorization(&sender_fingerprint),
            BrowserUploadAuthorization::LedgerUnavailable
        );
        assert_eq!(
            available.browser_upload_authorization(&sender_fingerprint),
            BrowserUploadAuthorization::NotAuthorized
        );
    }
}

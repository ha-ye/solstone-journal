// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 sol pbc

//! Read-only access to the local SPL link identity and service-token files.
//!
//! The owning Python link modules provision these files. This module never
//! creates, updates, or retains their contents after a failed read.

use std::{fs, io::ErrorKind, path::Path};

use serde_json::Value;

/// The persisted local identity used by the SPL service.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct LinkState {
    /// The provisioned home instance identifier.
    pub instance_id: String,
    /// The owner-facing name, or the supplied default when not stored as text.
    pub home_label: String,
    /// The provisioning lock timestamp when it is a JSON integer.
    pub locked_at: Option<i64>,
}

/// The result of loading `link/state.json` without mutating the journal.
#[derive(Clone, Debug, Eq, PartialEq)]
pub enum LinkStateRead {
    /// A valid identity was read.
    Present(LinkState),
    /// The identity file has not been provisioned.
    Missing,
    /// The identity file could not be read.
    Unreadable,
    /// The identity file was not a valid state object.
    Malformed,
}

/// A service token that intentionally has no formatting implementation.
#[derive(Clone, Eq, PartialEq)]
pub struct LinkServiceToken(String);

impl LinkServiceToken {
    /// Returns the token only to the authenticated relay request builder.
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

/// The result of loading `link/tokens/account.json` without retaining a last-good value.
pub enum LinkServiceTokenRead {
    /// A current non-empty token was read.
    Present(LinkServiceToken),
    /// The token file has not been provisioned.
    Missing,
    /// The token file could not be read.
    Unreadable,
    /// The token file was not a JSON object.
    Malformed,
}

enum JsonRead {
    Value(Value),
    Missing,
    Unreadable,
    Malformed,
}

/// Reads `link/state.json` beneath a journal root without creating any path.
pub fn load_link_state(journal_root: &Path, default_label: &str) -> LinkStateRead {
    let path = journal_root.join("link").join("state.json");
    let raw = match read_json(&path) {
        JsonRead::Value(raw) => raw,
        JsonRead::Missing => return LinkStateRead::Missing,
        JsonRead::Unreadable => return LinkStateRead::Unreadable,
        JsonRead::Malformed => return LinkStateRead::Malformed,
    };

    let Some(object) = raw.as_object() else {
        return LinkStateRead::Malformed;
    };
    let Some(instance_id) = object
        .get("instance_id")
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
    else {
        return LinkStateRead::Malformed;
    };

    let stored_label = object
        .get("home_label")
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty());
    let home_label = match stored_label {
        Some(value) => value.to_owned(),
        None => default_label.to_owned(),
    };
    let locked_at = object.get("locked_at").and_then(Value::as_i64);

    LinkStateRead::Present(LinkState {
        instance_id: instance_id.to_owned(),
        home_label,
        locked_at,
    })
}

/// Reads `link/tokens/account.json` beneath a journal root without creating any path.
pub fn load_link_service_token(journal_root: &Path) -> LinkServiceTokenRead {
    let path = journal_root
        .join("link")
        .join("tokens")
        .join("account.json");
    let raw = match read_json(&path) {
        JsonRead::Value(raw) => raw,
        JsonRead::Missing => return LinkServiceTokenRead::Missing,
        JsonRead::Unreadable => return LinkServiceTokenRead::Unreadable,
        JsonRead::Malformed => return LinkServiceTokenRead::Malformed,
    };

    let Some(object) = raw.as_object() else {
        return LinkServiceTokenRead::Malformed;
    };
    let candidate = object
        .get("service_token")
        .filter(|value| json_truthy(value))
        .or_else(|| object.get("account_token"));
    let token = candidate
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty());

    match token {
        Some(value) => LinkServiceTokenRead::Present(LinkServiceToken(value.to_owned())),
        None => LinkServiceTokenRead::Missing,
    }
}

fn json_truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(value) => *value,
        Value::Number(value) => value.as_f64().is_some_and(|value| value != 0.0),
        Value::String(value) => !value.is_empty(),
        Value::Array(value) => !value.is_empty(),
        Value::Object(value) => !value.is_empty(),
    }
}

fn read_json(path: &Path) -> JsonRead {
    let text = match fs::read_to_string(path) {
        Ok(text) => text,
        Err(error) if error.kind() == ErrorKind::NotFound => return JsonRead::Missing,
        Err(_) => return JsonRead::Unreadable,
    };

    match serde_json::from_str(&text) {
        Ok(value) => JsonRead::Value(value),
        Err(_) => JsonRead::Malformed,
    }
}

#[cfg(test)]
mod tests {
    use std::{
        error::Error,
        fs,
        path::{Path, PathBuf},
        sync::atomic::{AtomicU64, Ordering},
    };

    use super::{LinkServiceTokenRead, LinkStateRead, load_link_service_token, load_link_state};

    struct TempJournal {
        path: PathBuf,
    }

    impl TempJournal {
        fn new() -> Result<Self, Box<dyn Error>> {
            static NEXT_DIRECTORY: AtomicU64 = AtomicU64::new(0);

            for _ in 0..100 {
                let ordinal = NEXT_DIRECTORY.fetch_add(1, Ordering::Relaxed);
                let path = std::env::temp_dir().join(format!(
                    "solstone-core-spl-link-state-{}-{ordinal}",
                    std::process::id()
                ));
                match fs::create_dir(&path) {
                    Ok(()) => return Ok(Self { path }),
                    Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {}
                    Err(error) => return Err(error.into()),
                }
            }

            Err("could not allocate a disposable journal directory".into())
        }

        fn write(&self, relative: &str, contents: &str) -> Result<(), Box<dyn Error>> {
            let path = self.path.join(relative);
            let Some(parent) = path.parent() else {
                return Err("test path has no parent".into());
            };
            fs::create_dir_all(parent)?;
            fs::write(path, contents)?;
            Ok(())
        }

        fn path(&self) -> &Path {
            &self.path
        }
    }

    impl Drop for TempJournal {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.path);
        }
    }

    #[test]
    fn existing_state_is_read_without_rewriting_it() -> Result<(), Box<dyn Error>> {
        let journal = TempJournal::new()?;
        let state = r#"{"instance_id":"home-1","home_label":"Study","locked_at":1700000000}"#;
        journal.write("link/state.json", state)?;
        let state_path = journal.path().join("link/state.json");
        let before = fs::read(&state_path)?;

        let loaded = load_link_state(journal.path(), "solstone");

        match loaded {
            LinkStateRead::Present(value) => {
                assert_eq!(value.instance_id, "home-1");
                assert_eq!(value.home_label, "Study");
                assert_eq!(value.locked_at, Some(1_700_000_000));
            }
            _ => return Err("valid state was not loaded".into()),
        }
        assert_eq!(fs::read(state_path)?, before);
        Ok(())
    }

    #[test]
    fn missing_malformed_and_unreadable_state_remain_distinguishable() -> Result<(), Box<dyn Error>>
    {
        let missing = TempJournal::new()?;
        assert!(matches!(
            load_link_state(missing.path(), "solstone"),
            LinkStateRead::Missing
        ));
        assert!(!missing.path().join("link").exists());

        let malformed = TempJournal::new()?;
        malformed.write("link/state.json", "not json")?;
        assert!(matches!(
            load_link_state(malformed.path(), "solstone"),
            LinkStateRead::Malformed
        ));

        let unreadable = TempJournal::new()?;
        fs::create_dir_all(unreadable.path().join("link/state.json"))?;
        assert!(matches!(
            load_link_state(unreadable.path(), "solstone"),
            LinkStateRead::Unreadable
        ));
        Ok(())
    }

    #[test]
    fn state_uses_the_supplied_default_for_empty_or_nontext_labels() -> Result<(), Box<dyn Error>> {
        let journal = TempJournal::new()?;

        journal.write(
            "link/state.json",
            r#"{"instance_id":"home-1","home_label":""}"#,
        )?;
        match load_link_state(journal.path(), "Default Home") {
            LinkStateRead::Present(value) => assert_eq!(value.home_label, "Default Home"),
            _ => return Err("empty state label was not defaulted".into()),
        }

        journal.write(
            "link/state.json",
            r#"{"instance_id":"home-1","home_label":42}"#,
        )?;
        match load_link_state(journal.path(), "Default Home") {
            LinkStateRead::Present(value) => assert_eq!(value.home_label, "Default Home"),
            _ => return Err("nontext state label was not defaulted".into()),
        }
        Ok(())
    }

    #[test]
    fn state_accepts_only_integer_locked_at_values() -> Result<(), Box<dyn Error>> {
        let journal = TempJournal::new()?;
        let cases = [
            ("1700000000", Some(1_700_000_000)),
            ("\"1700000000\"", None),
            ("true", None),
            ("1700000000.5", None),
        ];

        for (locked_at, expected) in cases {
            let payload = format!("{{\"instance_id\":\"home-1\",\"locked_at\":{locked_at}}}");
            journal.write("link/state.json", &payload)?;
            match load_link_state(journal.path(), "solstone") {
                LinkStateRead::Present(value) => assert_eq!(value.locked_at, expected),
                _ => return Err("valid state was not loaded".into()),
            }
        }
        Ok(())
    }

    #[test]
    fn token_prefers_service_then_legacy_account_without_creating_files()
    -> Result<(), Box<dyn Error>> {
        let journal = TempJournal::new()?;
        assert!(matches!(
            load_link_service_token(journal.path()),
            LinkServiceTokenRead::Missing
        ));
        assert!(!journal.path().join("link").join("tokens").exists());

        journal.write(
            "tokens/account.json",
            r#"{"service_token":"wrong-path-token"}"#,
        )?;
        assert!(matches!(
            load_link_service_token(journal.path()),
            LinkServiceTokenRead::Missing
        ));

        journal.write(
            "link/tokens/account.json",
            r#"{"service_token":"current-token","account_token":"legacy-token"}"#,
        )?;
        match load_link_service_token(journal.path()) {
            LinkServiceTokenRead::Present(value) => assert!(value.as_str() == "current-token"),
            _ => return Err("current service token was not loaded".into()),
        }

        journal.write(
            "link/tokens/account.json",
            r#"{"service_token":"","account_token":"legacy-token"}"#,
        )?;
        match load_link_service_token(journal.path()) {
            LinkServiceTokenRead::Present(value) => assert!(value.as_str() == "legacy-token"),
            _ => return Err("legacy service token was not loaded".into()),
        }

        journal.write(
            "link/tokens/account.json",
            r#"{"service_token":false,"account_token":"legacy-token"}"#,
        )?;
        match load_link_service_token(journal.path()) {
            LinkServiceTokenRead::Present(value) => assert!(value.as_str() == "legacy-token"),
            _ => return Err("legacy service token was not loaded".into()),
        }

        journal.write(
            "link/tokens/account.json",
            r#"{"service_token":5,"account_token":"legacy-token"}"#,
        )?;
        assert!(matches!(
            load_link_service_token(journal.path()),
            LinkServiceTokenRead::Missing
        ));
        Ok(())
    }

    #[test]
    fn malformed_and_unreadable_tokens_do_not_supply_a_cached_value() -> Result<(), Box<dyn Error>>
    {
        let malformed = TempJournal::new()?;
        malformed.write("link/tokens/account.json", "not json")?;
        assert!(matches!(
            load_link_service_token(malformed.path()),
            LinkServiceTokenRead::Malformed
        ));

        let unreadable = TempJournal::new()?;
        fs::create_dir_all(unreadable.path().join("link/tokens/account.json"))?;
        assert!(matches!(
            load_link_service_token(unreadable.path()),
            LinkServiceTokenRead::Unreadable
        ));
        Ok(())
    }
}

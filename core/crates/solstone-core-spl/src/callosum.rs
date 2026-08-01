// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 sol pbc

//! The retained synchronous event seam shared by relay and supervision code.

use serde_json::Value;

/// Emits one owner-visible Callosum event with its JSON-object payload.
///
/// This belongs to the relay/service boundary: relay code supplies health and
/// tunnel events, while the service owns the enclosing lifecycle.
pub trait CallosumEmit: Send + Sync {
    /// Emits one named event with a JSON object payload.
    fn emit(&self, event: &'static str, payload: Value);
}

/// Operator verbosity for the supervised service.
///
/// `journal spl` is launched by the supervisor as `journal spl -v`, and the
/// Python it replaces logged its whole lifecycle at that level. Silence is a
/// regression an operator only discovers while diagnosing a live link.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum Verbosity {
    /// Only exceptional conditions reach stderr.
    #[default]
    Quiet,
    /// Lifecycle transitions reach stderr.
    Verbose,
    /// Lifecycle transitions plus the periodic health snapshot.
    Debug,
}

impl Verbosity {
    /// Resolves the level from the two CLI flags. `--debug` implies `--verbose`.
    #[must_use]
    pub fn from_flags(verbose: bool, debug: bool) -> Self {
        if debug {
            Self::Debug
        } else if verbose {
            Self::Verbose
        } else {
            Self::Quiet
        }
    }

    #[must_use]
    fn logs(self, event: &str) -> bool {
        match self {
            Self::Quiet => false,
            // `health` fires on every state change, every tunnel outcome and
            // every 30s. Python did not log it either; at -v it would bury the
            // transitions an operator is actually reading for.
            Self::Verbose => event != "health",
            Self::Debug => true,
        }
    }
}

/// Mirrors every callosum event to stderr at the requested verbosity.
///
/// Wrapping the emitter rather than sprinkling log statements keeps operator
/// output and the callosum vocabulary in lockstep by construction: a new
/// lifecycle event is observable the moment it is emitted, with nothing to
/// remember.
pub struct LoggingEmit {
    inner: std::sync::Arc<dyn CallosumEmit>,
    verbosity: Verbosity,
}

impl LoggingEmit {
    #[must_use]
    pub fn new(inner: std::sync::Arc<dyn CallosumEmit>, verbosity: Verbosity) -> Self {
        Self { inner, verbosity }
    }
}

impl CallosumEmit for LoggingEmit {
    fn emit(&self, event: &'static str, payload: Value) {
        if self.verbosity.logs(event) {
            match payload.as_object() {
                Some(fields) if !fields.is_empty() => {
                    let rendered = fields
                        .iter()
                        .map(|(key, value)| format!("{key}={value}"))
                        .collect::<Vec<_>>()
                        .join(" ");
                    eprintln!("spl service: {event} {rendered}");
                }
                _ => eprintln!("spl service: {event}"),
            }
        }
        self.inner.emit(event, payload);
    }
}

#[cfg(test)]
mod logging_tests {
    use super::*;
    use std::sync::Mutex;

    #[derive(Default)]
    struct Recorder(Mutex<Vec<&'static str>>);

    impl CallosumEmit for Recorder {
        fn emit(&self, event: &'static str, _payload: Value) {
            if let Ok(mut seen) = self.0.lock() {
                seen.push(event);
            }
        }
    }

    #[test]
    fn debug_implies_verbose_and_quiet_is_the_default() {
        assert_eq!(Verbosity::from_flags(false, false), Verbosity::Quiet);
        assert_eq!(Verbosity::from_flags(true, false), Verbosity::Verbose);
        assert_eq!(Verbosity::from_flags(false, true), Verbosity::Debug);
        assert_eq!(Verbosity::from_flags(true, true), Verbosity::Debug);
        assert_eq!(Verbosity::default(), Verbosity::Quiet);
    }

    #[test]
    fn verbose_reports_transitions_but_not_the_periodic_health_snapshot() {
        assert!(Verbosity::Verbose.logs("connected"));
        assert!(Verbosity::Verbose.logs("tunnel_pair"));
        assert!(!Verbosity::Verbose.logs("health"));
        assert!(Verbosity::Debug.logs("health"));
        assert!(!Verbosity::Quiet.logs("connected"));
    }

    #[test]
    fn wrapping_never_swallows_an_event_at_any_verbosity() {
        for verbosity in [Verbosity::Quiet, Verbosity::Verbose, Verbosity::Debug] {
            let recorder = std::sync::Arc::new(Recorder::default());
            let emitter = LoggingEmit::new(
                std::sync::Arc::clone(&recorder) as std::sync::Arc<dyn CallosumEmit>,
                verbosity,
            );
            emitter.emit("connecting", serde_json::json!({}));
            emitter.emit("tunnel_pair", serde_json::json!({"tunnel_id": "t-1"}));
            emitter.emit("health", serde_json::json!({"state": "connected"}));
            let seen = recorder.0.lock().expect("recorder lock");
            assert_eq!(seen.as_slice(), ["connecting", "tunnel_pair", "health"]);
        }
    }
}

// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 sol pbc

/// A cached service token that never exposes its contents through formatting.
#[derive(Clone, Eq, PartialEq)]
pub struct ServiceToken(String);

impl ServiceToken {
    /// Returns the token only for the authenticated request that needs it.
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

/// The latest value obtained from the local posture source.
#[derive(Clone, Debug, Eq, PartialEq)]
pub enum PostureInput {
    /// A successfully read posture value.
    Value(String),
    /// The posture source could not be read.
    ReadFailed,
}

/// The latest value obtained from the local service-token source.
#[derive(Clone, Eq, PartialEq)]
pub enum TokenInput {
    /// A successfully read token value.
    Value(String),
    /// The token source could not be read.
    ReadFailed,
}

/// The reason a relay connection is not permitted.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum RelayBlocked {
    /// No usable posture has been observed, or its value is not exactly `spl`.
    PostureNotSpl,
    /// Reading the posture source failed.
    PostureReadFailed,
    /// No non-empty cached service token is available.
    TokenMissing,
    /// Reading the service-token source failed.
    TokenReadFailed,
}

/// An allowed relay connection with its authenticated service token.
pub struct RelayPermit {
    token: ServiceToken,
}

impl RelayPermit {
    /// Returns the service token for the connection authentication header.
    pub fn token(&self) -> &ServiceToken {
        &self.token
    }
}

/// The current relay connection decision.
pub enum RelayDecision {
    /// The relay connection may open.
    Allowed(RelayPermit),
    /// The relay connection must remain closed.
    Blocked(RelayBlocked),
}

#[derive(Clone, Eq, PartialEq)]
enum PostureState {
    Unobserved,
    Value(String),
    ReadFailed,
}

enum TokenState {
    Empty,
    Cached(ServiceToken),
    ReadFailed,
}

/// Pure cache and admission state for the relay WebSocket.
///
/// Any posture transition clears the cached token before the next decision.
pub struct PostureGate {
    posture: PostureState,
    token: TokenState,
}

impl PostureGate {
    /// Creates a gate which blocks relay connections until both inputs are read.
    pub fn new() -> Self {
        Self {
            posture: PostureState::Unobserved,
            token: TokenState::Empty,
        }
    }

    /// Records the latest posture value and clears a token cache after a change.
    pub fn update_posture(&mut self, input: PostureInput) {
        let next = match input {
            PostureInput::Value(value) => PostureState::Value(value),
            PostureInput::ReadFailed => PostureState::ReadFailed,
        };

        if self.posture != next {
            self.token = TokenState::Empty;
        }
        self.posture = next;
    }

    /// Records the latest service-token read result.
    pub fn update_token(&mut self, input: TokenInput) {
        self.token = match input {
            TokenInput::Value(value) if value.is_empty() => TokenState::Empty,
            TokenInput::Value(value) => TokenState::Cached(ServiceToken(value)),
            TokenInput::ReadFailed => TokenState::ReadFailed,
        };
    }

    /// Returns the admission decision for a relay connection attempt.
    pub fn decision(&self) -> RelayDecision {
        match &self.posture {
            PostureState::Value(value) if value == "spl" => match &self.token {
                TokenState::Cached(token) => RelayDecision::Allowed(RelayPermit {
                    token: token.clone(),
                }),
                TokenState::Empty => RelayDecision::Blocked(RelayBlocked::TokenMissing),
                TokenState::ReadFailed => RelayDecision::Blocked(RelayBlocked::TokenReadFailed),
            },
            PostureState::ReadFailed => RelayDecision::Blocked(RelayBlocked::PostureReadFailed),
            PostureState::Unobserved | PostureState::Value(_) => {
                RelayDecision::Blocked(RelayBlocked::PostureNotSpl)
            }
        }
    }
}

impl Default for PostureGate {
    fn default() -> Self {
        Self::new()
    }
}

#[cfg(test)]
mod tests {
    use super::{PostureGate, PostureInput, RelayBlocked, RelayDecision, TokenInput};

    fn blocked(gate: &PostureGate) -> Option<RelayBlocked> {
        match gate.decision() {
            RelayDecision::Allowed(_) => None,
            RelayDecision::Blocked(reason) => Some(reason),
        }
    }

    #[test]
    fn unobserved_and_non_spl_postures_block_the_relay() {
        let mut gate = PostureGate::new();

        assert_eq!(blocked(&gate), Some(RelayBlocked::PostureNotSpl));

        gate.update_posture(PostureInput::Value("home".to_owned()));
        gate.update_token(TokenInput::Value("service-token".to_owned()));
        assert_eq!(blocked(&gate), Some(RelayBlocked::PostureNotSpl));
    }

    #[test]
    fn only_exact_spl_posture_is_eligible() {
        let mut gate = PostureGate::new();

        for posture in ["SPL", "spl ", " spl", "Spl"] {
            gate.update_posture(PostureInput::Value(posture.to_owned()));
            gate.update_token(TokenInput::Value("service-token".to_owned()));
            assert_eq!(blocked(&gate), Some(RelayBlocked::PostureNotSpl));
        }
    }

    #[test]
    fn exact_spl_with_nonempty_token_is_allowed() {
        let mut gate = PostureGate::new();
        gate.update_posture(PostureInput::Value("spl".to_owned()));
        gate.update_token(TokenInput::Value("service-token".to_owned()));

        match gate.decision() {
            RelayDecision::Allowed(permit) => assert_eq!(permit.token().as_str(), "service-token"),
            RelayDecision::Blocked(reason) => assert_eq!(reason, RelayBlocked::TokenMissing),
        }
    }

    #[test]
    fn empty_token_blocks_the_relay() {
        let mut gate = PostureGate::new();
        gate.update_posture(PostureInput::Value("spl".to_owned()));
        gate.update_token(TokenInput::Value(String::new()));

        assert_eq!(blocked(&gate), Some(RelayBlocked::TokenMissing));
    }

    #[test]
    fn posture_read_failure_remains_distinguishable() {
        let mut gate = PostureGate::new();
        gate.update_posture(PostureInput::ReadFailed);
        gate.update_token(TokenInput::Value("service-token".to_owned()));

        assert_eq!(blocked(&gate), Some(RelayBlocked::PostureReadFailed));
    }

    #[test]
    fn token_read_failure_remains_distinguishable() {
        let mut gate = PostureGate::new();
        gate.update_posture(PostureInput::Value("spl".to_owned()));
        gate.update_token(TokenInput::ReadFailed);

        assert_eq!(blocked(&gate), Some(RelayBlocked::TokenReadFailed));
    }

    #[test]
    fn posture_changes_invalidate_the_cached_token() {
        let mut gate = PostureGate::new();
        gate.update_posture(PostureInput::Value("spl".to_owned()));
        gate.update_token(TokenInput::Value("service-token".to_owned()));

        gate.update_posture(PostureInput::Value("home".to_owned()));
        assert_eq!(blocked(&gate), Some(RelayBlocked::PostureNotSpl));

        gate.update_posture(PostureInput::Value("spl".to_owned()));
        assert_eq!(blocked(&gate), Some(RelayBlocked::TokenMissing));
    }

    #[test]
    fn unchanged_posture_keeps_the_cached_token() {
        let mut gate = PostureGate::new();
        gate.update_posture(PostureInput::Value("spl".to_owned()));
        gate.update_token(TokenInput::Value("service-token".to_owned()));
        gate.update_posture(PostureInput::Value("spl".to_owned()));

        match gate.decision() {
            RelayDecision::Allowed(permit) => assert_eq!(permit.token().as_str(), "service-token"),
            RelayDecision::Blocked(reason) => assert_eq!(reason, RelayBlocked::TokenMissing),
        }
    }
}

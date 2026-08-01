// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 sol pbc

//! Supervision for the posture-gated SPL relay listener.
//!
//! The relay client and its listen task are deliberately separate ownership
//! domains. A client `stop()` cancels paired tunnels; the supervisor owns the
//! independently spawned listen task and must abort and await it afterwards.

use std::{convert::identity, future::Future, time::Duration};

use tokio::task::JoinHandle;

use crate::RelayStop;

/// The production posture polling interval.
pub const POSTURE_POLL_INTERVAL: Duration = Duration::from_secs(5);

/// A cached SPL service token that never exposes its contents through
/// formatting traits.
pub struct RelayServiceToken(String);

impl RelayServiceToken {
    /// Creates a token from a successfully read local credential.
    ///
    /// The token intentionally has no `Debug` or `Display` implementation;
    /// callers must likewise keep the returned wrapper out of diagnostics.
    #[must_use]
    pub const fn new(value: String) -> Self {
        Self(value)
    }

    /// Borrows the token solely for an authenticated relay request.
    #[must_use]
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

/// The supervisor-owned task for a separately running relay listener.
pub type RelayRunTask<RunError> = JoinHandle<Result<(), RunError>>;

/// A newly started relay controller and its separately owned listener task.
pub type StartedRelay<Client, RunError> = (Client, RelayRunTask<RunError>);

/// The result of one wait for the configured posture poll interval.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ServicePoll {
    /// The poll interval elapsed; perform another supervision cycle.
    Elapsed,
    /// The caller requested orderly service shutdown.
    Shutdown,
}

/// Class-only failures from the SPL service supervisor.
///
/// Underlying adapter, relay, and credential errors are intentionally not
/// retained: they may contain untrusted or secret-bearing context and none is
/// needed for the supervisor's externally visible contract.
#[derive(Clone, Copy, Debug, Eq, PartialEq, thiserror::Error)]
pub enum ServiceError {
    /// The relay client could not be started after posture and token admission.
    #[error("spl relay client start failed")]
    RelayStart,
    /// The relay client could not stop its paired tunnel work.
    #[error("spl relay client stop failed")]
    RelayStop,
    /// The independently owned relay listen task ended without supervision.
    #[error("spl relay client stopped unexpectedly")]
    RelayClientStoppedUnexpectedly,
}

/// Narrow environment contract for [`run_service`].
///
/// A future U4 relay adapter creates the client plus its independently spawned
/// listener task in [`ServiceDeps::start_relay`]. This separation is essential:
/// the client stop path alone does not close the listen WebSocket.
pub trait ServiceDeps {
    /// The relay controller whose tunnel work can be stopped.
    type Client: RelayStop;
    /// An opaque posture-reader failure. It is never surfaced by this module.
    type PostureError;
    /// An opaque relay-start failure. It is intentionally reduced to a class.
    type StartError;
    /// An opaque relay-run failure. It is intentionally reduced to a class.
    type RunError: Send + 'static;

    /// Reads the current local posture without normalizing its spelling.
    ///
    /// # Errors
    ///
    /// Returns an adapter-specific reader failure. Idle supervision treats it
    /// as `direct`, while a parked relay remains running through it.
    fn read_posture(&mut self) -> Result<String, Self::PostureError>;

    /// Reads a fresh local service token for a potential new relay listener.
    fn load_service_token(&mut self) -> Option<RelayServiceToken>;

    /// Starts a relay client and returns its separate, supervisor-owned run task.
    ///
    /// # Errors
    ///
    /// Returns an adapter-specific start failure. [`run_service`] reduces that
    /// failure to [`ServiceError::RelayStart`] without retaining its details.
    fn start_relay(
        &mut self,
        token: RelayServiceToken,
    ) -> Result<StartedRelay<Self::Client, Self::RunError>, Self::StartError>;

    /// Records the class-only missing-token notice, at most once per absence.
    fn missing_service_token(&mut self);

    /// Waits for either a poll interval or the caller's shutdown signal.
    ///
    /// Implementations use [`POSTURE_POLL_INTERVAL`] in production. Tests can
    /// deterministically drive the loop by honoring the passed interval and
    /// returning a scripted [`ServicePoll`].
    fn wait_for_poll(&mut self, interval: Duration) -> impl Future<Output = ServicePoll> + Send;

    /// Stops the service-owned Callosum connection. It runs exactly once from
    /// [`run_service`]'s final cleanup path, including failures.
    fn callosum_stop(&mut self) -> impl Future<Output = ()> + Send;
}

struct ParkedRelay<Client, RunError> {
    client: Client,
    run_task: Option<JoinHandle<Result<(), RunError>>>,
}

/// Runs the posture-gated SPL relay supervisor at the production poll cadence.
///
/// # Errors
///
/// Returns a stable class-only failure when a relay cannot start or stop, or
/// when its separately supervised listener task completes unexpectedly.
pub async fn run_service<Deps>(deps: &mut Deps) -> Result<(), ServiceError>
where
    Deps: ServiceDeps,
{
    run_service_with_interval(deps, POSTURE_POLL_INTERVAL).await
}

async fn run_service_with_interval<Deps>(
    deps: &mut Deps,
    poll_interval: Duration,
) -> Result<(), ServiceError>
where
    Deps: ServiceDeps,
{
    let mut parked: Option<ParkedRelay<Deps::Client, Deps::RunError>> = None;
    let mut missing_token_noticed = false;

    let outcome = 'service: loop {
        match parked.as_mut() {
            None => {
                let posture_read = deps.read_posture();
                if posture_read.is_err() {
                    // An idle posture-read failure is treated as `direct`, so the
                    // listener is NOT opened. This fails closed: opening a relay
                    // listener on an unreadable posture would defeat the gate.
                    missing_token_noticed = false;
                }
                let posture = posture_read.map_or(String::new(), identity);

                if posture == "spl" {
                    match deps.load_service_token() {
                        Some(token) => {
                            missing_token_noticed = false;
                            match deps.start_relay(token) {
                                Ok((client, run_task)) => {
                                    parked = Some(ParkedRelay {
                                        client,
                                        run_task: Some(run_task),
                                    });
                                }
                                Err(_) => break 'service Err(ServiceError::RelayStart),
                            }
                        }
                        None => {
                            if !missing_token_noticed {
                                deps.missing_service_token();
                                missing_token_noticed = true;
                            }
                        }
                    }
                } else {
                    // Includes idle posture-read failure, which is deliberately
                    // handled as `direct` rather than reusing stale admission.
                    missing_token_noticed = false;
                }

                if deps.wait_for_poll(poll_interval).await == ServicePoll::Shutdown {
                    break 'service Ok(());
                }
            }
            Some(active) => {
                if active
                    .run_task
                    .as_ref()
                    .is_some_and(JoinHandle::is_finished)
                {
                    if let Some(run_task) = active.run_task.take() {
                        let _ = run_task.await;
                    }
                    break 'service Err(ServiceError::RelayClientStoppedUnexpectedly);
                }

                match deps.read_posture() {
                    // A parked client must survive a transient read failure:
                    // stopping it would make this security boundary fail open.
                    Err(_) => {}
                    Ok(posture) if posture == "spl" => {}
                    Ok(_) => {
                        let Some(mut active) = parked.take() else {
                            break 'service Ok(());
                        };
                        let stopped = stop_parked_relay(&mut active).await;
                        if stopped.is_err() {
                            break 'service Err(ServiceError::RelayStop);
                        }
                        missing_token_noticed = false;
                        continue;
                    }
                }

                if deps.wait_for_poll(poll_interval).await == ServicePoll::Shutdown {
                    break 'service Ok(());
                }
            }
        }
    };

    let cleanup = match parked.as_mut() {
        Some(active) => stop_parked_relay(active).await,
        None => Ok(()),
    };
    deps.callosum_stop().await;

    match outcome {
        Err(error) => Err(error),
        Ok(()) => cleanup,
    }
}

async fn stop_parked_relay<Client, RunError>(
    parked: &mut ParkedRelay<Client, RunError>,
) -> Result<(), ServiceError>
where
    Client: RelayStop,
{
    let stopped = parked.client.stop().await;

    if let Some(run_task) = parked.run_task.take() {
        run_task.abort();
        let _ = run_task.await;
    }

    match stopped {
        Ok(()) => Ok(()),
        Err(_) => Err(ServiceError::RelayStop),
    }
}

#[cfg(test)]
mod tests {
    use std::{
        collections::VecDeque,
        convert::Infallible,
        convert::identity,
        future,
        sync::{Arc, Mutex},
        time::Duration,
    };

    use super::{
        POSTURE_POLL_INTERVAL, RelayServiceToken, ServiceDeps, ServiceError, ServicePoll,
        run_service, run_service_with_interval,
    };
    use crate::RelayStop;

    #[derive(Debug)]
    struct TestPostureError;

    enum RunPlan {
        Pending,
        Completed,
    }

    #[derive(Default)]
    struct TestState {
        events: Vec<&'static str>,
        intervals: Vec<Duration>,
    }

    struct TestDeps {
        postures: VecDeque<Result<String, TestPostureError>>,
        tokens: VecDeque<Option<RelayServiceToken>>,
        polls: VecDeque<ServicePoll>,
        runs: VecDeque<RunPlan>,
        fail_start: bool,
        state: Arc<Mutex<TestState>>,
    }

    impl TestDeps {
        fn new(
            postures: Vec<Result<String, TestPostureError>>,
            tokens: Vec<Option<RelayServiceToken>>,
            polls: Vec<ServicePoll>,
            runs: Vec<RunPlan>,
            state: Arc<Mutex<TestState>>,
        ) -> Self {
            Self {
                postures: postures.into(),
                tokens: tokens.into(),
                polls: polls.into(),
                runs: runs.into(),
                fail_start: false,
                state,
            }
        }

        fn record(&self, event: &'static str) {
            with_state(&self.state, |state| state.events.push(event));
        }
    }

    impl ServiceDeps for TestDeps {
        type Client = TestClient;
        type PostureError = TestPostureError;
        type StartError = ();
        type RunError = Infallible;

        fn read_posture(&mut self) -> Result<String, Self::PostureError> {
            self.postures
                .pop_front()
                .map_or(Err(TestPostureError), identity)
        }

        fn load_service_token(&mut self) -> Option<RelayServiceToken> {
            self.tokens.pop_front().flatten()
        }

        fn start_relay(
            &mut self,
            _token: RelayServiceToken,
        ) -> Result<super::StartedRelay<Self::Client, Self::RunError>, Self::StartError> {
            if self.fail_start {
                return Err(());
            }
            self.record("start");
            let plan = self.runs.pop_front().map_or(RunPlan::Pending, |plan| plan);
            let run_task = match plan {
                RunPlan::Pending => {
                    let state = Arc::clone(&self.state);
                    tokio::spawn(async move {
                        let _marker = RunCancellation { state };
                        future::pending::<()>().await;
                        Ok::<(), Infallible>(())
                    })
                }
                RunPlan::Completed => tokio::spawn(async { Ok::<(), Infallible>(()) }),
            };
            Ok((
                TestClient {
                    state: Arc::clone(&self.state),
                },
                run_task,
            ))
        }

        fn missing_service_token(&mut self) {
            self.record("missing-token");
        }

        fn wait_for_poll(
            &mut self,
            interval: Duration,
        ) -> impl Future<Output = ServicePoll> + Send {
            with_state(&self.state, |state| state.intervals.push(interval));
            let poll = self
                .polls
                .pop_front()
                .map_or(ServicePoll::Shutdown, |poll| poll);
            async move {
                tokio::task::yield_now().await;
                poll
            }
        }

        fn callosum_stop(&mut self) -> impl Future<Output = ()> + Send {
            self.record("callosum-stop");
            future::ready(())
        }
    }

    struct TestClient {
        state: Arc<Mutex<TestState>>,
    }

    #[derive(Debug, thiserror::Error)]
    #[error("test stop failure")]
    struct TestStopError;

    impl RelayStop for TestClient {
        type Error = TestStopError;

        fn stop(&mut self) -> impl Future<Output = Result<(), Self::Error>> + Send {
            let state = Arc::clone(&self.state);
            async move {
                with_state(&state, |state| state.events.push("stop"));
                Ok(())
            }
        }
    }

    struct RunCancellation {
        state: Arc<Mutex<TestState>>,
    }

    impl Drop for RunCancellation {
        fn drop(&mut self) {
            with_state(&self.state, |state| state.events.push("run-cancelled"));
        }
    }

    fn token() -> RelayServiceToken {
        RelayServiceToken::new("test-service-token".to_owned())
    }

    fn state() -> Arc<Mutex<TestState>> {
        Arc::new(Mutex::new(TestState::default()))
    }

    fn read_events(state: &Arc<Mutex<TestState>>) -> Vec<&'static str> {
        with_state(state, |state| state.events.clone())
    }

    fn read_intervals(state: &Arc<Mutex<TestState>>) -> Vec<Duration> {
        with_state(state, |state| state.intervals.clone())
    }

    fn with_state<Result>(
        state: &Arc<Mutex<TestState>>,
        operation: impl FnOnce(&mut TestState) -> Result,
    ) -> Result {
        match state.lock() {
            Ok(mut state) => operation(&mut state),
            Err(poisoned) => operation(&mut poisoned.into_inner()),
        }
    }

    #[tokio::test]
    async fn idle_posture_read_failure_fails_closed_without_starting_a_relay() {
        let state = state();
        let mut deps = TestDeps::new(
            vec![Err(TestPostureError)],
            Vec::new(),
            vec![ServicePoll::Shutdown],
            Vec::new(),
            Arc::clone(&state),
        );

        let result = run_service_with_interval(&mut deps, Duration::ZERO).await;

        assert!(result.is_ok());
        assert_eq!(read_events(&state), ["callosum-stop"]);
        assert_eq!(read_intervals(&state), [Duration::ZERO]);
    }

    #[tokio::test]
    async fn parked_posture_read_failure_keeps_the_existing_relay_until_shutdown() {
        let state = state();
        let mut deps = TestDeps::new(
            vec![Ok("spl".to_owned()), Err(TestPostureError)],
            vec![Some(token())],
            vec![ServicePoll::Elapsed, ServicePoll::Shutdown],
            vec![RunPlan::Pending],
            Arc::clone(&state),
        );

        let result = run_service_with_interval(&mut deps, Duration::ZERO).await;

        assert!(result.is_ok());
        assert_eq!(
            read_events(&state),
            ["start", "stop", "run-cancelled", "callosum-stop"]
        );
    }

    #[tokio::test]
    async fn only_exact_spl_starts_a_relay() {
        let state = state();
        let mut deps = TestDeps::new(
            vec![
                Ok("SPL".to_owned()),
                Ok("spl ".to_owned()),
                Ok(" spl".to_owned()),
                Ok("Spl".to_owned()),
            ],
            Vec::new(),
            vec![
                ServicePoll::Elapsed,
                ServicePoll::Elapsed,
                ServicePoll::Elapsed,
                ServicePoll::Shutdown,
            ],
            Vec::new(),
            Arc::clone(&state),
        );

        let result = run_service_with_interval(&mut deps, Duration::ZERO).await;

        assert!(result.is_ok());
        assert_eq!(read_events(&state), ["callosum-stop"]);
    }

    #[tokio::test]
    async fn exact_spl_with_a_token_starts_a_relay() {
        let state = state();
        let mut deps = TestDeps::new(
            vec![Ok("spl".to_owned())],
            vec![Some(token())],
            vec![ServicePoll::Shutdown],
            vec![RunPlan::Pending],
            Arc::clone(&state),
        );

        let result = run_service_with_interval(&mut deps, Duration::ZERO).await;

        assert!(result.is_ok());
        assert_eq!(
            read_events(&state),
            ["start", "stop", "run-cancelled", "callosum-stop"]
        );
    }

    #[tokio::test]
    async fn missing_token_notice_is_one_shot_and_resets_after_posture_change() {
        let state = state();
        let mut deps = TestDeps::new(
            vec![
                Ok("spl".to_owned()),
                Ok("spl".to_owned()),
                Ok("home".to_owned()),
                Ok("spl".to_owned()),
            ],
            vec![None, None, None],
            vec![
                ServicePoll::Elapsed,
                ServicePoll::Elapsed,
                ServicePoll::Elapsed,
                ServicePoll::Shutdown,
            ],
            Vec::new(),
            Arc::clone(&state),
        );

        let result = run_service_with_interval(&mut deps, Duration::ZERO).await;

        assert!(result.is_ok());
        assert_eq!(
            read_events(&state),
            ["missing-token", "missing-token", "callosum-stop"]
        );
    }

    #[tokio::test]
    async fn completed_relay_run_is_a_fatal_class_error() {
        let state = state();
        let mut deps = TestDeps::new(
            vec![Ok("spl".to_owned())],
            vec![Some(token())],
            vec![ServicePoll::Elapsed],
            vec![RunPlan::Completed],
            Arc::clone(&state),
        );

        let result = run_service_with_interval(&mut deps, Duration::ZERO).await;

        assert_eq!(result, Err(ServiceError::RelayClientStoppedUnexpectedly));
        assert_eq!(
            result.err().map(|error| error.to_string()),
            Some("spl relay client stopped unexpectedly".to_owned())
        );
        assert_eq!(read_events(&state), ["start", "stop", "callosum-stop"]);
    }

    #[tokio::test]
    async fn posture_departure_stops_tunnels_before_cancelling_the_run_task() {
        let state = state();
        let mut deps = TestDeps::new(
            vec![
                Ok("spl".to_owned()),
                Ok("home".to_owned()),
                Ok("home".to_owned()),
            ],
            vec![Some(token())],
            vec![ServicePoll::Elapsed, ServicePoll::Shutdown],
            vec![RunPlan::Pending],
            Arc::clone(&state),
        );

        let result = run_service_with_interval(&mut deps, Duration::ZERO).await;

        assert!(result.is_ok());
        assert_eq!(
            read_events(&state),
            ["start", "stop", "run-cancelled", "callosum-stop"]
        );
    }

    #[tokio::test]
    async fn final_callosum_hook_runs_when_relay_start_fails() {
        let state = state();
        let mut deps = TestDeps::new(
            vec![Ok("spl".to_owned())],
            vec![Some(token())],
            Vec::new(),
            Vec::new(),
            Arc::clone(&state),
        );
        deps.fail_start = true;

        let result = run_service(&mut deps).await;

        assert_eq!(result, Err(ServiceError::RelayStart));
        assert_eq!(read_events(&state), ["callosum-stop"]);
        assert_eq!(read_intervals(&state), Vec::<Duration>::new());
        assert_eq!(POSTURE_POLL_INTERVAL, Duration::from_secs(5));
    }
}

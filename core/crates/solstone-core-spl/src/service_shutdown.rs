// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 sol pbc

//! Supervised shutdown of the relay client and its listen task.
//!
//! C4 requires that a posture transition away from exact `spl` leaves the
//! process with no WebSocket to the relay. Calling the client `stop()` method
//! cancels tunnels, but does not close the listen WebSocket. The supervisor
//! must therefore cancel and await the separately-running listen task after
//! the client has stopped.
//!
//! The native supervisor keeps the same order: stop tunnel work, then cancel
//! and await the independently-owned listen task.

use std::{error::Error, future::Future};

use tokio::task::{JoinError, JoinHandle};

/// A relay client whose tunnel work can be stopped before its listen task ends.
pub trait RelayStop {
    /// The error returned when stopping tunnel work fails.
    type Error: Error + Send + Sync + 'static;

    /// Stops relay tunnel work without closing the separately-owned listen task.
    fn stop(&mut self) -> impl Future<Output = Result<(), Self::Error>> + Send;
}

/// A failure while stopping a relay client and its listen task.
#[derive(Debug, thiserror::Error)]
pub enum ServiceShutdownError<ClientError, RunError>
where
    ClientError: Error + 'static,
    RunError: Error + 'static,
{
    /// Stopping the relay client's tunnel work failed.
    #[error("failed to stop relay client")]
    ClientStop(#[source] ClientError),
    /// The listen task ended with an unexpected application failure.
    #[error("relay listen task failed during shutdown")]
    ListenRun(#[source] RunError),
    /// The listen task ended abnormally instead of being cancelled.
    #[error("relay listen task join failed during shutdown")]
    ListenJoin(#[source] JoinError),
}

/// Stops relay tunnel work, then cancels and awaits the relay listen task.
///
/// A cancelled listen task is the expected result. A task that finished with
/// an application or join failure before cancellation is returned to the
/// supervisor as a typed error.
pub async fn stop_relay_run<Client, RunError>(
    client: &mut Client,
    run_task: JoinHandle<Result<(), RunError>>,
) -> Result<(), ServiceShutdownError<Client::Error, RunError>>
where
    Client: RelayStop,
    RunError: Error + Send + Sync + 'static,
{
    client
        .stop()
        .await
        .map_err(ServiceShutdownError::ClientStop)?;

    run_task.abort();
    match run_task.await {
        Ok(Ok(())) => Ok(()),
        Ok(Err(error)) => Err(ServiceShutdownError::ListenRun(error)),
        Err(error) if error.is_cancelled() => Ok(()),
        Err(error) => Err(ServiceShutdownError::ListenJoin(error)),
    }
}

#[cfg(test)]
mod tests {
    use std::{
        convert::Infallible,
        future,
        sync::{Arc, Mutex},
    };

    use super::{RelayStop, ServiceShutdownError, stop_relay_run};

    #[derive(Debug, thiserror::Error)]
    #[error("test client stop failure")]
    struct TestClientError;

    struct TestClient {
        events: Arc<Mutex<Vec<&'static str>>>,
    }

    impl RelayStop for TestClient {
        type Error = TestClientError;

        async fn stop(&mut self) -> Result<(), Self::Error> {
            push_event(&self.events, "stop");
            Ok(())
        }
    }

    struct CancellationMarker {
        events: Arc<Mutex<Vec<&'static str>>>,
    }

    impl Drop for CancellationMarker {
        fn drop(&mut self) {
            push_event(&self.events, "listen-cancelled");
        }
    }

    #[tokio::test]
    async fn stops_before_cancelling_and_awaits_the_listen_task() {
        let events = Arc::new(Mutex::new(Vec::new()));
        let (started_send, started_receive) = tokio::sync::oneshot::channel();
        let task_events = Arc::clone(&events);
        let run_task = tokio::spawn(async move {
            let _ = started_send.send(());
            let _marker = CancellationMarker {
                events: task_events,
            };
            future::pending::<Result<(), Infallible>>().await
        });
        assert!(started_receive.await.is_ok());

        let mut client = TestClient {
            events: Arc::clone(&events),
        };
        let result = stop_relay_run(&mut client, run_task).await;

        assert!(result.is_ok());
        assert_eq!(read_events(&events), ["stop", "listen-cancelled"]);
    }

    #[derive(Debug, thiserror::Error)]
    #[error("test listen failure")]
    struct TestRunError;

    #[tokio::test]
    async fn reports_an_unexpected_listen_task_failure() {
        let events = Arc::new(Mutex::new(Vec::new()));
        let run_task = tokio::spawn(async { Err::<(), _>(TestRunError) });
        tokio::task::yield_now().await;
        let mut client = TestClient {
            events: Arc::clone(&events),
        };

        let result = stop_relay_run(&mut client, run_task).await;

        assert!(matches!(result, Err(ServiceShutdownError::ListenRun(_))));
        assert_eq!(read_events(&events), ["stop"]);
    }

    fn push_event(events: &Mutex<Vec<&'static str>>, event: &'static str) {
        match events.lock() {
            Ok(mut guard) => guard.push(event),
            Err(poisoned) => poisoned.into_inner().push(event),
        }
    }

    fn read_events(events: &Mutex<Vec<&'static str>>) -> Vec<&'static str> {
        match events.lock() {
            Ok(guard) => guard.clone(),
            Err(poisoned) => poisoned.into_inner().clone(),
        }
    }
}

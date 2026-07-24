// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 sol pbc

use std::cell::RefCell;
use std::collections::{HashMap, HashSet, VecDeque};
use std::io::{Error, ErrorKind, Result as IoResult};
use std::path::{Path, PathBuf};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use crate::error::ClientError;
use crate::transport::{
    ApiRequest, HttpResponse, SseRequest, SseStream, UploadRequest, memory_sse_stream,
    ordered_query_pairs,
};

pub trait HttpTransport {
    fn request(&self, request: ApiRequest) -> Result<HttpResponse, ClientError>;
    fn upload(&self, request: UploadRequest) -> Result<HttpResponse, ClientError>;
    fn open_sse(&self, request: SseRequest) -> Result<SseStream, ClientError>;
}

pub trait Clock {
    fn now(&self) -> SystemTime;
    fn monotonic(&self) -> Duration;
    fn sleep(&self, duration: Duration);
}

#[derive(Debug, Clone, PartialEq)]
pub enum ChatInput {
    SseEvent(serde_json::Value),
    SseEnded,
    PollTick,
    Interrupted,
}

pub trait ChatEventSource {
    fn open(&self, transport: &dyn HttpTransport) -> Result<(), ClientError>;
    fn next(&self, timeout: Duration, clock: &dyn Clock) -> ChatInput;
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProcessOutput {
    pub status: i32,
    pub stdout: Vec<u8>,
    pub stderr: Vec<u8>,
}

pub trait ProcessSpawner {
    fn run(&self, program: &str, args: &[String]) -> IoResult<ProcessOutput>;
}

pub trait BuildIdentityProvider {
    fn build_identity(&self, journal: &Path) -> Option<serde_json::Value>;
}

pub trait FileProvider {
    fn read(&self, path: &Path) -> IoResult<Vec<u8>>;
    fn read_to_string(&self, path: &Path) -> std::io::Result<String>;
    fn exists(&self, path: &Path) -> bool;
    fn canonicalize(&self, path: &Path) -> std::io::Result<PathBuf>;
}

#[derive(Debug, Clone, PartialEq)]
pub enum ExpectedHttpCall {
    Request {
        expected: ApiRequest,
        result: Result<HttpResponse, ClientError>,
    },
    Upload {
        expected: UploadRequest,
        result: Result<HttpResponse, ClientError>,
    },
    Sse {
        expected: SseRequest,
        chunks: Vec<Vec<u8>>,
    },
}

#[derive(Debug, Clone, PartialEq)]
pub enum RecordedHttpCall {
    Request {
        method: String,
        path: String,
        query: Vec<(String, String)>,
        json: Option<serde_json::Value>,
        headers: Vec<(String, String)>,
        timeout_policy: String,
    },
    Upload {
        path: String,
        files: Vec<RecordedMultipartFile>,
        data: Vec<(String, String)>,
        headers: Vec<(String, String)>,
        timeout_policy: String,
    },
    Sse {
        path: String,
        timeout_policy: String,
    },
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RecordedMultipartFile {
    pub field_name: String,
    pub filename: String,
    pub content_type: Option<String>,
    pub length: usize,
}

#[derive(Debug, Default)]
pub struct ScriptedHttpTransport {
    calls: RefCell<VecDeque<ExpectedHttpCall>>,
    recorded: RefCell<Vec<RecordedHttpCall>>,
}

#[derive(Debug)]
pub struct ScriptedChatEventSource {
    inputs: RefCell<VecDeque<ChatInput>>,
}

impl ScriptedChatEventSource {
    #[must_use]
    pub fn new(inputs: Vec<ChatInput>) -> Self {
        Self {
            inputs: RefCell::new(inputs.into()),
        }
    }
}

impl ChatEventSource for ScriptedChatEventSource {
    fn open(&self, transport: &dyn HttpTransport) -> Result<(), ClientError> {
        let _stream = transport.open_sse(SseRequest {
            path: "/sse/events".to_string(),
            policy: crate::transport::TimeoutPolicy::SseOpen,
        })?;
        Ok(())
    }

    fn next(&self, timeout: Duration, clock: &dyn Clock) -> ChatInput {
        match self.inputs.borrow_mut().pop_front() {
            Some(ChatInput::PollTick) | None => {
                clock.sleep(timeout);
                ChatInput::PollTick
            }
            Some(input) => input,
        }
    }
}

impl ScriptedHttpTransport {
    #[must_use]
    pub fn new(calls: Vec<ExpectedHttpCall>) -> Self {
        Self {
            calls: RefCell::new(calls.into()),
            recorded: RefCell::new(Vec::new()),
        }
    }

    pub fn assert_done(&self) {
        assert!(
            self.calls.borrow().is_empty(),
            "scripted HTTP calls were not exhausted"
        );
    }

    #[must_use]
    pub fn recorded(&self) -> Vec<RecordedHttpCall> {
        self.recorded.borrow().clone()
    }
}

impl HttpTransport for ScriptedHttpTransport {
    fn request(&self, request: ApiRequest) -> Result<HttpResponse, ClientError> {
        self.recorded.borrow_mut().push(RecordedHttpCall::Request {
            method: request.method.as_str().to_string(),
            path: request.path.clone(),
            query: ordered_query_pairs(&request.params),
            json: request.json.clone(),
            headers: request.headers.clone(),
            timeout_policy: request.policy.label().to_string(),
        });
        match self.calls.borrow_mut().pop_front() {
            Some(ExpectedHttpCall::Request { expected, result }) => {
                assert_eq!(request, expected);
                result
            }
            other => panic!("expected HTTP request call, got {other:?}"),
        }
    }

    fn upload(&self, request: UploadRequest) -> Result<HttpResponse, ClientError> {
        self.recorded.borrow_mut().push(RecordedHttpCall::Upload {
            path: request.path.clone(),
            files: request
                .files
                .iter()
                .map(|file| RecordedMultipartFile {
                    field_name: file.field_name.clone(),
                    filename: file.filename.clone(),
                    content_type: file.content_type.clone(),
                    length: file.body.len(),
                })
                .collect(),
            data: request
                .data
                .iter()
                .map(|field| (field.name.clone(), field.value.clone()))
                .collect(),
            headers: request.headers.clone(),
            timeout_policy: request.policy.label().to_string(),
        });
        match self.calls.borrow_mut().pop_front() {
            Some(ExpectedHttpCall::Upload { expected, result }) => {
                assert_eq!(request, expected);
                result
            }
            other => panic!("expected HTTP upload call, got {other:?}"),
        }
    }

    fn open_sse(&self, request: SseRequest) -> Result<SseStream, ClientError> {
        self.recorded.borrow_mut().push(RecordedHttpCall::Sse {
            path: request.path.clone(),
            timeout_policy: request.policy.label().to_string(),
        });
        match self.calls.borrow_mut().pop_front() {
            Some(ExpectedHttpCall::Sse { expected, chunks }) => {
                assert_eq!(request, expected);
                Ok(memory_sse_stream(chunks, request.policy))
            }
            other => panic!("expected HTTP SSE call, got {other:?}"),
        }
    }
}

#[derive(Debug)]
pub struct FakeClock {
    wall: RefCell<SystemTime>,
    monotonic: RefCell<Duration>,
}

impl FakeClock {
    #[must_use]
    pub fn new(wall: SystemTime) -> Self {
        Self {
            wall: RefCell::new(wall),
            monotonic: RefCell::new(Duration::ZERO),
        }
    }

    #[must_use]
    pub fn at_unix(seconds: u64) -> Self {
        Self::new(UNIX_EPOCH + Duration::from_secs(seconds))
    }

    pub fn advance(&self, duration: Duration) {
        *self.wall.borrow_mut() += duration;
        *self.monotonic.borrow_mut() += duration;
    }
}

impl Clock for FakeClock {
    fn now(&self) -> SystemTime {
        *self.wall.borrow()
    }

    fn monotonic(&self) -> Duration {
        *self.monotonic.borrow()
    }

    fn sleep(&self, duration: Duration) {
        self.advance(duration);
    }
}

#[derive(Debug, Default)]
pub struct FailingProcessSpawner;

impl ProcessSpawner for FailingProcessSpawner {
    fn run(&self, program: &str, args: &[String]) -> IoResult<ProcessOutput> {
        Err(Error::other(format!(
            "process spawning is disabled in native client tests: {program} {args:?}"
        )))
    }
}

#[derive(Debug, Clone, Default)]
pub struct FakeBuildIdentityProvider {
    value: Option<serde_json::Value>,
}

impl FakeBuildIdentityProvider {
    #[must_use]
    pub fn new(value: Option<serde_json::Value>) -> Self {
        Self { value }
    }
}

impl BuildIdentityProvider for FakeBuildIdentityProvider {
    fn build_identity(&self, _journal: &Path) -> Option<serde_json::Value> {
        self.value.clone()
    }
}

#[derive(Debug, Clone, Default)]
pub struct FixtureFileProvider {
    files: HashMap<PathBuf, Vec<u8>>,
    unreadable: HashSet<PathBuf>,
}

impl FixtureFileProvider {
    #[must_use]
    pub fn new(files: HashMap<PathBuf, Vec<u8>>) -> Self {
        Self {
            files,
            unreadable: HashSet::new(),
        }
    }

    pub fn insert(&mut self, path: impl Into<PathBuf>, body: impl Into<Vec<u8>>) {
        self.files.insert(path.into(), body.into());
    }

    pub fn mark_unreadable(&mut self, path: impl Into<PathBuf>) {
        self.unreadable.insert(path.into());
    }
}

impl FileProvider for FixtureFileProvider {
    fn read(&self, path: &Path) -> IoResult<Vec<u8>> {
        if self.unreadable.contains(path) {
            return Err(Error::new(
                ErrorKind::PermissionDenied,
                path.display().to_string(),
            ));
        }
        self.files
            .get(path)
            .cloned()
            .ok_or_else(|| Error::new(ErrorKind::NotFound, path.display().to_string()))
    }

    fn read_to_string(&self, path: &Path) -> IoResult<String> {
        String::from_utf8(self.read(path)?)
            .map_err(|error| Error::new(ErrorKind::InvalidData, error))
    }

    fn exists(&self, path: &Path) -> bool {
        self.files.contains_key(path)
    }

    fn canonicalize(&self, path: &Path) -> IoResult<PathBuf> {
        if self.exists(path) {
            Ok(path.to_path_buf())
        } else {
            Err(Error::new(ErrorKind::NotFound, path.display().to_string()))
        }
    }
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;
    use crate::transport::{HttpMethod, QueryParam, TimeoutPolicy};

    #[test]
    fn scripted_http_transport_validates_request_shape() {
        let request = ApiRequest {
            method: HttpMethod::Get,
            path: "/x".to_string(),
            params: vec![QueryParam::single("a", "b")],
            json: None,
            headers: vec![],
            policy: TimeoutPolicy::Api,
        };
        let response = HttpResponse {
            status: 200,
            headers: vec![],
            body: br#"{"ok":true}"#.to_vec(),
            policy: TimeoutPolicy::Api,
        };
        let transport = ScriptedHttpTransport::new(vec![ExpectedHttpCall::Request {
            expected: request.clone(),
            result: Ok(response.clone()),
        }]);
        assert_eq!(transport.request(request), Ok(response));
        transport.assert_done();
    }

    #[test]
    fn fake_clock_advances_without_sleeping() {
        let clock = FakeClock::at_unix(100);
        clock.sleep(Duration::from_secs(3));
        assert_eq!(clock.monotonic(), Duration::from_secs(3));
        assert_eq!(
            clock.now(),
            UNIX_EPOCH + Duration::from_secs(103),
            "wall clock advances with fake sleep"
        );
    }

    #[test]
    fn failing_spawner_always_errors() {
        let spawner = FailingProcessSpawner;
        assert!(spawner.run("python", &["-V".to_string()]).is_err());
    }

    #[test]
    fn deterministic_build_identity_fake_returns_configured_value() {
        let provider = FakeBuildIdentityProvider::new(Some(json!({"revision": "build-1"})));
        assert_eq!(
            provider.build_identity(Path::new("/tmp/journal")),
            Some(json!({"revision": "build-1"}))
        );
    }

    #[test]
    fn scripted_chat_source_opens_sse_and_advances_on_poll() {
        let request = SseRequest {
            path: "/sse/events".to_string(),
            policy: crate::transport::TimeoutPolicy::SseOpen,
        };
        let transport = ScriptedHttpTransport::new(vec![ExpectedHttpCall::Sse {
            expected: request,
            chunks: vec![],
        }]);
        let clock = FakeClock::at_unix(0);
        let source = ScriptedChatEventSource::new(vec![
            ChatInput::SseEnded,
            ChatInput::PollTick,
            ChatInput::Interrupted,
        ]);

        source.open(&transport).expect("open sse");
        assert_eq!(
            source.next(Duration::from_secs(2), &clock),
            ChatInput::SseEnded
        );
        assert_eq!(
            source.next(Duration::from_secs(2), &clock),
            ChatInput::PollTick
        );
        assert_eq!(clock.monotonic(), Duration::from_secs(2));
        assert_eq!(
            source.next(Duration::from_secs(2), &clock),
            ChatInput::Interrupted
        );
    }

    #[test]
    fn fixture_file_provider_reads_bytes_and_strings() {
        let mut provider = FixtureFileProvider::default();
        provider.insert(PathBuf::from("/tmp/file.txt"), b"hello".to_vec());
        assert_eq!(
            provider
                .read(Path::new("/tmp/file.txt"))
                .expect("read bytes"),
            b"hello"
        );
        assert_eq!(
            provider
                .read_to_string(Path::new("/tmp/file.txt"))
                .expect("read string"),
            "hello"
        );
        assert!(provider.exists(Path::new("/tmp/file.txt")));
        assert!(provider.canonicalize(Path::new("/tmp/missing")).is_err());
    }

    #[test]
    fn scripted_sse_returns_readable_stream() {
        let request = SseRequest {
            path: "/sse/events".to_string(),
            policy: TimeoutPolicy::SseOpen,
        };
        let transport = ScriptedHttpTransport::new(vec![ExpectedHttpCall::Sse {
            expected: request.clone(),
            chunks: vec![br#"data: {"ok":true}"#.to_vec()],
        }]);
        let mut stream = transport.open_sse(request).expect("open sse");
        let mut body = String::new();
        stream.body.read_to_string(&mut body).expect("read sse");
        assert_eq!(body, r#"data: {"ok":true}"#);
    }

    #[test]
    fn scripted_upload_validates_multipart_metadata() {
        let request = UploadRequest {
            path: "/upload".to_string(),
            files: vec![],
            data: vec![],
            headers: vec![("X-Test".to_string(), "1".to_string())],
            boundary: Some("BOUNDARY".to_string()),
            policy: TimeoutPolicy::Upload,
        };
        let response = HttpResponse {
            status: 200,
            headers: vec![],
            body: serde_json::to_vec(&json!({"ok": true})).expect("json"),
            policy: TimeoutPolicy::Upload,
        };
        let transport = ScriptedHttpTransport::new(vec![ExpectedHttpCall::Upload {
            expected: request.clone(),
            result: Ok(response.clone()),
        }]);
        assert_eq!(transport.upload(request), Ok(response));
    }
}

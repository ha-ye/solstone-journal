// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 sol pbc

mod pyannote;
mod session;
mod wespeaker;

use std::error::Error;
use std::fmt;

use solstone_core_speakers::{PYANNOTE_WINDOW_S, WESPEAKER_MEL_BINS};

pub use pyannote::PyannoteSegmenter;
pub use wespeaker::{SpeakerEmbedding, WespeakerEmbedder};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PlatformFamily {
    Apple,
    Other,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PlatformDescriptor {
    pub family: PlatformFamily,
}

impl PlatformDescriptor {
    pub fn current() -> Self {
        Self {
            family: if cfg!(target_vendor = "apple") {
                PlatformFamily::Apple
            } else {
                PlatformFamily::Other
            },
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SpeakerExecutionProvider {
    CoreMl,
    Cpu,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SpeakerOnnxError {
    EmptyProviderPlan,
    ProviderUnavailable {
        provider: &'static str,
    },
    InvalidFeatureMatrix {
        frames: usize,
        bins: usize,
    },
    InvalidAudioWindow {
        expected_samples: usize,
        actual_samples: usize,
    },
    InvalidModelIo {
        detail: String,
    },
    MissingOutput {
        name: String,
    },
    Ort {
        message: String,
    },
}

impl fmt::Display for SpeakerOnnxError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::EmptyProviderPlan => formatter.write_str("speaker ONNX provider plan is empty"),
            Self::ProviderUnavailable { provider } => {
                write!(
                    formatter,
                    "speaker ONNX provider is unavailable: {provider}"
                )
            }
            Self::InvalidFeatureMatrix { frames, bins } => write!(
                formatter,
                "speaker ONNX features must have at least one frame and {WESPEAKER_MEL_BINS} bins, got frames={frames} bins={bins}"
            ),
            Self::InvalidAudioWindow {
                expected_samples,
                actual_samples,
            } => write!(
                formatter,
                "pyannote ONNX audio window must have {expected_samples} samples ({PYANNOTE_WINDOW_S}s at 16 kHz), got {actual_samples}"
            ),
            Self::InvalidModelIo { detail } => {
                write!(formatter, "speaker ONNX model IO mismatch: {detail}")
            }
            Self::MissingOutput { name } => {
                write!(formatter, "speaker ONNX output was not returned: {name}")
            }
            Self::Ort { message } => write!(formatter, "speaker ONNX Runtime error: {message}"),
        }
    }
}

impl Error for SpeakerOnnxError {}

impl<R> From<ort::Error<R>> for SpeakerOnnxError {
    fn from(error: ort::Error<R>) -> Self {
        Self::Ort {
            message: error.to_string(),
        }
    }
}

pub fn default_speaker_execution_providers(
    platform: PlatformDescriptor,
) -> Vec<SpeakerExecutionProvider> {
    match platform.family {
        PlatformFamily::Apple => vec![
            SpeakerExecutionProvider::CoreMl,
            SpeakerExecutionProvider::Cpu,
        ],
        PlatformFamily::Other => vec![SpeakerExecutionProvider::Cpu],
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::test_support::source_tree_needle_count;

    #[test]
    fn provider_plan_selects_coreml_then_cpu_for_synthetic_apple() {
        let plan = default_speaker_execution_providers(PlatformDescriptor {
            family: PlatformFamily::Apple,
        });

        assert_eq!(
            plan,
            vec![
                SpeakerExecutionProvider::CoreMl,
                SpeakerExecutionProvider::Cpu
            ]
        );
    }

    #[test]
    fn provider_plan_selects_cpu_for_non_apple() {
        let plan = default_speaker_execution_providers(PlatformDescriptor {
            family: PlatformFamily::Other,
        });

        assert_eq!(plan, vec![SpeakerExecutionProvider::Cpu]);
    }

    #[test]
    fn session_builder_has_single_production_site_scans_src_tree() {
        let needle = concat!("Session", "::builder()?");
        let (visited_files, count) = source_tree_needle_count(needle);

        assert!(
            visited_files >= 4,
            "source walk visited too few Rust files: {visited_files}"
        );
        assert_eq!(count, 1);
    }
}

#[cfg(test)]
pub(crate) mod test_support {
    use serde_json::Value;
    use std::path::{Path, PathBuf};

    pub(crate) const FIXTURE: &str = include_str!("../../../fixtures/speaker_filterbank.json");

    pub(crate) fn repo_root() -> std::path::PathBuf {
        std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../..")
    }

    pub(crate) fn fixture() -> Value {
        serde_json::from_str(FIXTURE).expect("fixture JSON")
    }

    pub(crate) fn decode_waveform(fixture: &Value) -> Vec<f32> {
        let encoded = fixture["waveform"]["samples_f32_le_base64"]
            .as_str()
            .expect("waveform base64");
        let bytes = decode_base64(encoded);
        bytes
            .chunks_exact(4)
            .map(|chunk| f32::from_le_bytes(chunk.try_into().expect("f32 bytes")))
            .collect()
    }

    pub(crate) fn decode_base64(input: &str) -> Vec<u8> {
        let mut out = Vec::with_capacity(input.len() / 4 * 3);
        let mut quartet = [0_u8; 4];
        let mut len = 0;
        for byte in input.bytes().filter(|byte| !byte.is_ascii_whitespace()) {
            quartet[len] = match byte {
                b'A'..=b'Z' => byte - b'A',
                b'a'..=b'z' => byte - b'a' + 26,
                b'0'..=b'9' => byte - b'0' + 52,
                b'+' => 62,
                b'/' => 63,
                b'=' => 64,
                _ => panic!("invalid base64 byte: {byte}"),
            };
            len += 1;
            if len == 4 {
                out.push((quartet[0] << 2) | (quartet[1] >> 4));
                if quartet[2] != 64 {
                    out.push((quartet[1] << 4) | (quartet[2] >> 2));
                }
                if quartet[3] != 64 {
                    out.push((quartet[2] << 6) | quartet[3]);
                }
                len = 0;
            }
        }
        assert_eq!(len, 0);
        out
    }

    pub(crate) fn source_tree_needle_count(needle: &str) -> (usize, usize) {
        let src = Path::new(env!("CARGO_MANIFEST_DIR")).join("src");
        let mut files = Vec::new();
        collect_rust_files(&src, &mut files);
        let count = files
            .iter()
            .map(|path| {
                std::fs::read_to_string(path)
                    .unwrap_or_else(|error| panic!("failed to read {}: {error}", path.display()))
                    .matches(needle)
                    .count()
            })
            .sum();
        (files.len(), count)
    }

    fn collect_rust_files(path: &Path, files: &mut Vec<PathBuf>) {
        for entry in std::fs::read_dir(path)
            .unwrap_or_else(|error| panic!("failed to read {}: {error}", path.display()))
        {
            let entry = entry.expect("directory entry");
            let path = entry.path();
            if path.is_dir() {
                collect_rust_files(&path, files);
            } else if path.extension().and_then(|value| value.to_str()) == Some("rs") {
                files.push(path);
            }
        }
    }
}

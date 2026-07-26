// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 sol pbc

use std::error::Error;
use std::fmt;
use std::path::Path;

use ort::ep::{CPU, CoreML, ExecutionProviderDispatch};
use ort::session::Session;
use ort::value::{Tensor, TensorElementType, ValueType};
use solstone_core_speakers::{FeatureMatrix, WESPEAKER_EMBEDDING_SIZE, WESPEAKER_MEL_BINS};

const INPUT_NAME: &str = "feats";
const OUTPUT_NAME: &str = "embs";

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

#[derive(Debug, Clone, PartialEq)]
pub struct SpeakerEmbedding {
    values: [f32; WESPEAKER_EMBEDDING_SIZE],
}

impl SpeakerEmbedding {
    pub fn values(&self) -> &[f32; WESPEAKER_EMBEDDING_SIZE] {
        &self.values
    }
}

#[derive(Debug)]
pub struct WespeakerEmbedder {
    session: Session,
    input_name: String,
    output_name: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SpeakerOnnxError {
    EmptyProviderPlan,
    ProviderUnavailable { provider: &'static str },
    InvalidFeatureMatrix { frames: usize, bins: usize },
    InvalidModelIo { detail: String },
    MissingOutput { name: String },
    Ort { message: String },
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

impl WespeakerEmbedder {
    pub fn open(
        model_path: &Path,
        providers: &[SpeakerExecutionProvider],
    ) -> Result<Self, SpeakerOnnxError> {
        if providers.is_empty() {
            return Err(SpeakerOnnxError::EmptyProviderPlan);
        }
        let dispatches = provider_dispatches(providers)?;
        let session = Session::builder()?
            .with_execution_providers(dispatches)?
            .commit_from_file(model_path)?;
        validate_session_io(&session)?;
        Ok(Self {
            session,
            input_name: INPUT_NAME.to_string(),
            output_name: OUTPUT_NAME.to_string(),
        })
    }

    pub fn embed(
        &mut self,
        features: &FeatureMatrix,
    ) -> Result<SpeakerEmbedding, SpeakerOnnxError> {
        if features.frames() == 0 || features.bins() != WESPEAKER_MEL_BINS {
            return Err(SpeakerOnnxError::InvalidFeatureMatrix {
                frames: features.frames(),
                bins: features.bins(),
            });
        }
        let input = Tensor::from_array((
            [1_usize, features.frames(), WESPEAKER_MEL_BINS],
            features.data().to_vec().into_boxed_slice(),
        ))?;
        let mut outputs = self
            .session
            .run(ort::inputs![self.input_name.as_str() => input])?;
        let output =
            outputs
                .remove(&self.output_name)
                .ok_or_else(|| SpeakerOnnxError::MissingOutput {
                    name: self.output_name.clone(),
                })?;
        let (shape, values) = output.try_extract_tensor::<f32>()?;
        if shape[..] != [1, WESPEAKER_EMBEDDING_SIZE as i64] {
            return Err(SpeakerOnnxError::InvalidModelIo {
                detail: format!("output shape {shape} is not [1, {WESPEAKER_EMBEDDING_SIZE}]"),
            });
        }
        let mut embedding = [0.0; WESPEAKER_EMBEDDING_SIZE];
        embedding.copy_from_slice(values);
        Ok(SpeakerEmbedding { values: embedding })
    }
}

fn provider_dispatches(
    providers: &[SpeakerExecutionProvider],
) -> Result<Vec<ExecutionProviderDispatch>, SpeakerOnnxError> {
    let mut dispatches = Vec::with_capacity(providers.len());
    for provider in providers {
        match provider {
            SpeakerExecutionProvider::CoreMl => {
                if !cfg!(target_vendor = "apple") {
                    return Err(SpeakerOnnxError::ProviderUnavailable { provider: "coreml" });
                }
                dispatches.push(CoreML::default().build());
            }
            SpeakerExecutionProvider::Cpu => {
                dispatches.push(CPU::default().build());
            }
        }
    }
    Ok(dispatches)
}

fn validate_session_io(session: &Session) -> Result<(), SpeakerOnnxError> {
    let inputs = session.inputs();
    let outputs = session.outputs();
    if inputs.len() != 1 {
        return Err(SpeakerOnnxError::InvalidModelIo {
            detail: format!("expected one input, got {}", inputs.len()),
        });
    }
    if outputs.len() != 1 {
        return Err(SpeakerOnnxError::InvalidModelIo {
            detail: format!("expected one output, got {}", outputs.len()),
        });
    }
    expect_tensor(
        "input",
        inputs[0].name(),
        inputs[0].dtype(),
        INPUT_NAME,
        &[
            ExpectedDim::Any,
            ExpectedDim::Any,
            ExpectedDim::Exact(WESPEAKER_MEL_BINS as i64),
        ],
    )?;
    expect_tensor(
        "output",
        outputs[0].name(),
        outputs[0].dtype(),
        OUTPUT_NAME,
        &[
            ExpectedDim::Any,
            ExpectedDim::Exact(WESPEAKER_EMBEDDING_SIZE as i64),
        ],
    )?;
    Ok(())
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum ExpectedDim {
    Any,
    Exact(i64),
}

fn expect_tensor(
    label: &str,
    name: &str,
    value_type: &ValueType,
    expected_name: &str,
    expected_shape: &[ExpectedDim],
) -> Result<(), SpeakerOnnxError> {
    if name != expected_name {
        return Err(SpeakerOnnxError::InvalidModelIo {
            detail: format!("{label} name {name:?} is not {expected_name:?}"),
        });
    }
    let ValueType::Tensor { ty, shape, .. } = value_type else {
        return Err(SpeakerOnnxError::InvalidModelIo {
            detail: format!("{label} {name:?} is not a tensor"),
        });
    };
    if *ty != TensorElementType::Float32 {
        return Err(SpeakerOnnxError::InvalidModelIo {
            detail: format!("{label} {name:?} is {ty}, not float32"),
        });
    }
    if shape.len() != expected_shape.len() {
        return Err(SpeakerOnnxError::InvalidModelIo {
            detail: format!("{label} {name:?} shape {shape} has wrong rank"),
        });
    }
    for (index, (actual, expected)) in shape.iter().zip(expected_shape).enumerate() {
        match expected {
            ExpectedDim::Any => {}
            ExpectedDim::Exact(value) if actual == value => {}
            ExpectedDim::Exact(value) => {
                return Err(SpeakerOnnxError::InvalidModelIo {
                    detail: format!("{label} {name:?} dim {index} is {actual}, not {value}"),
                });
            }
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use solstone_core_speakers::{WESPEAKER_SAMPLE_RATE_HZ, compute_wespeaker_filterbank_cmn};

    const FIXTURE: &str = include_str!("../../../fixtures/speaker_filterbank.json");

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
    fn coreml_provider_open_is_rejected_on_non_apple_builds() {
        if cfg!(target_vendor = "apple") {
            return;
        }
        let error = provider_dispatches(&[SpeakerExecutionProvider::CoreMl]).unwrap_err();
        assert_eq!(
            error,
            SpeakerOnnxError::ProviderUnavailable { provider: "coreml" }
        );
    }

    #[test]
    fn committed_wespeaker_model_accepts_fixture_features_and_returns_256_floats() {
        let fixture = fixture();
        let audio = decode_waveform(&fixture);
        let features =
            compute_wespeaker_filterbank_cmn(&audio, WESPEAKER_SAMPLE_RATE_HZ).expect("features");
        let model_path = repo_root().join(
            "packages/solstone-journal-models/solstone_journal_models/assets/wespeaker-resnet34-256.onnx",
        );
        let mut embedder = WespeakerEmbedder::open(&model_path, &[SpeakerExecutionProvider::Cpu])
            .expect("embedder");

        let embedding = embedder.embed(&features).expect("embedding");

        assert_eq!(embedding.values().len(), WESPEAKER_EMBEDDING_SIZE);
        assert!(embedding.values().iter().all(|value| value.is_finite()));
    }

    #[test]
    fn session_builder_has_single_production_site() {
        let source = include_str!("lib.rs");
        let needle = concat!("Session", "::builder()?");
        assert_eq!(source.matches(needle).count(), 1);
    }

    fn repo_root() -> std::path::PathBuf {
        std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../..")
    }

    fn fixture() -> serde_json::Value {
        serde_json::from_str(FIXTURE).expect("fixture JSON")
    }

    fn decode_waveform(fixture: &serde_json::Value) -> Vec<f32> {
        let encoded = fixture["waveform"]["samples_f32_le_base64"]
            .as_str()
            .expect("waveform base64");
        let bytes = decode_base64(encoded);
        bytes
            .chunks_exact(4)
            .map(|chunk| f32::from_le_bytes(chunk.try_into().expect("f32 bytes")))
            .collect()
    }

    fn decode_base64(input: &str) -> Vec<u8> {
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
}

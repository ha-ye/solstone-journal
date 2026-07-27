// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 sol pbc

use crate::{FeatureMatrix, SpeakerFeatureError, compute_wespeaker_filterbank_cmn};

/// From solstone/observe/transcribe/main.py:140. This statement gate is
/// deliberately distinct from diarization::MIN_INTERVAL_S = 0.5.
pub const MIN_STATEMENT_DURATION_S: f64 = 0.3;

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct StatementSpan {
    pub statement_id: i64,
    pub start_s: Option<f64>,
    pub end_s: Option<f64>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct AdmittedStatement {
    pub statement_id: i64,
    pub features: FeatureMatrix,
    pub duration_s: f64,
}

#[derive(Debug, Clone, PartialEq)]
pub struct StatementAdmissionResult {
    pub admitted: Vec<AdmittedStatement>,
    pub skipped_count: usize,
}

pub fn admit_statement_features(
    audio: &[f32],
    sample_rate_hz: u32,
    spans: &[StatementSpan],
) -> Result<StatementAdmissionResult, SpeakerFeatureError> {
    let mut admitted = Vec::new();
    let audio_duration_s = audio.len() as f64 / sample_rate_hz as f64;
    let min_samples = (MIN_STATEMENT_DURATION_S * sample_rate_hz as f64) as usize;

    for span in spans {
        let (Some(start_s), Some(end_s)) = (span.start_s, span.end_s) else {
            continue;
        };
        if !start_s.is_finite() || !end_s.is_finite() {
            continue;
        }

        let start_s = start_s.max(0.0).min(audio_duration_s);
        let end_s = end_s.max(0.0).min(audio_duration_s);
        if end_s - start_s < MIN_STATEMENT_DURATION_S {
            continue;
        }

        let start_sample = (start_s * sample_rate_hz as f64) as usize;
        let end_sample = (end_s * sample_rate_hz as f64) as usize;
        let realized_start = start_sample.min(audio.len());
        let realized_end = end_sample.min(audio.len());
        let statement_audio = if realized_start < realized_end {
            &audio[realized_start..realized_end]
        } else {
            &[]
        };

        if statement_audio.len() < min_samples {
            continue;
        }

        let features = compute_wespeaker_filterbank_cmn(statement_audio, sample_rate_hz)?;
        if features.frames() == 0 {
            continue;
        }

        admitted.push(AdmittedStatement {
            statement_id: span.statement_id,
            features,
            // Python records the duration from pre-slice integer indices
            // (main.py:681), not from the realized slice length.
            duration_s: (end_sample - start_sample) as f64 / sample_rate_hz as f64,
        });
    }

    Ok(StatementAdmissionResult {
        skipped_count: spans.len() - admitted.len(),
        admitted,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn audio(seconds: f64) -> Vec<f32> {
        let samples = (seconds * 16_000.0) as usize;
        (0..samples).map(|idx| (idx as f32 * 0.001).sin()).collect()
    }

    fn span(statement_id: i64, start_s: Option<f64>, end_s: Option<f64>) -> StatementSpan {
        StatementSpan {
            statement_id,
            start_s,
            end_s,
        }
    }

    #[test]
    fn statement_admission_rejects_missing_and_nonnumeric_bounds() {
        let audio = audio(1.0);
        let spans = [
            span(1, None, Some(0.5)),
            span(2, Some(0.0), None),
            span(3, Some(0.1), Some(0.5)),
        ];

        let result = admit_statement_features(&audio, 16_000, &spans).expect("admission");

        assert_eq!(result.admitted.len(), 1);
        assert_eq!(result.admitted[0].statement_id, 3);
        assert_eq!(result.skipped_count, 2);
    }

    #[test]
    fn statement_admission_clamps_before_duration_gate() {
        let audio = audio(1.0);
        let spans = [
            span(1, Some(0.8), Some(1.2)),
            span(2, Some(-0.1), Some(0.31)),
        ];

        let result = admit_statement_features(&audio, 16_000, &spans).expect("admission");

        assert_eq!(result.admitted.len(), 1);
        assert_eq!(result.admitted[0].statement_id, 2);
        assert_eq!(result.skipped_count, 1);
    }

    #[test]
    fn statement_admission_uses_realized_slice_gate() {
        let audio = vec![0.0_f32; 70];
        let spans = [span(
            1,
            Some(0.114_285_714_285_714_28),
            Some(0.414_285_714_285_714_26),
        )];

        let result = admit_statement_features(&audio, 70, &spans).expect("admission");

        assert!(result.admitted.is_empty());
        assert_eq!(result.skipped_count, 1);
    }

    #[test]
    fn statement_admission_records_duration_from_indices() {
        let audio = audio(1.0);
        let spans = [span(1, Some(0.0), Some(0.333_375))];

        let result = admit_statement_features(&audio, 16_000, &spans).expect("admission");

        assert_eq!(result.admitted.len(), 1);
        assert_eq!(result.admitted[0].duration_s, 5334.0 / 16_000.0);
    }

    #[test]
    fn statement_span_past_audio_end_clamps_and_counts_once() {
        let audio = audio(1.0);
        let spans = [span(1, Some(0.6), Some(1.2))];

        let result = admit_statement_features(&audio, 16_000, &spans).expect("admission");

        assert_eq!(result.admitted.len(), 1);
        assert_eq!(result.skipped_count, 0);
        assert_eq!(result.admitted[0].duration_s, 0.4);
    }

    #[test]
    fn statement_span_passes_before_clamp_fails_after_clamp() {
        let audio = audio(1.0);
        let spans = [span(1, Some(0.8), Some(1.2))];

        let result = admit_statement_features(&audio, 16_000, &spans).expect("admission");

        assert!(result.admitted.is_empty());
        assert_eq!(result.skipped_count, 1);
    }

    #[test]
    fn statement_zero_length_span_skips() {
        let audio = audio(1.0);
        let spans = [span(1, Some(0.5), Some(0.5))];

        let result = admit_statement_features(&audio, 16_000, &spans).expect("admission");

        assert!(result.admitted.is_empty());
        assert_eq!(result.skipped_count, 1);
    }
}

// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 sol pbc

//! Content-type selection for decoded SPL blob entries.

/// Returns the media type assigned by the legacy SPL blob receiver.
///
/// Suffix matching is deliberately case-sensitive to preserve the existing
/// service behaviour.
pub fn blob_content_type(name: &str) -> &'static str {
    if name.ends_with(".jsonl") {
        "application/jsonl"
    } else if name.ends_with(".json") {
        "application/json"
    } else {
        "application/octet-stream"
    }
}

#[cfg(test)]
mod tests {
    use super::blob_content_type;

    #[test]
    fn selects_jsonl_before_json() {
        assert_eq!(blob_content_type("journal.jsonl"), "application/jsonl");
    }

    #[test]
    fn selects_json_for_the_json_suffix() {
        assert_eq!(blob_content_type("blob.json"), "application/json");
    }

    #[test]
    fn preserves_case_sensitive_suffix_matching() {
        assert_eq!(blob_content_type("blob.JSONL"), "application/octet-stream");
        assert_eq!(blob_content_type("blob.Json"), "application/octet-stream");
    }

    #[test]
    fn assigns_octet_stream_to_arbitrary_names() {
        assert_eq!(
            blob_content_type("archive.tar.gz"),
            "application/octet-stream"
        );
        assert_eq!(blob_content_type("json"), "application/octet-stream");
        assert_eq!(blob_content_type(""), "application/octet-stream");
    }
}

// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 sol pbc

use solstone_core_sol_client::decode::decode_response;
use solstone_core_sol_client::json_format::{
    json_compact_ascii, json_compact_utf8, json_pretty_ascii, json_pretty_utf8,
    sorted_json_pretty_ascii,
};
use solstone_core_sol_client::transport::{HttpResponse, TimeoutPolicy};

fn response(body: Vec<u8>) -> HttpResponse {
    HttpResponse {
        status: 200,
        headers: vec![],
        body,
        policy: TimeoutPolicy::Api,
    }
}

fn assert_all_modes(
    body: Vec<u8>,
    sorted_pretty_ascii: &str,
    pretty_ascii: &str,
    pretty_utf8: &str,
    compact_ascii: &str,
    compact_utf8: &str,
) {
    let decoded = decode_response(&response(body)).expect("decode response");
    assert_eq!(sorted_json_pretty_ascii(&decoded), sorted_pretty_ascii);
    assert_eq!(json_pretty_ascii(&decoded), pretty_ascii);
    assert_eq!(json_pretty_utf8(&decoded), pretty_utf8);
    assert_eq!(json_compact_ascii(&decoded), compact_ascii);
    assert_eq!(json_compact_utf8(&decoded), compact_utf8);
}

#[test]
fn float_literals_match_python_json_spelling() {
    // Python-parity: these literals must survive raw response decoding byte-exactly.
    for literal in [
        "22.184388937230562",
        "9.785157957944147",
        "-9.785157957944147",
    ] {
        assert_all_modes(
            format!(r#"{{"n":{literal}}}"#).into_bytes(),
            &format!("{{\n  \"n\": {literal}\n}}"),
            &format!("{{\n  \"n\": {literal}\n}}"),
            &format!("{{\n  \"n\": {literal}\n}}"),
            &format!("{{\"n\": {literal}}}"),
            &format!("{{\"n\": {literal}}}"),
        );
    }
}

#[test]
fn float_edge_literals_follow_renderer_policy() {
    // Renderer policy: edge spellings already match and guard serde_json output policy.
    for literal in [
        "5e-324",
        "1.7976931348623157e+308",
        "1e+300",
        "2.2250738585072014e-308",
    ] {
        assert_all_modes(
            format!(r#"{{"n":{literal}}}"#).into_bytes(),
            &format!("{{\n  \"n\": {literal}\n}}"),
            &format!("{{\n  \"n\": {literal}\n}}"),
            &format!("{{\n  \"n\": {literal}\n}}"),
            &format!("{{\"n\": {literal}}}"),
            &format!("{{\"n\": {literal}}}"),
        );
    }
}

#[test]
fn integers_stay_unchanged_and_sorted_mode_orders_keys() {
    // Renderer policy: integers stay unchanged while sorted mode reorders object keys.
    assert_all_modes(
        br#"{"z":42,"a":1}"#.to_vec(),
        "{\n  \"a\": 1,\n  \"z\": 42\n}",
        "{\n  \"z\": 42,\n  \"a\": 1\n}",
        "{\n  \"z\": 42,\n  \"a\": 1\n}",
        "{\"z\": 42, \"a\": 1}",
        "{\"z\": 42, \"a\": 1}",
    );
}

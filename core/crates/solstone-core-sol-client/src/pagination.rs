// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 sol pbc

use serde_json::Value;

use crate::decode::decode_response;
use crate::error::ClientError;
use crate::seam::HttpTransport;
use crate::transport::{ApiRequest, HttpMethod, QueryParam, TimeoutPolicy};

pub const DEFAULT_PAGE_SIZE: usize = 100;

pub fn paginate_collection(
    transport: &dyn HttpTransport,
    path: &str,
    params: Vec<QueryParam>,
    top: Option<usize>,
) -> Result<Vec<Value>, ClientError> {
    let mut collected = Vec::new();
    let mut offset = 0_usize;

    loop {
        let mut page_params = params.clone();
        page_params.push(QueryParam::single("limit", DEFAULT_PAGE_SIZE.to_string()));
        page_params.push(QueryParam::single("offset", offset.to_string()));
        let response = transport.request(ApiRequest {
            method: HttpMethod::Get,
            path: path.to_string(),
            params: page_params,
            json: None,
            headers: vec![],
            policy: TimeoutPolicy::Api,
        })?;
        let body = decode_response(&response)?;
        let Some(object) = body.as_object() else {
            return Err(malformed());
        };
        let Some(items) = object.get("items").and_then(Value::as_array) else {
            return Err(malformed());
        };
        let Some(total) = object.get("total").and_then(Value::as_i64) else {
            return Err(malformed());
        };

        collected.extend(items.iter().cloned());
        offset += items.len();

        if top.is_some_and(|top| collected.len() >= top) {
            break;
        }
        if (collected.len() as i64) >= total {
            break;
        }
        if items.is_empty() {
            return Err(malformed());
        }
    }

    if let Some(top) = top {
        collected.truncate(top);
    }
    Ok(collected)
}

fn malformed() -> ClientError {
    ClientError::MalformedSuccess { status: None }
}

#[cfg(test)]
mod tests {
    use serde_json::{Value, json};

    use super::*;
    use crate::error::MALFORMED_RESPONSE_MESSAGE;
    use crate::seam::{ExpectedHttpCall, RecordedHttpCall, ScriptedHttpTransport};
    use crate::transport::HttpResponse;

    fn response(body: Value) -> Result<HttpResponse, ClientError> {
        Ok(HttpResponse {
            status: 200,
            headers: vec![],
            body: serde_json::to_vec(&body).expect("response JSON"),
            policy: TimeoutPolicy::Api,
        })
    }

    fn expected(
        path: &str,
        params: Vec<QueryParam>,
        offset: usize,
        body: Value,
    ) -> ExpectedHttpCall {
        let mut page_params = params;
        page_params.push(QueryParam::single("limit", DEFAULT_PAGE_SIZE.to_string()));
        page_params.push(QueryParam::single("offset", offset.to_string()));
        ExpectedHttpCall::Request {
            expected: ApiRequest {
                method: HttpMethod::Get,
                path: path.to_string(),
                params: page_params,
                json: None,
                headers: vec![],
                policy: TimeoutPolicy::Api,
            },
            result: response(body),
        }
    }

    fn request_query(call: &RecordedHttpCall) -> Vec<(String, String)> {
        match call {
            RecordedHttpCall::Request { query, .. } => query.clone(),
            other => panic!("unexpected call {other:?}"),
        }
    }

    #[test]
    fn empty_collection_requests_one_page() {
        let transport = ScriptedHttpTransport::new(vec![expected(
            "/items",
            vec![],
            0,
            json!({"items": [], "total": 0}),
        )]);
        assert_eq!(
            paginate_collection(&transport, "/items", vec![], None).expect("page"),
            Vec::<Value>::new()
        );
        assert_eq!(
            request_query(&transport.recorded()[0]),
            vec![
                ("limit".to_string(), "100".to_string()),
                ("offset".to_string(), "0".to_string())
            ]
        );
    }

    #[test]
    fn one_page_collection_returns_items() {
        let transport = ScriptedHttpTransport::new(vec![expected(
            "/items",
            vec![QueryParam::single("state", "open")],
            0,
            json!({"items": [{"id": "a"}, {"id": "b"}], "total": 2}),
        )]);
        assert_eq!(
            paginate_collection(
                &transport,
                "/items",
                vec![QueryParam::single("state", "open")],
                None,
            )
            .expect("page"),
            vec![json!({"id": "a"}), json!({"id": "b"})]
        );
        assert_eq!(
            request_query(&transport.recorded()[0]),
            vec![
                ("state".to_string(), "open".to_string()),
                ("limit".to_string(), "100".to_string()),
                ("offset".to_string(), "0".to_string())
            ]
        );
    }

    #[test]
    fn multi_page_collection_advances_offset_by_items_returned() {
        let transport = ScriptedHttpTransport::new(vec![
            expected(
                "/items",
                vec![],
                0,
                json!({"items": [{"id": "a"}, {"id": "b"}], "total": 3}),
            ),
            expected(
                "/items",
                vec![],
                2,
                json!({"items": [{"id": "c"}], "total": 3}),
            ),
        ]);
        assert_eq!(
            paginate_collection(&transport, "/items", vec![], None).expect("pages"),
            vec![json!({"id": "a"}), json!({"id": "b"}), json!({"id": "c"})]
        );
    }

    #[test]
    fn top_truncation_stops_after_collected_reaches_top() {
        let transport = ScriptedHttpTransport::new(vec![expected(
            "/items",
            vec![],
            0,
            json!({"items": [{"id": "a"}, {"id": "b"}, {"id": "c"}], "total": 10}),
        )]);
        assert_eq!(
            paginate_collection(&transport, "/items", vec![], Some(2)).expect("top"),
            vec![json!({"id": "a"}), json!({"id": "b"})]
        );
        assert_eq!(transport.recorded().len(), 1);
    }

    #[test]
    fn premature_empty_page_is_malformed() {
        let transport = ScriptedHttpTransport::new(vec![
            expected(
                "/items",
                vec![],
                0,
                json!({"items": [{"id": "a"}], "total": 2}),
            ),
            expected("/items", vec![], 1, json!({"items": [], "total": 2})),
        ]);
        let error = paginate_collection(&transport, "/items", vec![], None)
            .expect_err("empty before total is malformed");
        assert_eq!(error.message(), MALFORMED_RESPONSE_MESSAGE);
    }

    #[test]
    fn malformed_total_is_malformed() {
        let transport = ScriptedHttpTransport::new(vec![expected(
            "/items",
            vec![],
            0,
            json!({"items": [], "total": "0"}),
        )]);
        let error = paginate_collection(&transport, "/items", vec![], None)
            .expect_err("bad total is malformed");
        assert_eq!(error.message(), MALFORMED_RESPONSE_MESSAGE);
    }

    #[test]
    fn malformed_items_is_malformed() {
        let transport = ScriptedHttpTransport::new(vec![expected(
            "/items",
            vec![],
            0,
            json!({"items": {}, "total": 0}),
        )]);
        let error = paginate_collection(&transport, "/items", vec![], None)
            .expect_err("bad items is malformed");
        assert_eq!(error.message(), MALFORMED_RESPONSE_MESSAGE);
    }
}

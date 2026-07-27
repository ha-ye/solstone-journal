// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 sol pbc

use std::sync::Arc;

use rustls::ClientConfig;
use spl_core::pairlink::Endpoint;
use spl_transport::connection::request_once;
use spl_transport::pairing::{
    DirectPairPrepareFuture, DirectPairSendFuture, DirectPairingSeam, PreparedDirectPairConnection,
};

pub(crate) struct SplDirectPairingSeam;

impl DirectPairingSeam for SplDirectPairingSeam {
    fn prepare<'a>(
        &'a self,
        config: Arc<ClientConfig>,
        endpoint: &'a Endpoint,
    ) -> DirectPairPrepareFuture<'a> {
        let host = endpoint.host.clone();
        let port = endpoint.port;
        Box::pin(async move {
            Ok(
                Box::new(SplPreparedDirectPairConnection { config, host, port })
                    as Box<dyn PreparedDirectPairConnection>,
            )
        })
    }
}

struct SplPreparedDirectPairConnection {
    config: Arc<ClientConfig>,
    host: String,
    port: u16,
}

impl PreparedDirectPairConnection for SplPreparedDirectPairConnection {
    fn send<'a>(
        self: Box<Self>,
        method: &'a str,
        path: &'a str,
        headers: &'a [(String, String)],
        body: &'a [u8],
    ) -> DirectPairSendFuture<'a> {
        let Self { config, host, port } = *self;
        Box::pin(
            async move { request_once(config, &host, port, method, path, headers, body).await },
        )
    }
}

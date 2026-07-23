// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 sol pbc

use std::collections::VecDeque;

use serde_json::Value;

pub fn iter_sse_events<I>(chunks: I) -> SseEvents<I::IntoIter>
where
    I: IntoIterator<Item = Vec<u8>>,
{
    SseEvents {
        chunks: chunks.into_iter(),
        decoder: SseDecoder::default(),
        done: false,
    }
}

#[derive(Debug, Default)]
pub struct SseDecoder {
    buffer: String,
    data_lines: Vec<String>,
    pending: VecDeque<Value>,
}

impl SseDecoder {
    pub fn push_chunk(&mut self, chunk: &[u8]) {
        self.buffer
            .push_str(String::from_utf8_lossy(chunk).as_ref());
        self.drain_lines();
    }

    pub fn pop_event(&mut self) -> Option<Value> {
        self.pending.pop_front()
    }

    fn drain_lines(&mut self) {
        while let Some(index) = self.buffer.find('\n') {
            let mut line = self.buffer[..index].to_string();
            self.buffer = self.buffer[index + 1..].to_string();
            if line.ends_with('\r') {
                line.pop();
            }
            self.handle_line(&line);
        }
    }

    fn handle_line(&mut self, line: &str) {
        if line.is_empty() {
            self.flush();
            return;
        }
        if line.starts_with(':') {
            return;
        }
        if let Some(rest) = line.strip_prefix("data:") {
            let value = rest.strip_prefix(' ').unwrap_or(rest);
            self.data_lines.push(value.to_string());
        }
    }

    fn flush(&mut self) {
        if self.data_lines.is_empty() {
            return;
        }
        let raw = self.data_lines.join("\n");
        self.data_lines.clear();
        let Ok(value) = serde_json::from_str::<Value>(&raw) else {
            return;
        };
        if value.is_object() {
            self.pending.push_back(value);
        }
    }
}

pub struct SseEvents<I>
where
    I: Iterator<Item = Vec<u8>>,
{
    chunks: I,
    decoder: SseDecoder,
    done: bool,
}

impl<I> Iterator for SseEvents<I>
where
    I: Iterator<Item = Vec<u8>>,
{
    type Item = Value;

    fn next(&mut self) -> Option<Self::Item> {
        loop {
            if let Some(event) = self.decoder.pop_event() {
                return Some(event);
            }
            if self.done {
                return None;
            }
            match self.chunks.next() {
                Some(chunk) => self.decoder.push_chunk(&chunk),
                None => {
                    self.done = true;
                    return None;
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;

    fn collect(chunks: &[&[u8]]) -> Vec<Value> {
        iter_sse_events(chunks.iter().map(|chunk| chunk.to_vec())).collect()
    }

    #[test]
    fn skips_comments_and_heartbeats() {
        assert_eq!(collect(&[b": heartbeat\n\n"]), Vec::<Value>::new());
    }

    #[test]
    fn ignores_event_name_and_yields_data_object() {
        assert_eq!(
            collect(&[b"event: progress\ndata: {\"ok\":true}\n\n"]),
            vec![json!({"ok": true})]
        );
    }

    #[test]
    fn joins_multiline_data_with_newline() {
        assert_eq!(
            collect(&[b"data: {\"a\":1,\ndata: \"b\":2}\n\n"]),
            vec![json!({"a": 1, "b": 2})]
        );
    }

    #[test]
    fn strips_only_one_leading_space_after_data_colon() {
        assert_eq!(
            collect(&[b"data:  {\"space\":true}\n\n"]),
            vec![json!({"space": true})]
        );
    }

    #[test]
    fn blank_line_flushes_frame() {
        assert_eq!(
            collect(&[b"data: {\"one\":1}\n\ndata: {\"two\":2}\n\n"]),
            vec![json!({"one": 1}), json!({"two": 2})]
        );
    }

    #[test]
    fn clean_eof_does_not_flush_partial_frame() {
        assert_eq!(collect(&[b"data: {\"partial\":true}"]), Vec::<Value>::new());
    }

    #[test]
    fn mid_stream_interruption_keeps_completed_frames_only() {
        assert_eq!(
            collect(&[b"data: {\"done\":true}\n\ndata: {\"partial\":true}"]),
            vec![json!({"done": true})]
        );
    }

    #[test]
    fn ignores_malformed_json() {
        assert_eq!(collect(&[b"data: not-json\n\n"]), Vec::<Value>::new());
    }

    #[test]
    fn ignores_non_object_json() {
        assert_eq!(collect(&[b"data: [1,2]\n\n"]), Vec::<Value>::new());
    }

    #[test]
    fn handles_crlf() {
        assert_eq!(
            collect(&[b"data: {\"crlf\":true}\r\n\r\n"]),
            vec![json!({"crlf": true})]
        );
    }

    #[test]
    fn decodes_utf8_with_replacement() {
        let events = collect(&[b"data: {\"bad\":\"\xff\"}\n\n"]);
        assert_eq!(events, vec![json!({"bad": "�"})]);
    }

    #[test]
    fn decoder_preserves_partial_frame_across_chunks() {
        let mut decoder = SseDecoder::default();
        decoder.push_chunk(b"data: {\"ok");
        assert_eq!(decoder.pop_event(), None);
        decoder.push_chunk(b"\":true}\n\n");
        assert_eq!(decoder.pop_event(), Some(json!({"ok": true})));
    }
}

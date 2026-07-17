// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 sol pbc

use pulldown_cmark::{Event, HeadingLevel, Parser, Tag, TagEnd};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MarkdownChunk {
    pub markdown: String,
}

pub fn chunk_markdown(input: &str) -> Vec<MarkdownChunk> {
    let mut chunks = Vec::new();
    let mut headers: Vec<(HeadingLevel, String)> = Vec::new();
    let mut heading_level = None;
    let mut heading_text = String::new();
    let mut block_text = String::new();
    let mut in_heading = false;

    for event in Parser::new(input) {
        match event {
            Event::Start(Tag::Heading { level, .. }) => {
                flush_block(&headers, &mut block_text, &mut chunks);
                in_heading = true;
                heading_level = Some(level);
                heading_text.clear();
            }
            Event::End(TagEnd::Heading(_)) => {
                if let Some(level) = heading_level.take() {
                    while headers.last().is_some_and(|(existing, _text)| {
                        heading_rank(*existing) >= heading_rank(level)
                    }) {
                        headers.pop();
                    }
                    let trimmed = heading_text.trim();
                    if !trimmed.is_empty() {
                        headers.push((level, trimmed.to_string()));
                    }
                }
                in_heading = false;
                heading_text.clear();
            }
            Event::Text(text) | Event::Code(text) => {
                if in_heading {
                    heading_text.push_str(&text);
                } else {
                    if !block_text.is_empty() && !block_text.ends_with([' ', '\n']) {
                        block_text.push(' ');
                    }
                    block_text.push_str(&text);
                }
            }
            Event::SoftBreak | Event::HardBreak => {
                if in_heading {
                    heading_text.push(' ');
                } else {
                    block_text.push('\n');
                }
            }
            Event::End(
                TagEnd::Paragraph
                | TagEnd::Item
                | TagEnd::CodeBlock
                | TagEnd::TableRow
                | TagEnd::BlockQuote(_),
            ) => flush_block(&headers, &mut block_text, &mut chunks),
            _ => {}
        }
    }
    flush_block(&headers, &mut block_text, &mut chunks);

    if chunks.is_empty() {
        let trimmed = input.trim();
        if !trimmed.is_empty() {
            chunks.push(MarkdownChunk {
                markdown: trimmed.to_string(),
            });
        }
    }

    chunks
}

fn flush_block(
    headers: &[(HeadingLevel, String)],
    block_text: &mut String,
    chunks: &mut Vec<MarkdownChunk>,
) {
    let trimmed = block_text.trim();
    if trimmed.is_empty() {
        block_text.clear();
        return;
    }

    let mut markdown = String::new();
    for (level, text) in headers {
        markdown.push_str(&"#".repeat(heading_rank(*level)));
        markdown.push(' ');
        markdown.push_str(text);
        markdown.push_str("\n\n");
    }
    markdown.push_str(trimmed);
    chunks.push(MarkdownChunk { markdown });
    block_text.clear();
}

fn heading_rank(level: HeadingLevel) -> usize {
    match level {
        HeadingLevel::H1 => 1,
        HeadingLevel::H2 => 2,
        HeadingLevel::H3 => 3,
        HeadingLevel::H4 => 4,
        HeadingLevel::H5 => 5,
        HeadingLevel::H6 => 6,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn chunks_preserve_header_context_and_non_empty_content() {
        let chunks = chunk_markdown("# Title\n\nIntro\n\n## Section\n\nBody");
        assert_eq!(chunks.len(), 2);
        assert_eq!(chunks[0].markdown, "# Title\n\nIntro");
        assert_eq!(chunks[1].markdown, "# Title\n\n## Section\n\nBody");
        assert!(chunks.iter().all(|chunk| !chunk.markdown.trim().is_empty()));
    }
}

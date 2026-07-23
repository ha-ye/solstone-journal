// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 sol pbc

use crate::command::{CommandContext, CommandOutput};
use crate::generated::inventory;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct InventoryEntry {
    pub surface: &'static str,
    pub path: &'static [&'static str],
    pub kind: &'static str,
    pub help: &'static str,
    pub params_json: &'static str,
    pub entry_type: &'static str,
    pub operation_id: &'static str,
    pub method: Option<&'static str>,
    pub route: Option<&'static str>,
    pub contract_operation_id: Option<&'static str>,
    pub handler: &'static str,
}

pub type Handler = for<'a> fn(CommandContext<'a>) -> CommandOutput;

#[must_use]
pub fn entries() -> &'static [InventoryEntry] {
    inventory::ENTRIES
}

#[must_use]
pub fn handler_bindings() -> &'static [Handler] {
    inventory::HANDLERS
}

#[must_use]
pub fn handler_for(path: &[&str]) -> Option<(&'static InventoryEntry, Handler)> {
    inventory::ENTRIES
        .iter()
        .zip(inventory::HANDLERS.iter().copied())
        .find(|(entry, _handler)| entry.path == path)
}

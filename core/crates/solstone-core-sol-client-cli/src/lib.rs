// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 sol pbc

use std::collections::BTreeMap;
use std::ffi::{OsStr, OsString};

use solstone_core_sol_client::aggregate;
use solstone_core_sol_client::command::{CommandContext, CommandOutput};
use solstone_core_sol_client::seam::{
    BuildIdentityProvider, ChatEventSource, ClientItemIdProvider, Clock, FileProvider,
    HttpTransport,
};

pub mod help;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Outcome {
    Migrated { path: Vec<OsString> },
    Chat { args: Vec<OsString> },
    Import { args: Vec<OsString> },
    MovedStub { name: OsString },
    Unsupported { args: Vec<OsString> },
}

pub struct DispatchSeams<'a> {
    pub transport: &'a dyn HttpTransport,
    pub clock: Option<&'a dyn Clock>,
    pub chat_events: Option<&'a dyn ChatEventSource>,
    pub files: Option<&'a dyn FileProvider>,
    pub build_identity: Option<&'a dyn BuildIdentityProvider>,
    pub client_item_ids: Option<&'a dyn ClientItemIdProvider>,
}

#[must_use]
pub fn evaluate_args(args: &[OsString]) -> Outcome {
    match args {
        [command, rest @ ..] if command == OsStr::new("call") => evaluate_call(rest),
        [command, rest @ ..] if command == OsStr::new("chat") => {
            match_generated_surface_path("sol-chat", &[String::from("chat")]).map_or_else(
                || Outcome::Unsupported {
                    args: args.to_vec(),
                },
                |_entry| Outcome::Chat {
                    args: rest.to_vec(),
                },
            )
        }
        [command, rest @ ..] if command == OsStr::new("import") => {
            match_generated_surface_path("sol-import", &[String::from("import")]).map_or_else(
                || Outcome::Unsupported {
                    args: args.to_vec(),
                },
                |_entry| Outcome::Import {
                    args: rest.to_vec(),
                },
            )
        }
        _ => Outcome::Unsupported {
            args: args.to_vec(),
        },
    }
}

#[must_use]
pub fn dispatch_sol_chat_with_seams(
    args: &[String],
    env: &BTreeMap<String, String>,
    stdin: &str,
    today: &str,
    seams: DispatchSeams<'_>,
) -> CommandOutput {
    let Some((_, handler)) = match_generated_surface_path("sol-chat", &[String::from("chat")])
    else {
        return CommandOutput::failure("Unsupported native sol command.\n", 64);
    };
    handler(CommandContext {
        args,
        env,
        stdin,
        today,
        transport: seams.transport,
        clock: seams.clock,
        chat_events: seams.chat_events,
        files: seams.files,
        build_identity: seams.build_identity,
        client_item_ids: seams.client_item_ids,
    })
}

#[must_use]
pub fn dispatch_sol_import_with_seams(
    args: &[String],
    env: &BTreeMap<String, String>,
    stdin: &str,
    today: &str,
    seams: DispatchSeams<'_>,
) -> CommandOutput {
    let Some((_, handler)) = match_generated_surface_path("sol-import", &[String::from("import")])
    else {
        return CommandOutput::failure("Unsupported native sol command.\n", 64);
    };
    handler(CommandContext {
        args,
        env,
        stdin,
        today,
        transport: seams.transport,
        clock: seams.clock,
        chat_events: None,
        files: seams.files,
        build_identity: seams.build_identity,
        client_item_ids: seams.client_item_ids,
    })
}

fn evaluate_call(args: &[OsString]) -> Outcome {
    let Some((entry, len)) = match_generated_path(args) else {
        return Outcome::Unsupported {
            args: args.to_vec(),
        };
    };
    match entry.entry_type {
        "http" | "local" => Outcome::Migrated {
            path: args[..len].to_vec(),
        },
        "moved-stub" => Outcome::MovedStub {
            name: args[0].clone(),
        },
        _ => Outcome::Unsupported {
            args: args.to_vec(),
        },
    }
}

#[must_use]
pub fn dispatch_sol_call(
    args: &[String],
    env: &BTreeMap<String, String>,
    stdin: &str,
    today: &str,
    transport: &dyn HttpTransport,
) -> CommandOutput {
    dispatch_sol_call_with_seams(
        args,
        env,
        stdin,
        today,
        DispatchSeams {
            transport,
            clock: None,
            chat_events: None,
            files: None,
            build_identity: None,
            client_item_ids: None,
        },
    )
}

#[must_use]
pub fn dispatch_sol_call_with_seams(
    args: &[String],
    env: &BTreeMap<String, String>,
    stdin: &str,
    today: &str,
    seams: DispatchSeams<'_>,
) -> CommandOutput {
    let Some((_, handler, len)) = match_generated_str_path(args) else {
        return CommandOutput::failure("Unsupported native sol command.\n", 64);
    };
    let remaining = args[len..].to_vec();
    handler(CommandContext {
        args: &remaining,
        env,
        stdin,
        today,
        transport: seams.transport,
        clock: seams.clock,
        chat_events: None,
        files: seams.files,
        build_identity: seams.build_identity,
        client_item_ids: seams.client_item_ids,
    })
}

#[must_use]
pub fn resolve_sol_call_leaf(args: &[String]) -> Option<&'static aggregate::InventoryEntry> {
    match_generated_str_path(args).map(|(entry, _handler, _len)| entry)
}

#[must_use]
pub fn resolve_surface_leaf(
    surface: &str,
    args: &[String],
) -> Option<&'static aggregate::InventoryEntry> {
    if surface == "sol-call" {
        return resolve_sol_call_leaf(args);
    }
    match_generated_surface_path(surface, args).map(|(entry, _handler)| entry)
}

fn match_generated_path(args: &[OsString]) -> Option<(&'static aggregate::InventoryEntry, usize)> {
    let utf8 = args
        .iter()
        .map(|arg| arg.to_str().map(str::to_string))
        .collect::<Option<Vec<_>>>()?;
    match_generated_str_path(&utf8).map(|(entry, _handler, len)| (entry, len))
}

fn match_generated_str_path(
    args: &[String],
) -> Option<(
    &'static aggregate::InventoryEntry,
    aggregate::Handler,
    usize,
)> {
    let max_len = aggregate::entries()
        .iter()
        .map(|entry| entry.path.len())
        .max()
        .unwrap_or(0);
    for len in (1..=args.len().min(max_len)).rev() {
        let path = args[..len].iter().map(String::as_str).collect::<Vec<_>>();
        if let Some((entry, handler)) = aggregate::handler_for(&path) {
            if entry.surface != "sol-call" {
                continue;
            }
            return Some((entry, handler, len));
        }
    }
    None
}

fn match_generated_surface_path(
    surface: &str,
    args: &[String],
) -> Option<(&'static aggregate::InventoryEntry, aggregate::Handler)> {
    let path = args.iter().map(String::as_str).collect::<Vec<_>>();
    aggregate::handler_for(&path).and_then(|(entry, handler)| {
        if entry.surface == surface {
            Some((entry, handler))
        } else {
            None
        }
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn args(values: &[&str]) -> Vec<OsString> {
        values.iter().map(OsString::from).collect()
    }

    #[test]
    fn routes_named_builtins_to_generated_authority() {
        assert_eq!(
            evaluate_args(&args(&["call", "identity"])),
            Outcome::MovedStub {
                name: OsString::from("identity")
            }
        );
    }

    #[test]
    fn classifies_call_leaf_as_migrated_shell() {
        assert_eq!(
            evaluate_args(&args(&["call", "activities", "list"])),
            Outcome::Migrated {
                path: args(&["activities", "list"])
            }
        );
    }

    #[test]
    fn routes_top_level_chat_to_chat_shell() {
        assert_eq!(
            evaluate_args(&args(&["chat", "hello"])),
            Outcome::Chat {
                args: args(&["hello"])
            }
        );
    }

    #[test]
    fn routes_top_level_import_to_import_shell() {
        assert_eq!(
            evaluate_args(&args(&["import", "sample.txt"])),
            Outcome::Import {
                args: args(&["sample.txt"])
            }
        );
    }

    #[test]
    fn classifies_unported_call_as_unsupported_without_spawn_path() {
        assert_eq!(
            evaluate_args(&args(&["call", "transcripts", "list"])),
            Outcome::Unsupported {
                args: args(&["transcripts", "list"])
            }
        );
    }
}

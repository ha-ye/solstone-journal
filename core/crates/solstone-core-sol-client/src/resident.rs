// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 sol pbc

use crate::command::{CommandContext, CommandOutput};

pub trait ShutdownSignal {
    fn wait(&self);
}

type ResidentServe<'a> = dyn FnOnce(&dyn ShutdownSignal) -> CommandOutput + 'a;

pub struct ResidentCommand<'a> {
    startup: String,
    serve: Box<ResidentServe<'a>>,
}

impl<'a> ResidentCommand<'a> {
    #[must_use]
    pub fn new(
        startup: impl Into<String>,
        serve: impl FnOnce(&dyn ShutdownSignal) -> CommandOutput + 'a,
    ) -> Self {
        Self {
            startup: startup.into(),
            serve: Box::new(serve),
        }
    }

    #[must_use]
    pub fn startup(&self) -> &str {
        &self.startup
    }

    pub fn serve(self, shutdown: &dyn ShutdownSignal) -> CommandOutput {
        (self.serve)(shutdown)
    }
}

pub type ResidentHandler =
    for<'a> fn(CommandContext<'a>) -> Result<ResidentCommand<'a>, CommandOutput>;

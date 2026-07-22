# Release Channel Adapters

## Purpose

The channel adapters let the release rail call external build and proof machines
without storing reach details in the repository. The rail owns request and
response validation; the adapters only move bytes, run the rail-approved commands,
and write the JSON files the rail already parses.

Adapter entry points:

- `scripts/channel_adapters/adapter_common.py`
- `scripts/channel_adapters/build_host_macos.py`
- `scripts/channel_adapters/proof_host.py`

## Safety Contract

Do not put real host names, users, addresses, ports, key paths, signing session
names, or machine-local working paths in source, tests, docs, or commit messages.
Operator-specific values belong only in the JSON config file.

The repo-wide scrub gate is `scripts/check_channel_adapter_scrub.py`.
It scans tracked UTF-8 text files; tracked NUL-binary, undecodable, or
unreadable paths are skipped and reported by count.

## Config Location

By default, the adapters read:

- `$XDG_CONFIG_HOME/solstone/channel-adapters.json`
- `~/.config/solstone/channel-adapters.json` when `XDG_CONFIG_HOME` is unset

`RELEASE_CHANNEL_ADAPTER_CONFIG` overrides the config path only. It does not
override individual fields.

## Schema

The config is a JSON object with `schema_version`, `build`, and `proof` keys.
The build lane and every proof target known to the rail must be present.

```json
{
  "schema_version": 1,
  "build": {
    "macos-arm64": {
      "mode": "ssh",
      "host": "build-host.example",
      "port": 2222,
      "user": "builder",
      "identity_file": "~/.ssh/solstone-channel-adapter-build",
      "extra_ssh_options": ["-o", "BatchMode=yes"],
      "remote_python": "python3",
      "remote_work_prefix": "/tmp/solstone-channel-adapter",
      "tmux_window": "adapter:build",
      "unlock_workdir": "~/projects/build-worktree"
    }
  },
  "proof": {
    "linux-x86_64-musl": {
      "mode": "local"
    },
    "linux-aarch64-musl": {
      "mode": "ssh",
      "host": "proof-aarch64.example",
      "user": "proof",
      "remote_python": "python3",
      "remote_work_prefix": "/tmp/solstone-channel-adapter"
    },
    "macos-arm64": {
      "mode": "ssh",
      "host": "proof-macos.example",
      "user": "proof",
      "remote_python": "python3",
      "remote_work_prefix": "/tmp/solstone-channel-adapter"
    }
  }
}
```

## Lane Modes

`mode: "ssh"` requires `host`. `port`, `user`, `identity_file`,
`extra_ssh_options`, `remote_python`, and `remote_work_prefix` are optional.

`mode: "local"` means run in-process with no SSH or SCP. It is valid only for
the `linux-x86_64-musl` proof lane.

The macOS build lane also requires `tmux_window` and `unlock_workdir`; these name
the already-prepared operator session used to run the existing build target.

## Tool Evidence

The macOS build adapter derives expected tool evidence from the rail pins. For
host-variant tools it emits the real observed banner and validates that the
parsed version identity matches the pin. Every emitted evidence string is passed
through the public-evidence validator before `response.json` is written.

## Failure Modes

Config validation happens before network or filesystem side effects. Missing or
unknown keys fail closed and name the config path plus
`RELEASE_CHANNEL_ADAPTER_CONFIG`.

Remote sentinel checks require both exit status zero and the expected success
token. Missing tokens fail the adapter even when the subprocess exits zero.

Retrieved proof files are checked for regular-file presence, non-empty bytes, and
the digest reported by the proof harness before the proof response is written.

## Operator Checklist

Create the config file outside the repository, keep its values out of shell
history and commits, and run the scrub gate before handing a branch to the rail.

When adding or changing proof targets in the rail, update the config at the same
time. Adapter tests assert that the configured lane set derives from the rail's
target map.

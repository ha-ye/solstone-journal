# L1 Design: `journal start --app-supervised`

Opt-in app-supervised start mode for the L2 macOS Swift child-runner. This
stage designs the contract only; no production code is implemented here.

## Contract Pinned For L2

- Selector constants live in new `solstone/think/app_supervised.py`:
  - `FLAG = "--app-supervised"`
  - `SELECTOR_ENV = "SOLSTONE_APP_SUPERVISED"`
  - `PARENT_FD_ENV = "SOLSTONE_PARENT_FD"`
- App-supervised selection is OR semantics:
  - true if `--app-supervised` is present in argv.
  - true if `SOLSTONE_APP_SUPERVISED=1`.
  - false otherwise. There is no "off" override.
- Env namespace rationale: new Swift child-runner cross-process vars use
  `SOLSTONE_`; `SOL_*` remains the internal supervisor-spawn convention
  (`SOL_SUPERVISOR_SPAWNED`, `SOL_SKIP_SUPERVISOR_CHECK`).
- Parent fd handshake:
  - Parent creates a pipe, keeps the write end open, and passes the read fd as
    `SOLSTONE_PARENT_FD=<n>`.
  - EOF on that read fd means parent is dead.
  - If `SOLSTONE_PARENT_FD` is absent or not parseable, default watched fd is
    stdin, fd `0`.
  - Usability gate: only EOF-watch a fd when it is readable and
    `stat.S_ISFIFO(os.fstat(fd).st_mode)` is true. Non-pipe, bad, or unreadable
    fd falls back to the `getppid()` belt. This prevents `/dev/null` stdin from
    causing instant false shutdown.
- Watcher integration:
  - In app-supervised mode only, `supervisor.main()` starts one daemon thread
    after children are started, after `_managed_procs = procs`, and after
    `signal_ready()`, immediately before `asyncio.run(supervise(...))`.
  - Primary path for usable pipe fd: block in `os.read(fd, ...)`; EOF returns
    parent-gone; `OSError` after the fd was accepted is fail-safe parent-gone.
    Nonempty bytes are ignored and the read continues.
  - Belt path for no usable pipe fd: poll `os.getppid() == 1` every about
    `1.0s`. This is the only polling path.
  - Convergence: parent-gone detection sends `os.kill(os.getpid(),
    signal.SIGTERM)` at most once and uses the existing `handle_shutdown`
    signal path. No second teardown sequence is added.
- Backstop: the same daemon thread waits `10.0s` from sending SIGTERM. If the
    supervisor process is still alive, it best-effort SIGKILLs every entry in
    `_managed_procs`, then calls `os._exit(1)`.
- Compressed graceful timeout ceiling:
  - Pin `HANDLE_SHUTDOWN_REAP_S = 3.0` and use it in `handle_shutdown`; do not
    branch the shared signal handler by mode.
  - Pin `APP_SUPERVISED_SHUTDOWN_CEILING_S = 10.0`.
  - In app-supervised mode only, compress the `finally` block knobs:
    `_task_queue.shutdown(timeout=APP_SUPERVISED_TASK_DRAIN_S)`,
    `_stop_process(..., timeout_cap=APP_SUPERVISED_CHILD_STOP_S)`, and
    `stop_callosum_in_process(join_timeout=APP_SUPERVISED_CALLOSUM_JOIN_S)`.
  - Pin the compressed knobs at `2.0s` each:
    `APP_SUPERVISED_TASK_DRAIN_S = 2.0`,
    `APP_SUPERVISED_CHILD_STOP_S = 2.0`, and
    `APP_SUPERVISED_CALLOSUM_JOIN_S = 2.0`.
  - Add `app_supervised_graceful_budget_s() -> float`, returning
    `HANDLE_SHUTDOWN_REAP_S + APP_SUPERVISED_TASK_DRAIN_S +
    APP_SUPERVISED_CALLOSUM_JOIN_S`.
  - New AC: the budget must stay strictly under
    `APP_SUPERVISED_SHUTDOWN_CEILING_S` (`3.0 + 2.0 + 2.0 = 7.0 < 10.0`) so a
    non-wedge app-supervised quit completes without the `os._exit` backstop.
  - `os._exit` remains reserved for a genuine wedge: a child that survives
    SIGKILL or hung teardown after the compressed normal path.
  - `os._exit` bypasses `finally` cleanup (`clear_ready`, callosum stop, lock
    release), but the kernel releases flock/fds on process exit, and SIGKILLed
    children free their ports.

## Residual Confirmations

- A. Skill refresh does no service-unit work.
  - `start._refresh_skill_links()` calls only `install_project(...)`
    (`solstone/think/start.py:41-42`).
  - `install_project()` discovers sources, creates project skill dirs, installs
    symlinks, removes stale symlinks, and returns a report
    (`solstone/think/skills_cli.py:384-399`).
  - Project source install is symlink/unlink only
    (`solstone/think/skills_cli.py:323-362`); stale cleanup is unlink only
    (`solstone/think/skills_cli.py:365-381`).
  - `rg "launchctl|systemctl|plist|systemd|subprocess" solstone/think/skills_cli.py`
    returns no matches. Service-unit work is in `solstone/think/service.py`, not
    skill refresh.
- B. Supervision and SIGTERM handling run on the main thread.
  - `supervisor.main()` is the synchronous CLI entry (`supervisor.py:2254`) and
    installs `signal.signal(SIGINT/SIGTERM, handle_shutdown)` before the
    supervise loop (`supervisor.py:2344-2346`).
  - `asyncio.run(supervise(...))` is called directly in that same `main()`
    (`supervisor.py:2502-2508`).
  - `rg "add_signal_handler|loop\\.add_signal_handler" solstone/think/supervisor.py`
    returns no matches, so asyncio does not replace the SIGTERM handler.
  - A daemon thread's `os.kill(os.getpid(), signal.SIGTERM)` therefore routes
    through `handle_shutdown()`, sets `shutdown_requested`, and raises
    `KeyboardInterrupt` out of the `asyncio.run` call
    (`supervisor.py:2194-2235`, `supervisor.py:2509-2511`).
- C. Local model server is covered by `_managed_procs`.
  - Darwin MLX path launches a managed process in `_start_mlx_local_server()`
    (`supervisor.py:1362-1399`), and `start_local_server()` returns it on Darwin
    (`supervisor.py:1424-1427`).
  - Non-Darwin local path launches a managed process at
    `supervisor.py:1471-1494`.
  - Startup appends the returned local process to `procs`
    (`supervisor.py:2439-2442`), then assigns `_managed_procs = procs`
    (`supervisor.py:2455-2456`).
  - Runtime `start_local` requests also append to `_managed_procs`
    (`supervisor.py:1190-1192`). Both `handle_shutdown` and the watcher backstop
    can sweep the local server.

## Proposed Code Shape

- Add `solstone/think/app_supervised.py`.
  - `is_app_supervised(argv: Sequence[str] | None = None) -> bool`
  - `resolve_parent_fd() -> int`
  - Constants only: `FLAG`, `SELECTOR_ENV`, `PARENT_FD_ENV`.
  - Keep it lightweight so `start.py` can scan argv/env without importing the
    full supervisor module.
- Update `solstone/think/start.py`.
  - Import `is_app_supervised`.
  - Change `_refresh_for_version_marker(skip_reconcile: bool = False) -> None`.
  - At current `start.py:53`, call `reconcile_installed_unit()` only when
    `not skip_reconcile`.
  - In `main()`, compute `app_supervised = is_app_supervised(sys.argv)`.
  - At current `start.py:60`, call `reconcile_installed_unit()` only when
    `not app_supervised`.
  - At current `start.py:61`, call
    `_refresh_for_version_marker(skip_reconcile=app_supervised)`.
  - Do not argparse or consume args in `start.py`; `supervisor.main()` reparses
    the same argv normally.
- Update `solstone/think/supervisor.py`.
  - Import `stat`, likely `fcntl`, and the app-supervised constants/helpers.
  - `parse_args()` adds `FLAG` as `action="store_true"` so
    `setup_cli(parser).parse_args()` accepts it (`utils.py:902-921`,
    `supervisor.py:2143-2191`).
  - Add `HANDLE_SHUTDOWN_REAP_S = 3.0`.
  - Add `APP_SUPERVISED_SHUTDOWN_CEILING_S = 10.0`.
  - Add `APP_SUPERVISED_TASK_DRAIN_S = 2.0`.
  - Add `APP_SUPERVISED_CHILD_STOP_S = 2.0`.
  - Add `APP_SUPERVISED_CALLOSUM_JOIN_S = 2.0`.
  - Add `PARENT_DEATH_POLL_INTERVAL_S = 1.0`.
  - Add `app_supervised_graceful_budget_s() -> float`.
  - Add `_parent_death_sigterm_sent = threading.Event()`.
  - Add `_parent_fd_is_usable(fd: int) -> bool`: return true only for a readable
    pipe/FIFO. Use `os.fstat` plus `stat.S_ISFIFO`; check access mode so a
    write-only pipe fd falls back instead of becoming a read-error shutdown.
  - Add `wait_until_parent_gone(parent_fd: int, *, poll_interval: float = PARENT_DEATH_POLL_INTERVAL_S) -> str`.
  - Add `enforce_parent_death_shutdown_deadline(reason: str, *, ceiling: float = APP_SUPERVISED_SHUTDOWN_CEILING_S, managed_procs: Iterable[RunnerManagedProcess] | None = None, sent_event: threading.Event | None = None, kill: Callable[[int, int], None] | None = None, exit_now: Callable[[int], NoReturn] | None = None, monotonic: Callable[[], float] | None = None, sleep: Callable[[float], None] | None = None) -> None`.
  - Add `_parent_death_watcher_main(parent_fd: int, *, poll_interval: float, ceiling: float) -> None`.
  - Add `start_parent_death_watcher(parent_fd: int | None = None, *, poll_interval: float = PARENT_DEATH_POLL_INTERVAL_S, ceiling: float = APP_SUPERVISED_SHUTDOWN_CEILING_S) -> threading.Thread`.
  - In `main()`, compute app-supervised selection with `is_app_supervised(sys.argv)`
    and start the watcher only between current `signal_ready()`
    (`supervisor.py:2501`) and `asyncio.run(...)` (`supervisor.py:2502`).

## Implementation Sequence

1. Add `app_supervised.py` constants and pure helper tests.
2. Update `supervisor.parse_args()` to accept `--app-supervised`.
3. Update `start.py` reconcile gating and marker-refresh signature.
4. Add supervisor watcher helper functions, isolated from `main()` first.
5. Wire watcher startup into `supervisor.main()` after `signal_ready()`.
6. Add tests in the order below.

## Test Plan

- `tests/test_app_supervised.py::test_is_app_supervised_uses_cli_flag_or_env_with_or_semantics`
  - Covers no selector false, flag true, env `1` true, env `0` false, and flag
    winning over env `0`.
- `tests/test_app_supervised.py::test_resolve_parent_fd_defaults_to_stdin_and_ignores_bad_env`
  - Covers absent env -> `0`, parseable env -> int, bad env -> `0`.
- `tests/test_supervisor.py::test_parse_args_app_supervised_flag`
  - Mirrors existing parse-arg tests around `tests/test_supervisor.py:132-149`;
    asserts parser accepts `--app-supervised` without disturbing positional port
    or `--no-*` flags.
- `tests/test_journal_start.py::test_app_supervised_start_skips_reconcile_but_refreshes_version_marker_artifacts`
  - Mirrors reconcile-gating tests in `tests/test_journal_start.py`.
  - With old marker and app-supervised selector, assert top-level reconcile and
    marker-internal reconcile are skipped, wrappers/skills/marker still run, and
    supervisor is invoked.
- `tests/test_supervisor.py::test_supervisor_starts_parent_watcher_only_after_ready_in_app_supervised_mode`
  - Mirrors `test_graceful_shutdown_calls_stop_process_for_each_managed_proc`
    (`tests/test_supervisor.py:211-279`).
  - Capture event order: `_managed_procs` assigned, `signal_ready`, watcher
    start, `asyncio.run`. Also assert default mode does not start the watcher.
- `tests/test_supervisor_shutdown.py::test_parent_watcher_waits_for_pipe_eof_without_ppid_poll`
  - Mirrors shutdown helper style in `tests/test_supervisor_shutdown.py`.
  - Pass an `os.pipe()` read fd, close the write fd, assert EOF reason and no
    `getppid()` polling.
- `tests/test_supervisor_shutdown.py::test_parent_watcher_non_pipe_and_bad_fd_fall_back_to_ppid`
  - Parametrize tempfile or `/dev/null` fd plus invalid fd. Assert fallback polls
    until mocked `getppid()` returns `1`; no EOF read is attempted on non-pipe.
- `tests/test_supervisor_shutdown.py::test_parent_watcher_shutdown_deadline_sends_sigterm_once_kills_children_and_hard_exits`
  - Patch `os.kill`, `os._exit`, clock, and sleep through injected callables.
  - Assert one self-SIGTERM, SIGKILL for running fake managed processes after
    the ceiling, and `exit_now(1)`. Repeat with pre-set `sent_event` to assert
    no duplicate self-SIGTERM.
- `tests/test_supervisor_shutdown.py::test_app_supervised_graceful_budget_stays_under_hard_ceiling`
  - New AC: assert `app_supervised_graceful_budget_s()` is strictly less than
    `APP_SUPERVISED_SHUTDOWN_CEILING_S`.
- `tests/test_supervisor_shutdown.py::test_stop_process_applies_timeout_cap`
  - Assert `_stop_process(..., timeout_cap=2.0)` applies `min(service_timeout,
    timeout_cap)` and the default path still uses the service timeout.

## Risks And Open Questions

- No hard conflict found between pinned decisions and current code.
- Implementation note: `stat.S_ISFIFO` alone does not prove the fd is readable.
  To satisfy the pinned "unreadable fd -> getppid fallback" rule, implement the
  usability gate with a read-access check before entering the blocking
  `os.read` loop.
- Backstop `os._exit(1)` intentionally bypasses existing cleanup, but normal
  app-supervised shutdown should finish under the hard ceiling because the
  `finally` block timeouts are compressed in that mode only. Tests should keep
  `os._exit` behind an injected `exit_now` so unit tests never terminate the
  pytest process.

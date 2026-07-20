# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Cross-platform background service management for solstone.

Usage:
    journal service install [--port PORT]  Install solstone as a background service
    journal service uninstall              Remove the background service
    journal service start                  Start the background service
    journal service stop                   Stop the background service
    journal service restart [--if-installed]  Restart the background service
    journal service status                 Show service installation and runtime status
    journal service logs                   View service logs
    journal service logs -f                Follow service logs

    journal up                             Start (if not running) and show status — service must already be installed
    journal down                           Stop the background service

Default convey port for installed services is 5015.
"""

from __future__ import annotations

import logging
import os
import plistlib
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from xml.parsers.expat import ExpatError

import psutil

from solstone.think.install_guard import (
    parse_wrapper,
    validate_journal_path_for_wrapper,
)
from solstone.think.readiness import clear_ready, wait_ready
from solstone.think.utils import get_journal, get_journal_info

SERVICE_LABEL = "org.solpbc.solstone"
SYSTEMD_UNIT = "solstone"
DEFAULT_SERVICE_PORT = 5015
READY_TIMEOUT_SECONDS = 120.0
SERVICE_START_TIMEOUT_SECONDS = 120
SERVICE_FILE_DESCRIPTOR_LIMIT = 4096
_LAUNCHD_UNLOAD_POLL_INTERVAL_S = 0.1
_LAUNCHD_UNLOAD_TIMEOUT_S = 30.0
_LAUNCHD_BOOTSTRAP_MAX_ATTEMPTS = 5
_LAUNCHD_BOOTSTRAP_RETRY_WAIT_S = 1.0
_SUPERVISOR_CONFLICT_LAUNCHCTL_TIMEOUT_S = 2.0
_JOURNAL_APP_EXECUTABLE_SUFFIX = "/journal.app/Contents/MacOS/journal"
SOLSTONE_APP_BUNDLE_PATH = "/Applications/solstone.app"

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Reconciled:
    was_stale: bool
    stale_binary: str | None
    stale_verb: str | None
    canonical_path: Path | None


@dataclass(frozen=True)
class ServiceTargetIdentity:
    """Read-only report on which install the installed service will invoke."""

    installed: bool
    target: str
    resolved_target: str
    matches_current_install: bool
    detail: str


@dataclass(frozen=True)
class ForeignLauncherMatch:
    label: str
    plist_path: str
    service_target: str


@dataclass(frozen=True)
class ForeignLauncherScan:
    matches: tuple[ForeignLauncherMatch, ...]
    incomplete_paths: tuple[str, ...]

    @property
    def is_incomplete(self) -> bool:
        return bool(self.incomplete_paths)


@dataclass(frozen=True)
class SupervisorConflictEvidence:
    """Read-only macOS topology evidence for competing journal supervisors."""

    plist_path: str
    plist_state: str
    label_state: str
    label_pid: int | None
    app_state: str
    app_pid: int | None
    app_executable: str | None
    detail: str
    foreign: ForeignLauncherScan

    @property
    def plist_installed(self) -> bool:
        return self.plist_state in {"present", "malformed"}

    @property
    def unknown_axes(self) -> tuple[str, ...]:
        axes: list[str] = []
        if self.plist_state == "unknown":
            axes.append("plist")
        if self.label_state == "unknown":
            axes.append("label")
        if self.app_state == "unknown":
            axes.append("app")
        return tuple(axes)

    @property
    def exact_label_conflict(self) -> bool:
        return self.app_state == "running" and (
            self.plist_installed or self.label_state == "loaded"
        )

    @property
    def foreign_conflict(self) -> bool:
        return bool(self.foreign.matches)

    @property
    def is_conflict(self) -> bool:
        return self.exact_label_conflict or self.foreign_conflict


def _ready_timeout_message() -> str:
    return (
        f"Service did not become ready within {READY_TIMEOUT_SECONDS:g}s — "
        "run 'journal service status' or 'journal doctor' for diagnostics"
    )


def _platform() -> str:
    """Return 'darwin', 'linux', or raise on unsupported."""
    if sys.platform == "darwin":
        return "darwin"
    elif sys.platform.startswith("linux"):
        return "linux"
    else:
        print(f"Error: unsupported platform '{sys.platform}'", file=sys.stderr)
        sys.exit(1)


def _plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{SERVICE_LABEL}.plist"


def _unit_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / f"{SYSTEMD_UNIT}.service"


def _managed_wrapper(binary: str) -> str:
    """Return absolute path to a managed user wrapper."""
    return str(Path.home() / ".local" / "bin" / binary)


def service_is_installed() -> bool:
    """Return whether the user service definition is installed."""
    return _plist_path().exists() if _platform() == "darwin" else _unit_path().exists()


def service_is_running() -> bool:
    """Return whether the background service is currently running."""
    if not service_is_installed():
        return False
    if _platform() == "darwin":
        result = subprocess.run(
            ["launchctl", "print", f"gui/{os.getuid()}/{SERVICE_LABEL}"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return False
        return "\n\tstate = running\n" in result.stdout
    result = subprocess.run(
        ["systemctl", "--user", "is-active", SYSTEMD_UNIT],
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() == "active"


def service_is_failed() -> bool:
    """Return whether the installed user service is in a failed state."""
    if not service_is_installed():
        return False
    if _platform() == "darwin":
        try:
            result = subprocess.run(
                ["launchctl", "print", f"gui/{os.getuid()}/{SERVICE_LABEL}"],
                capture_output=True,
                text=True,
                timeout=2.0,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        if result.returncode != 0:
            return False
        return (
            "\n\tstate = crashed\n" in result.stdout
            or "state = crashed" in result.stdout
        )
    try:
        result = subprocess.run(
            ["systemctl", "--user", "is-failed", SYSTEMD_UNIT],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.stdout.strip() == "failed"


def _collect_env() -> dict[str, str]:
    """Collect environment variables for the service file.

    Captures HOME and PATH (with venv bin prepended). Real PATH is read
    from os.environ so installed services inherit the shell's tool
    visibility. Falls back to /usr/local/bin:/usr/bin:/bin if PATH is unset.
    Sets PYTHONUNBUFFERED so service logs are flushed promptly.
    API keys are NOT written into service files — the supervisor reads them
    from journal.json at process startup via setup_cli().

    Never propagate SOLSTONE_JOURNAL into service files. Installed services
    invoke ~/.local/bin/journal, which is a managed wrapper that sets
    SOLSTONE_JOURNAL itself. The service's job is to start the wrapper, not
    to configure the journal path.
    """
    venv_bin = str(Path(sys.executable).parent)
    base_path = os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")
    path = ":".join(dict.fromkeys([venv_bin] + base_path.split(":")))

    return {
        "HOME": str(Path.home()),
        "PATH": path,
        "PYTHONUNBUFFERED": "1",
    }


def _generate_plist(
    env: dict[str, str],
    *,
    port: int = DEFAULT_SERVICE_PORT,
    journal_path: str,
) -> bytes:
    """Generate a launchd plist for the solstone supervisor."""
    validate_journal_path_for_wrapper(journal_path)
    journal = _managed_wrapper("journal")
    service_log = str(Path(journal_path) / "health" / "service.log")

    plist = {
        "Label": SERVICE_LABEL,
        "ProgramArguments": [journal, "start", str(port)],
        "EnvironmentVariables": env,
        "StandardOutPath": service_log,
        "StandardErrorPath": service_log,
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "SoftResourceLimits": {"NumberOfFiles": SERVICE_FILE_DESCRIPTOR_LIMIT},
    }
    return plistlib.dumps(plist)


def _not_loaded_markers() -> tuple[str, ...]:
    return (
        "could not find",
        "service not found",
        "no such process",
        "not currently loaded",
    )


def _parse_exec_start(line: str) -> list[str] | None:
    if not line.startswith("ExecStart="):
        return None
    value = line.removeprefix("ExecStart=").strip()
    if not value:
        return None
    return shlex.split(value)


def _exec_start_parts_from_lines(path: Path, lines: list[str]) -> list[str] | None:
    for line in lines:
        try:
            parts = _parse_exec_start(line.rstrip("\n"))
        except ValueError as exc:
            print(f"skipping {path}: invalid ExecStart: {exc}", file=sys.stderr)
            return None
        if parts is not None:
            return parts
    print(f"skipping {path}: no ExecStart", file=sys.stderr)
    return None


def _systemd_exec_start_parts(path: Path) -> tuple[list[str] | None, list[str]]:
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    parts = _exec_start_parts_from_lines(path, lines)
    return parts, lines


def _load_launchd_plist(path: Path) -> dict | None:
    try:
        with path.open("rb") as handle:
            data = plistlib.load(handle)
    except (plistlib.InvalidFileException, ValueError, ExpatError, OSError):
        return None
    return data if isinstance(data, dict) else None


def _launchd_program_arguments(path: Path) -> list[str] | None:
    """Return a launchd plist's ProgramArguments as strings (read-only).

    Returns None when the plist cannot be parsed or has no ProgramArguments
    list. Mirrors the inline read in ``_reconcile_launchd_plist`` but is a
    pure reader: it never prints or mutates.
    """
    data = _load_launchd_plist(path)
    if data is None:
        return None

    program_arguments = data.get("ProgramArguments")
    if not isinstance(program_arguments, list):
        return None

    return [str(arg) for arg in program_arguments]


def _contains_control_char(value: str) -> bool:
    return any(ord(char) < 0x20 or ord(char) == 0x7F for char in value)


def _display_safe(value: str) -> str:
    return "".join(
        "?" if ord(char) < 0x20 or ord(char) == 0x7F else char for char in value
    )


def _is_foreign_launcher_label(label: str) -> bool:
    return label != SERVICE_LABEL and not label.startswith(f"{SERVICE_LABEL}.")


def _keepalive_is_persistent(value: object) -> bool:
    if value is True:
        return True
    return isinstance(value, dict) and bool(value)


def _launchd_command_strings(data: dict) -> tuple[str, ...]:
    strings: list[str] = []
    program = data.get("Program")
    if isinstance(program, str):
        strings.append(program)
    program_arguments = data.get("ProgramArguments")
    if isinstance(program_arguments, list):
        strings.extend(str(arg) for arg in program_arguments)
    return tuple(strings)


def _is_app_path_continuation_char(char: str) -> bool:
    return char.isalnum() or char in "._-/"


def _has_leading_app_path_boundary(value: str, index: int) -> bool:
    return index == 0 or not _is_app_path_continuation_char(value[index - 1])


def _has_trailing_app_path_boundary(value: str, end: int) -> bool:
    return (
        end == len(value)
        or value[end] == "/"
        or not _is_app_path_continuation_char(value[end])
    )


def _mentions_solstone_app_bundle(value: str) -> bool:
    start = 0
    while True:
        index = value.find(SOLSTONE_APP_BUNDLE_PATH, start)
        if index == -1:
            return False
        end = index + len(SOLSTONE_APP_BUNDLE_PATH)
        if _has_leading_app_path_boundary(
            value, index
        ) and _has_trailing_app_path_boundary(value, end):
            return True
        start = index + 1


def _foreign_launcher_match(
    data: dict, path: Path, uid: int
) -> ForeignLauncherMatch | None:
    label = data.get("Label")
    if not isinstance(label, str) or not label:
        return None
    if not _is_foreign_launcher_label(label):
        return None
    if not _keepalive_is_persistent(data.get("KeepAlive")):
        return None
    if not any(
        _mentions_solstone_app_bundle(command)
        for command in _launchd_command_strings(data)
    ):
        return None
    return ForeignLauncherMatch(
        label=label,
        plist_path=str(path),
        service_target=f"gui/{uid}/{label}",
    )


def _scan_foreign_solstone_app_launchers() -> ForeignLauncherScan:
    scan_dir = _plist_path().parent
    try:
        if not scan_dir.is_dir():
            return ForeignLauncherScan((), ())
        paths = sorted(scan_dir.glob("*.plist"))
    except OSError:
        return ForeignLauncherScan((), (str(scan_dir),))

    matches: list[ForeignLauncherMatch] = []
    incomplete_paths: list[str] = []
    uid = os.getuid()
    for path in paths:
        path_text = str(path)
        if _contains_control_char(path_text):
            incomplete_paths.append(path_text)
            continue

        data = _load_launchd_plist(path)
        if data is None:
            incomplete_paths.append(path_text)
            continue

        label = data.get("Label")
        if isinstance(label, str) and _contains_control_char(label):
            incomplete_paths.append(path_text)
            continue

        match = _foreign_launcher_match(data, path, uid)
        if match is not None:
            matches.append(match)

    return ForeignLauncherScan(
        tuple(sorted(matches, key=lambda match: (match.label, match.plist_path))),
        tuple(sorted(incomplete_paths)),
    )


def _classify_unit_args(args: list[str]) -> tuple[str, str, list[str], bool] | None:
    if len(args) < 2:
        return None
    binary = Path(args[0]).name
    verb = args[1]
    rest = args[2:]
    if binary not in {"sol", "journal"}:
        return None
    is_canonical = binary == "journal" and verb == "start"
    return binary, verb, rest, is_canonical


def _canonical_start_args(rest: list[str]) -> list[str]:
    return [_managed_wrapper("journal"), "start", *rest]


def _reconcile_systemd_unit() -> Reconciled:
    path = _unit_path()
    if not path.exists():
        return Reconciled(False, None, None, None)

    parts, lines = _systemd_exec_start_parts(path)
    if parts is None:
        return Reconciled(False, None, None, path)

    classified = _classify_unit_args(parts)
    if classified is None:
        return Reconciled(False, None, None, path)

    binary, verb, rest, is_canonical = classified
    canonical = _canonical_start_args(rest)
    if is_canonical and parts == canonical:
        return Reconciled(False, None, None, path)

    if verb not in {"supervisor", "start"}:
        return Reconciled(False, None, None, path)

    new_exec = f"ExecStart={shlex.join(canonical)}\n"
    rewritten = []
    replaced = False
    for line in lines:
        if not replaced and line.startswith("ExecStart="):
            rewritten.append(new_exec if line.endswith("\n") else new_exec.rstrip("\n"))
            replaced = True
        else:
            rewritten.append(line)

    path.write_text("".join(rewritten), encoding="utf-8")
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    return Reconciled(True, binary, verb, path)


def _reconcile_launchd_plist() -> Reconciled:
    path = _plist_path()
    if not path.exists():
        return Reconciled(False, None, None, None)

    try:
        with path.open("rb") as handle:
            data = plistlib.load(handle)
    except (plistlib.InvalidFileException, ValueError, ExpatError) as exc:
        print(f"skipping {path}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return Reconciled(False, None, None, path)

    program_arguments = data.get("ProgramArguments")
    if not isinstance(program_arguments, list):
        print(f"skipping {path}: no ProgramArguments", file=sys.stderr)
        return Reconciled(False, None, None, path)

    args = [str(arg) for arg in program_arguments]
    classified = _classify_unit_args(args)
    if classified is None:
        return Reconciled(False, None, None, path)

    binary, verb, rest, is_canonical = classified
    canonical = _canonical_start_args(rest)
    if is_canonical and args == canonical:
        return Reconciled(False, None, None, path)

    if verb not in {"supervisor", "start"}:
        return Reconciled(False, None, None, path)

    data["ProgramArguments"] = canonical
    path.write_bytes(plistlib.dumps(data))

    uid = os.getuid()
    result = _reload_launchd_unit(uid, path)
    if result.returncode != 0:
        raise RuntimeError(f"launchctl bootstrap {path}: {result.stderr.strip()}")

    return Reconciled(True, binary, verb, path)


def reconcile_installed_unit() -> Reconciled:
    """Rewrite installed service units to the canonical ``journal start`` shape."""
    if sys.platform == "darwin":
        return _reconcile_launchd_plist()
    if sys.platform.startswith("linux"):
        return _reconcile_systemd_unit()
    return Reconciled(False, None, None, None)


def _resolve_service_target(target: str) -> Path:
    """Resolve a service-target binary to its real executable (read-only).

    The target is typically ``~/.local/bin/journal``, which may be:
      (a) a managed bash wrapper (source-checkout install) — not a symlink;
          parse it for the embedded ``sol_bin`` venv binary;
      (b) a symlink (packaged uv-tool / pipx install) — follow it;
      (c) a plain executable — resolve as-is.
    """
    path = Path(target)
    if path.is_symlink():
        return path.resolve()
    if path.is_file():
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            pass
        else:
            parsed = parse_wrapper(content)
            if parsed:
                return Path(parsed["sol_bin"]).resolve()
    return path.resolve()


def check_service_target_identity() -> ServiceTargetIdentity:
    """Report whether the installed user service targets the current install.

    Read-only. Inspects the launchd plist (darwin) or systemd unit (linux),
    extracts the service-target binary, resolves it through the managed
    wrapper / symlink, and compares it to the running install's bin
    directory. Returns a clean not-installed result (not an exception) when
    no service is installed or the platform is unsupported.
    """
    if sys.platform == "darwin":
        path = _plist_path()
        if not path.exists():
            return ServiceTargetIdentity(False, "", "", False, "service not installed")
        parts = _launchd_program_arguments(path)
    elif sys.platform.startswith("linux"):
        path = _unit_path()
        if not path.exists():
            return ServiceTargetIdentity(False, "", "", False, "service not installed")
        parts, _lines = _systemd_exec_start_parts(path)
    else:
        return ServiceTargetIdentity(False, "", "", False, "service not installed")

    if not parts:
        return ServiceTargetIdentity(
            True,
            "",
            "",
            False,
            f"service unit is malformed: no executable target in {path}",
        )

    raw_target = str(parts[0])
    resolved = _resolve_service_target(raw_target)
    expected = (Path(sys.executable).parent / "journal").resolve()
    matches = resolved == expected

    if matches:
        detail = f"service target matches current install: {raw_target} -> {resolved}"
    else:
        detail = (
            "service target mismatch: "
            f"{raw_target} resolves to {resolved}, expected {expected}"
        )

    return ServiceTargetIdentity(
        True,
        raw_target,
        str(resolved),
        matches,
        detail,
    )


def _supervisor_conflict_detail(
    plist_path: Path,
    plist_state: str,
    label_state: str,
    label_pid: int | None,
    app_state: str,
    app_pid: int | None,
    app_executable: str | None,
    foreign: ForeignLauncherScan,
) -> str:
    label_pid_text = str(label_pid) if label_pid is not None else "none"
    app_pid_text = str(app_pid) if app_pid is not None else "none"
    app_executable_text = app_executable if app_executable is not None else "none"
    detail = (
        f"plist_path={plist_path} plist_state={plist_state}; "
        f"launchd_label={label_state} pid={label_pid_text}; "
        f"journal_app={app_state} pid={app_pid_text} exe={app_executable_text}"
    )
    foreign_parts: list[str] = []
    if foreign.matches:
        matches = "|".join(
            f"{_display_safe(match.label)}@{_display_safe(match.plist_path)}"
            for match in foreign.matches
        )
        foreign_parts.append(f"foreign={matches}")
    if foreign.incomplete_paths:
        incomplete = "|".join(_display_safe(path) for path in foreign.incomplete_paths)
        foreign_parts.append(f"foreign_incomplete={incomplete}")
    if foreign_parts:
        detail += f"; foreign_launchers={len(foreign.matches)} "
        detail += " ".join(foreign_parts)
    return detail


def _inspect_supervisor_conflict_plist(path: Path) -> str:
    try:
        os.stat(path)
    except (FileNotFoundError, NotADirectoryError):
        return "absent"
    except OSError:
        return "unknown"
    return "present" if _launchd_program_arguments(path) else "malformed"


def _launchctl_print_pid(stdout: str) -> int | None:
    match = re.search(r"^\s*pid = (\d+)\s*$", stdout, re.MULTILINE)
    return int(match.group(1)) if match else None


def _inspect_supervisor_conflict_label(uid: int) -> tuple[str, int | None]:
    try:
        result = subprocess.run(
            ["launchctl", "print", f"gui/{uid}/{SERVICE_LABEL}"],
            capture_output=True,
            text=True,
            timeout=_SUPERVISOR_CONFLICT_LAUNCHCTL_TIMEOUT_S,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return "unknown", None
    if result.returncode == 0:
        return "loaded", _launchctl_print_pid(result.stdout or "")
    output = f"{result.stdout or ''}\n{result.stderr or ''}".lower()
    if any(marker in output for marker in _not_loaded_markers()):
        return "unloaded", None
    return "unknown", None


def _inspect_supervisor_conflict_app(uid: int) -> tuple[str, int | None, str | None]:
    found_unknown = False
    try:
        # Do not pass attrs: psutil prefetches them by calling exe() for every process.
        processes = psutil.process_iter()
        for proc in processes:
            try:
                uids = proc.uids()
            except psutil.Error:
                # UID was not readable, so we never confirmed this process is ours.
                continue

            if uids.real != uid:
                continue

            # Filter by UID before exe(); other users' processes commonly deny exe().
            try:
                exe = proc.exe()
            except psutil.NoSuchProcess:
                continue
            except psutil.AccessDenied:
                found_unknown = True
                continue
            except psutil.Error:
                found_unknown = True
                continue
            if exe.endswith(_JOURNAL_APP_EXECUTABLE_SUFFIX):
                return "running", proc.pid, exe
    except psutil.Error:
        return "unknown", None, None
    return ("unknown" if found_unknown else "absent"), None, None


def inspect_supervisor_conflict() -> SupervisorConflictEvidence:
    """Inspect whether journal.app and the legacy LaunchAgent both supervise.

    Read-only. On non-macOS platforms this returns a clean non-conflict result
    without invoking launchctl or enumerating processes.
    """
    if sys.platform != "darwin":
        foreign = ForeignLauncherScan((), ())
        return SupervisorConflictEvidence(
            str(_plist_path()),
            "absent",
            "unloaded",
            None,
            "absent",
            None,
            None,
            "macOS supervisor topology is not applicable on this platform",
            foreign,
        )

    uid = os.getuid()
    plist_path = _plist_path()
    plist_state = _inspect_supervisor_conflict_plist(plist_path)
    label_state, label_pid = _inspect_supervisor_conflict_label(uid)
    app_state, app_pid, app_executable = _inspect_supervisor_conflict_app(uid)
    foreign = _scan_foreign_solstone_app_launchers()
    return SupervisorConflictEvidence(
        str(plist_path),
        plist_state,
        label_state,
        label_pid,
        app_state,
        app_pid,
        app_executable,
        _supervisor_conflict_detail(
            plist_path,
            plist_state,
            label_state,
            label_pid,
            app_state,
            app_pid,
            app_executable,
            foreign,
        ),
        foreign,
    )


def remove_stale_plists() -> tuple[int, int]:
    """Remove stale launchd plists from prior installs."""
    if sys.platform != "darwin":
        return (0, 0)

    scan_dir = _plist_path().parent
    if not scan_dir.is_dir():
        return (0, 0)

    current_wrappers = {_managed_wrapper("sol"), _managed_wrapper("journal")}
    uid = os.getuid()
    removed = 0
    failed = 0
    not_loaded_markers = _not_loaded_markers()

    for path in sorted(scan_dir.glob("*.plist")):
        try:
            with path.open("rb") as handle:
                data = plistlib.load(handle)
        except (plistlib.InvalidFileException, ValueError, ExpatError, OSError) as exc:
            print(f"skipping {path}: {type(exc).__name__}: {exc}", file=sys.stderr)
            continue

        label = data.get("Label")
        if not isinstance(label, str):
            continue
        if label != SERVICE_LABEL and not label.startswith(f"{SERVICE_LABEL}."):
            continue

        program_arguments = data.get("ProgramArguments")
        program = data.get("Program")
        if (
            isinstance(program_arguments, list)
            and program_arguments
            and program_arguments[0]
        ):
            extracted = str(program_arguments[0])
        elif program:
            extracted = str(program)
        else:
            print(f"skipping {path}: no Program or ProgramArguments", file=sys.stderr)
            continue

        if not extracted.endswith(("/sol", "/journal")):
            continue

        if extracted in current_wrappers:
            continue

        result = subprocess.run(
            ["launchctl", "bootout", f"gui/{uid}", str(path)],
            capture_output=True,
            text=True,
        )
        stderr = result.stderr or ""
        if result.returncode != 0 and not any(
            marker in stderr.lower() for marker in not_loaded_markers
        ):
            print(
                f"launchctl bootout {path}: exit={result.returncode} stderr={stderr!r}",
                file=sys.stderr,
            )

        try:
            path.unlink()
        except (OSError, PermissionError) as exc:
            print(f"failed to remove {path}: {exc}", file=sys.stderr)
            failed += 1
            continue

        print(
            f"Removed stale launchd plist {path} "
            f"(referenced {extracted}, current wrappers are {sorted(current_wrappers)})"
        )
        removed += 1

    return (removed, failed)


def _generate_systemd_unit(
    env: dict[str, str],
    *,
    port: int = DEFAULT_SERVICE_PORT,
    journal_path: str,
) -> str:
    """Generate a systemd user unit for the solstone supervisor."""
    validate_journal_path_for_wrapper(journal_path)
    journal = _managed_wrapper("journal")
    env_lines = "\n".join(f"Environment={k}={v}" for k, v in sorted(env.items()))
    service_log = str(Path(journal_path) / "health" / "service.log")

    return (
        f"[Unit]\n"
        f"Description=Solstone Supervisor\n"
        f"After=default.target\n"
        f"StartLimitIntervalSec=120\n"
        f"StartLimitBurst=10\n"
        f"\n"
        f"[Service]\n"
        f"Type=notify\n"
        f"TimeoutStartSec={SERVICE_START_TIMEOUT_SECONDS}\n"
        f"ExecStart={journal} start {port}\n"
        f"Restart=on-failure\n"
        f"RestartSec=5\n"
        f"KillMode=control-group\n"
        f"TimeoutStopSec=30\n"
        f"LimitNOFILE={SERVICE_FILE_DESCRIPTOR_LIMIT}\n"
        f"StandardOutput=append:{service_log}\n"
        f"StandardError=append:{service_log}\n"
        f"{env_lines}\n"
        f"\n"
        f"[Install]\n"
        f"WantedBy=default.target\n"
    )


def _check_linger() -> None:
    """Warn if systemd linger is not enabled for the current user."""
    try:
        result = subprocess.run(
            ["loginctl", "show-user", os.environ.get("USER", ""), "--property=Linger"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and "Linger=no" in result.stdout:
            print(
                "Warning: systemd linger is not enabled. "
                "The service will stop when you log out.\n"
                "Enable it with: sudo loginctl enable-linger $USER"
            )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass


def _is_launchd_eio(result: subprocess.CompletedProcess) -> bool:
    """True iff a launchctl result is the known async-unload EIO race.

    `launchctl bootout` is async; bootstrapping while the old label is still
    loaded intermittently fails with "Bootstrap failed: 5: Input/output error".
    Match the EIO strerror specifically so unrelated launchd failures
    (permission, plist-format, already-bootstrapped) still fail fast.
    """
    return "Input/output error" in (result.stderr or "")


def _bootstrap_launchd(uid: int, path: Path) -> subprocess.CompletedProcess:
    """Bootstrap the plist, retrying through the async-unload EIO race.

    On the EIO race we reprobe the label, wait briefly, and retry up to
    `_LAUNCHD_BOOTSTRAP_MAX_ATTEMPTS`. Any non-EIO failure fails fast on the
    first attempt. Bounded by attempt count regardless of label state.
    """
    result: subprocess.CompletedProcess | None = None
    for attempt in range(_LAUNCHD_BOOTSTRAP_MAX_ATTEMPTS):
        if attempt > 0:
            try:
                subprocess.run(
                    ["launchctl", "print", f"gui/{uid}/{SERVICE_LABEL}"],
                    capture_output=True,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError):
                pass
            time.sleep(_LAUNCHD_BOOTSTRAP_RETRY_WAIT_S)
        result = subprocess.run(
            ["launchctl", "bootstrap", f"gui/{uid}", str(path)],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 or not _is_launchd_eio(result):
            return result
    assert result is not None  # loop runs >= 1 time; satisfies the type checker
    return result


def _reload_launchd_unit(uid: int, path: Path) -> subprocess.CompletedProcess:
    """Boot out the label, wait (bounded) for unload, then bootstrap with EIO retry."""
    subprocess.run(
        ["launchctl", "bootout", f"gui/{uid}/{SERVICE_LABEL}"],
        capture_output=True,
        check=False,
    )

    # bootout is async; wait (bounded) for launchd to drop the old label before
    # bootstrapping. Any residual unload race is handled by the bounded bootstrap
    # retry below.
    deadline = time.monotonic() + _LAUNCHD_UNLOAD_TIMEOUT_S
    while time.monotonic() < deadline:
        try:
            probe = subprocess.run(
                ["launchctl", "print", f"gui/{uid}/{SERVICE_LABEL}"],
                capture_output=True,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            break
        if probe.returncode != 0:
            break
        time.sleep(_LAUNCHD_UNLOAD_POLL_INTERVAL_S)

    return _bootstrap_launchd(uid, path)


def _install(port: int = DEFAULT_SERVICE_PORT) -> int:
    platform = _platform()
    env = _collect_env()

    journal_path, _source = get_journal_info()
    Path(journal_path, "health").mkdir(parents=True, exist_ok=True)
    clear_ready()

    if platform == "darwin":
        remove_stale_plists()
        plist_data = _generate_plist(env, port=port, journal_path=journal_path)
        path = _plist_path()
        path.parent.mkdir(parents=True, exist_ok=True)

        uid = os.getuid()
        path.write_bytes(plist_data)
        print(f"Wrote {path}")

        result = _reload_launchd_unit(uid, path)
        if result.returncode != 0:
            if _is_launchd_eio(result):
                logger.warning(
                    "launchd label %s did not unload after %d bootstrap attempts; giving up",
                    SERVICE_LABEL,
                    _LAUNCHD_BOOTSTRAP_MAX_ATTEMPTS,
                )
            print(f"Error loading service: {result.stderr.strip()}", file=sys.stderr)
            return 1
        print("Service loaded into launchd")

    else:
        unit_content = _generate_systemd_unit(env, port=port, journal_path=journal_path)
        path = _unit_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(unit_content)
        print(f"Wrote {path}")

        print("Reloading systemd user units...")
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        print("Enabling solstone.service...")
        subprocess.run(["systemctl", "--user", "enable", SYSTEMD_UNIT], check=True)
        print("Service enabled")

        _check_linger()

    return 0


def _uninstall() -> int:
    platform = _platform()

    if platform == "darwin":
        path = _plist_path()
        uid = os.getuid()
        subprocess.run(
            ["launchctl", "bootout", f"gui/{uid}/{SERVICE_LABEL}"],
            capture_output=True,
            check=False,
        )
        if path.exists():
            path.unlink()
            print(f"Removed {path}")
        else:
            print("Service was not installed")

    else:
        path = _unit_path()
        subprocess.run(
            ["systemctl", "--user", "stop", SYSTEMD_UNIT],
            capture_output=True,
        )
        subprocess.run(
            ["systemctl", "--user", "disable", SYSTEMD_UNIT],
            capture_output=True,
        )
        if path.exists():
            path.unlink()
            subprocess.run(
                ["systemctl", "--user", "daemon-reload"],
                capture_output=True,
            )
            print(f"Removed {path}")
        else:
            print("Service was not installed")

    return 0


def _start() -> int:
    platform = _platform()
    if platform == "darwin":
        uid = os.getuid()
        path = _plist_path()
        if not path.exists():
            print(
                "Error: service not installed. Run 'journal service install' first.",
                file=sys.stderr,
            )
            return 1
        result = subprocess.run(
            ["launchctl", "kickstart", f"gui/{uid}/{SERVICE_LABEL}"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(f"Error starting service: {result.stderr.strip()}", file=sys.stderr)
            return 1
    else:
        if not _unit_path().exists():
            print(
                "Error: service not installed. Run 'journal service install' first.",
                file=sys.stderr,
            )
            return 1
        result = subprocess.run(
            ["systemctl", "--user", "start", SYSTEMD_UNIT],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(f"Error starting service: {result.stderr.strip()}", file=sys.stderr)
            return 1

    print("Service started")
    return 0


def _stop() -> int:
    platform = _platform()
    if platform == "darwin":
        uid = os.getuid()
        result = subprocess.run(
            ["launchctl", "kill", "SIGTERM", f"gui/{uid}/{SERVICE_LABEL}"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(f"Error stopping service: {result.stderr.strip()}", file=sys.stderr)
            return 1
    else:
        result = subprocess.run(
            ["systemctl", "--user", "stop", SYSTEMD_UNIT],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(f"Error stopping service: {result.stderr.strip()}", file=sys.stderr)
            return 1

    print("Service stopped")
    return 0


def _restart(if_installed: bool = False) -> int:
    platform = _platform()
    if not service_is_installed():
        if if_installed:
            return 0
        print(
            "Error: service not installed. Run 'journal service install' first.",
            file=sys.stderr,
        )
        return 1

    print(
        "Stopping old supervisor (waits for in-flight work to finish — may take a moment)..."
    )

    if platform == "darwin":
        uid = os.getuid()
        subprocess.run(
            ["launchctl", "kill", "SIGTERM", f"gui/{uid}/{SERVICE_LABEL}"],
            capture_output=True,
        )
        clear_ready()
        result = subprocess.run(
            ["launchctl", "kickstart", f"gui/{uid}/{SERVICE_LABEL}"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(f"Error restarting service: {result.stderr.strip()}", file=sys.stderr)
            return 1
    else:
        clear_ready()
        result = subprocess.run(
            ["systemctl", "--user", "restart", SYSTEMD_UNIT],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(f"Error restarting service: {result.stderr.strip()}", file=sys.stderr)
            return 1

    if wait_ready(timeout=READY_TIMEOUT_SECONDS) is None:
        print(_ready_timeout_message(), file=sys.stderr)
        return 1

    print("Service restarted.")
    return 0


def _status() -> int:
    platform = _platform()

    if not service_is_installed():
        print("Service: not installed")
        print("Run 'journal setup' or 'journal service install' to install it.")
        return 1

    print("Service: installed")

    if service_is_running():
        if platform == "darwin":
            print("State: running (launchd)")
        else:
            print("State: running (systemd)")
    elif platform == "darwin":
        print("State: stopped")
        return 0
    else:
        result = subprocess.run(
            ["systemctl", "--user", "is-active", SYSTEMD_UNIT],
            capture_output=True,
            text=True,
        )
        state = result.stdout.strip()
        print(f"State: {state}")
        return 0

    print()
    from solstone.think.health_cli import health_check

    return health_check()


def _logs(follow: bool = False) -> int:
    _platform()
    journal_path = Path(get_journal())
    service_log = journal_path / "health" / "service.log"

    if follow:
        if not service_log.exists():
            print("No service log file found", file=sys.stderr)
            return 1
        result = subprocess.run(["/usr/bin/tail", "-f", str(service_log)])
        return result.returncode
    else:
        if service_log.exists():
            print(f"=== {service_log.name} ===")
            print(service_log.read_text(errors="replace")[-10000:])
        else:
            print(f"=== {service_log.name} === (not found)")
        return 0


def _up() -> int:
    """Start if not running and show status. Requires an installed service unit."""
    if not service_is_installed():
        print(
            "Error: service not installed. Run 'journal setup' or "
            "'journal service install' to install it first.",
            file=sys.stderr,
        )
        return 1

    if not service_is_running():
        print("Starting service...")
        clear_ready()
        rc = _start()
        if rc != 0:
            if wait_ready(timeout=READY_TIMEOUT_SECONDS) is None:
                return rc
            _status()
            return 0

    if wait_ready(timeout=READY_TIMEOUT_SECONDS) is None:
        print(_ready_timeout_message(), file=sys.stderr)
        return 1

    # wait_ready() succeeding is the authoritative readiness signal per the
    # readiness primitive contract. _status() is invoked for human-readable
    # output, but its return code (which folds in health_check()'s 10s callosum
    # status probe) is NOT the gate. The callosum bus warms up over ~30-90s
    # post-readiness while convey/cortex/link come online; allowing _status()
    # to fail _up() here re-introduces the same premature-failure that the
    # readiness primitive was meant to retire.
    _status()
    return 0


def _down() -> int:
    """Stop the service."""
    return _stop()


_SUBCOMMANDS = {
    "uninstall": _uninstall,
    "start": _start,
    "stop": _stop,
    "status": _status,
    "down": lambda **_kw: _down(),
}


def _print_usage() -> None:
    print("Usage: journal service <install|uninstall|start|stop|restart|status|logs>")
    print("       journal service install [--port PORT]  (default: 5015)")
    print(
        "       journal service restart [--if-installed]  "
        "(restart; --if-installed noops if not installed)"
    )
    print(
        "       journal up                             (start + status; service must be installed)"
    )
    print("       journal down                           (stop)")


def _parse_port(args: list[str]) -> int:
    """Extract --port PORT from args, return DEFAULT_SERVICE_PORT if absent."""
    for i, arg in enumerate(args):
        if arg == "--port" and i + 1 < len(args):
            try:
                return int(args[i + 1])
            except ValueError:
                print(f"Error: invalid port '{args[i + 1]}'", file=sys.stderr)
                sys.exit(1)
        if arg.startswith("--port="):
            try:
                return int(arg.split("=", 1)[1])
            except ValueError:
                print(f"Error: invalid port '{arg}'", file=sys.stderr)
                sys.exit(1)
    return DEFAULT_SERVICE_PORT


def main() -> None:
    """Entry point for ``journal service``."""
    args = sys.argv[1:]

    if args and args[0] == "logs":
        follow = "-f" in args[1:] or "--follow" in args[1:]
        sys.exit(_logs(follow=follow))

    if "--help" in args or "-h" in args:
        _print_usage()
        sys.exit(0)

    if not args:
        _print_usage()
        sys.exit(1)

    subcmd = args[0]
    rest = args[1:]

    if subcmd == "install":
        sys.exit(_install(port=_parse_port(rest)))
    elif subcmd == "up":
        sys.exit(_up())
    elif subcmd == "restart":
        if_installed = "--if-installed" in rest
        sys.exit(_restart(if_installed=if_installed))
    elif subcmd in _SUBCOMMANDS:
        sys.exit(_SUBCOMMANDS[subcmd]())
    else:
        print(f"Unknown subcommand: {subcmd}", file=sys.stderr)
        print("Available: install, uninstall, start, stop, restart, status, logs")
        sys.exit(1)

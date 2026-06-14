# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Transfer observed segments between solstone instances.

Provides export, import, and send commands for transferring fully-processed
observation segments between solstone instances.

Usage:
    journal transfer export --day YYYYMMDD [--output PATH]
    journal transfer import --archive PATH [--dry-run]
    journal transfer send --to LABEL [--day YYYYMMDD] [--dry-run]

Use `journal transfer send --to LABEL` to send segments to a paired peer.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import platform
import re
import shutil
import tarfile
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from solstone.observe.peer_lookup import PeerInfo, PeerLookupError, resolve_peer
from solstone.observe.pl_http import PlHttpSession
from solstone.think.callosum import callosum_send
from solstone.think.journal_io import contained_path, install_file
from solstone.think.link.bundle import load_client_identity
from solstone.think.link.dialer import TunnelClient, TunnelRequestError
from solstone.think.link.paths import relay_url
from solstone.think.utils import (
    CHRONICLE_DIR,
    day_path,
    get_journal,
    get_project_root,
    iter_segments,
    now_ms,
    require_solstone,
    setup_cli,
)

from .utils import compute_file_sha256, find_available_segment

logger = logging.getLogger(__name__)

# Archive manifest version
MANIFEST_VERSION = 1
RETRY_BACKOFF = [1, 5, 15]
UPLOAD_TIMEOUT = 300


def _get_hostname() -> str:
    """Get hostname for archive naming."""
    return platform.node() or "unknown"


def _list_segment_dirs(day_dir: Path) -> list[tuple[str, str, Path]]:
    """List all valid segment directories in a day directory.

    Args:
        day_dir: Path to day directory

    Returns:
        List of (stream_name, segment_path) tuples sorted by segment_key
    """
    return iter_segments(day_dir)


def _build_segment_manifest(segment_dir: Path) -> dict[str, Any]:
    """Build manifest entry for a segment directory.

    Args:
        segment_dir: Path to segment directory

    Returns:
        Dict with file list and SHA256 hashes
    """
    files = []
    for file_path in sorted(segment_dir.iterdir()):
        if file_path.is_file():
            files.append(
                {
                    "name": file_path.name,
                    "sha256": compute_file_sha256(file_path),
                    "size": file_path.stat().st_size,
                }
            )
    return {"files": files}


def create_archive(day: str, output_path: Path | None = None) -> Path:
    """Create a day archive with all segments.

    Args:
        day: Day in YYYYMMDD format
        output_path: Optional output path (default: scratch/{day}_{hostname}.tgz)

    Returns:
        Path to created archive

    Raises:
        ValueError: If day directory doesn't exist or has no segments
    """
    day_dir = day_path(day, create=False)

    if not day_dir.exists():
        raise ValueError(f"Day directory does not exist: {day_dir}")

    segment_entries = _list_segment_dirs(day_dir)
    if not segment_entries:
        raise ValueError(f"No segments found in {day_dir}")

    # Build manifest
    manifest: dict[str, Any] = {
        "version": MANIFEST_VERSION,
        "day": day,
        "created_at": now_ms(),
        "host": _get_hostname(),
        "segments": {},
    }

    for stream_name, seg_key, seg_path in segment_entries:
        arc_key = f"{stream_name}/{seg_key}"
        manifest["segments"][arc_key] = _build_segment_manifest(seg_path)

    # Determine output path (default: scratch/ in project root)
    if output_path is None:
        scratch_dir = Path(get_project_root()) / "scratch"
        scratch_dir.mkdir(exist_ok=True)
        output_path = scratch_dir / f"{day}_{_get_hostname()}.tgz"

    # Create archive
    logger.info(f"Creating archive: {output_path}")
    logger.info(f"  Day: {day}")
    logger.info(f"  Segments: {len(segment_entries)}")

    with tarfile.open(output_path, "w:gz") as tar:
        # Add manifest
        manifest_json = json.dumps(manifest, indent=2).encode("utf-8")
        import io

        manifest_info = tarfile.TarInfo(name="manifest.json")
        manifest_info.size = len(manifest_json)
        manifest_info.mtime = int(time.time())
        tar.addfile(manifest_info, io.BytesIO(manifest_json))

        # Add segment directories (archived as stream/segment/file)
        for stream_name, seg_key, seg_path in segment_entries:
            for file_path in seg_path.iterdir():
                if file_path.is_file():
                    arcname = f"{stream_name}/{seg_key}/{file_path.name}"
                    tar.add(file_path, arcname=arcname)
                    logger.debug(f"  Added: {arcname}")

    total_size = output_path.stat().st_size
    logger.info(f"  Archive size: {total_size / (1024 * 1024):.1f} MB")

    return output_path


def _read_manifest(archive_path: Path) -> dict[str, Any]:
    """Read and validate manifest from archive.

    Args:
        archive_path: Path to archive file

    Returns:
        Manifest dict

    Raises:
        ValueError: If manifest is missing or invalid
    """
    with tarfile.open(archive_path, "r:gz") as tar:
        try:
            manifest_file = tar.extractfile("manifest.json")
            if manifest_file is None:
                raise ValueError("manifest.json not found in archive")
            manifest = json.load(manifest_file)
        except KeyError:
            raise ValueError("manifest.json not found in archive")

    if manifest.get("version") != MANIFEST_VERSION:
        raise ValueError(
            f"Unsupported manifest version: {manifest.get('version')} "
            f"(expected {MANIFEST_VERSION})"
        )

    if "day" not in manifest or "segments" not in manifest:
        raise ValueError("Invalid manifest: missing required fields")

    return manifest


def _check_segment_match(
    day_dir: Path, arc_key: str, manifest_files: list[dict]
) -> bool:
    """Check if local segment matches manifest exactly.

    Args:
        day_dir: Path to day directory
        arc_key: Archive key (stream/segment format)
        manifest_files: List of file dicts from manifest

    Returns:
        True if all files exist with matching SHA256
    """
    segment_dir = day_dir / arc_key
    if not segment_dir.exists():
        return False

    manifest_by_name = {f["name"]: f["sha256"] for f in manifest_files}

    # Check all manifest files exist with correct hash
    for name, expected_sha256 in manifest_by_name.items():
        file_path = segment_dir / name
        if not file_path.exists():
            return False
        if compute_file_sha256(file_path) != expected_sha256:
            return False

    return True


def validate_archive(archive_path: Path) -> dict[str, Any]:
    """Validate archive and check for conflicts.

    Args:
        archive_path: Path to archive file

    Returns:
        Dict with validation results:
        - manifest: The parsed manifest
        - skip: List of segments to skip (already synced)
        - import_as: Dict mapping original segment -> target segment
        - deconflicted: List of segments that needed key adjustment
    """
    manifest = _read_manifest(archive_path)
    day = manifest["day"]

    day_dir = day_path(day, create=False)

    result = {
        "manifest": manifest,
        "skip": [],
        "import_as": {},
        "deconflicted": [],
    }

    for arc_key, segment_data in manifest["segments"].items():
        files = segment_data.get("files", [])

        if _check_segment_match(day_dir, arc_key, files):
            # Full match - skip
            result["skip"].append(arc_key)
            continue

        # arc_key is stream/segment - extract parts for deconfliction
        parts = arc_key.split("/", 1)
        stream_name = parts[0] if len(parts) == 2 else ""
        seg_key = parts[1] if len(parts) == 2 else parts[0]
        stream_dir = day_dir / stream_name if stream_name else day_dir

        # Check if segment exists but doesn't match
        if (stream_dir / seg_key).exists():
            # Need deconfliction within the stream directory
            new_seg_key = find_available_segment(stream_dir, seg_key)
            if new_seg_key is None:
                raise ValueError(f"Cannot find available slot for segment {arc_key}")
            new_arc_key = f"{stream_name}/{new_seg_key}" if stream_name else new_seg_key
            result["import_as"][arc_key] = new_arc_key
            result["deconflicted"].append(arc_key)
        else:
            # Original slot available
            result["import_as"][arc_key] = arc_key

    return result


def import_archive(
    archive_path: Path,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Import archive into journal.

    Args:
        archive_path: Path to archive file
        dry_run: If True, validate only without extracting

    Returns:
        Dict with import results
    """
    validation = validate_archive(archive_path)
    manifest = validation["manifest"]
    day = manifest["day"]

    logger.info(f"Importing archive: {archive_path}")
    logger.info(f"  Day: {day}")
    logger.info(f"  Source host: {manifest.get('host', 'unknown')}")
    logger.info(f"  Total segments: {len(manifest['segments'])}")
    logger.info(f"  Skip (already synced): {len(validation['skip'])}")
    logger.info(f"  Import: {len(validation['import_as'])}")
    if validation["deconflicted"]:
        logger.info(f"  Deconflicted: {len(validation['deconflicted'])}")

    if dry_run:
        logger.info("Dry run - no changes made")
        return {
            "status": "dry_run",
            "validation": validation,
        }

    if not validation["import_as"]:
        logger.info("Nothing to import - all segments already synced")
        return {
            "status": "nothing_to_import",
            "validation": validation,
        }

    journal = get_journal()

    def _reject_if_unsafe(component: str, kind: str) -> None:
        try:
            contained_path(journal, component)
        except ValueError as exc:
            raise ValueError(
                f"Refusing to import archive: unsafe {kind} {component!r} "
                f"escapes the journal ({exc})"
            ) from exc

    imported = []
    with tarfile.open(archive_path, "r:gz") as tar:
        members = tar.getmembers()
        plan: list[tuple[str, str, Path, list[tuple[tarfile.TarInfo, Path]]]] = []

        for original_arc_key, target_arc_key in validation["import_as"].items():
            _reject_if_unsafe(target_arc_key, "segment key")
            target_dir = contained_path(journal, f"{day}/{target_arc_key}")

            planned_files = []
            prefix = f"{original_arc_key}/"
            for member in members:
                if member.name.startswith(prefix) and member.isfile():
                    filename = member.name[len(prefix) :]
                    if not filename:
                        raise ValueError(
                            "Refusing to import archive member with empty filename "
                            f"for segment {original_arc_key!r} -> "
                            f"{target_arc_key!r}: {member.name!r}"
                        )
                    _reject_if_unsafe(filename, "member filename")
                    target_path = contained_path(
                        journal,
                        f"{day}/{target_arc_key}/{filename}",
                    )
                    planned_files.append((member, target_path))

            plan.append((original_arc_key, target_arc_key, target_dir, planned_files))

        for original_arc_key, target_arc_key, target_dir, planned_files in plan:
            target_dir.mkdir(parents=True, exist_ok=True)

            for member, target_path in planned_files:
                source = tar.extractfile(member)
                if source:
                    temp_path = None
                    temp_handle = None
                    promoted = False
                    try:
                        temp_handle = tempfile.NamedTemporaryFile(
                            mode="wb",
                            dir=target_dir,
                            prefix=".import_",
                            suffix=".tmp",
                            delete=False,
                        )
                        temp_path = Path(temp_handle.name)
                        shutil.copyfileobj(source, temp_handle)
                        temp_handle.close()
                        install_file(temp_path, target_path)
                        promoted = True
                        # Preserve modification time (install_file does not)
                        os.utime(target_path, (member.mtime, member.mtime))
                    finally:
                        if temp_handle is not None and not temp_handle.closed:
                            temp_handle.close()
                        if temp_path is not None and not promoted:
                            temp_path.unlink(missing_ok=True)

            if original_arc_key != target_arc_key:
                logger.info(f"  Imported: {original_arc_key} -> {target_arc_key}")
            else:
                logger.info(f"  Imported: {original_arc_key}")

            imported.append(target_arc_key)

    # Trigger indexer rescan via supervisor queue (fire-and-forget)
    # Supervisor serializes indexer runs to prevent concurrent writes
    logger.info(f"Requesting indexer rescan for {day}...")
    sent = callosum_send(
        "supervisor",
        "request",
        cmd=["journal", "indexer", "--rescan"],
    )
    if sent:
        logger.info("  Indexer rescan queued")
    else:
        logger.warning("  Failed to queue indexer rescan (supervisor not running?)")

    return {
        "status": "imported",
        "day": day,
        "imported": imported,
        "skipped": validation["skip"],
        "deconflicted": validation["deconflicted"],
    }


def _normalize_url(to: str) -> str:
    """Normalize remote URL for observer endpoints."""
    to = to.rstrip("/")
    if to.startswith(("http://", "https://")):
        return to
    return f"https://{to}"


def _resolve_destination(
    parser: argparse.ArgumentParser,
    to: str,
) -> PeerInfo:
    try:
        return resolve_peer(to)
    except PeerLookupError as exc:
        parser.error(str(exc))


def _parse_day_spec(day_spec: str | None, journal_root: Path) -> list[str]:
    """Parse a single day, day range, or default to all journal days."""
    if day_spec is None:
        day_root = (
            journal_root / CHRONICLE_DIR
            if (journal_root / CHRONICLE_DIR).is_dir()
            else journal_root
        )
        return sorted(
            [
                day_dir.name
                for day_dir in day_root.iterdir()
                if day_dir.is_dir() and re.match(r"^\d{8}$", day_dir.name)
            ]
        )

    if re.match(r"^\d{8}$", day_spec):
        return [day_spec]

    if re.match(r"^\d{8}-\d{8}$", day_spec):
        start_str, end_str = day_spec.split("-", 1)
        start = datetime.strptime(start_str, "%Y%m%d")
        end = datetime.strptime(end_str, "%Y%m%d")
        if start > end:
            raise ValueError(
                "Invalid day format: start day must be on or before end day"
            )

        days = []
        current = start
        while current <= end:
            days.append(current.strftime("%Y%m%d"))
            current += timedelta(days=1)
        return days

    raise ValueError("Invalid day format: use YYYYMMDD or YYYYMMDD-YYYYMMDD")


def _query_journal_segments(
    session: PlHttpSession,
    base_url: str,
    key_prefix: str,
) -> dict[str, Any]:
    url = f"{base_url}/app/import/journal/{key_prefix}/manifest/segments"
    try:
        response = session.get(url, timeout=UPLOAD_TIMEOUT)
        if response.status_code == 200:
            return response.json()
        logger.warning(
            "Remote segment manifest query failed: %s %s",
            response.status_code,
            response.text,
        )
    except (TunnelRequestError, OSError) as exc:
        logger.warning("Remote segment manifest query failed: %s", exc)
    return {}


def _upload_segment_journal(
    session: PlHttpSession,
    base_url: str,
    key_prefix: str,
    day: str,
    segment_key: str,
    stream_name: str,
    segment_path: Path,
) -> tuple[str, int]:
    files = [
        file_path
        for file_path in sorted(segment_path.iterdir())
        if file_path.is_file() and file_path.name != "stream.json"
    ]
    if not files:
        return ("skip", 0)

    bytes_sent = sum(file_path.stat().st_size for file_path in files)
    metadata = {
        "segments": [
            {
                "day": day,
                "stream": stream_name,
                "segment_key": segment_key,
                "files": [file_path.name for file_path in files],
            }
        ]
    }
    url = f"{base_url}/app/import/journal/{key_prefix}/ingest/segments/{day}"

    for attempt, delay in enumerate(RETRY_BACKOFF):
        file_handles = []
        files_data = []
        try:
            for file_path in files:
                fh = open(file_path, "rb")
                file_handles.append(fh)
                files_data.append(
                    ("files_0", (file_path.name, fh, "application/octet-stream"))
                )

            response = session.post(
                url,
                data={"metadata": json.dumps(metadata)},
                files=files_data,
                timeout=UPLOAD_TIMEOUT,
            )
            if response.status_code == 200:
                return ("sent", bytes_sent)
            if response.status_code == 401:
                return ("auth_invalid", 0)
            if response.status_code == 403:
                return ("auth_revoked", 0)
            if 500 <= response.status_code <= 599:
                logger.warning(
                    "PL upload attempt %s failed for %s/%s/%s: %s %s",
                    attempt + 1,
                    day,
                    stream_name,
                    segment_key,
                    response.status_code,
                    response.text,
                )
            else:
                logger.warning(
                    "PL upload rejected for %s/%s/%s: %s %s",
                    day,
                    stream_name,
                    segment_key,
                    response.status_code,
                    response.text,
                )
                return ("error", 0)
        except (TunnelRequestError, OSError) as exc:
            logger.warning(
                "PL upload attempt %s failed for %s/%s/%s: %s",
                attempt + 1,
                day,
                stream_name,
                segment_key,
                exc,
            )
        finally:
            for fh in file_handles:
                try:
                    fh.close()
                except Exception:
                    pass

        if attempt < len(RETRY_BACKOFF) - 1:
            time.sleep(delay)

    return ("error", 0)


def send_segments_pl(peer: PeerInfo, days: list[str], dry_run: bool) -> None:
    """Send local journal segments to a paired peer over PL."""
    identity = load_client_identity(peer.dir)
    key_prefix = peer.instance_id[:8]
    base_url = "https://pl.peer"

    sent = 0
    skipped = 0
    failed = 0
    bytes_total = 0

    with TunnelClient(identity, relay_url=relay_url()) as tunnel:
        session = PlHttpSession(tunnel)
        remote_manifest = _query_journal_segments(session, base_url, key_prefix)

        for day in days:
            day_dir = day_path(day, create=False)
            if not day_dir.exists():
                logger.debug(f"Day directory not found: {day}")
                continue

            segment_entries = iter_segments(day_dir)
            if not segment_entries:
                continue

            for stream_name, seg_key, seg_path in segment_entries:
                manifest = _build_segment_manifest(seg_path)
                local_files = {
                    file_info["name"]: file_info["sha256"]
                    for file_info in manifest["files"]
                    if file_info["name"] != "stream.json"
                }
                remote_entry = remote_manifest.get(day, {}).get(
                    f"{stream_name}/{seg_key}", {}
                )
                remote_files = {
                    file_info["name"]: file_info["sha256"]
                    for file_info in remote_entry.get("files", [])
                }
                if local_files == remote_files:
                    logger.info(f"  [skip] {day}/{stream_name}/{seg_key}")
                    skipped += 1
                    continue

                if dry_run:
                    logger.info(f"  [would send] {day}/{stream_name}/{seg_key}")
                    sent += 1
                    continue

                status, bytes_sent = _upload_segment_journal(
                    session,
                    base_url,
                    key_prefix,
                    day,
                    seg_key,
                    stream_name,
                    seg_path,
                )
                if status == "sent":
                    logger.info(
                        f"  [sent] {day}/{stream_name}/{seg_key} ({bytes_sent} bytes)"
                    )
                    sent += 1
                    bytes_total += bytes_sent
                elif status == "skip":
                    logger.info(f"  [skip] {day}/{stream_name}/{seg_key}")
                    skipped += 1
                elif status == "auth_invalid":
                    print(
                        "Authentication failed: invalid or missing paired-link identity"
                    )
                    return
                elif status == "auth_revoked":
                    print(
                        "Authentication failed: paired-link identity revoked or disabled"
                    )
                    return
                else:
                    logger.info(f"  [FAILED] {day}/{stream_name}/{seg_key}")
                    failed += 1

    total = sent + skipped + failed
    if total == 0:
        print("No segments found to transfer")
        return

    if dry_run:
        print(f"\nDry run: would send {sent}, skip {skipped}")
        return

    print(
        f"\nTransfer complete: {sent} sent, {skipped} skipped, "
        f"{failed} failed, {bytes_total} bytes transferred"
    )
    if sent == 0 and skipped > 0 and failed == 0:
        print("Nothing to send - remote is up to date")


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Transfer observed segments between solstone instances"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Export subcommand
    export_parser = subparsers.add_parser(
        "export", help="Create archive from day's segments"
    )
    export_parser.add_argument(
        "--day",
        required=True,
        help="Day to export (YYYYMMDD format)",
    )
    export_parser.add_argument(
        "--output",
        "-o",
        type=Path,
        help="Output archive path (default: scratch/{day}_{hostname}.tgz)",
    )

    # Import subcommand
    import_parser = subparsers.add_parser("import", help="Import archive into journal")
    import_parser.add_argument(
        "--archive",
        "-a",
        required=True,
        type=Path,
        help="Archive file to import",
    )
    import_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate archive without extracting",
    )

    # Send subcommand
    send_parser = subparsers.add_parser(
        "send",
        help="Send segments to paired peer",
    )
    send_parser.add_argument(
        "--to",
        required=True,
        help="Paired peer label",
    )
    send_parser.add_argument(
        "--day",
        help="Day or range (YYYYMMDD or YYYYMMDD-YYYYMMDD, default: all days)",
    )
    send_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be sent without uploading",
    )

    args = setup_cli(parser)
    require_solstone()

    if args.command == "export":
        try:
            output = create_archive(args.day, args.output)
            print(f"Created archive: {output}")
        except ValueError as e:
            parser.error(str(e))

    elif args.command == "import":
        if not args.archive.exists():
            parser.error(f"Archive not found: {args.archive}")

        try:
            result = import_archive(args.archive, dry_run=args.dry_run)
            if result["status"] == "imported":
                print(f"Imported {len(result['imported'])} segments to {result['day']}")
                if result["skipped"]:
                    print(f"Skipped {len(result['skipped'])} already-synced segments")
                if result["deconflicted"]:
                    print(f"Deconflicted {len(result['deconflicted'])} segments")
            elif result["status"] == "nothing_to_import":
                print("Nothing to import - all segments already synced")
            elif result["status"] == "dry_run":
                v = result["validation"]
                print("Dry run validation:")
                print(f"  Would skip: {len(v['skip'])} segments")
                print(f"  Would import: {len(v['import_as'])} segments")
                if v["deconflicted"]:
                    print(f"  Would deconflict: {len(v['deconflicted'])} segments")
        except ValueError as e:
            parser.error(str(e))

    elif args.command == "send":
        peer = _resolve_destination(send_parser, args.to)
        journal = get_journal()
        try:
            days = _parse_day_spec(args.day, Path(journal))
        except ValueError as e:
            parser.error(str(e))
        send_segments_pl(peer, days, args.dry_run)

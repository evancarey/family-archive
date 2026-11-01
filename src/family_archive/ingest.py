"""Preservation pipeline for strictly supported ordinary optical data media."""

import json
import logging
import os
import re
import stat
import subprocess
import time
import uuid
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from family_archive.catalog import Catalog, catalog_lock
from family_archive.config import Config
from family_archive.hashing import hash_file
from family_archive.image_mount import mount_image, require_detached
from family_archive.imaging import capture, open_device
from family_archive.manifest import (
    FILES_ONLY_SCHEMA_VERSION,
    MULTISESSION_SCHEMA_VERSION,
    SCHEMA_VERSION,
    inventory,
    serialize,
)
from family_archive.media import Inspection, inspect_media
from family_archive.multisession import (
    RawCdDataReader,
    Track,
    build_composite,
    capture_tracks,
    detect_unreadable_iso_tao_tail,
    inventory_sessions,
    track_metadata,
)
from family_archive.storage import Storage, StorageError


class IngestionError(Exception):
    pass


def _open_inspection_device(device: str):
    """Retry a transient macOS raw-node recreation after Disk Arbitration."""
    for attempt in range(15):
        try:
            return open_device(device)
        except FileNotFoundError:
            if attempt == 14:
                raise
            time.sleep(1)


def now() -> str:
    return datetime.now(UTC).isoformat()


def select_source(device: str, files_only: bool = False) -> Inspection:
    reports = [report for report in inspect_media() if report.device == device]
    if len(reports) != 1:
        raise ValueError(f"Expected one optical source at {device}; run inspect first")
    report = reports[0]
    if (
        files_only
        and report.classification == "NEEDS_REVIEW"
        and report.reason.startswith("Unable to query CD session layout: drutil toc")
        and report.media.upper() in {"CD-ROM", "CD-R", "CD-RW"}
        and report.filesystem.lower() in {"cd9660", "iso9660"}
        and report.tracks == 1
        and report.sessions == 1
        and report.hardware
        and report.hardware.get("OpticalMediaNumAudioTracks", 0) == 0
        and report.hardware.get("VolumeContent") == "CD_ROM_Mode_1"
        and report.mount_point
        and Path(report.mount_point).is_dir()
    ):
        # Files-only mode preserves files exposed by the verified read-only Mode 1
        # filesystem. It does not need a raw-track TOC or authorize image capture.
        report = replace(
            report,
            classification="DATA CD",
            inspection_supported=True,
            reason=(
                "Mounted read-only Mode 1 filesystem confirmed as a single-track, "
                "single-session data CD; raw TOC unavailable, files-only scope."
            ),
        )
    if report.multisession_candidate:
        if files_only:
            raise ValueError(
                f"{device}: files-only mode currently requires an ordinary mounted data CD/DVD"
            )
        if (
            report.sessions is None
            or report.sessions < 2
            or len(report.session_layout) != report.sessions
        ):
            raise ValueError(f"{device}: session layout is not confirmed; run inspect again")
        return report
    if report.classification not in {"DATA CD", "DATA DVD"}:
        raise ValueError(
            f"{device}: unsupported for ingestion: {report.classification}: {report.reason}"
        )
    if files_only:
        if not report.inspection_supported or report.multisession_candidate:
            raise ValueError(f"{device}: files-only mode requires a confirmed mounted data CD/DVD")
        if not report.mount_point or not Path(report.mount_point).is_dir():
            mount_point = report.mount_point or "(no mount point)"
            raise ValueError(
                f"{device}: mounted volume is unavailable at {mount_point}; "
                "mount the disc in Finder and retry"
            )
        return report
    if not report.image_size or report.image_size % 2048:
        raise ValueError(f"{device}: raw media size is missing or not a whole 2048-byte sector")
    if (
        report.hardware
        and report.hardware.get("DeviceBlockSize") == 2352
        and not (
            len(report.session_layout) == 1
            and report.session_layout[0].get("session") == 1
            and report.session_layout[0].get("track") == 1
            and report.session_layout[0].get("sectors", 0) * 2048 == report.image_size
        )
    ):
        raise ValueError(f"{device}: could not confirm the complete raw data-track extent")
    if (
        report.hardware
        and report.hardware.get("DeviceBlockSize") == 2352
        and len(report.session_layout) == 1
        and report.image_size == report.session_layout[0].get("sectors", 0) * 2048
    ):
        layout = report.session_layout[0]
        with _open_inspection_device(report.device) as raw_source:
            tail = detect_unreadable_iso_tao_tail(
                raw_source,
                layout["start_lba"],
                layout["sectors"],
            )
        if tail:
            raw_sector_mode, volume_sectors = tail
            report = replace(
                report,
                archive_image_size=volume_sectors * 2048,
                archive_sector_count=volume_sectors,
                excluded_track_sectors=layout["sectors"] - volume_sectors,
                raw_sector_mode=raw_sector_mode,
            )
    return report


def select_files_only_resume(device: str, saved_inspection: dict) -> Inspection:
    """Confirm a mounted CD resume from saved metadata when DiskManagement is unavailable."""
    if (
        saved_inspection.get("device") != device
        or saved_inspection.get("classification") != "DATA CD"
        or saved_inspection.get("media", "").upper() not in {"CD-R", "CD-RW", "CD-ROM"}
        or saved_inspection.get("tracks") != 1
        or saved_inspection.get("sessions") != 1
        or not saved_inspection.get("inspection_supported")
        or saved_inspection.get("hardware", {}).get("WritableMedia") is not False
    ):
        raise ValueError(f"{device}: saved source is not a confirmed single-session data CD")

    volume_device = saved_inspection.get("volume_device")
    mount_point = saved_inspection.get("mount_point")
    volume_name = saved_inspection.get("volume")
    if (
        not isinstance(volume_device, str)
        or not isinstance(mount_point, str)
        or not mount_point.startswith("/Volumes/")
        or Path(mount_point).name != volume_name
        or not Path(mount_point).is_dir()
    ):
        raise ValueError(f"{device}: the saved mounted-volume path is not available")

    try:
        result = subprocess.run(
            ["/sbin/mount"], capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f"{device}: cannot confirm the mounted read-only volume: {exc}") from exc
    prefix = f"{volume_device} on {mount_point} ("
    matches = [line for line in result.stdout.splitlines() if line.startswith(prefix)]
    if result.returncode or len(matches) != 1 or not matches[0].endswith(")"):
        raise ValueError(f"{device}: mounted volume does not match the saved source")
    mount_options = [part.strip() for part in matches[0][len(prefix) : -1].split(",")]
    if not mount_options or mount_options[0] != "cd9660" or "read-only" not in mount_options:
        raise ValueError(f"{device}: mounted source is not a read-only CD filesystem")

    return Inspection(**saved_inspection)


def hold_mounted_volume(device: str, saved_inspection: dict | None = None) -> tuple[int, str]:
    """Open and pin the read-only optical volume before discovery traverses it."""
    try:
        result = subprocess.run(
            ["/sbin/mount"], capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f"{device}: cannot inspect mounted optical volumes: {exc}") from exc
    source_prefix = device
    matches = []
    for line in result.stdout.splitlines():
        source, separator, remainder = line.partition(" on ")
        mount_point, option_separator, options_text = remainder.rpartition(" (")
        if (
            not separator
            or not option_separator
            or not options_text.endswith(")")
            or not re.fullmatch(re.escape(source_prefix) + r"(?:s\d+)?", source)
        ):
            continue
        options = [part.strip() for part in options_text[:-1].split(",")]
        if options[0] not in {"cd9660", "iso9660", "udf"} or "read-only" not in options:
            continue
        if saved_inspection and (
            source != saved_inspection.get("volume_device")
            or mount_point != saved_inspection.get("mount_point")
        ):
            continue
        matches.append((source, mount_point))
    if result.returncode or len(matches) != 1:
        raise ValueError(f"{device}: expected one mounted read-only optical data volume")
    _source, mount_point = matches[0]
    try:
        descriptor = os.open(
            mount_point,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
    except OSError as exc:
        raise ValueError(
            f"{device}: mounted volume is unavailable at {mount_point}: {exc}"
        ) from exc
    return descriptor, mount_point


def ingest(
    report: Inspection,
    config: Config,
    storage: Storage,
    resume: str | None = None,
    notes: str = "",
    announce=print,
    device_opener=None,
    files_only: bool = False,
) -> str:
    if files_only and (not report.mount_point or not Path(report.mount_point).is_dir()):
        raise ValueError(
            f"{report.device}: mounted volume is unavailable at "
            f"{report.mount_point or '(no mount point)'}; mount the disc in Finder and retry"
        )
    multi = report.multisession_candidate
    ordinary = (
        report.classification in {"DATA CD", "DATA DVD"}
        and report.image_size is not None
        and report.image_size % 2048 == 0
        and report.tracks == 1
        and report.sessions == 1
        and report.inspection_supported
    )
    ordinary_files_only = (
        files_only
        and report.classification in {"DATA CD", "DATA DVD"}
        and report.tracks == 1
        and report.sessions == 1
        and report.inspection_supported
        and bool(report.mount_point)
    )
    supported_multi = (
        multi
        and report.media.upper() in {"CD-R", "CD-RW"}
        and report.filesystem.lower() in {"cd9660", "iso9660"}
        and report.tracks == report.sessions
        and report.tracks is not None
        and report.tracks >= 2
        and len(report.session_layout) == report.sessions
        and bool(report.volume_device)
    )
    if not (ordinary_files_only or (ordinary or supported_multi) and not files_only):
        raise ValueError(
            "Only confirmed ordinary data CDs/DVDs or one-data-track-per-session "
            "Mode 2 multisession CDs can be ingested"
        )
    config.validate_paths(Path(report.mount_point))
    if config.database.resolve().is_relative_to(config.work_dir.resolve()):
        raise ValueError("Keep the catalog database outside the image working directory")
    with catalog_lock(config.database):
        catalog = Catalog(config.database)
        try:
            return _ingest(
                catalog,
                report,
                config,
                storage,
                resume,
                notes,
                announce,
                device_opener or open_device,
                files_only,
            )
        finally:
            catalog.close()


def _ingest(
    catalog, report, config, storage, resume, notes, announce, device_opener, files_only=False
):
    multi = report.multisession_candidate
    raw_sector_cd = bool(
        report.hardware
        and report.hardware.get("DeviceBlockSize") == 2352
        and len(report.session_layout) == 1
        and report.session_layout[0].get("session") == 1
        and report.session_layout[0].get("track") == 1
        and report.session_layout[0].get("sectors", 0) * 2048 == report.image_size
    )
    archive_size = report.archive_image_size or report.image_size
    archive_mode = "files_only" if files_only else "image"
    destination = {"bucket": config.bucket, "prefix": config.prefix}
    if resume:
        record = catalog.get(resume)
        if record.get("recovery", {}).get("kind") == "incomplete_namespace":
            raise ValueError(
                f"{resume}: only an incomplete namespace was recovered; "
                "restore a checkpoint backup to resume"
            )
        if record["destination"] != destination:
            raise ValueError(f"{resume}: destination differs from original ingestion")
        old_mode = record.get("archive_mode", "image")
        if old_mode != archive_mode:
            can_switch_to_files = (
                files_only
                and record["state"] in {"FAILED", "NEEDS_REVIEW", "INTERRUPTED"}
                and not record.get("image_sha256")
                and not record.get("upload")
                and not record.get("manifest")
            )
            if not can_switch_to_files:
                raise ValueError(f"{resume}: archive mode differs from the original ingestion")
            record["archive_mode"] = archive_mode
        original = record["inspection"]
        for key in (
            "media",
            "volume",
            "filesystem",
            "image_size",
            "tracks",
            "sessions",
        ):
            if original.get(key) != getattr(report, key):
                raise ValueError(f"{resume}: inserted media metadata differs: {key}")
        if (
            (multi or raw_sector_cd)
            and original.get("session_layout")
            and original.get("session_layout") != json.loads(json.dumps(report.session_layout))
        ):
            raise ValueError(f"{resume}: inserted session TOC differs")
        if raw_sector_cd and not original.get("session_layout"):
            record["inspection"] = asdict(report)
        if record["state"] == "COMPLETE":
            announce(f"{resume} is already COMPLETE; no changes made.")
            return resume
        if record["work_dir"] != str(config.work_dir):
            raise ValueError(f"{resume}: use the original work directory: {record['work_dir']}")
    else:
        record = catalog.create(
            {
                "inspection": asdict(report),
                "destination": destination,
                "started_at": now(),
                "notes": notes,
                "work_dir": str(config.work_dir),
                "archive_mode": archive_mode,
            }
        )
    source_id = record["source_id"]
    announce(f"{source_id}: destination s3://{config.bucket}/{config.key('sources/' + source_id)}/")
    announce(f"Resume with --resume {source_id}; keep the catalog and work directory.")
    work = config.work_dir / source_id
    mount = Path(report.mount_point).resolve()
    image_name = f"{source_id}.bin" if multi else f"{source_id}.img"
    outputs = [work / name for name in (image_name, "manifest.json", "archive.db", "archive.csv")]
    outputs.append(config.log_dir / f"{source_id}.log")
    for path in outputs:
        if path.resolve().is_relative_to(mount):
            raise ValueError(f"Local archive output resolves onto source media: {path}")
        if path.is_symlink() or (
            path.exists() and (not stat.S_ISREG(path.stat().st_mode) or path.stat().st_nlink != 1)
        ):
            raise ValueError(f"Archive output must be an unlinked regular file: {path}")
    work.mkdir(parents=True, exist_ok=True)
    config.log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(f"family_archive.ingest.{uuid.uuid4()}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = logging.FileHandler(config.log_dir / f"{source_id}.log")
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    logger.addHandler(handler)
    run_id = catalog.start_run(source_id, now())
    stage = "reserve source namespace"

    def save():
        catalog.save(record)

    def step(message):
        nonlocal stage
        stage = message
        logger.info("%s %s", source_id, message)
        announce(f"{source_id}: {message}")

    raw_sector_mode = None

    def capture_source(image_path, expected_size, progress=None):
        nonlocal raw_sector_mode
        capture_device = report.device if raw_sector_cd else (report.image_device or report.device)
        with device_opener(capture_device) as raw_source:
            source = (
                RawCdDataReader(
                    raw_source,
                    report.session_layout[0]["start_lba"],
                    report.archive_sector_count or report.session_layout[0]["sectors"],
                )
                if raw_sector_cd
                else raw_source
            )
            digest = capture(source, image_path, expected_size, progress)
            if raw_sector_cd:
                if raw_sector_mode is not None and source.mode != raw_sector_mode:
                    raise ValueError("Source CD sector mode changed between physical reads")
                raw_sector_mode = source.mode
            return digest

    try:
        record["state"] = "INGESTING"
        save()
        reservation = json.dumps(
            {"source_id": source_id, "token": record["token"], "schema_version": 1}, sort_keys=True
        ).encode()
        storage.ensure_bytes(config.key(f"sources/{source_id}/source.json"), reservation)
        if files_only:
            return _ingest_files_only(
                catalog,
                record,
                source_id,
                report,
                config,
                storage,
                work,
                run_id,
                destination,
                step,
                save,
                announce,
            )
        image = work / image_name
        tracks: list[Track] = []
        track_records = []
        if multi:
            step("capture and verify each raw Mode 2 session (source remains read-only)")
            track_dir = work / "session-tracks"

            def save_tracks(items):
                nonlocal track_records
                offset = 0
                track_records = []
                for item in items:
                    track_records.append(track_metadata(item, offset))
                    offset += track_records[-1]["size"]
                record["session_tracks"] = track_records
                save()

            tracks = capture_tracks(
                report.device,
                report.volume_device,
                Path(report.mount_point),
                [item["start_lba"] for item in report.session_layout],
                [item["sectors"] for item in report.session_layout],
                track_dir,
                record.get("session_tracks"),
                save_tracks,
            )
            step("validate sector EDC/ECC and inventory every Joliet session view")
            entries = inventory_sessions(tracks)
            catalog.save_files(source_id, entries)
            record["inventory_origin"] = "multisession_tracks"
            record["file_count"] = sum(entry["kind"] == "file" for entry in entries)
            size, digest, track_records = build_composite(tracks, image)
            save_tracks(tracks)
        else:
            step("capture and validate raw image (source opened read-only)")
            require_detached(image)
            last_percent = -1

            def progress(done, total):
                nonlocal last_percent
                percent = done * 100 // total
                if percent // 10 != last_percent // 10:
                    announce(f"{source_id}: image {percent}% ({done:,}/{total:,} bytes)")
                    last_percent = percent

            digest = capture_source(image, archive_size, progress)
            size, local_digest = hash_file(image)
            if size != archive_size or digest != local_digest:
                raise ValueError("Local image failed SHA-256 read-back verification")
            step("inventory and SHA-256 files from the verified local image (mounted read-only)")
            previous = catalog.files(source_id) if record.get("inventory_origin") == "image" else []
            if record.get("inventory_origin") != "image":
                catalog.save_files(source_id, [])
                record["inventory_origin"] = "image"
                save()
            with mount_image(image, report.filesystem) as image_root:
                entries = inventory(
                    image_root,
                    previous,
                    lambda entry: catalog.save_file(source_id, entry),
                )
            catalog.save_files(source_id, entries)
            record["file_count"] = sum(entry["kind"] == "file" for entry in entries)
        if record.get("image_sha256") and record["image_sha256"] != digest:
            raise ValueError("Image identity differs from the previously captured source")
        record.update(image_sha256=digest, image_size=size)
        save()
        # Retain the second full optical read as an independent source check.
        step("confirm source identity after inventory")
        if multi:
            recheck = capture_tracks(
                report.device,
                report.volume_device,
                Path(report.mount_point),
                [item["start_lba"] for item in report.session_layout],
                [item["sectors"] for item in report.session_layout],
                work / "source-recheck",
            )
            if [track.sha256 for track in recheck] != [track.sha256 for track in tracks]:
                raise ValueError("Source changed during inventory")
        else:
            if capture_source(image, size) != digest:
                raise ValueError("Source changed during inventory")
        record["state"] = "VERIFYING"
        save()
        image_key = config.key(
            f"sources/{source_id}/image/{source_id}{'.bin' if multi else '.img'}"
        )
        step("upload/resume image and verify full S3 SHA-256 read-back")
        checkpoint = record.setdefault("upload", {})
        storage.ensure_file(image_key, image, digest, checkpoint, save)
        step("publish and verify portable manifest")
        if "manifest" not in record:
            schema_version = MULTISESSION_SCHEMA_VERSION if multi else SCHEMA_VERSION
            if multi:
                scope = (
                    "Ordered Mode 2 Form 1 data tracks; two TAO link blocks omitted per session; "
                    "pregap/lead-in/lead-out and subchannels excluded"
                )
            elif raw_sector_cd:
                mode_name = "Mode 1" if raw_sector_mode == 1 else "Mode 2 Form 1"
                if report.excluded_track_sectors:
                    scope = (
                        f"ISO volume Track 1 {mode_name} logical sectors; "
                        f"{report.excluded_track_sectors} trailing TOC sectors were unreadable "
                        "and excluded outside the ISO volume; raw headers, EDC/ECC, "
                        "pregap/lead-in/lead-out, and subchannels excluded"
                    )
                else:
                    scope = (
                        f"Track 1 {mode_name} logical sectors; raw headers, EDC/ECC, "
                        "pregap/lead-in/lead-out, and subchannels excluded"
                    )
            elif report.image_device and report.image_device != report.device:
                scope = (
                    "Mounted data-track logical sectors; excludes raw CD sector EDC/ECC, "
                    "pregap/lead-in/lead-out, and subchannels"
                )
            else:
                scope = "Whole-device logical address space; excludes raw optical subchannels"
            preservation = {
                "format": (
                    "concatenated_mode2_raw_session_tracks" if multi else "raw_logical_sector_image"
                ),
                "sector_size": 2352 if multi else 2048,
                "key": image_key,
                "size": size,
                "sha256": digest,
                "scope": scope,
            }
            if multi:
                preservation["tracks"] = track_records
            elif raw_sector_cd:
                preservation["source_track"] = {
                    "track": 1,
                    "start_lba": report.session_layout[0]["start_lba"],
                    "declared_track_sectors": report.session_layout[0]["sectors"],
                    "captured_sectors": report.archive_sector_count
                    or report.session_layout[0]["sectors"],
                    "excluded_trailing_sectors": report.excluded_track_sectors,
                    "source_sector_size": 2352,
                    "image_sector_size": 2048,
                    "sector_mode": "Mode 1" if raw_sector_mode == 1 else "Mode 2 Form 1",
                    "tail_exclusion_reason": (
                        "ISO volume descriptor declares the captured sector count; "
                        "the two trailing TOC sectors were unreadable"
                        if report.excluded_track_sectors
                        else None
                    ),
                }
            record["manifest"] = {
                "schema_version": schema_version,
                "source_id": source_id,
                "source_type": "multisession_data_cd"
                if multi
                else report.classification.lower().replace(" ", "_"),
                "archive_status": "COMPLETE",
                "started_at": record["started_at"],
                "completed_at": now(),
                "notes": record["notes"],
                "inspection": record["inspection"],
                "destination": destination,
                "preservation": preservation,
                "inventory_origin": "multisession_tracks" if multi else "image",
                "file_count": record["file_count"],
                "directory_count": sum(entry["kind"] == "directory" for entry in entries),
                "logical_file_size": sum(entry.get("size", 0) for entry in entries),
                "files": entries,
            }
            save()
        manifest_bytes = serialize(record["manifest"])
        (work / "manifest.json").write_bytes(manifest_bytes)
        storage.ensure_bytes(config.key(f"sources/{source_id}/manifest.json"), manifest_bytes)
        step("back up catalog checkpoint and CSV")
        db_backup, csv_backup = work / "archive.db", work / "archive.csv"
        catalog.export(db_backup, csv_backup)
        for path in (db_backup, csv_backup):
            _, sha256 = hash_file(path)
            storage.ensure_file(
                config.key(f"catalog/backups/{source_id}/{run_id}/{path.name}"),
                path,
                sha256,
                {},
                lambda: None,
            )
        record["state"] = "COMPLETE"
        record["completed_at"] = record["manifest"]["completed_at"]
        save()
        catalog.end_run(run_id, "COMPLETE", now())
        step("COMPLETE: image, manifest, and catalog backups verified; local cache retained")
        return source_id
    except BaseException as exc:
        if record["state"] == "COMPLETE":
            raise
        state = "INTERRUPTED" if isinstance(exc, (KeyboardInterrupt, SystemExit)) else "FAILED"
        if isinstance(exc, ValueError):
            state = "NEEDS_REVIEW"
        record["state"] = state
        # Local errors contain useful paths; never persist SDK response bodies.
        detail = (
            str(exc) if isinstance(exc, (OSError, ValueError, StorageError)) else type(exc).__name__
        )
        record["last_error"] = {
            "operation": stage,
            "error_type": type(exc).__name__,
            "detail": detail,
            "time": now(),
        }
        save()
        catalog.end_run(run_id, state, now())
        logger.error("%s %s during %s (%s)", source_id, state, stage, detail)
        message = (
            f"{source_id}: {state} during {stage}: {exc}. "
            f"Uploads may already exist and are retained. Retry with --resume {source_id}; "
            "conflicting data requires review. No source data was written."
        )
        if isinstance(exc, KeyboardInterrupt):
            announce(message)
            raise
        raise IngestionError(message) from exc
    finally:
        handler.close()
        logger.removeHandler(handler)


def _ingest_files_only(
    catalog,
    record,
    source_id,
    report,
    config,
    storage,
    work,
    run_id,
    destination,
    step,
    save,
    announce,
):
    """Archive visible regular files without requiring a readable full-disc image."""
    source_root = Path(report.mount_point).resolve()
    step("inventory and SHA-256 visible files from the mounted source")
    entries = inventory(source_root)
    catalog.save_files(source_id, entries)
    record["inventory_origin"] = "source_mount"
    record["source_type"] = report.classification.lower().replace(" ", "_")
    record["file_count"] = sum(entry["kind"] == "file" for entry in entries)
    record["directory_count"] = sum(entry["kind"] == "directory" for entry in entries)
    record["logical_file_size"] = sum(entry.get("size", 0) for entry in entries)
    save()

    file_prefix = config.key(f"sources/{source_id}/data")
    uploads = record.setdefault("file_uploads", {})
    for index, entry in enumerate(entries, start=1):
        if entry["kind"] != "file":
            continue
        relative = PurePosixPath(entry["path"])
        source_path = source_root.joinpath(*relative.parts)
        object_key = f"{file_prefix}/{entry['path']}"
        step(f"upload and verify file {index} of {len(entries)}: {entry['path']}")
        checkpoint = uploads.setdefault(entry["path"], {})
        if entry["size"] <= 64 * 1024 * 1024 and hasattr(storage, "ensure_small_file"):
            storage.ensure_small_file(object_key, source_path, entry["sha256"])
        else:
            storage.ensure_file(object_key, source_path, entry["sha256"], checkpoint, save)
        entry["object_key"] = object_key
        catalog.save_file(source_id, entry)
        uploads[entry["path"]] = {
            "object_key": object_key,
            "size": entry["size"],
            "sha256": entry["sha256"],
            "verified": True,
        }
        save()

    step("recheck mounted source files after upload")
    recheck = inventory(source_root)
    fields = ("path", "kind", "size", "sha256", "target", "mtime_ns", "mode")
    if [[entry.get(key) for key in fields] for entry in entries] != [
        [entry.get(key) for key in fields] for entry in recheck
    ]:
        raise ValueError("Mounted source changed during files-only archive")
    prior_manifest = record.get("manifest")
    if prior_manifest and prior_manifest.get("files") != entries:
        raise ValueError("Mounted source inventory differs from the already published manifest")

    record["files"] = entries
    record["file_count"] = sum(entry["kind"] == "file" for entry in entries)
    record["directory_count"] = sum(entry["kind"] == "directory" for entry in entries)
    record["logical_file_size"] = sum(entry.get("size", 0) for entry in entries)
    record["state"] = "VERIFYING"
    save()
    manifest = record.get("manifest")
    if manifest is None:
        manifest = {
            "schema_version": FILES_ONLY_SCHEMA_VERSION,
            "source_id": source_id,
            "source_type": report.classification.lower().replace(" ", "_"),
            "archive_status": "COMPLETE",
            "started_at": record["started_at"],
            "completed_at": now(),
            "notes": record["notes"],
            "inspection": record["inspection"],
            "destination": destination,
            "preservation": {
                "format": "mounted_files",
                "full_disc_image": False,
                "object_prefix": file_prefix,
                "scope": (
                    "Regular files visible on the mounted read-only filesystem, with original "
                    "relative paths. Raw disc sectors, boot structures, and content not exposed "
                    "by the mounted filesystem are not included."
                ),
            },
            "inventory_origin": "source_mount",
            "file_count": record["file_count"],
            "directory_count": record["directory_count"],
            "logical_file_size": record["logical_file_size"],
            "files": entries,
        }
        record["manifest"] = manifest
        record["completed_at"] = manifest["completed_at"]
        save()
    manifest_bytes = serialize(manifest)
    work.mkdir(parents=True, exist_ok=True)
    (work / "manifest.json").write_bytes(manifest_bytes)
    step("publish and verify files-only manifest")
    storage.ensure_bytes(config.key(f"sources/{source_id}/manifest.json"), manifest_bytes)
    step("back up catalog checkpoint and CSV")
    db_backup, csv_backup = work / "archive.db", work / "archive.csv"
    catalog.export(db_backup, csv_backup)
    for path in (db_backup, csv_backup):
        _, sha256 = hash_file(path)
        storage.ensure_file(
            config.key(f"catalog/backups/{source_id}/{run_id}/{path.name}"),
            path,
            sha256,
            {},
            lambda: None,
        )
    record["state"] = "COMPLETE"
    save()
    catalog.end_run(run_id, "COMPLETE", now())
    step(
        "COMPLETE: visible files, manifest, and catalog backups verified; "
        "no full disc image captured"
    )
    return source_id

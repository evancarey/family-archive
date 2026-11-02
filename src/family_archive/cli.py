"""Terminal interface for inspection and verified optical ingestion."""

import argparse
import json
import mimetypes
import os
import sqlite3
import sys
from pathlib import Path
from urllib.parse import quote

from family_archive.media import InspectionError, inspect_media


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="family-archive")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("inspect", help="Inspect optical media on macOS without modifying it")
    for name, help_text in (
        ("status", "Summarize the local archive catalog"),
        ("list", "List archived sources"),
        ("search", "Search source IDs, labels, paths, and filenames"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument(
            "--config", type=Path, help="TOML config (default ~/.family-archive/config.toml)"
        )
        command.add_argument("--database", type=Path, help="Override the local SQLite catalog path")
        if name == "search":
            command.add_argument("term", nargs="?")
            command.add_argument("--media", choices=("photo", "video", "audio"))
    library_parser = commands.add_parser(
        "library", help="Build read-only derived family-library metadata"
    )
    library_commands = library_parser.add_subparsers(dest="library_command", required=True)
    library_index = library_commands.add_parser(
        "index", help="Build a local JSON index from complete source manifests"
    )
    library_index.add_argument(
        "--output", required=True, type=Path, help="New JSON output path; never overwritten"
    )
    library_index.add_argument("--config", type=Path)
    library_index.add_argument("--bucket")
    library_index.add_argument("--prefix")
    library_index.add_argument("--profile")
    library_index.add_argument("--database", type=Path)
    library_materialize = library_commands.add_parser(
        "materialize", help="Download and verify media into a private local library cache"
    )
    library_materialize.add_argument("--cache-dir", required=True, type=Path)
    library_materialize.add_argument("--config", type=Path)
    library_materialize.add_argument("--bucket")
    library_materialize.add_argument("--prefix")
    library_materialize.add_argument("--profile")
    library_materialize.add_argument("--database", type=Path)
    library_materialize.add_argument(
        "--dry-run",
        action="store_true",
        help="Estimate reads and storage without downloading media",
    )
    library_materialize.add_argument(
        "--publish",
        action="store_true",
        help=(
            "Publish verified originals, thumbnails, and an index under the private "
            "library/ prefix"
        ),
    )
    library_materialize.add_argument(
        "--reuse-verified-cache",
        action="store_true",
        help="Re-hash an existing local cache against manifests without reading media from S3",
    )
    library_materialize.add_argument(
        "--incremental",
        action="store_true",
        help=(
            "Reuse each valid content-addressed cache file and fetch only missing media; "
            "the complete index is still published"
        ),
    )
    library_materialize.add_argument(
        "--video-playback",
        action="store_true",
        help=(
            "Create browser-friendly H.264/AAC MP4 derivatives from indexed videos; "
            "requires ffmpeg and ffprobe"
        ),
    )
    ingest_parser = commands.add_parser(
        "ingest", help="Archive a data CD/DVD or local directory to S3"
    )
    source_group = ingest_parser.add_mutually_exclusive_group(required=True)
    source_group.add_argument("--device", help="Inspected whole optical device, e.g. /dev/disk5")
    source_group.add_argument(
        "--directory", type=Path, help="Local directory tree to archive without modifying it"
    )
    ingest_parser.add_argument("--resume", metavar="SRC-ID", help="Resume this catalog source ID")
    ingest_parser.add_argument(
        "--config", type=Path, help="TOML config (default ~/.family-archive/config.toml)"
    )
    ingest_parser.add_argument("--bucket")
    ingest_parser.add_argument("--prefix")
    ingest_parser.add_argument("--profile", help="Standard AWS profile")
    ingest_parser.add_argument("--database", type=Path)
    ingest_parser.add_argument("--work-dir", type=Path)
    ingest_parser.add_argument("--log-dir", type=Path)
    ingest_parser.add_argument("--notes", default="")
    ingest_parser.add_argument(
        "--files-only",
        action="store_true",
        help="Archive visible files individually; do not require a complete disc image",
    )
    ingest_parser.add_argument(
        "--dry-run", action="store_true", help="Inspect and preview; no local or S3 writes"
    )
    for name, help_text in (
        ("verify", "Read back and SHA-256 verify an archived source and its manifest"),
        ("recover", "Rebuild or restore a catalog into a new local database"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--config", type=Path)
        command.add_argument("--bucket")
        command.add_argument("--prefix")
        command.add_argument("--profile")
        command.add_argument("--database", type=Path)
        command.add_argument("--work-dir", type=Path)
        if name == "verify":
            command.add_argument("source_id")
            command.add_argument(
                "--no-local-anchor",
                action="store_true",
                help="Verify using the remote manifest only",
            )
        else:
            output = command.add_mutually_exclusive_group(required=True)
            output.add_argument("--output", type=Path, help="New database path; never overwritten")
            output.add_argument("--list-backups", action="store_true")
            command.add_argument("--backup-key", help="Full S3 key of a SQLite checkpoint backup")
    args = parser.parse_args(argv)
    if args.command in {"status", "list", "search"}:
        return catalog_command(args)
    if args.command in {"verify", "recover"}:
        return archive_command(args)
    if args.command == "library":
        return library_command(args)
    if args.command == "ingest":
        return ingest_command(args)
    try:
        reports = inspect_media()
    except InspectionError as exc:
        print(f"Inspection failed: {exc}\nSafe to retry. No data was uploaded.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Inspection interrupted. Safe to retry.", file=sys.stderr)
        return 130
    if not reports:
        print("No optical media detected. Insert and mount a CD or DVD, then retry.")
        return 0
    for index, report in enumerate(reports):
        if index:
            print()
        fields = (
            ("Device", report.device),
            ("Media", report.media),
            ("Volume", report.volume),
            ("Mount", report.mount_point),
            ("Filesystem", report.filesystem),
            ("Capacity", f"{report.capacity:,} bytes" if report.capacity else "Unknown"),
        )
        if report.classification in {"DATA CD", "DATA DVD"}:
            fields += (
                ("Image device", report.image_device or report.device),
                ("Image size", f"{report.image_size:,} bytes" if report.image_size else "Unknown"),
            )
        for label, value in fields + (
            ("Tracks", report.tracks or "Unknown"),
            ("Sessions", report.sessions or "Unknown"),
            ("Inspection", "Supported" if report.inspection_supported else "Needs review"),
            ("Classification", report.classification),
            ("Details", report.reason),
        ):
            print(f"{label + ':':16}{value}")
    return 2 if any(report.classification == "NEEDS_REVIEW" for report in reports) else 0


def catalog_command(args) -> int:
    """Query the working catalog without creating or modifying it."""
    import tomllib

    try:
        database = args.database
        if database is None:
            config_path = args.config or Path("~/.family-archive/config.toml").expanduser()
            if config_path.exists():
                with config_path.open("rb") as stream:
                    database = Path(
                        tomllib.load(stream).get("database", "~/.family-archive/archive.db")
                    )
            elif args.config is not None:
                raise ValueError(f"Configuration file does not exist: {args.config}")
            else:
                database = Path("~/.family-archive/archive.db")
        database = database.expanduser().resolve()
        if not database.is_file():
            print(f"No catalog found at {database}. Run an ingestion first, or pass --database.")
            return 0
        uri = "file:" + quote(str(database), safe="/") + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
        try:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version != 1:
                raise ValueError(f"Unsupported catalog schema version: {version}")
            rows = connection.execute(
                "SELECT source_id, state, record FROM sources ORDER BY number"
            ).fetchall()
            sources = [(row["source_id"], row["state"], json.loads(row["record"])) for row in rows]
            if args.command == "status":
                _print_catalog_status(sources)
            elif args.command == "list":
                _print_source_list(sources)
            else:
                if args.term is None and args.media is None:
                    raise ValueError("search needs a term or --media")
                _print_search_results(connection, sources, args.term or "", args.media)
        finally:
            connection.close()
        return 0
    except (OSError, sqlite3.Error, ValueError, tomllib.TOMLDecodeError) as exc:
        print(f"{args.command} failed: {exc}", file=sys.stderr)
        return 1


def _print_catalog_status(sources: list[tuple[str, str, dict]]) -> None:
    counts = {
        state: sum(source[1] == state for source in sources)
        for state in (
            "COMPLETE",
            "INGESTING",
            "INTERRUPTED",
            "VERIFYING",
            "FAILED",
            "NEEDS_REVIEW",
            "NEW",
        )
    }
    files = sum(record.get("file_count", 0) for _, _, record in sources)
    size = sum(record.get("logical_file_size", 0) for _, _, record in sources)
    print("Archive")
    print(f"Sources:             {len(sources)}")
    print(f"Complete:            {counts['COMPLETE']}")
    print(f"Incomplete:          {len(sources) - counts['COMPLETE']}")
    print(f"Files discovered:    {files:,}")
    print(f"Logical data:        {size:,} bytes")
    active = next(
        (
            (sid, state, record)
            for sid, state, record in reversed(sources)
            if state in {"INGESTING", "INTERRUPTED", "VERIFYING", "FAILED", "NEEDS_REVIEW"}
        ),
        None,
    )
    if active:
        sid, state, record = active
        print("\nCurrent source:")
        print(f"{sid}  {record.get('source_type', 'unknown')}  {state}")


def _print_source_list(sources: list[tuple[str, str, dict]]) -> None:
    if not sources:
        print("No sources in catalog.")
        return
    print(f"{'SOURCE':<12} {'TYPE':<22} {'LABEL':<24} STATUS")
    for source_id, state, record in sources:
        inspection = record.get("inspection", {})
        source_type = record.get("source_type", inspection.get("classification", "unknown"))
        print(f"{source_id:<12} {source_type:<22.22} {inspection.get('volume', ''):<24.24} {state}")


def _print_search_results(connection, sources, term: str, media: str | None = None) -> None:
    needle = term.casefold()
    hits = []
    if not media and needle:
        for source_id, _state, record in sources:
            inspection = record.get("inspection", {})
            if (
                needle in source_id.casefold()
                or needle in str(inspection.get("volume", "")).casefold()
            ):
                hits.append((source_id, "", inspection.get("volume", "")))
    media_prefix = {"photo": "image/", "video": "video/", "audio": "audio/"}.get(media)
    for row in connection.execute("SELECT source_id, record FROM files ORDER BY source_id, path"):
        entry = json.loads(row["record"])
        path = entry.get("path", "")
        mime_type = entry.get("mime_type") or mimetypes.guess_type(path)[0] or ""
        matches_media = not media_prefix or mime_type.startswith(media_prefix)
        matches_term = (
            not needle
            or needle in path.casefold()
            or needle in str(entry.get("filename", "")).casefold()
        )
        if entry.get("kind") == "file" and matches_media and matches_term:
            hits.append((row["source_id"], path, ""))
    for source_id, path, label in hits:
        print(f"{source_id}  {path or f'[volume: {label}]'}")
    if not hits:
        print("No matches found.")


def ingest_command(args) -> int:
    from family_archive.catalog import Catalog
    from family_archive.config import load_config
    from family_archive.ingest import (
        IngestionError,
        directory_inspection,
        hold_mounted_volume,
        ingest,
        ingest_directory,
        select_files_only_resume,
        select_source,
    )

    mount_guard_fd = None

    def announce(message):
        print(message, flush=True)

    try:
        config = load_config(
            args.config,
            bucket=args.bucket,
            prefix=args.prefix,
            profile=args.profile,
            database=args.database,
            work_dir=args.work_dir,
            log_dir=args.log_dir,
        )
        storage = None
        if not args.dry_run:
            from family_archive.storage.s3 import connect

            storage = connect(config.bucket, config.profile)
        if args.directory is not None:
            if args.files_only:
                raise ValueError(
                    "--files-only applies to optical devices; --directory is already files-only"
                )
            report = directory_inspection(args.directory)
            config.validate_paths(Path(report.mount_point))
            if args.dry_run:
                print(f"Source directory: {report.mount_point}")
                print(f"Destination: s3://{config.bucket}/{config.key('sources/')}")
                print(
                    "Plan: inventory all entries, preserve relative paths, upload each regular "
                    "file, SHA-256 read-back, manifest, catalog backup."
                )
                print("Dry run only. No local archive data or S3 objects were written.")
                return 0
            ingest_directory(
                args.directory, config, storage, args.resume, args.notes, announce=announce
            )
            return 0
        if args.files_only:
            saved_inspection = None
            if args.resume:
                catalog = Catalog(config.database)
                try:
                    saved_inspection = catalog.get(args.resume)["inspection"]
                finally:
                    catalog.close()
            is_saved_cd = bool(
                saved_inspection
                and saved_inspection.get("classification") == "DATA CD"
                and saved_inspection.get("media", "").upper() in {"CD-R", "CD-RW", "CD-ROM"}
            )
            mount_guard_fd, pinned_mount = hold_mounted_volume(args.device, saved_inspection)
            if is_saved_cd:
                report = select_files_only_resume(args.device, saved_inspection)
                print(
                    "Source check: matched the saved single-session CD to its mounted "
                    "read-only filesystem."
                )
            else:
                report = select_source(args.device, True)
            if report.mount_point != pinned_mount:
                raise ValueError(
                    f"{args.device}: inspected mount differs from the pinned read-only volume"
                )
        else:
            report = select_source(args.device)
        config.validate_paths(Path(report.mount_point))
        if args.dry_run:
            print(f"Source: {report.device} ({report.classification}, {report.volume})")
            if args.files_only:
                print(f"Source files: mounted read-only volume at {report.mount_point}")
                print(f"Destination: s3://{config.bucket}/{config.key('sources/')}")
                print(
                    "Plan: inventory visible files, preserve original paths, upload each file, "
                    "SHA-256 read-back, manifest, catalog backup."
                )
                print(
                    "Files-only mode: no complete disc image will be captured; "
                    "the manifest will record that scope."
                )
                print("Dry run only. No local archive data or S3 objects were written.")
                return 0
            if report.multisession_candidate:
                captured = sum(item["sectors"] - 2 for item in report.session_layout) * 2352
                print(
                    f"Preservation image: {captured:,} raw Mode 2 bytes across "
                    f"{report.sessions} sessions; local cache: {config.work_dir}"
                )
                print(
                    "Capture dependency: cdrdao; two presumed TAO link blocks omitted per session."
                )
            else:
                raw_cd_capture = bool(
                    report.hardware
                    and report.hardware.get("DeviceBlockSize") == 2352
                    and len(report.session_layout) == 1
                )
                image_device = (
                    report.device if raw_cd_capture else (report.image_device or report.device)
                )
                print(
                    f"Raw image: {(report.archive_image_size or report.image_size):,} bytes "
                    f"from {image_device}; "
                    f"local cache: {config.work_dir}"
                )
                if report.excluded_track_sectors:
                    print(
                        f"Scope: ISO volume; {report.excluded_track_sectors} unreadable "
                        "trailing track sectors excluded and recorded in the manifest."
                    )
            print(f"Destination: s3://{config.bucket}/{config.key('sources/')}")
            print("Plan: capture, inventory, upload, SHA-256 read-back, manifest, catalog backup.")
            print("Dry run only. No local archive data or S3 objects were written.")
            return 0
        ingest(
            report,
            config,
            storage,
            args.resume,
            args.notes,
            announce=announce,
            files_only=args.files_only,
        )
        return 0
    except KeyboardInterrupt:
        print(
            "Ingestion interrupted; checkpoints retained. Resume using the source ID above.",
            file=sys.stderr,
        )
        return 130
    except ModuleNotFoundError as exc:
        print(
            f"Missing Python dependency: {exc.name}. "
            "Install project dependencies with: python -m pip install -e .",
            file=sys.stderr,
        )
        return 1
    except (InspectionError, IngestionError, ValueError, OSError) as exc:
        print(f"Ingestion failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        # Avoid printing credential resolution objects or a traceback to the terminal.
        print(
            f"Ingestion could not start ({type(exc).__name__}); check AWS configuration.",
            file=sys.stderr,
        )
        return 1
    finally:
        if mount_guard_fd is not None:
            os.close(mount_guard_fd)


def archive_command(args) -> int:
    from family_archive.config import load_config
    from family_archive.recovery import recover_catalog
    from family_archive.storage.s3 import connect
    from family_archive.verification import local_anchor, source_number, verify_source

    try:
        config = load_config(
            args.config,
            bucket=args.bucket,
            prefix=args.prefix,
            profile=args.profile,
            database=args.database,
            work_dir=args.work_dir,
        )
        if args.command == "verify":
            source_number(args.source_id)
            anchor = None if args.no_local_anchor else local_anchor(config, args.source_id)
        elif args.backup_key and args.list_backups:
            raise ValueError("--backup-key requires --output")
        reader = connect(config.bucket, config.profile)
        if args.command == "verify":
            verify_source(reader, config, args.source_id, anchor=anchor)
        elif args.list_backups:
            keys = [
                key
                for key in reader.keys(config.key("catalog/backups/"))
                if key.endswith("/archive.db")
            ]
            print("\n".join(sorted(keys)) if keys else "No catalog backups found.")
        else:
            recover_catalog(reader, config, args.output, args.backup_key)
        return 0
    except KeyboardInterrupt:
        print(f"{args.command} interrupted. No archive objects were changed.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"{args.command} failed: {exc}. No archive objects were changed.", file=sys.stderr)
        return 1


def library_command(args) -> int:
    """Build or materialize the derived family-library index."""
    import os

    from family_archive.config import load_config
    from family_archive.library import (
        load_library_index,
        serialize_library_index,
    )
    from family_archive.library_assets import load_materialized_cache, materialize_library
    from family_archive.library_publish import publish_library
    from family_archive.library_thumbnails import generate_thumbnails
    from family_archive.library_video import generate_video_playbacks
    from family_archive.storage.s3 import connect

    try:
        config = load_config(
            args.config,
            bucket=args.bucket,
            prefix=args.prefix,
            profile=args.profile,
            database=args.database,
        )
        reader = connect(config.bucket, config.profile)
        print("Reading complete source manifests...", flush=True)
        index = load_library_index(reader, config)
        if args.library_command == "index":
            output = args.output.expanduser().resolve()
            if output.is_relative_to(Path("/Volumes").resolve()):
                raise ValueError("Library index output must not be written to mounted source media")
            data = serialize_library_index(index)
            descriptor = os.open(
                output,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
            print(
                f"Wrote {len(index.items):,} media records from "
                f"{len(index.sources):,} sources to {output}"
            )
            print(
                "Index contains metadata and archive references only; "
                "no media bytes were copied."
            )
            return 0

        if args.reuse_verified_cache and args.incremental:
            raise ValueError("Choose either --reuse-verified-cache or --incremental")
        if args.dry_run:
            _print_materialization_plan(
                index,
                args.cache_dir,
                publish=args.publish,
                reuse_cache=args.reuse_verified_cache,
                incremental=args.incremental,
                video_playback=args.video_playback,
            )
            return 0

        def report_progress(stage, completed, total):
            if completed == 1 or completed % 25 == 0 or completed == total:
                print(f"{stage}: {completed:,}/{total:,}", flush=True)

        if args.reuse_verified_cache:
            print("Hashing every local media cache file (full cache audit)...", flush=True)
            result = load_materialized_cache(
                index, args.cache_dir, progress=report_progress
            )
        else:
            print("Checking cached media and fetching missing files...", flush=True)
            result = materialize_library(
                reader,
                index,
                args.cache_dir,
                reuse_existing=args.incremental,
                progress=report_progress,
            )
        print(
            f"Materialized {len(result.assets):,} unique media files into "
            f"{args.cache_dir.expanduser().resolve()}"
        )
        if args.incremental:
            print(
                f"Reused {result.reused_assets:,} locally verified media files; "
                f"fetched {len(result.assets) - result.reused_assets:,} missing files."
            )
        if result.unsupported_items:
            print(f"Skipped {len(result.unsupported_items):,} multisession media items:")
            for item in result.unsupported_items:
                print(f"  {item}")
        playback_assets = ()
        if args.video_playback:
            print("Preparing browser-playable video copies...", flush=True)
            playback_result = generate_video_playbacks(
                index,
                result,
                args.cache_dir.expanduser() / ".video-playback",
                progress=report_progress,
            )
            playback_assets = playback_result.assets
            print(
                f"Created or reused {len(playback_assets):,} browser-playable video copies "
                f"({sum(asset.size for asset in playback_assets):,} bytes)."
            )
            if playback_result.failed_items:
                print(
                    f"Could not create playback copies for {len(playback_result.failed_items):,} "
                    "videos; their originals remain available for download."
                )
                for item in playback_result.failed_items:
                    print(f"  {item}")
        if args.publish:
            print("Generating or reusing thumbnails...", flush=True)
            thumbnail_result = generate_thumbnails(
                index,
                result,
                args.cache_dir.expanduser() / ".thumbnails",
                include_videos=args.video_playback,
                progress=report_progress,
            )
            if thumbnail_result.failed_items:
                print(
                    f"Could not create thumbnails for {len(thumbnail_result.failed_items):,} "
                    "media items; their verified originals will still be published."
                )
                for item in thumbnail_result.failed_items:
                    print(f"  {item}")
            print("Publishing verified library objects and the index last...", flush=True)
            published = publish_library(
                reader,
                config,
                index,
                result,
                thumbnail_result.assets,
                playback_assets,
                reuse_published_objects=args.incremental,
                progress=report_progress,
            )
            print(
                f"Published {published.item_count:,} media records from "
                f"{published.source_count:,} sources; "
                f"{published.media_bytes:,} media bytes and "
                f"{published.thumbnail_bytes:,} thumbnail bytes and "
                f"{published.playback_bytes:,} video playback bytes."
            )
            print(f"Library index: {config.bucket}/{published.key}")
            if args.incremental:
                print(
                    f"Reused {published.reused_objects:,} previously verified S3 objects; "
                    "new or unreferenced objects were verified before publishing."
                )
        return 0
    except KeyboardInterrupt:
        retained = (
            "Previously verified content-addressed library objects may remain; retry is safe."
            if getattr(args, "publish", False)
            else "verified local files are retained and safe to reuse."
        )
        print(
            f"library {args.library_command} interrupted. Source archive objects were not changed; "
            f"{retained}",
            file=sys.stderr,
        )
        return 130
    except Exception as exc:
        if getattr(args, "dry_run", False):
            retained = "Dry run made no local or S3 writes."
        elif getattr(args, "publish", False):
            retained = (
                "Verified content-addressed library objects may remain; retry is safe and "
                "the index is published last."
            )
        else:
            retained = "Completed local derived files are retained."
        print(
            f"library {args.library_command} failed: {exc}. "
            f"{retained}",
            file=sys.stderr,
        )
        return 1


def _print_materialization_plan(
    index,
    cache_dir: Path,
    *,
    publish: bool = False,
    reuse_cache: bool = False,
    incremental: bool = False,
    video_playback: bool = False,
) -> None:
    supported = [
        item for item in index.items if item.original.kind in {"file_object", "image_entry"}
    ]
    source_objects = {item.original.object_key: item.original.object_size for item in supported}
    assets = {item.sha256: item.size for item in supported}
    print(f"Media records:          {len(index.items):,}")
    print(f"Materializable records: {len(supported):,}")
    print(f"Unique cache bytes:     {sum(assets.values()):,}")
    source_bytes = 0 if reuse_cache else sum(source_objects.values())
    print(f"Source bytes to verify: {source_bytes:,}")
    if reuse_cache:
        print(
            "Local-cache mode:       verify against manifest hashes; no media S3 reads"
        )
    elif incremental:
        print(
            "Incremental estimate:   source bytes are an upper bound; valid local "
            "cache files are reused"
        )
    print(f"Cache directory:        {cache_dir.expanduser().resolve()}")
    if len(supported) < len(index.items):
        print(
            f"Unsupported:            {len(index.items) - len(supported):,} "
            "multisession media records"
        )
    if publish:
        photo_count = len({item.sha256 for item in supported if item.media_type == "photo"})
        print(f"Unique photo thumbnails: {photo_count:,} (up to 512 pixels)")
        print("Publish plan: verified originals and thumbnails under private library/; index last.")
    if video_playback:
        videos = {item.sha256: item.size for item in supported if item.media_type == "video"}
        print(
            f"Video playback copies: {len(videos):,} unique videos "
            f"({sum(videos.values()):,} input bytes)"
        )
        print("Encoding plan: H.264/AAC MP4 derivatives; originals remain unchanged.")
    print("Dry run only. No media payloads were downloaded; no local or S3 objects were written.")

"""Recover into a new catalog; never overwrite a database or change archive objects."""

import json
import os
import shutil
import sqlite3
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from family_archive.catalog import STATES, Catalog
from family_archive.config import Config
from family_archive.hashing import CHUNK_SIZE, hash_file
from family_archive.storage import ArchiveReader, ObjectNotFound
from family_archive.verification import read_metadata, source_number, verify_source

BACKUP_LIMIT = 512 * 1024 * 1024


def reservation(reader: ArchiveReader, config: Config, source_id: str) -> dict | None:
    try:
        value = json.loads(read_metadata(reader, config.key(f"sources/{source_id}/source.json")))
    except ObjectNotFound:
        return None
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != 1
        or value.get("source_id") != source_id
        or not isinstance(value.get("token"), str)
    ):
        raise ValueError(f"{source_id}: invalid source reservation")
    UUID(value["token"])
    return value


def ensure_output_new(output: Path) -> None:
    for path in (
        output,
        *(Path(str(output) + suffix) for suffix in ("-wal", "-shm", "-journal", ".lock")),
    ):
        if path.exists() or path.is_symlink():
            raise ValueError(f"Recovery requires a new output path; will not overwrite: {path}")


def protect_source_path(output: Path, record: dict) -> None:
    mount = record.get("inspection", {}).get("mount_point")
    if (
        isinstance(mount, str)
        and mount.startswith("/")
        and output.is_relative_to(Path(mount).resolve())
    ):
        raise ValueError(f"Recovery output must not be on original source media: {output}")


def insert_source(catalog: Catalog, record: dict, entries: list[dict]) -> None:
    number = source_number(record["source_id"])
    if record["state"] not in STATES:
        raise ValueError("Invalid recovered source state")
    with catalog.db:
        catalog.db.execute(
            "INSERT INTO sources(number,source_id,state,record) VALUES (?,?,?,?)",
            (number, record["source_id"], record["state"], json.dumps(record)),
        )
        catalog.db.executemany(
            "INSERT INTO files VALUES (?,?,?,?)",
            [
                (record["source_id"], entry["path"], json.dumps(entry), entry.get("sha256"))
                for entry in entries
            ],
        )


def complete_record(config: Config, verified, claim: dict | None, old: dict | None = None) -> dict:
    manifest = verified.manifest
    record = {
        **(old or {}),
        "source_id": manifest["source_id"],
        "state": "COMPLETE",
        "inspection": manifest["inspection"],
        "destination": manifest["destination"],
        "started_at": manifest["started_at"],
        "completed_at": manifest["completed_at"],
        "notes": manifest.get("notes", ""),
        "manifest": manifest,
        "file_count": manifest["file_count"],
        "work_dir": str(config.work_dir),
        "recovery": {
            "kind": "verified_manifest",
            "time": datetime.now(UTC).isoformat(),
            "manifest_sha256": verified.manifest_sha256,
        },
    }
    if claim:
        record["token"] = claim["token"]
    if manifest.get("inventory_origin") == "image":
        record["inventory_origin"] = "image"
        record["image_sha256"] = verified.image_sha256
        record["image_size"] = verified.image_size
    elif manifest.get("inventory_origin") == "source_mount":
        record["inventory_origin"] = "source_mount"
        record["archive_mode"] = "files_only"
        record["logical_file_size"] = manifest["logical_file_size"]
        record["directory_count"] = manifest["directory_count"]
    return record


def source_namespaces(reader, config) -> tuple[set[str], set[str]]:
    prefix = config.key("sources/")
    keys = set(reader.keys(prefix))
    ids = set()
    for key in keys:
        if not key.startswith(prefix):
            raise ValueError("Storage listing returned a key outside the requested namespace")
        relative = key[len(prefix) :]
        if relative:
            source_id = relative.split("/")[0]
            source_number(source_id)
            ids.add(source_id)
    return keys, ids


def namespace_record(reader, config, source_id, keys, output, announce):
    claim = reservation(reader, config, source_id)
    if config.key(f"sources/{source_id}/manifest.json") in keys:
        verified = verify_source(reader, config, source_id, announce=announce)
        record = complete_record(config, verified, claim)
        protect_source_path(output, record)
        return record, verified.manifest["files"], True
    record = {
        "source_id": source_id,
        "state": "NEEDS_REVIEW",
        "destination": {"bucket": config.bucket, "prefix": config.prefix},
        "inspection": {"volume": "Unknown"},
        "work_dir": str(config.work_dir),
        "recovery": {
            "kind": "incomplete_namespace",
            "reason": "No completed manifest; restore a checkpoint backup to resume",
        },
    }
    if claim:
        record["token"] = claim["token"]
    announce(f"{source_id}: NEEDS_REVIEW; ID retained, no completed manifest")
    return record, [], False


def _from_manifests(reader, config, staged, output, announce):
    keys, ids = source_namespaces(reader, config)
    if not ids:
        raise ValueError("No source namespaces found; no catalog created")
    catalog = Catalog(staged)
    complete = incomplete = 0
    try:
        for source_id in sorted(ids, key=source_number):
            record, entries, verified = namespace_record(
                reader, config, source_id, keys, output, announce
            )
            insert_source(catalog, record, entries)
            complete += int(verified)
            incomplete += int(not verified)
        if keys != set(reader.keys(config.key("sources/"))):
            raise ValueError("Archive changed during recovery; stop ingestion and retry")
    finally:
        catalog.close()
    return complete, incomplete


def _validate_backup(path: Path) -> list[dict]:
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        db.execute("PRAGMA trusted_schema=OFF")
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("SQLite backup integrity check failed")
        if db.execute("PRAGMA user_version").fetchone()[0] != 1:
            raise ValueError("Unsupported SQLite backup schema version")
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if tables != {"sources", "files", "ingestion_runs", "sqlite_sequence"}:
            raise ValueError("Unexpected SQLite backup tables")
        if db.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type IN ('trigger','view')"
        ).fetchone()[0]:
            raise ValueError("Unexpected SQLite backup triggers or views")
        expected = Catalog(Path(":memory:"))
        try:

            def schema(connection):
                return sorted(
                    (kind, name, table, " ".join(sql.split()))
                    for kind, name, table, sql in connection.execute(
                        "SELECT type,name,tbl_name,sql FROM sqlite_master "
                        "WHERE name NOT LIKE 'sqlite_%'"
                    )
                )

            if schema(db) != schema(expected.db):
                raise ValueError("SQLite backup schema does not match schema version 1")
        finally:
            expected.close()
        for table in ("files", "ingestion_runs"):
            if db.execute(
                f"SELECT COUNT(*) FROM {table} t LEFT JOIN sources s "
                "ON s.source_id=t.source_id WHERE s.source_id IS NULL"
            ).fetchone()[0]:
                raise ValueError("SQLite backup contains orphan catalog records")
        records = []
        for number, source_id, state, data in db.execute(
            "SELECT number,source_id,state,record FROM sources"
        ):
            record = json.loads(data)
            if not isinstance(record, dict):
                raise ValueError("SQLite source record must be a JSON object")
            if (
                source_number(source_id) != number
                or record.get("source_id") != source_id
                or record.get("state") != state
                or state not in STATES
            ):
                raise ValueError("Inconsistent SQLite backup source record")
            records.append(record)
        return records
    finally:
        db.close()


def _from_backup(reader, config, staged, output, key, announce):
    prefix = config.key("catalog/backups/")
    if not key.startswith(prefix):
        raise ValueError("Backup key must be under the configured catalog/backups/ prefix")
    parts = key[len(prefix) :].split("/")
    if len(parts) != 3 or parts[2] != "archive.db":
        raise ValueError("Expected a catalog/backups/SRC-ID/run-uuid/archive.db object key")
    source_number(parts[0])
    UUID(parts[1])
    size = 0
    with reader.open_read(key) as stream, staged.open("xb") as target:
        while data := stream.read(CHUNK_SIZE):
            size += len(data)
            if size > BACKUP_LIMIT:
                raise ValueError("SQLite backup exceeds the 512 MiB recovery limit")
            target.write(data)
    records = _validate_backup(staged)
    remote_keys, remote_ids = source_namespaces(reader, config)
    if not records:
        raise ValueError("Backup contains no source records")
    catalog = Catalog(staged)
    catalog.db.execute("PRAGMA trusted_schema=OFF")
    complete = incomplete = 0
    try:
        for old in records:
            source_id = old["source_id"]
            protect_source_path(output, old)
            if old.get("destination") != {"bucket": config.bucket, "prefix": config.prefix}:
                raise ValueError(f"{source_id}: backup destination differs from configured archive")
            claim = reservation(reader, config, source_id)
            if claim and claim["token"] != old.get("token"):
                raise ValueError(f"{source_id}: backup token differs from source reservation")
            # Only missing manifests permit an incomplete checkpoint; missing images
            # or other verification errors must fail recovery, not be hidden.
            try:
                read_metadata(reader, config.key(f"sources/{source_id}/manifest.json"))
            except ObjectNotFound:
                if old["state"] == "COMPLETE":
                    raise ValueError(
                        f"{source_id}: completed backup source has no archived manifest"
                    ) from None
                record = {
                    **old,
                    "state": "INTERRUPTED",
                    "work_dir": str(config.work_dir),
                    "recovery": {"kind": "checkpoint", "time": datetime.now(UTC).isoformat()},
                }
                record.pop("inventory_origin", None)
                # Rehash inventory rather than trusting interrupted cached hashes.
                entries = []
                incomplete += 1
                announce(
                    f"{source_id}: INTERRUPTED checkpoint restored; resume with the original source"
                )
            else:
                verified = verify_source(reader, config, source_id, anchor=old, announce=announce)
                record = complete_record(config, verified, claim, old)
                protect_source_path(output, record)
                entries = verified.manifest["files"]
                complete += 1
            with catalog.db:
                catalog.db.execute(
                    "UPDATE sources SET state=?,record=? WHERE source_id=?",
                    (record["state"], json.dumps(record), source_id),
                )
            catalog.save_files(source_id, entries)
        old_ids = {record["source_id"] for record in records}
        for source_id in sorted(remote_ids - old_ids, key=source_number):
            record, entries, verified = namespace_record(
                reader, config, source_id, remote_keys, output, announce
            )
            insert_source(catalog, record, entries)
            complete += int(verified)
            incomplete += int(not verified)
        if remote_keys != set(reader.keys(config.key("sources/"))):
            raise ValueError("Archive changed during recovery; stop ingestion and retry")
        with catalog.db:
            catalog.db.execute(
                "UPDATE ingestion_runs SET state='INTERRUPTED', ended_at=? WHERE ended_at IS NULL",
                (datetime.now(UTC).isoformat(),),
            )
    finally:
        catalog.close()
    return complete, incomplete


def recover_catalog(
    reader: ArchiveReader,
    config: Config,
    output: Path,
    backup_key: str | None = None,
    announce=print,
) -> tuple[int, int]:
    output = output.expanduser().absolute()
    # Check before resolving: a dangling symlink is still an existing target.
    ensure_output_new(output)
    output = output.parent.resolve() / output.name
    ensure_output_new(output)
    if output == config.database:
        raise ValueError("Choose a recovery output separate from the configured working catalog")
    with tempfile.TemporaryDirectory(prefix="family-archive-recovery-") as scratch:
        staged = Path(scratch) / "archive.db"
        if backup_key:
            result = _from_backup(reader, config, staged, output, backup_key, announce)
        else:
            result = _from_manifests(reader, config, staged, output, announce)
        # Atomic publication without overwriting a target created during recovery.
        ensure_output_new(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix=".family-archive-publish-", dir=output.parent
        ) as publish:
            ready = Path(publish) / "archive.db"
            shutil.copyfile(staged, ready)
            os.chmod(ready, 0o600)
            with ready.open("rb") as stream:
                os.fsync(stream.fileno())
            if hash_file(ready) != hash_file(staged):
                raise ValueError("Recovered database copy failed SHA-256 read-back")
            ensure_output_new(output)
            os.link(ready, output)
    announce(f"Recovered catalog: {output}; {result[0]} verified complete, {result[1]} incomplete")
    announce("The configured working catalog was not replaced. No archive objects were changed.")
    return result

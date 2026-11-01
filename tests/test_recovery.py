import hashlib
import io
import json
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from uuid import UUID

import pytest

from family_archive import cli
from family_archive.catalog import Catalog
from family_archive.config import Config
from family_archive.manifest import serialize
from family_archive.media import Inspection
from family_archive.recovery import recover_catalog
from family_archive.storage import ObjectNotFound
from family_archive.verification import local_anchor, verify_source


class Reader:
    def __init__(self, objects):
        self.objects = objects
        self.opened = []
        self.closed = []

    @contextmanager
    def open_read(self, key):
        if key not in self.objects:
            raise ObjectNotFound(f"Missing object: {key}")
        stream = io.BytesIO(self.objects[key])
        self.opened.append(key)
        try:
            yield stream
        finally:
            stream.close()
            self.closed.append(key)

    def keys(self, prefix):
        yield from sorted(key for key in self.objects if key.startswith(prefix))


@pytest.fixture
def archive(tmp_path):
    config = Config(
        "archive-test",
        prefix="family",
        database=tmp_path / "working.db",
        work_dir=tmp_path / "work",
        log_dir=tmp_path / "logs",
    )
    raw = b"x" * 4096
    report = Inspection(
        "/dev/disk5",
        "DVD+R",
        "FAMILY",
        str(tmp_path / "disc"),
        "udf",
        2048,
        1,
        "DATA DVD",
        "data",
        sessions=1,
        inspection_supported=True,
        image_size=4096,
    )
    file_record = {
        "kind": "file",
        "path": "Photos/a.jpg",
        "filename": "a.jpg",
        "size": 5,
        "sha256": hashlib.sha256(b"photo").hexdigest(),
        "mtime_ns": 1,
        "mode": 0o444,
        "mime_type": "image/jpeg",
    }
    second = {**file_record, "path": "Photos/copy.jpg", "filename": "copy.jpg"}
    manifest = {
        "schema_version": 1,
        "source_id": "SRC-0001",
        "source_type": "data_dvd",
        "archive_status": "COMPLETE",
        "started_at": "2026-01-01T00:00:00+00:00",
        "completed_at": "2026-01-01T01:00:00+00:00",
        "inspection": asdict(report),
        "notes": "fixture",
        "destination": {"bucket": config.bucket, "prefix": config.prefix},
        "preservation": {
            "format": "raw_logical_sector_image",
            "sector_size": 2048,
            "key": config.key("sources/SRC-0001/image/SRC-0001.img"),
            "size": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        },
        "file_count": 2,
        "directory_count": 1,
        "logical_file_size": 10,
        "files": [
            {
                "kind": "directory",
                "path": "Photos",
                "filename": "Photos",
                "mode": 0o555,
                "mtime_ns": 1,
            },
            file_record,
            second,
        ],
    }
    claim = {"schema_version": 1, "source_id": "SRC-0001", "token": str(UUID(int=1))}
    reader = Reader(
        {
            config.key("sources/SRC-0001/manifest.json"): serialize(manifest),
            manifest["preservation"]["key"]: raw,
            config.key("sources/SRC-0001/source.json"): json.dumps(claim).encode(),
        }
    )
    return config, reader, manifest, claim


def checkpoint(archive, tmp_path):
    config, reader, manifest, claim = archive
    catalog = Catalog(config.database)
    try:
        record = catalog.create(
            {
                "inspection": manifest["inspection"],
                "destination": manifest["destination"],
                "started_at": manifest["started_at"],
                "notes": "fixture",
                "work_dir": str(config.work_dir),
            }
        )
        record.update(state="INGESTING", token=claim["token"])
        catalog.save(record)
        catalog.start_run(record["source_id"], manifest["started_at"])
        record.update(
            state="VERIFYING",
            image_sha256=manifest["preservation"]["sha256"],
            image_size=4096,
            manifest=manifest,
            file_count=2,
            upload={"key": manifest["preservation"]["key"], "upload_id": "retained"},
        )
        catalog.save(record)
        catalog.save_files("SRC-0001", manifest["files"])
        backup = tmp_path / "backup.db"
        catalog.export(backup, tmp_path / "backup.csv")
    finally:
        catalog.close()
    key = config.key(f"catalog/backups/SRC-0001/{UUID(int=2)}/archive.db")
    reader.objects[key] = backup.read_bytes()
    return key


def test_verify_without_local_catalog(archive):
    config, reader, manifest, _ = archive
    before = deepcopy(reader.objects)
    messages = []
    result = verify_source(reader, config, "SRC-0001", announce=messages.append)
    assert result.image_size == 4096
    assert result.image_sha256 == manifest["preservation"]["sha256"]
    assert not result.anchored
    assert "no independent local hash anchor" in "\n".join(messages)
    assert not config.database.exists()
    assert reader.objects == before
    assert reader.opened == reader.closed


@pytest.mark.parametrize("bad", [b"x" * 4095, b"x" * 4097, b"y" * 4096])
def test_corrupt_image_fails_and_stream_is_closed(archive, bad):
    config, reader, manifest, _ = archive
    reader.objects[manifest["preservation"]["key"]] = bad
    with pytest.raises(ValueError, match="size|FAILED"):
        verify_source(reader, config, "SRC-0001", announce=lambda message: None)
    assert reader.opened == reader.closed


def test_files_only_manifest_verifies_each_archived_file(archive):
    config, _reader, original, _claim = archive
    content = b"original media file"
    key = config.key("sources/SRC-0001/data/Photos/a.jpg")
    entry = {
        "kind": "file",
        "path": "Photos/a.jpg",
        "filename": "a.jpg",
        "size": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
        "mtime_ns": 1,
        "mode": 0o444,
        "mime_type": "image/jpeg",
        "object_key": key,
    }
    manifest = {
        "schema_version": 3,
        "source_id": "SRC-0001",
        "source_type": "data_dvd",
        "archive_status": "COMPLETE",
        "started_at": original["started_at"],
        "completed_at": original["completed_at"],
        "notes": "files-only test",
        "inspection": original["inspection"],
        "destination": original["destination"],
        "preservation": {
            "format": "mounted_files",
            "full_disc_image": False,
            "object_prefix": config.key("sources/SRC-0001/data"),
            "scope": "Visible mounted files only.",
        },
        "inventory_origin": "source_mount",
        "file_count": 1,
        "directory_count": 0,
        "logical_file_size": len(content),
        "files": [entry],
    }
    reader = Reader(
        {
            config.key("sources/SRC-0001/manifest.json"): serialize(manifest),
            key: content,
        }
    )
    verified = verify_source(reader, config, "SRC-0001", announce=lambda _message: None)
    assert verified.image_size == 0
    assert verified.image_sha256 == ""
    assert reader.opened == [config.key("sources/SRC-0001/manifest.json"), key]

    reader.objects[key] = b"tampered media file"
    with pytest.raises(ValueError, match="archived file verification failed"):
        verify_source(reader, config, "SRC-0001", announce=lambda _message: None)


def test_recover_files_only_source_from_manifest(archive, tmp_path):
    config, _reader, original, _claim = archive
    content = b"original media file"
    key = config.key("sources/SRC-0001/data/Photos/a.jpg")
    entry = {
        "kind": "file",
        "path": "Photos/a.jpg",
        "filename": "a.jpg",
        "size": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
        "mtime_ns": 1,
        "mode": 0o444,
        "mime_type": "image/jpeg",
        "object_key": key,
    }
    manifest = {
        "schema_version": 3,
        "source_id": "SRC-0001",
        "source_type": "data_dvd",
        "archive_status": "COMPLETE",
        "started_at": original["started_at"],
        "completed_at": original["completed_at"],
        "notes": "files-only recovery test",
        "inspection": original["inspection"],
        "destination": original["destination"],
        "preservation": {
            "format": "mounted_files",
            "full_disc_image": False,
            "object_prefix": config.key("sources/SRC-0001/data"),
            "scope": "Visible mounted files only.",
        },
        "inventory_origin": "source_mount",
        "file_count": 1,
        "directory_count": 0,
        "logical_file_size": len(content),
        "files": [entry],
    }
    reader = Reader(
        {
            config.key("sources/SRC-0001/manifest.json"): serialize(manifest),
            key: content,
        }
    )
    output = tmp_path / "files-only-recovered.db"
    assert recover_catalog(reader, config, output, announce=lambda _message: None) == (1, 0)
    catalog = Catalog(output)
    try:
        record = catalog.get("SRC-0001")
        assert record["state"] == "COMPLETE"
        assert record["archive_mode"] == "files_only"
        assert catalog.files("SRC-0001")[0]["object_key"] == key
    finally:
        catalog.close()


@pytest.mark.parametrize(
    "change",
    [
        {"source_id": "SRC-0002"},
        {"schema_version": 99},
        {"destination": {"bucket": "other-bucket", "prefix": "family"}},
    ],
)
def test_bad_manifest_rejected_before_image_read(archive, change):
    config, reader, manifest, _ = archive
    manifest.update(change)
    reader.objects[config.key("sources/SRC-0001/manifest.json")] = json.dumps(manifest).encode()
    with pytest.raises(ValueError):
        verify_source(reader, config, "SRC-0001", announce=lambda message: None)
    assert len(reader.opened) == 1


def test_manifest_cannot_redirect_image_key(archive):
    config, reader, manifest, _ = archive
    manifest["preservation"]["key"] = config.key("sources/SRC-0002/image/private.img")
    reader.objects[config.key("sources/SRC-0001/manifest.json")] = serialize(manifest)
    with pytest.raises(ValueError, match="namespace"):
        verify_source(reader, config, "SRC-0001")
    assert len(reader.opened) == 1


def test_local_anchor_is_read_only_and_detects_changed_manifest(archive, tmp_path):
    config, reader, manifest, _ = archive
    checkpoint(archive, tmp_path)
    before = config.database.read_bytes()
    anchor = local_anchor(config, "SRC-0001")
    assert verify_source(reader, config, "SRC-0001", anchor, announce=lambda message: None).anchored
    manifest["notes"] = "changed"
    reader.objects[config.key("sources/SRC-0001/manifest.json")] = serialize(manifest)
    with pytest.raises(ValueError, match="differs from local"):
        verify_source(reader, config, "SRC-0001", anchor)
    assert config.database.read_bytes() == before


def test_rebuild_retains_files_hashes_provenance_and_highest_id(archive, tmp_path):
    config, reader, manifest, claim = archive
    claim2 = {**claim, "source_id": "SRC-0042"}
    reader.objects[config.key("sources/SRC-0042/source.json")] = json.dumps(claim2).encode()
    before = deepcopy(reader.objects)
    output = tmp_path / "recovered.db"
    assert recover_catalog(reader, config, output, announce=lambda message: None) == (1, 1)
    catalog = Catalog(output)
    try:
        restored = catalog.get("SRC-0001")
        assert restored["state"] == "COMPLETE"
        assert restored["manifest"] == manifest
        assert restored["token"] == claim["token"]
        assert len(catalog.files("SRC-0001")) == 3
        assert catalog.files("SRC-0001")[1]["sha256"] == catalog.files("SRC-0001")[2]["sha256"]
        assert catalog.get("SRC-0042")["state"] == "NEEDS_REVIEW"
        assert catalog.create({})["source_id"] == "SRC-0043"
    finally:
        catalog.close()
    assert reader.objects == before
    assert not config.database.exists()


def test_missing_claim_does_not_prevent_completed_manifest_reconstruction(archive, tmp_path):
    config, reader, _, _ = archive
    del reader.objects[config.key("sources/SRC-0001/source.json")]
    assert recover_catalog(
        reader, config, tmp_path / "recovered.db", announce=lambda message: None
    ) == (1, 0)


@pytest.mark.parametrize("target", ["existing", "symlink", "journal"])
def test_recovery_never_overwrites_existing_targets(archive, tmp_path, target):
    config, reader, _, _ = archive
    output = tmp_path / "existing.db"
    if target == "existing":
        output.write_bytes(b"keep")
    elif target == "symlink":
        output.symlink_to(tmp_path / "absent")
    else:
        Path(str(output) + "-wal").write_bytes(b"keep")
    with pytest.raises(ValueError, match="overwrite"):
        recover_catalog(reader, config, output)
    assert reader.opened == []


def test_failed_recovery_publishes_no_database(archive, tmp_path):
    config, reader, manifest, _ = archive
    del reader.objects[manifest["preservation"]["key"]]
    output = tmp_path / "recovered.db"
    with pytest.raises(ObjectNotFound):
        recover_catalog(reader, config, output, announce=lambda message: None)
    assert not output.exists()


def test_recovery_checks_source_path_before_writing_there(archive, tmp_path):
    config, reader, _, _ = archive
    output = tmp_path / "disc" / "forbidden.db"
    with pytest.raises(ValueError, match="source media"):
        recover_catalog(reader, config, output, announce=lambda message: None)
    assert not output.parent.exists()


def test_restore_backup_reverifies_images_and_preserves_working_catalog(archive, tmp_path):
    config, reader, _, _ = archive
    key = checkpoint(archive, tmp_path)
    before = config.database.read_bytes()
    output = tmp_path / "restored.db"
    assert recover_catalog(reader, config, output, key, announce=lambda message: None) == (1, 0)
    catalog = Catalog(output)
    try:
        assert catalog.get("SRC-0001")["state"] == "COMPLETE"
        assert catalog.get("SRC-0001")["upload"]["upload_id"] == "retained"
        assert catalog.db.execute("SELECT state FROM ingestion_runs").fetchone()[0] == "INTERRUPTED"
    finally:
        catalog.close()
    assert config.database.read_bytes() == before


def test_restore_incomplete_checkpoint_keeps_upload_and_clears_cached_hashes(archive, tmp_path):
    config, reader, manifest, _ = archive
    key = checkpoint(archive, tmp_path)
    del reader.objects[config.key("sources/SRC-0001/manifest.json")]
    del reader.objects[manifest["preservation"]["key"]]
    output = tmp_path / "restored.db"
    assert recover_catalog(reader, config, output, key, announce=lambda message: None) == (0, 1)
    catalog = Catalog(output)
    try:
        record = catalog.get("SRC-0001")
        assert record["state"] == "INTERRUPTED"
        assert record["upload"]["upload_id"] == "retained"
        assert catalog.files("SRC-0001") == []
    finally:
        catalog.close()


def test_old_backup_does_not_reuse_newer_reserved_ids(archive, tmp_path):
    config, reader, _, claim = archive
    key = checkpoint(archive, tmp_path)
    reader.objects[config.key("sources/SRC-0070/source.json")] = json.dumps(
        {**claim, "source_id": "SRC-0070"}
    ).encode()
    output = tmp_path / "restored.db"
    assert recover_catalog(reader, config, output, key, announce=lambda message: None) == (1, 1)
    catalog = Catalog(output)
    try:
        assert catalog.create({})["source_id"] == "SRC-0071"
    finally:
        catalog.close()


def test_corrupt_backup_is_not_published(archive, tmp_path):
    config, reader, _, _ = archive
    key = checkpoint(archive, tmp_path)
    reader.objects[key] = b"not sqlite"
    output = tmp_path / "bad.db"
    with pytest.raises(Exception):
        recover_catalog(reader, config, output, key, announce=lambda message: None)
    assert not output.exists()


def test_cli_verify_uses_no_disc_or_catalog_writes(archive, monkeypatch, capsys):
    from family_archive.storage import s3

    config, reader, _, _ = archive
    monkeypatch.setattr(s3, "connect", lambda *args: reader)
    assert (
        cli.main(
            [
                "verify",
                "SRC-0001",
                "--bucket",
                config.bucket,
                "--prefix",
                config.prefix,
                "--database",
                str(config.database),
            ]
        )
        == 0
    )
    assert "VERIFIED" in capsys.readouterr().out
    assert not config.database.exists()


def test_archive_changing_during_recovery_publishes_nothing(archive, tmp_path, monkeypatch):
    config, reader, _, _ = archive
    original = reader.keys
    calls = 0

    def changing(prefix):
        nonlocal calls
        calls += 1
        yield from original(prefix)
        if calls == 2:
            yield config.key("sources/SRC-0009/source.json")

    monkeypatch.setattr(reader, "keys", changing)
    with pytest.raises(ValueError, match="changed during recovery"):
        recover_catalog(reader, config, tmp_path / "out.db", announce=lambda message: None)
    assert not (tmp_path / "out.db").exists()


def test_interrupted_verification_closes_stream(archive, monkeypatch):
    config, reader, manifest, _ = archive
    original = reader.open_read

    @contextmanager
    def interrupted(key):
        if key == manifest["preservation"]["key"]:

            class Interrupted(io.BytesIO):
                def read(self, size=-1):
                    raise KeyboardInterrupt()

            with Interrupted(b"") as stream:
                try:
                    yield stream
                finally:
                    reader.closed.append(key)
        else:
            with original(key) as stream:
                yield stream

    monkeypatch.setattr(reader, "open_read", interrupted)
    with pytest.raises(KeyboardInterrupt):
        verify_source(reader, config, "SRC-0001", announce=lambda message: None)
    assert manifest["preservation"]["key"] in reader.closed
    assert not config.database.exists()


def test_backup_reservation_mismatch_is_refused(archive, tmp_path):
    config, reader, _, claim = archive
    key = checkpoint(archive, tmp_path)
    reader.objects[config.key("sources/SRC-0001/source.json")] = json.dumps(
        {**claim, "token": str(UUID(int=9))}
    ).encode()
    with pytest.raises(ValueError, match="token differs"):
        recover_catalog(reader, config, tmp_path / "out.db", key, announce=lambda message: None)
    assert not (tmp_path / "out.db").exists()


def test_missing_image_is_not_treated_as_incomplete_backup(archive, tmp_path):
    config, reader, manifest, _ = archive
    key = checkpoint(archive, tmp_path)
    del reader.objects[manifest["preservation"]["key"]]
    with pytest.raises(ObjectNotFound):
        recover_catalog(reader, config, tmp_path / "out.db", key, announce=lambda message: None)
    assert not (tmp_path / "out.db").exists()


def test_metadata_limit_rejects_oversize(archive, monkeypatch):
    from family_archive import verification

    config, reader, _, _ = archive
    monkeypatch.setattr(verification, "METADATA_LIMIT", 16)
    with pytest.raises(ValueError, match="metadata exceeds"):
        verify_source(reader, config, "SRC-0001")
    assert reader.opened == reader.closed


def test_restore_rejects_altered_schema(archive, tmp_path):
    import sqlite3

    config, reader, _, _ = archive
    key = checkpoint(archive, tmp_path)
    path = tmp_path / "altered.db"
    path.write_bytes(reader.objects[key])
    with sqlite3.connect(path) as db:
        db.execute("CREATE INDEX extra_index ON sources(state)")
    reader.objects[key] = path.read_bytes()
    with pytest.raises(ValueError, match="schema"):
        recover_catalog(reader, config, tmp_path / "out.db", key, announce=lambda message: None)
    assert not (tmp_path / "out.db").exists()


def test_cli_recover_and_list_backups(archive, tmp_path, monkeypatch, capsys):
    from family_archive.storage import s3

    config, reader, _, _ = archive
    monkeypatch.setattr(s3, "connect", lambda *args: reader)
    output = tmp_path / "new.db"
    common = [
        "--bucket",
        config.bucket,
        "--prefix",
        config.prefix,
        "--database",
        str(config.database),
    ]
    assert cli.main(["recover", "--output", str(output), *common]) == 0
    assert output.exists()
    key = checkpoint(archive, tmp_path)
    assert cli.main(["recover", "--list-backups", *common]) == 0
    assert key in capsys.readouterr().out
    assert cli.main(["recover", "--output", str(output), *common]) == 1
    assert "overwrite" in capsys.readouterr().err


def test_incomplete_namespace_cannot_be_resumed_without_checkpoint(archive, tmp_path):
    from dataclasses import replace

    from family_archive.ingest import ingest

    config, reader, manifest, _ = archive
    del reader.objects[config.key("sources/SRC-0001/manifest.json")]
    output = tmp_path / "new.db"
    recover_catalog(reader, config, output, announce=lambda message: None)
    report = Inspection(**manifest["inspection"])
    with pytest.raises(ValueError, match="restore a checkpoint"):
        ingest(
            report,
            replace(config, database=output),
            None,
            resume="SRC-0001",
            announce=lambda message: None,
        )


def test_target_created_during_recovery_is_not_overwritten(archive, tmp_path, monkeypatch):
    config, reader, _, _ = archive
    output = tmp_path / "race.db"
    original = reader.keys
    calls = 0

    def concurrent_target(prefix):
        nonlocal calls
        calls += 1
        yield from original(prefix)
        if calls == 2:
            output.write_bytes(b"created by another process")

    monkeypatch.setattr(reader, "keys", concurrent_target)
    with pytest.raises(ValueError, match="overwrite"):
        recover_catalog(reader, config, output, announce=lambda message: None)
    assert output.read_bytes() == b"created by another process"


def test_short_reads_do_not_truncate_metadata_or_images(archive, monkeypatch):
    config, reader, _, _ = archive

    @contextmanager
    def partial_read(key):
        class ShortRead(io.BytesIO):
            def read(self, size=-1):
                return super().read(min(31, size))

        with ShortRead(reader.objects[key]) as stream:
            yield stream

    monkeypatch.setattr(reader, "open_read", partial_read)
    assert (
        verify_source(reader, config, "SRC-0001", announce=lambda message: None).image_size == 4096
    )


def test_cli_verify_failure_does_not_change_complete_state(archive, tmp_path, monkeypatch, capsys):
    from family_archive.storage import s3

    config, reader, manifest, _ = archive
    checkpoint(archive, tmp_path)
    catalog = Catalog(config.database)
    record = catalog.get("SRC-0001")
    record["state"] = "COMPLETE"
    catalog.save(record)
    catalog.close()
    before = config.database.read_bytes()
    reader.objects[manifest["preservation"]["key"]] = b"corrupt"
    monkeypatch.setattr(s3, "connect", lambda *args: reader)
    assert (
        cli.main(
            [
                "verify",
                "SRC-0001",
                "--bucket",
                config.bucket,
                "--prefix",
                config.prefix,
                "--database",
                str(config.database),
            ]
        )
        == 1
    )
    assert "FAILED" in capsys.readouterr().err
    assert config.database.read_bytes() == before


def test_recovery_checks_local_database_copy_before_publication(archive, tmp_path, monkeypatch):
    from family_archive import recovery

    config, reader, _, _ = archive

    def corrupt_copy(source, target):
        Path(target).write_bytes(b"bad local copy")

    monkeypatch.setattr(recovery.shutil, "copyfile", corrupt_copy)
    output = tmp_path / "out.db"
    with pytest.raises(ValueError, match="read-back"):
        recover_catalog(reader, config, output, announce=lambda message: None)
    assert not output.exists()

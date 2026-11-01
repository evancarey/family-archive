import hashlib
import io
import json
import sqlite3
from contextlib import contextmanager, nullcontext
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from family_archive import ingest as pipeline
from family_archive.catalog import Catalog, catalog_lock
from family_archive.config import Config, load_config
from family_archive.imaging import capture
from family_archive.manifest import deserialize, inventory, serialize
from family_archive.media import Inspection
from family_archive.storage import StorageError


class MemoryStorage:
    def __init__(self):
        self.objects = {}
        self.fail_at = None
        self.corrupt_image = False
        self.writes = []

    def ensure_bytes(self, key, data):
        if self.fail_at and self.fail_at in key:
            raise OSError("simulated network failure")
        if key in self.objects:
            if self.objects[key] != data:
                raise StorageError(f"Conflict: {key}")
        else:
            self.objects[key] = data
            self.writes.append(key)

    def ensure_file(self, key, path, sha256, checkpoint, save):
        assert hashlib.sha256(path.read_bytes()).hexdigest() == sha256
        self.ensure_bytes(key, path.read_bytes())
        if self.corrupt_image and key.endswith(".img"):
            raise StorageError("simulated verification failure")


@pytest.fixture
def setup(tmp_path, monkeypatch):
    mount = tmp_path / "disc"
    mount.mkdir()
    (mount / "Photos").mkdir()
    (mount / "Photos" / "a.jpg").write_bytes(b"family photo")
    (mount / "duplicate.jpg").write_bytes(b"family photo")
    (mount / "empty").mkdir()
    report = Inspection(
        "/dev/disk5",
        "DVD+R",
        "FAMILY",
        str(mount),
        "udf",
        2048,
        1,
        "DATA DVD",
        "data",
        sessions=1,
        inspection_supported=True,
        image_size=4096,
    )
    config = Config(
        "archive-test",
        database=tmp_path / "archive.db",
        work_dir=tmp_path / "work",
        log_dir=tmp_path / "logs",
    )

    @contextmanager
    def fake_mount(image, filesystem):
        assert image.read_bytes() == b"a" * 4096
        yield mount

    monkeypatch.setattr(pipeline, "mount_image", fake_mount)
    storage = MemoryStorage()
    raw = b"a" * 4096
    return report, config, storage, raw


def run(setup, resume=None, opener=None):
    report, config, storage, raw = setup
    return pipeline.ingest(
        report,
        config,
        storage,
        resume,
        announce=lambda message: None,
        device_opener=opener or (lambda device: io.BytesIO(raw)),
    )


def get_record(config):
    catalog = Catalog(config.database)
    try:
        return catalog.get("SRC-0001")
    finally:
        catalog.close()


def test_complete_archive(setup):
    report, config, storage, raw = setup
    assert run(setup) == "SRC-0001"
    record = get_record(config)
    assert record["state"] == "COMPLETE"
    manifest = deserialize(storage.objects["sources/SRC-0001/manifest.json"])
    assert manifest["inspection"]["volume"] == "FAMILY"
    assert manifest["preservation"]["size"] == 4096  # not filesystem size 2048
    assert storage.objects[manifest["preservation"]["key"]] == raw
    files = [entry for entry in manifest["files"] if entry["kind"] == "file"]
    assert len(files) == 2
    assert files[0]["sha256"] == files[1]["sha256"]
    assert manifest["directory_count"] == 2
    assert manifest["logical_file_size"] == len(b"family photo") * 2
    assert any(key.endswith("/archive.db") for key in storage.objects)
    assert any(key.endswith("/archive.csv") for key in storage.objects)
    with sqlite3.connect(config.database) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM files WHERE sha256 IS NOT NULL").fetchone()[0] == 2
        assert db.execute("SELECT state FROM ingestion_runs").fetchone()[0] == "COMPLETE"
    assert (config.log_dir / "SRC-0001.log").is_file()
    assert (config.work_dir / "SRC-0001" / "SRC-0001.img").read_bytes() == raw
    before = dict(storage.objects)
    run(setup, "SRC-0001")
    assert storage.objects == before


def test_archive_image_size_can_exclude_recorded_unreadable_track_tail(setup, monkeypatch):
    report, config, storage, _ = setup

    @contextmanager
    def fake_mount(image, filesystem):
        assert image.read_bytes() == b"a" * 2048
        yield Path(report.mount_point)

    monkeypatch.setattr(pipeline, "mount_image", fake_mount)
    trimmed = replace(report, archive_image_size=2048)
    pipeline.ingest(
        trimmed,
        config,
        storage,
        device_opener=lambda device: io.BytesIO(b"a" * 2048),
        announce=lambda message: None,
    )
    record = get_record(config)
    assert record["state"] == "COMPLETE"
    assert record["image_size"] == 2048


@pytest.mark.parametrize("failure", [".img", "manifest.json", "archive.db", "archive.csv"])
def test_network_failures_resume(setup, failure):
    _, config, storage, _ = setup
    storage.fail_at = failure
    with pytest.raises(pipeline.IngestionError, match="SRC-0001"):
        run(setup)
    assert get_record(config)["state"] != "COMPLETE"
    written = set(storage.writes)
    storage.fail_at = None
    run(setup, "SRC-0001")
    assert get_record(config)["state"] == "COMPLETE"
    assert len(storage.writes) == len(set(storage.writes))
    assert written <= set(storage.writes)


def test_verification_failure_never_completes(setup):
    _, config, storage, _ = setup
    storage.corrupt_image = True
    with pytest.raises(pipeline.IngestionError, match="verification failure"):
        run(setup)
    assert get_record(config)["state"] == "FAILED"
    assert "sources/SRC-0001/manifest.json" not in storage.objects
    storage.corrupt_image = False
    run(setup, "SRC-0001")
    assert get_record(config)["state"] == "COMPLETE"


def test_interruption_and_partial_capture(setup, monkeypatch):
    _, config, storage, raw = setup
    monkeypatch.setattr("family_archive.imaging.CHUNK_SIZE", 2048)

    class Interrupted(io.BytesIO):
        def read(self, size=-1):
            if self.tell() == 2048:
                raise KeyboardInterrupt()
            return super().read(size)

    with pytest.raises(KeyboardInterrupt):
        run(setup, opener=lambda device: Interrupted(raw))
    assert get_record(config)["state"] == "INTERRUPTED"
    image = config.work_dir / "SRC-0001" / "SRC-0001.img"
    assert image.stat().st_size == 2048
    run(setup, "SRC-0001")
    assert image.read_bytes() == raw
    assert get_record(config)["state"] == "COMPLETE"


def test_wrong_disc_resume_refused(setup):
    _, config, storage, _ = setup
    storage.fail_at = ".img"
    with pytest.raises(pipeline.IngestionError):
        run(setup)
    before = dict(storage.objects)
    storage.fail_at = None
    with pytest.raises(pipeline.IngestionError, match="differs from cached image"):
        run(setup, "SRC-0001", lambda device: io.BytesIO(b"b" * 4096))
    assert storage.objects == before
    assert get_record(config)["state"] == "NEEDS_REVIEW"


def test_source_read_error(setup):
    _, config, storage, _ = setup
    with pytest.raises(pipeline.IngestionError, match="truncated source"):
        run(setup, opener=lambda device: io.BytesIO(b"a" * 2048))
    assert get_record(config)["state"] != "COMPLETE"
    assert not any(key.endswith(".img") for key in storage.objects)


def test_destination_change_rejected(setup):
    report, config, storage, raw = setup
    storage.fail_at = ".img"
    with pytest.raises(pipeline.IngestionError):
        run(setup)
    with pytest.raises(ValueError, match="destination differs"):
        pipeline.ingest(report, replace(config, bucket="other-bucket"), storage, "SRC-0001")


def test_no_writes_on_source(setup):
    report, config, storage, _ = setup
    for key in ("database", "work_dir", "log_dir"):
        bad = replace(config, **{key: __import__("pathlib").Path(report.mount_point) / "archive"})
        with pytest.raises(ValueError, match="source media"):
            pipeline.ingest(report, bad, storage)
    assert not storage.objects


def test_unknown_and_unsupported_ingestion(setup, monkeypatch):
    report, config, storage, _ = setup
    for classification in ("NEEDS_REVIEW", "LIKELY VIDEO DVD", "LIKELY AUDIO DVD"):
        unsupported = replace(report, classification=classification)
        with pytest.raises(ValueError):
            pipeline.ingest(unsupported, config, storage)
        monkeypatch.setattr(pipeline, "inspect_media", lambda: [unsupported])
        with pytest.raises(ValueError, match="unsupported"):
            pipeline.select_source(report.device)
    monkeypatch.setattr(pipeline, "inspect_media", lambda: [replace(report, image_size=None)])
    with pytest.raises(ValueError, match="raw media size"):
        pipeline.select_source(report.device)


def test_files_only_refuses_unmounted_volume_before_reserving_source(setup, monkeypatch, tmp_path):
    report, config, storage, _ = setup
    unmounted = replace(report, mount_point=str(tmp_path / "not-mounted"))
    monkeypatch.setattr(pipeline, "inspect_media", lambda: [unmounted])
    with pytest.raises(ValueError, match="mounted volume is unavailable"):
        pipeline.select_source(report.device, files_only=True)
    with pytest.raises(ValueError, match="mounted volume is unavailable"):
        pipeline.ingest(unmounted, config, storage, files_only=True)
    assert not storage.objects


def test_files_only_can_use_verified_mode1_volume_when_raw_toc_is_unavailable(
    setup, monkeypatch
):
    report, _config, _storage, _ = setup
    cd_review = replace(
        report,
        media="CD-RW",
        filesystem="cd9660",
        classification="NEEDS_REVIEW",
        reason=(
            "Unable to query CD session layout: drutil toc session numbering is "
            "incomplete or ambiguous"
        ),
        tracks=1,
        sessions=1,
        inspection_supported=False,
        hardware={
            "OpticalMediaNumAudioTracks": 0,
            "VolumeContent": "CD_ROM_Mode_1",
        },
    )
    monkeypatch.setattr(pipeline, "inspect_media", lambda: [cd_review])

    selected = pipeline.select_source(report.device, files_only=True)

    assert selected.classification == "DATA CD"
    assert selected.inspection_supported
    assert "files-only scope" in selected.reason
    with pytest.raises(ValueError, match="unsupported for ingestion"):
        pipeline.select_source(report.device)


def test_files_only_toc_fallback_rejects_audio_or_mode2_cd(setup, monkeypatch):
    report, _config, _storage, _ = setup
    cd_review = replace(
        report,
        media="CD-RW",
        filesystem="cd9660",
        classification="NEEDS_REVIEW",
        reason="Unable to query CD session layout: drutil toc unavailable",
        tracks=1,
        sessions=1,
        inspection_supported=False,
        hardware={
            "OpticalMediaNumAudioTracks": 1,
            "VolumeContent": "CD_ROM_Mode_2_Form_1",
        },
    )
    monkeypatch.setattr(pipeline, "inspect_media", lambda: [cd_review])
    with pytest.raises(ValueError, match="unsupported for ingestion"):
        pipeline.select_source(report.device, files_only=True)


def test_files_only_resume_can_confirm_saved_cd_without_diskmanagement(setup, monkeypatch):
    report, _config, _storage, _ = setup
    saved = asdict(
        replace(
            report,
            device="/dev/disk5",
            media="CD-R",
            volume="FAMILY",
            mount_point="/Volumes/FAMILY",
            filesystem="cd9660",
            classification="DATA CD",
            volume_device="/dev/disk5s0",
            hardware={"WritableMedia": False},
            session_layout=[{"session": 1, "track": 1, "start_lba": 0, "sectors": 350}],
        )
    )
    original_is_dir = Path.is_dir
    monkeypatch.setattr(
        Path,
        "is_dir",
        lambda path: str(path) == "/Volumes/FAMILY" or original_is_dir(path),
    )
    monkeypatch.setattr(
        pipeline.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout=("/dev/disk5s0 on /Volumes/FAMILY (cd9660, local, read-only, noowners)\n"),
        ),
    )
    selected = pipeline.select_files_only_resume("/dev/disk5", saved)
    assert selected.device == "/dev/disk5"
    assert selected.mount_point == "/Volumes/FAMILY"
    assert selected.classification == "DATA CD"


def test_raw_inspection_open_retries_transient_missing_node(monkeypatch):
    attempts = []

    def opener(device):
        attempts.append(device)
        if len(attempts) < 3:
            raise FileNotFoundError(device)
        return nullcontext("raw device")

    monkeypatch.setattr(pipeline, "open_device", opener)
    monkeypatch.setattr(pipeline.time, "sleep", lambda _: None)

    with pipeline._open_inspection_device("/dev/disk5") as result:
        assert result == "raw device"
    assert attempts == ["/dev/disk5"] * 3


def test_source_id_migration_and_lock(tmp_path):
    path = tmp_path / "archive.db"
    catalog = Catalog(path)
    assert catalog.create({})["source_id"] == "SRC-0001"
    assert catalog.create({})["source_id"] == "SRC-0002"
    with pytest.raises(ValueError, match="Invalid source ID"):
        catalog.get("../bad")
    with pytest.raises(ValueError, match="Invalid source state"):
        catalog.save({"source_id": "SRC-0001", "state": "SKIP"})
    catalog.close()
    Catalog(path).close()
    with catalog_lock(path), pytest.raises(ValueError, match="Another ingestion"):
        with catalog_lock(path):
            pass
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version = 999")
    with pytest.raises(ValueError, match="schema version"):
        Catalog(path)


def test_capture_checks_partial_chunk_and_exact_size(tmp_path):
    raw = b"abcde" * 1000
    image = tmp_path / "disc.img"
    image.write_bytes(raw[:123])
    digest = capture(io.BytesIO(raw), image, len(raw))
    assert image.read_bytes() == raw
    assert digest == hashlib.sha256(raw).hexdigest()
    with pytest.raises(ValueError, match="exceeds"):
        capture(io.BytesIO(raw), image, len(raw) - 1)
    image.unlink()
    with pytest.raises(ValueError, match="exceeds reported"):
        capture(io.BytesIO(raw), image, len(raw) - 1)


def test_inventory_links_unicode_and_empty_files(tmp_path):
    (tmp_path / "é space").mkdir()
    (tmp_path / "é space" / "empty").touch()
    (tmp_path / "outside").symlink_to("/does/not/exist")
    entries = inventory(tmp_path)
    assert next(e for e in entries if e["path"] == "outside")["target"] == "/does/not/exist"
    empty = next(e for e in entries if e["kind"] == "file")
    assert empty["path"] == "é space/empty"
    assert empty["sha256"] == hashlib.sha256(b"").hexdigest()
    assert inventory(tmp_path, entries) == entries


def test_config(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('bucket = "archive-test"\nprefix = "family"\nprofile = "personal"\n')
    config = load_config(path, bucket="override-test", work_dir=tmp_path / "cache")
    assert config.bucket == "override-test"
    assert config.key("sources/SRC-0001") == "family/sources/SRC-0001"
    assert config.profile == "personal"
    with pytest.raises(ValueError, match="prefix"):
        load_config(path, prefix="../bad")
    with pytest.raises(ValueError, match="bucket"):
        load_config(path, bucket="s3://bucket")


def test_manifest_version_and_paths(setup):
    run(setup)
    manifest = json.loads(setup[2].objects["sources/SRC-0001/manifest.json"])
    assert deserialize(serialize(manifest)) == manifest
    manifest["schema_version"] = 99
    with pytest.raises(ValueError, match="schema version"):
        serialize(manifest)
    manifest["schema_version"] = 1
    manifest["files"][0]["path"] = "../unsafe"
    with pytest.raises(ValueError, match="Unsafe"):
        serialize(manifest)


def test_cli_dry_run_writes_nothing(setup, monkeypatch, capsys):
    from family_archive import cli

    report, config, storage, _ = setup
    monkeypatch.setattr(pipeline, "select_source", lambda device: report)
    assert (
        cli.main(
            [
                "ingest",
                "--device",
                report.device,
                "--bucket",
                config.bucket,
                "--database",
                str(config.database),
                "--work-dir",
                str(config.work_dir),
                "--log-dir",
                str(config.log_dir),
                "--dry-run",
            ]
        )
        == 0
    )
    assert "4,096 bytes" in capsys.readouterr().out
    assert not config.database.exists()
    assert not config.work_dir.exists()
    assert not config.log_dir.exists()
    assert not storage.objects


def test_cli_missing_dependency_has_install_advice(setup, monkeypatch, capsys):
    from family_archive import cli
    from family_archive.storage import s3

    report, config, _, _ = setup
    monkeypatch.setattr(pipeline, "select_source", lambda device: report)

    def missing_dependency(*args):
        raise ModuleNotFoundError("No module named boto3", name="boto3")

    monkeypatch.setattr(s3, "connect", missing_dependency)
    assert cli.main(["ingest", "--device", report.device, "--bucket", config.bucket]) == 1
    error = capsys.readouterr().err
    assert "Missing Python dependency: boto3" in error
    assert "pip install -e ." in error


def test_cached_inventory_hashes_are_resumed(setup, monkeypatch):
    _, config, storage, _ = setup
    storage.fail_at = ".img"
    with pytest.raises(pipeline.IngestionError):
        run(setup)
    storage.fail_at = None

    def should_not_rehash(stream):
        raise AssertionError(
            "Existing file inventory should be reused for verified identical source"
        )

    monkeypatch.setattr("family_archive.manifest.hash_stream", should_not_rehash)
    run(setup, "SRC-0001")
    assert get_record(config)["state"] == "COMPLETE"


def test_unreadable_file_is_recorded_and_fails(setup, monkeypatch):
    from family_archive import manifest

    _, config, storage, _ = setup
    original = manifest.os.open

    def open_file(path, flags, *args, **kwargs):
        if str(path).endswith("a.jpg"):
            raise PermissionError(f"Cannot read {path}")
        return original(path, flags, *args, **kwargs)

    monkeypatch.setattr(manifest.os, "open", open_file)
    with pytest.raises(pipeline.IngestionError, match="a.jpg"):
        run(setup)
    record = get_record(config)
    assert record["state"] == "FAILED"
    assert "a.jpg" in record["last_error"]["detail"]
    assert "a.jpg" in (config.log_dir / "SRC-0001.log").read_text()
    assert "sources/SRC-0001/manifest.json" not in storage.objects


def test_source_and_cache_links_never_written(setup):
    from pathlib import Path

    report, config, storage, _ = setup
    source_file = Path(report.mount_point) / "duplicate.jpg"
    work = config.work_dir / "SRC-0001"
    work.mkdir(parents=True)
    (work / "manifest.json").symlink_to(source_file)
    before = source_file.read_bytes()
    with pytest.raises(ValueError, match="source media"):
        run(setup)
    assert source_file.read_bytes() == before
    assert not storage.objects


def test_catalog_completion_requires_verification(tmp_path):
    catalog = Catalog(tmp_path / "catalog.db")
    try:
        record = catalog.create({})
        record["state"] = "COMPLETE"
        with pytest.raises(ValueError, match="transition"):
            catalog.save(record)
        record["state"] = "INGESTING"
        catalog.save(record)
        record["state"] = "VERIFYING"
        catalog.save(record)
        record["state"] = "COMPLETE"
        catalog.save(record)
        record["state"] = "INGESTING"
        with pytest.raises(ValueError, match="transition"):
            catalog.save(record)
    finally:
        catalog.close()


def test_files_only_archive_preserves_paths_and_verifies_each_file(setup):
    report, config, storage, _ = setup
    source_id = pipeline.ingest(report, config, storage, files_only=True)
    assert source_id == "SRC-0001"
    record = get_record(config)
    assert record["state"] == "COMPLETE"
    assert record["archive_mode"] == "files_only"
    assert record["inventory_origin"] == "source_mount"
    assert record["logical_file_size"] == len(b"family photo") * 2
    manifest = deserialize(storage.objects["sources/SRC-0001/manifest.json"])
    assert manifest["schema_version"] == 3
    assert manifest["preservation"]["full_disc_image"] is False
    assert "sources/SRC-0001/image/SRC-0001.img" not in storage.objects
    first = next(item for item in manifest["files"] if item["path"] == "Photos/a.jpg")
    key = "sources/SRC-0001/data/Photos/a.jpg"
    assert first["object_key"] == key
    assert storage.objects[key] == b"family photo"
    assert first["sha256"] == hashlib.sha256(b"family photo").hexdigest()


def test_failed_image_capture_can_resume_as_files_only(setup):
    report, config, storage, _ = setup

    with pytest.raises(pipeline.IngestionError, match="capture and validate raw image"):
        pipeline.ingest(
            report,
            config,
            storage,
            announce=lambda _message: None,
            device_opener=lambda _device: io.BytesIO(b""),
        )

    assert (
        pipeline.ingest(
            report,
            config,
            storage,
            "SRC-0001",
            announce=lambda _message: None,
            files_only=True,
        )
        == "SRC-0001"
    )
    assert get_record(config)["state"] == "COMPLETE"
    assert get_record(config)["archive_mode"] == "files_only"


def test_files_only_cannot_replace_existing_image_checkpoint(setup):
    report, config, storage, _ = setup
    with pytest.raises(pipeline.IngestionError):
        pipeline.ingest(
            report,
            config,
            storage,
            announce=lambda _message: None,
            device_opener=lambda _device: io.BytesIO(b""),
        )
    record = get_record(config)
    record["image_sha256"] = "a" * 64
    catalog = Catalog(config.database)
    try:
        catalog.save(record)
    finally:
        catalog.close()
    with pytest.raises(ValueError, match="archive mode differs"):
        pipeline.ingest(
            report,
            config,
            storage,
            "SRC-0001",
            announce=lambda _message: None,
            files_only=True,
        )


def test_catalog_backup_is_usable(setup, tmp_path):
    run(setup)
    storage = setup[2]
    key = next(key for key in storage.objects if key.endswith("/archive.db"))
    restored = tmp_path / "restored.db"
    restored.write_bytes(storage.objects[key])
    catalog = Catalog(restored)
    try:
        assert catalog.get("SRC-0001")["state"] == "VERIFYING"
        assert len(catalog.files("SRC-0001")) == 4
        assert catalog.get("SRC-0001")["manifest"]["archive_status"] == "COMPLETE"
    finally:
        catalog.close()


def test_stale_run_is_interrupted_on_restart(tmp_path):
    catalog = Catalog(tmp_path / "archive.db")
    try:
        catalog.create({})
        old = catalog.start_run("SRC-0001", "first")
        catalog.start_run("SRC-0001", "second")
        assert (
            catalog.db.execute(
                "SELECT state FROM ingestion_runs WHERE run_id=?", (old,)
            ).fetchone()[0]
            == "INTERRUPTED"
        )
    finally:
        catalog.close()


def test_cached_image_symlink_refused(tmp_path):
    source = tmp_path / "source"
    source.write_bytes(b"original")
    image = tmp_path / "image"
    image.symlink_to(source)
    with pytest.raises(ValueError, match="symlink"):
        capture(io.BytesIO(b"original appended"), image, 17)
    assert source.read_bytes() == b"original"


def test_macos_raw_device_open_is_read_only(monkeypatch):
    import os
    import stat
    from types import SimpleNamespace

    from family_archive import imaging

    calls = []
    stream = io.BytesIO(b"sector")

    def open_raw(path, flags):
        calls.append((path, flags))
        return 123

    monkeypatch.setattr(imaging.os, "open", open_raw)
    monkeypatch.setattr(
        imaging.os, "fstat", lambda descriptor: SimpleNamespace(st_mode=stat.S_IFCHR)
    )
    monkeypatch.setattr(imaging.os, "fdopen", lambda *args, **kwargs: stream)
    assert imaging.open_device("/dev/disk5") is stream
    assert imaging.open_device("/dev/disk5s0") is stream
    assert calls == [
        ("/dev/rdisk5", os.O_RDONLY | os.O_NOFOLLOW),
        ("/dev/rdisk5s0", os.O_RDONLY | os.O_NOFOLLOW),
    ]
    for invalid in ("/dev/rdisk5", "/tmp/image", "/dev/disk5/other"):
        with pytest.raises(ValueError, match="whole optical device"):
            imaging.open_device(invalid)


def test_image_capture_uses_inspected_data_track_device(setup):
    report, config, storage, raw = setup
    report = replace(report, image_device="/dev/disk5s0")
    opened = []

    def opener(device):
        opened.append(device)
        return io.BytesIO(raw)

    pipeline.ingest(
        report,
        config,
        storage,
        announce=lambda message: None,
        device_opener=opener,
    )
    assert opened == ["/dev/disk5s0", "/dev/disk5s0"]


def test_inventory_reads_image_root_and_detaches_before_upload(setup, tmp_path, monkeypatch):
    report, config, storage, raw = setup
    image_root = tmp_path / "snapshot"
    image_root.mkdir()
    (image_root / "image-only.txt").write_bytes(b"from the image")
    mounted = False
    reads = []

    @contextmanager
    def snapshot_mount(image, filesystem):
        nonlocal mounted
        assert image.read_bytes() == raw
        mounted = True
        try:
            yield image_root
        finally:
            mounted = False

    monkeypatch.setattr(pipeline, "mount_image", snapshot_mount)
    upload = storage.ensure_file

    def ensure_detached(*args, **kwargs):
        assert not mounted
        return upload(*args, **kwargs)

    monkeypatch.setattr(storage, "ensure_file", ensure_detached)

    def source_reader(device):
        reads.append(device)
        return io.BytesIO(raw)

    run(setup, opener=source_reader)
    manifest = deserialize(storage.objects["sources/SRC-0001/manifest.json"])
    assert [entry["path"] for entry in manifest["files"]] == ["image-only.txt"]
    assert manifest["files"][0]["sha256"] == hashlib.sha256(b"from the image").hexdigest()
    assert manifest["inventory_origin"] == "image"
    assert reads == [report.device, report.device]  # capture plus retained source recheck
    assert get_record(config)["state"] == "COMPLETE"


def test_mount_failure_never_falls_back_to_source_files(setup, monkeypatch):
    from family_archive.image_mount import ImageMountError

    _, config, storage, _ = setup

    @contextmanager
    def failed_mount(*args):
        raise ImageMountError("Image filesystem cannot be mounted")
        yield

    def no_inventory(*args, **kwargs):
        pytest.fail("No filesystem should be read after an unsuccessful image mount")

    monkeypatch.setattr(pipeline, "mount_image", failed_mount)
    monkeypatch.setattr(pipeline, "inventory", no_inventory)
    with pytest.raises(pipeline.IngestionError, match="cannot be mounted"):
        run(setup)
    assert get_record(config)["state"] == "FAILED"
    assert not any(key.endswith(".img") for key in storage.objects)


def test_legacy_resume_rehashes_inventory_and_preserves_published_manifest(setup):
    _, config, storage, _ = setup
    storage.fail_at = "archive.db"
    with pytest.raises(pipeline.IngestionError):
        run(setup)
    catalog = Catalog(config.database)
    try:
        record = catalog.get("SRC-0001")
        record.pop("inventory_origin")
        record["manifest"].pop("inventory_origin")
        catalog.save(record)
        old_manifest = serialize(record["manifest"])
        storage.objects["sources/SRC-0001/manifest.json"] = old_manifest
        entries = catalog.files("SRC-0001")
        for entry in entries:
            if entry["kind"] == "file":
                entry["sha256"] = "0" * 64
        catalog.save_files("SRC-0001", entries)
    finally:
        catalog.close()
    storage.fail_at = None
    run(setup, "SRC-0001")
    assert storage.objects["sources/SRC-0001/manifest.json"] == old_manifest
    catalog = Catalog(config.database)
    try:
        assert all(
            entry["sha256"] == hashlib.sha256(b"family photo").hexdigest()
            for entry in catalog.files("SRC-0001")
            if entry["kind"] == "file"
        )
        assert catalog.get("SRC-0001")["state"] == "COMPLETE"
    finally:
        catalog.close()



def test_hold_mounted_volume_accepts_whole_device_mount(tmp_path, monkeypatch):
    mount = tmp_path / "dvd"
    mount.mkdir()
    monkeypatch.setattr(
        pipeline.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout=f"/dev/disk5 on {mount} (cd9660, local, read-only, noowners)\n",
        ),
    )
    descriptor, mount_point = pipeline.hold_mounted_volume(
        "/dev/disk5",
        {"volume_device": "/dev/disk5", "mount_point": str(mount)},
    )
    try:
        assert mount_point == str(mount)
    finally:
        pipeline.os.close(descriptor)

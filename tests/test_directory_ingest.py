import hashlib
import threading
import time
from pathlib import Path

import pytest

from family_archive.cli import main
from family_archive.config import Config
from family_archive.ingest import IngestionError, directory_inspection, ingest_directory
from family_archive.manifest import deserialize
from family_archive.storage import StorageError


class MemoryStorage:
    def __init__(self, fail_key=None):
        self.objects = {}
        self.fail_key = fail_key
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0

    def ensure_bytes(self, key, data):
        if key == self.fail_key:
            raise OSError("simulated upload interruption")
        if key in self.objects and self.objects[key] != data:
            raise StorageError(f"Conflict: {key}")
        self.objects[key] = data

    def ensure_file(self, key, path, sha256, checkpoint, save):
        data = Path(path).read_bytes()
        if hashlib.sha256(data).hexdigest() != sha256:
            raise StorageError(f"Source hash changed: {key}")
        self.ensure_bytes(key, data)

    def ensure_small_file(self, key, path, sha256):
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            time.sleep(0.02)
            data = Path(path).read_bytes()
            if hashlib.sha256(data).hexdigest() != sha256:
                raise StorageError(f"Source hash changed: {key}")
            self.ensure_bytes(key, data)
        finally:
            with self.lock:
                self.active -= 1


@pytest.fixture
def archive(tmp_path):
    source = tmp_path / "Pictures to archive"
    (source / "1998" / "Birthday").mkdir(parents=True)
    (source / "1998" / "Birthday" / "photo one.jpg").write_bytes(b"photo bytes")
    (source / "notes.txt").write_text("family notes", encoding="utf-8")
    (source / "empty").mkdir()
    config = Config(
        "archive-test",
        database=tmp_path / "archive.db",
        work_dir=tmp_path / "work",
        log_dir=tmp_path / "logs",
    )
    return source, config


def test_directory_source_is_explicit_and_rejects_symlink_root(tmp_path):
    source = tmp_path / "pictures"
    source.mkdir()
    alias = tmp_path / "pictures-alias"
    alias.symlink_to(source)

    report = directory_inspection(source)

    assert report.classification == "DIRECTORY"
    assert report.mount_point == str(source)
    with pytest.raises(ValueError, match="symbolic link"):
        directory_inspection(alias)


def test_archives_directory_files_and_empty_directories_with_provenance(archive):
    source, config = archive
    storage = MemoryStorage()

    source_id = ingest_directory(source, config, storage, announce=lambda _message: None)

    assert source_id == "SRC-0001"
    manifest = deserialize(storage.objects["sources/SRC-0001/manifest.json"])
    assert manifest["source_type"] == "directory"
    assert manifest["inventory_origin"] == "directory"
    assert manifest["file_count"] == 2
    assert "empty" in {entry["path"] for entry in manifest["files"]}
    photo = next(entry for entry in manifest["files"] if entry["path"].endswith("photo one.jpg"))
    assert photo["sha256"] == hashlib.sha256(b"photo bytes").hexdigest()
    assert storage.objects[photo["object_key"]] == b"photo bytes"
    assert source.joinpath("1998", "Birthday", "photo one.jpg").read_bytes() == b"photo bytes"
    assert storage.max_active > 1


def test_cli_directory_dry_run_has_no_local_side_effects(archive, tmp_path, capsys):
    source, _config = archive
    database = tmp_path / "dry-run.db"

    result = main(
        [
            "ingest",
            "--directory",
            str(source),
            "--bucket",
            "archive-test",
            "--database",
            str(database),
            "--work-dir",
            str(tmp_path / "dry-run-work"),
            "--log-dir",
            str(tmp_path / "dry-run-logs"),
            "--dry-run",
        ]
    )

    assert result == 0
    assert "Plan: inventory all entries" in capsys.readouterr().out
    assert not database.exists()
    assert not (tmp_path / "dry-run-work").exists()


def test_resume_reuses_directory_source_and_rejects_different_path(archive, tmp_path):
    source, config = archive
    failed = MemoryStorage(fail_key="sources/SRC-0001/data/notes.txt")
    with pytest.raises(IngestionError, match="simulated upload interruption"):
        ingest_directory(source, config, failed, announce=lambda _message: None)

    recovered = MemoryStorage()
    source_id = ingest_directory(
        source, config, recovered, resume="SRC-0001", announce=lambda _message: None
    )
    assert source_id == "SRC-0001"

    other = tmp_path / "other"
    other.mkdir()
    with pytest.raises(ValueError, match="directory path differs"):
        ingest_directory(
            other, config, recovered, resume="SRC-0001", announce=lambda _message: None
        )

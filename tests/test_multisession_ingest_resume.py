import hashlib
import multiprocessing
import os
import signal
import sqlite3
import time
from pathlib import Path

from family_archive.catalog import Catalog
from family_archive.config import Config
from family_archive.manifest import deserialize
from family_archive.media import Inspection
from family_archive.multisession import Track
from family_archive.storage import StorageError


class DirectoryStorage:
    """Immutable fake object store shared by independent worker processes."""

    def __init__(self, root):
        self.root = Path(root)

    def _path(self, key):
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root.resolve()):
            raise ValueError("Unsafe object key")
        return path

    def ensure_bytes(self, key, data):
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if path.read_bytes() != data:
                raise StorageError(f"Conflicting fake object: {key}")
        else:
            path.write_bytes(data)

    def ensure_file(self, key, path, sha256, checkpoint, save):
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != sha256:
            raise StorageError(f"Bad local fake image: {key}")
        self.ensure_bytes(key, data)


def run_worker(database, work_dir, log_dir, object_dir, hard_exit, resume):
    from family_archive import ingest as pipeline

    mount = Path(work_dir).parent / "disc"
    mount.mkdir(exist_ok=True)
    report = Inspection(
        device="/dev/fake-disk",
        media="CD-RW",
        volume="TEST MULTI",
        mount_point=str(mount),
        filesystem="cd9660",
        capacity=10_000,
        tracks=2,
        classification="NEEDS_REVIEW",
        reason="Test multisession candidate",
        sessions=2,
        image_size=10_000,
        volume_device="/dev/fake-disk-s0",
        multisession_candidate=True,
        session_layout=[
            {"session": 1, "track": 1, "start_lba": 0, "sectors": 1},
            {"session": 2, "track": 2, "start_lba": 100, "sectors": 1},
        ],
    )
    config = Config(
        "archive-test",
        database=Path(database),
        work_dir=Path(work_dir),
        log_dir=Path(log_dir),
    )

    def fake_capture_tracks(
        device,
        volume_device,
        mount_point,
        start_lbas,
        track_lengths,
        directory,
        previous=None,
        checkpoint=None,
    ):
        directory.mkdir(parents=True, exist_ok=True)
        saved = {item["session"]: item for item in (previous or [])}
        tracks = []
        for session, (start_lba, sectors) in enumerate(
            zip(start_lbas, track_lengths, strict=True), start=1
        ):
            raw = bytes([session]) * (sectors * 2352)
            path = directory / f"session-{session:02d}.bin"
            path.write_bytes(raw)
            digest = hashlib.sha256(raw).hexdigest()
            if session in saved and saved[session]["sha256"] != digest:
                raise ValueError(f"session {session} differs from saved checkpoint")
            toc = f'CD_ROM_XA\nTRACK MODE2_RAW\nDATAFILE "session-{session:02d}.bin" 00:00:01\n'
            (directory / f"session-{session:02d}.toc").write_text(toc)
            track = Track(session, session, start_lba, sectors, path, digest, toc)
            tracks.append(track)
            if checkpoint:
                checkpoint(tracks)
            if hard_exit and session == 1:
                os._exit(97)
        return tracks

    def fake_inventory(tracks):
        return [
            {
                "path": f"Sessions/{track.session:02d}/file-{track.session}.bin",
                "source_path": f"file-{track.session}.bin",
                "session": track.session,
                "filename": f"file-{track.session}.bin",
                "kind": "file",
                "size": 4,
                "sha256": hashlib.sha256(bytes([track.session]) * 4).hexdigest(),
            }
            for track in tracks
        ]

    pipeline.capture_tracks = fake_capture_tracks
    pipeline.inventory_sessions = fake_inventory
    pipeline.ingest(
        report,
        config,
        DirectoryStorage(object_dir),
        resume=resume,
        announce=lambda message: None,
    )


def get_record(database):
    catalog = Catalog(database)
    try:
        return catalog.get("SRC-0001")
    finally:
        catalog.close()


def test_multisession_ingestion_resumes_after_hard_process_exit(tmp_path):
    database = tmp_path / "archive.db"
    work_dir = tmp_path / "work"
    log_dir = tmp_path / "logs"
    object_dir = tmp_path / "objects"
    context = multiprocessing.get_context("spawn")

    interrupted = context.Process(
        target=run_worker,
        args=(database, work_dir, log_dir, object_dir, True, None),
    )
    interrupted.start()
    interrupted.join(timeout=30)
    assert not interrupted.is_alive()
    assert interrupted.exitcode == 97
    assert get_record(database)["state"] == "INGESTING"
    saved = get_record(database)["session_tracks"]
    assert [track["session"] for track in saved] == [1]
    assert not (object_dir / "sources/SRC-0001/image/SRC-0001.bin").exists()

    resumed = context.Process(
        target=run_worker,
        args=(database, work_dir, log_dir, object_dir, False, "SRC-0001"),
    )
    resumed.start()
    resumed.join(timeout=30)
    assert not resumed.is_alive()
    assert resumed.exitcode == 0

    record = get_record(database)
    assert record["state"] == "COMPLETE"
    manifest = deserialize((object_dir / "sources/SRC-0001/manifest.json").read_bytes())
    assert manifest["schema_version"] == 2
    assert manifest["source_type"] == "multisession_data_cd"
    assert [track["session"] for track in manifest["preservation"]["tracks"]] == [1, 2]
    assert manifest["file_count"] == 2
    assert (object_dir / "sources/SRC-0001/image/SRC-0001.bin").read_bytes() == (
        bytes([1]) * 2352 + bytes([2]) * 2352
    )
    with sqlite3.connect(database) as db:
        assert db.execute(
            "SELECT state FROM ingestion_runs ORDER BY started_at, run_id"
        ).fetchall() == [("INTERRUPTED",), ("COMPLETE",)]


def run_cli_worker(
    database, work_dir, log_dir, object_dir, config_path, ready_path, hard_exit, resume
):
    from family_archive import cli, ingest
    from family_archive.storage import s3

    mount = Path(work_dir).parent / "disc"
    mount.mkdir(exist_ok=True)
    report = Inspection(
        device="/dev/fake-disk",
        media="CD-RW",
        volume="TEST MULTI",
        mount_point=str(mount),
        filesystem="cd9660",
        capacity=10_000,
        tracks=2,
        classification="NEEDS_REVIEW",
        reason="Test multisession candidate",
        sessions=2,
        image_size=10_000,
        volume_device="/dev/fake-disk-s0",
        multisession_candidate=True,
        session_layout=[
            {"session": 1, "track": 1, "start_lba": 0, "sectors": 1},
            {"session": 2, "track": 2, "start_lba": 100, "sectors": 1},
        ],
    )

    class LocalStorage(DirectoryStorage):
        def ensure_file(self, key, path, sha256, checkpoint, save):
            data = Path(path).read_bytes()
            if hashlib.sha256(data).hexdigest() != sha256:
                raise StorageError(f"Bad local fake image: {key}")
            self.ensure_bytes(key, data)

    def fake_capture_tracks(
        device,
        volume_device,
        mount_point,
        start_lbas,
        track_lengths,
        directory,
        previous=None,
        checkpoint=None,
    ):
        directory.mkdir(parents=True, exist_ok=True)
        saved = {item["session"]: item for item in (previous or [])}
        tracks = []
        for session, (start_lba, sectors) in enumerate(
            zip(start_lbas, track_lengths, strict=True), start=1
        ):
            raw = bytes([session]) * (sectors * 2352)
            path = directory / f"session-{session:02d}.bin"
            path.write_bytes(raw)
            digest = hashlib.sha256(raw).hexdigest()
            if session in saved and saved[session]["sha256"] != digest:
                raise ValueError(f"session {session} differs from saved checkpoint")
            toc = f'CD_ROM_XA\nTRACK MODE2_RAW\nDATAFILE "session-{session:02d}.bin" 00:00:01\n'
            (directory / f"session-{session:02d}.toc").write_text(toc)
            tracks.append(Track(session, session, start_lba, sectors, path, digest, toc))
            if checkpoint:
                checkpoint(tracks)
            if hard_exit and session == 1:
                Path(ready_path).touch()
                while True:
                    time.sleep(1)
        return tracks

    def fake_inventory(tracks):
        return [
            {
                "path": f"Sessions/{track.session:02d}/file-{track.session}.bin",
                "source_path": f"file-{track.session}.bin",
                "session": track.session,
                "filename": f"file-{track.session}.bin",
                "kind": "file",
                "size": 4,
                "sha256": hashlib.sha256(bytes([track.session]) * 4).hexdigest(),
            }
            for track in tracks
        ]

    ingest.select_source = lambda device: report
    ingest.capture_tracks = fake_capture_tracks
    ingest.inventory_sessions = fake_inventory
    s3.connect = lambda bucket, profile: LocalStorage(object_dir)
    argv = [
        "ingest",
        "--device",
        report.device,
        "--config",
        str(config_path),
        "--database",
        str(database),
        "--work-dir",
        str(work_dir),
        "--log-dir",
        str(log_dir),
    ]
    if resume:
        argv.extend(["--resume", resume])
    raise SystemExit(cli.main(argv))


def test_cli_resumes_after_hard_process_kill(tmp_path):
    database = tmp_path / "archive.db"
    work_dir = tmp_path / "work"
    log_dir = tmp_path / "logs"
    object_dir = tmp_path / "objects"
    config_path = tmp_path / "config.toml"
    config_path.write_text('bucket = "archive-test"\n')
    ready_path = tmp_path / "checkpoint-ready"
    context = multiprocessing.get_context("spawn")

    interrupted = context.Process(
        target=run_cli_worker,
        args=(database, work_dir, log_dir, object_dir, config_path, ready_path, True, None),
    )
    interrupted.start()
    deadline = time.monotonic() + 30
    while not ready_path.exists() and time.monotonic() < deadline:
        if not interrupted.is_alive():
            break
        time.sleep(0.05)
    assert ready_path.exists(), "CLI did not reach its durable session checkpoint"
    os.kill(interrupted.pid, signal.SIGKILL)
    interrupted.join(timeout=10)
    assert not interrupted.is_alive()
    assert interrupted.exitcode == -signal.SIGKILL
    assert get_record(database)["state"] == "INGESTING"
    assert [track["session"] for track in get_record(database)["session_tracks"]] == [1]
    assert not (object_dir / "sources/SRC-0001/image/SRC-0001.bin").exists()

    resumed = context.Process(
        target=run_cli_worker,
        args=(database, work_dir, log_dir, object_dir, config_path, ready_path, False, "SRC-0001"),
    )
    resumed.start()
    resumed.join(timeout=30)
    assert not resumed.is_alive()
    assert resumed.exitcode == 0
    assert get_record(database)["state"] == "COMPLETE"
    with sqlite3.connect(database) as db:
        assert db.execute(
            "SELECT state FROM ingestion_runs ORDER BY rowid"
        ).fetchall() == [("INTERRUPTED",), ("COMPLETE",)]
    manifest = deserialize((object_dir / "sources/SRC-0001/manifest.json").read_bytes())
    assert manifest["archive_status"] == "COMPLETE"
    assert manifest["file_count"] == 2

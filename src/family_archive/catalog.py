"""Local durable checkpoints and a deliberately small, migrated catalog."""

import csv
import fcntl
import json
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

STATES = {"NEW", "INGESTING", "INTERRUPTED", "VERIFYING", "COMPLETE", "FAILED", "NEEDS_REVIEW"}
TRANSITIONS = {
    "NEW": {"INGESTING"},
    "INGESTING": {"VERIFYING", "INTERRUPTED", "FAILED", "NEEDS_REVIEW"},
    "VERIFYING": {"INGESTING", "COMPLETE", "INTERRUPTED", "FAILED", "NEEDS_REVIEW"},
    "INTERRUPTED": {"INGESTING"},
    "FAILED": {"INGESTING"},
    "NEEDS_REVIEW": {"INGESTING"},
    "COMPLETE": set(),
}


@contextmanager
def catalog_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(path.suffix + ".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("Another ingestion is using this catalog") from exc
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


class Catalog:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version > 1:
            self.db.close()
            raise ValueError(f"Unsupported catalog schema version: {version}")
        if version == 0:
            with self.db:
                self.db.executescript("""
                    BEGIN;
                    CREATE TABLE sources (
                        number INTEGER PRIMARY KEY AUTOINCREMENT,
                        source_id TEXT UNIQUE,
                        state TEXT NOT NULL,
                        record TEXT NOT NULL
                    );
                    CREATE TABLE files (
                        source_id TEXT NOT NULL, path TEXT NOT NULL, record TEXT NOT NULL,
                        sha256 TEXT, PRIMARY KEY(source_id, path)
                    );
                    CREATE INDEX files_sha256 ON files(sha256);
                    CREATE TABLE ingestion_runs (
                        run_id TEXT PRIMARY KEY, source_id TEXT NOT NULL,
                        started_at TEXT NOT NULL, ended_at TEXT, state TEXT NOT NULL
                    );
                    PRAGMA user_version = 1;
                    COMMIT;
                """)

    def close(self):
        self.db.close()

    def create(self, record: dict) -> dict:
        with self.db:
            cursor = self.db.execute(
                "INSERT INTO sources(state, record) VALUES (?, ?)", ("NEW", "{}")
            )
            record = {
                **record,
                "source_id": f"SRC-{cursor.lastrowid:04d}",
                "state": "NEW",
                "token": str(uuid4()),
            }
            self.db.execute(
                "UPDATE sources SET source_id=?, record=? WHERE number=?",
                (record["source_id"], json.dumps(record), cursor.lastrowid),
            )
        return record

    def get(self, source_id: str) -> dict:
        if not re.fullmatch(r"SRC-\d{4,}", source_id):
            raise ValueError("Invalid source ID; expected SRC-0001")
        row = self.db.execute(
            "SELECT record FROM sources WHERE source_id=?", (source_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"Source not found: {source_id}")
        return json.loads(row["record"])

    def save(self, record: dict) -> None:
        if record["state"] not in STATES:
            raise ValueError("Invalid source state")
        old = self.get(record["source_id"])["state"]
        if old != record["state"] and record["state"] not in TRANSITIONS[old]:
            raise ValueError(f"Invalid source transition: {old} -> {record['state']}")
        with self.db:
            self.db.execute(
                "UPDATE sources SET state=?, record=? WHERE source_id=?",
                (record["state"], json.dumps(record), record["source_id"]),
            )

    def save_files(self, source_id: str, entries: list[dict]):
        with self.db:
            self.db.execute("DELETE FROM files WHERE source_id=?", (source_id,))
            self.db.executemany(
                "INSERT INTO files VALUES (?, ?, ?, ?)",
                [
                    (source_id, item["path"], json.dumps(item), item.get("sha256"))
                    for item in entries
                ],
            )

    def save_file(self, source_id: str, entry: dict):
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO files VALUES (?, ?, ?, ?)",
                (source_id, entry["path"], json.dumps(entry), entry.get("sha256")),
            )

    def files(self, source_id: str) -> list[dict]:
        return [
            json.loads(row[0])
            for row in self.db.execute(
                "SELECT record FROM files WHERE source_id=? ORDER BY path", (source_id,)
            )
        ]

    def start_run(self, source_id: str, started_at: str) -> str:
        run_id = str(uuid4())
        with self.db:
            # A process killed without cleanup leaves a historical interrupted run.
            self.db.execute(
                "UPDATE ingestion_runs SET state='INTERRUPTED', ended_at=? "
                "WHERE source_id=? AND ended_at IS NULL",
                (started_at, source_id),
            )
            self.db.execute(
                "INSERT INTO ingestion_runs VALUES (?, ?, ?, NULL, ?)",
                (run_id, source_id, started_at, "INGESTING"),
            )
        return run_id

    def end_run(self, run_id: str, state: str, ended_at: str):
        with self.db:
            self.db.execute(
                "UPDATE ingestion_runs SET state=?, ended_at=? WHERE run_id=?",
                (state, ended_at, run_id),
            )

    def export(self, database: Path, csv_path: Path):
        backup = sqlite3.connect(database)
        try:
            self.db.backup(backup)
        finally:
            backup.close()
        with csv_path.open("w", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(["source_id", "state", "volume_label", "image_sha256", "file_count"])
            for row in self.db.execute(
                "SELECT source_id, state, record FROM sources ORDER BY number"
            ):
                item = json.loads(row["record"])
                writer.writerow(
                    [
                        row["source_id"],
                        row["state"],
                        item["inspection"]["volume"],
                        item.get("image_sha256", ""),
                        item.get("file_count", 0),
                    ]
                )

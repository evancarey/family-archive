"""Read-only archive verification independent of local ingestion state."""

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import PurePosixPath

from family_archive.config import Config
from family_archive.hashing import CHUNK_SIZE
from family_archive.manifest import deserialize
from family_archive.storage import ArchiveReader

METADATA_LIMIT = 64 * 1024 * 1024


def source_number(source_id: str) -> int:
    if not isinstance(source_id, str) or not re.fullmatch(r"SRC-\d{4,}", source_id):
        raise ValueError("Invalid source ID; expected SRC-0001")
    number = int(source_id[4:])
    if number < 1 or source_id != f"SRC-{number:04d}" or number > 2**63 - 1:
        raise ValueError(f"Noncanonical or out-of-range source ID: {source_id}")
    return number


def read_metadata(reader: ArchiveReader, key: str) -> bytes:
    with reader.open_read(key) as stream:
        chunks, size = [], 0
        while chunk := stream.read(min(CHUNK_SIZE, METADATA_LIMIT - size + 1)):
            size += len(chunk)
            if size > METADATA_LIMIT:
                raise ValueError(f"Archive metadata exceeds {METADATA_LIMIT} bytes: {key}")
            chunks.append(chunk)
        return b"".join(chunks)


@dataclass(frozen=True)
class VerifiedSource:
    manifest: dict
    manifest_sha256: str
    image_size: int
    image_sha256: str
    anchored: bool


def local_anchor(config: Config, source_id: str) -> dict | None:
    """Open an existing catalog read-only, without migrations or creating a database."""
    if not config.database.exists():
        return None
    db = sqlite3.connect(config.database.as_uri() + "?mode=ro", uri=True)
    try:
        db.execute("PRAGMA trusted_schema=OFF")
        if db.execute("PRAGMA user_version").fetchone()[0] != 1:
            raise ValueError("Unsupported local catalog schema version")
        row = db.execute("SELECT record FROM sources WHERE source_id=?", (source_id,)).fetchone()
        if row is None:
            return None
        record = json.loads(row[0])
        if not isinstance(record, dict):
            raise ValueError(f"{source_id}: local catalog record is not an object")
        return record
    finally:
        db.close()


def load_manifest(reader: ArchiveReader, config: Config, source_id: str) -> tuple[dict, bytes]:
    source_number(source_id)
    data = read_metadata(reader, config.key(f"sources/{source_id}/manifest.json"))
    try:
        manifest = deserialize(data)
    except ValueError as exc:
        raise ValueError(f"{source_id}: invalid manifest: {exc}") from exc
    if manifest["source_id"] != source_id:
        raise ValueError(f"Manifest source ID does not match {source_id}")
    if manifest.get("destination") != {"bucket": config.bucket, "prefix": config.prefix}:
        raise ValueError(f"{source_id}: manifest destination does not match configured archive")
    preservation = manifest["preservation"]
    if manifest["schema_version"] == 3:
        prefix = preservation["object_prefix"]
        expected = config.key(f"sources/{source_id}/data")
        if prefix != expected:
            raise ValueError(f"{source_id}: file object prefix differs from configured namespace")
    else:
        key = preservation.get("key")
        if (
            not isinstance(key, str)
            or not key.startswith(config.key(f"sources/{source_id}/image/"))
            or str(PurePosixPath(key)) != key
            or ".." in PurePosixPath(key).parts
            or "\x00" in key
        ):
            raise ValueError(f"{source_id}: image key is outside its source image namespace")
    inspection = manifest.get("inspection")
    if not isinstance(inspection, dict) or not isinstance(inspection.get("volume"), str):
        raise ValueError(f"{source_id}: missing inspection provenance")
    return manifest, data


def verify_source(
    reader: ArchiveReader,
    config: Config,
    source_id: str,
    anchor: dict | None = None,
    announce=print,
) -> VerifiedSource:
    """Hash the entire stored image; never accept its metadata or ETag as proof."""
    manifest, data = load_manifest(reader, config, source_id)
    image = manifest["preservation"]
    if anchor:
        if anchor.get("source_id") != source_id:
            raise ValueError("Local catalog source ID mismatch")
        if anchor.get("manifest") is not None and anchor["manifest"] != manifest:
            raise ValueError(f"{source_id}: remote manifest differs from local catalog copy")
        if manifest["schema_version"] != 3:
            for field, value in (("image_sha256", image["sha256"]), ("image_size", image["size"])):
                if anchor.get(field) is not None and anchor[field] != value:
                    raise ValueError(
                        f"{source_id}: remote image metadata differs from local {field}"
                    )
    anchored = bool(anchor and (anchor.get("manifest") or anchor.get("image_sha256")))
    if manifest["schema_version"] == 3:
        file_entries = [entry for entry in manifest["files"] if entry["kind"] == "file"]
        announce(f"{source_id}: verifying {len(file_entries):,} archived files")
        for entry in file_entries:
            digest = hashlib.sha256()
            size = 0
            with reader.open_read(entry["object_key"]) as stream:
                while chunk := stream.read(CHUNK_SIZE):
                    size += len(chunk)
                    if size > entry["size"]:
                        raise ValueError(
                            f"{source_id}: archived file exceeds manifest size: {entry['path']}"
                        )
                    digest.update(chunk)
            if size != entry["size"] or digest.hexdigest() != entry["sha256"]:
                raise ValueError(f"{source_id}: archived file verification failed: {entry['path']}")
        announce(
            f"{source_id}: VERIFIED {len(file_entries):,} files and manifest; "
            "no full disc image was captured"
        )
        if not anchored:
            announce(
                "Compared against the remote manifest; no independent local hash anchor "
                "was available."
            )
        return VerifiedSource(manifest, hashlib.sha256(data).hexdigest(), 0, "", anchored)
    announce(f"{source_id}: verifying {image['size']:,} image bytes with SHA-256")
    digest, size, last_tenth = hashlib.sha256(), 0, -1
    with reader.open_read(image["key"]) as stream:
        tracks = image.get("tracks")
        if tracks:
            for track in tracks:
                track_digest = hashlib.sha256()
                remaining = track["size"]
                while remaining:
                    chunk = stream.read(min(CHUNK_SIZE, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    size += len(chunk)
                    digest.update(chunk)
                    track_digest.update(chunk)
                    tenth = size * 10 // image["size"]
                    if tenth > last_tenth:
                        announce(
                            f"{source_id}: verification {min(100, size * 100 // image['size'])}%"
                        )
                        last_tenth = tenth
                if remaining or track_digest.hexdigest() != track["sha256"]:
                    raise ValueError(
                        f"{source_id}: session {track['session']} track image verification FAILED"
                    )
            if stream.read(1):
                raise ValueError(f"{source_id}: image exceeds manifest track ranges")
        else:
            while chunk := stream.read(min(CHUNK_SIZE, image["size"] - size + 1)):
                size += len(chunk)
                if size > image["size"]:
                    raise ValueError(f"{source_id}: image exceeds manifest size")
                digest.update(chunk)
                tenth = size * 10 // image["size"]
                if tenth > last_tenth:
                    announce(f"{source_id}: verification {min(100, size * 100 // image['size'])}%")
                    last_tenth = tenth
    actual = digest.hexdigest()
    if size != image["size"] or actual != image["sha256"]:
        raise ValueError(
            f"{source_id}: image verification FAILED: expected "
            f"{image['size']} bytes / {image['sha256']}, got {size} bytes / {actual}"
        )
    announce(f"{source_id}: VERIFIED image and manifest; {manifest['file_count']:,} file records")
    if not anchored:
        announce(
            "Compared against the remote manifest; no independent local hash anchor was available."
        )
    announce(
        "File hashes are validated metadata; individual files were not extracted and rehashed."
    )
    return VerifiedSource(manifest, hashlib.sha256(data).hexdigest(), size, actual, anchored)

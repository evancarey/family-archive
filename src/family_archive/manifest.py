"""Versioned, portable manifests for disc images and mounted-file archives."""

import json
import mimetypes
import os
import re
import stat
from datetime import datetime
from pathlib import Path, PurePosixPath

from family_archive.hashing import hash_stream

SCHEMA_VERSION = 1
MULTISESSION_SCHEMA_VERSION = 2
FILES_ONLY_SCHEMA_VERSION = 3


def serialize(manifest: dict) -> bytes:
    validate(manifest)
    return (json.dumps(manifest, sort_keys=True, ensure_ascii=True, indent=2) + "\n").encode()


def deserialize(data: bytes) -> dict:
    manifest = json.loads(data)
    validate(manifest)
    return manifest


def validate(manifest: dict) -> None:
    try:
        _validate(manifest)
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError(f"Malformed manifest: {exc}") from exc


def _validate(manifest: dict) -> None:
    if not isinstance(manifest, dict):
        raise ValueError("Manifest must be a JSON object")
    if type(manifest.get("schema_version")) is not int or manifest["schema_version"] not in {
        SCHEMA_VERSION,
        MULTISESSION_SCHEMA_VERSION,
        FILES_ONLY_SCHEMA_VERSION,
    }:
        raise ValueError("Unsupported manifest schema version")
    if manifest.get("archive_status") != "COMPLETE":
        raise ValueError("Only verified complete manifests can be published")
    if not re.fullmatch(r"SRC-\d{4,}", manifest.get("source_id", "")):
        raise ValueError("Invalid manifest source ID")
    version = manifest["schema_version"]
    allowed_types = (
        {"data_cd", "data_dvd"}
        if version in {1, FILES_ONLY_SCHEMA_VERSION}
        else {"multisession_data_cd"}
    )
    if manifest.get("source_type") not in allowed_types:
        raise ValueError("Unsupported manifest source type")
    for key in ("started_at", "completed_at"):
        if datetime.fromisoformat(manifest[key]).tzinfo is None:
            raise ValueError(f"Manifest timestamp requires a timezone: {key}")
    image = manifest["preservation"]
    if version == 1:
        if (
            image.get("format") != "raw_logical_sector_image"
            or image.get("sector_size") != 2048
            or type(image.get("size")) is not int
            or image["size"] <= 0
            or image["size"] % 2048
            or not re.fullmatch(r"[a-f0-9]{64}", image.get("sha256", ""))
        ):
            raise ValueError("Invalid raw image metadata")
    elif version == MULTISESSION_SCHEMA_VERSION:
        _validate_multisession_image(image)
    else:
        prefix = image.get("object_prefix")
        if (
            image.get("format") != "mounted_files"
            or image.get("full_disc_image") is not False
            or not isinstance(prefix, str)
            or not prefix.endswith(f"sources/{manifest['source_id']}/data")
            or str(PurePosixPath(prefix)) != prefix
            or PurePosixPath(prefix).is_absolute()
            or ".." in PurePosixPath(prefix).parts
            or "\x00" in prefix
        ):
            raise ValueError("Invalid files-only preservation metadata")
    seen = set()
    for entry in manifest["files"]:
        path = PurePosixPath(entry["path"])
        if (
            not path.parts
            or path.is_absolute()
            or ".." in path.parts
            or str(path) != entry["path"]
            or "\x00" in entry["path"]
        ):
            raise ValueError(f"Unsafe manifest path: {entry['path']}")
        if entry["path"] in seen:
            raise ValueError(f"Duplicate manifest path: {entry['path']}")
        seen.add(entry["path"])
        if version == 2:
            source_path = PurePosixPath(entry.get("source_path", ""))
            session = entry.get("session")
            if (
                type(session) is not int
                or session < 1
                or not source_path.parts
                or source_path.is_absolute()
                or ".." in source_path.parts
                or str(source_path) != entry.get("source_path")
                or entry["path"] != f"Sessions/{session:02d}/{source_path}"
            ):
                raise ValueError("Invalid session-scoped original path")
        if entry.get("filename") != path.name:
            raise ValueError("Manifest filename does not match path")
        if entry.get("kind") == "file":
            if (
                type(entry.get("size")) is not int
                or entry["size"] < 0
                or not re.fullmatch(r"[a-f0-9]{64}", entry.get("sha256", ""))
            ):
                raise ValueError(f"Invalid file integrity metadata: {entry['path']}")
            if version == FILES_ONLY_SCHEMA_VERSION and entry.get("object_key") != (
                f"{image['object_prefix']}/{entry['path']}"
            ):
                raise ValueError(f"Invalid archived file object key: {entry['path']}")
        elif entry.get("kind") == "symlink":
            if not isinstance(entry.get("target"), str):
                raise ValueError("Missing symlink target")
        elif entry.get("kind") != "directory":
            raise ValueError(f"Unsupported manifest entry: {entry['path']}")
    for key, expected in (
        ("file_count", sum(e["kind"] == "file" for e in manifest["files"])),
        ("directory_count", sum(e["kind"] == "directory" for e in manifest["files"])),
        ("logical_file_size", sum(e.get("size", 0) for e in manifest["files"])),
    ):
        if type(manifest.get(key)) is not int or manifest.get(key) != expected:
            raise ValueError(f"Manifest summary mismatch: {key}")


def _validate_multisession_image(image: dict) -> None:
    if (
        image.get("format") != "concatenated_mode2_raw_session_tracks"
        or image.get("sector_size") != 2352
        or type(image.get("size")) is not int
        or image["size"] <= 0
        or image["size"] % 2352
        or not re.fullmatch(r"[a-f0-9]{64}", image.get("sha256", ""))
    ):
        raise ValueError("Invalid multisession raw track image metadata")
    tracks = image.get("tracks")
    if not isinstance(tracks, list) or len(tracks) < 2:
        raise ValueError("A multisession image requires at least two track records")
    offset = 0
    prior_lba = -1
    for session, track in enumerate(tracks, start=1):
        if (
            not isinstance(track, dict)
            or track.get("session") != session
            or track.get("track") != session
            or type(track.get("start_lba")) is not int
            or track["start_lba"] <= prior_lba
            or type(track.get("sectors")) is not int
            or track["sectors"] < 1
            or track.get("sector_size") != 2352
            or track.get("offset_bytes") != offset
            or track.get("size") != track["sectors"] * 2352
            or not isinstance(track.get("toc"), str)
            or len(track["toc"]) > 1024 * 1024
            or not re.fullmatch(r"[a-f0-9]{64}", track.get("sha256", ""))
        ):
            raise ValueError(f"Invalid multisession track record: session {session}")
        offset += track["size"]
        prior_lba = track["start_lba"]
    if offset != image["size"]:
        raise ValueError("Multisession track ranges do not sum to image size")


def inventory(root: Path, previous: list[dict] | None = None, checkpoint=None) -> list[dict]:
    """Inventory every entry. Reused hashes require the same immutable source image.

    The caller must verify the complete raw-image identity before reusing entries.
    Symlink targets are recorded, never followed. Special entries fail visibly.
    """
    cached = {item["path"]: item for item in (previous or [])}
    entries = []

    def visit(directory: Path) -> None:
        with os.scandir(directory) as scan:
            children = sorted(scan, key=lambda item: item.name)
        for child in children:
            path = Path(child.path)
            relative = path.relative_to(root).as_posix()
            info = child.stat(follow_symlinks=False)
            entry = {
                "path": relative,
                "filename": child.name,
                "mtime_ns": info.st_mtime_ns,
                "mode": stat.S_IMODE(info.st_mode),
            }
            if stat.S_ISLNK(info.st_mode):
                entry.update(kind="symlink", target=os.readlink(path))
            elif stat.S_ISDIR(info.st_mode):
                entry.update(kind="directory")
            elif stat.S_ISREG(info.st_mode):
                entry.update(kind="file", size=info.st_size)
                old = cached.get(relative)
                if old and all(old.get(k) == v for k, v in entry.items()):
                    entry = old
                else:
                    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                    with os.fdopen(descriptor, "rb") as stream:
                        size, sha256 = hash_stream(stream)
                        after = os.fstat(stream.fileno())
                    if size != info.st_size or after.st_mtime_ns != info.st_mtime_ns:
                        raise ValueError(f"Source changed during hashing: {relative}")
                    entry.update(sha256=sha256, mime_type=mimetypes.guess_type(child.name)[0])
            else:
                raise ValueError(f"Unsupported source entry: {relative}; nothing will be omitted")
            entries.append(entry)
            if checkpoint:
                checkpoint(entry)
            if entry["kind"] == "directory":
                visit(path)

    visit(root)
    return entries

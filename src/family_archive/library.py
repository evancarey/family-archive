"""Pure transformations for a read-only, provenance-preserving family library."""

import json
import mimetypes
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from typing import Literal

from family_archive.config import Config
from family_archive.manifest import validate
from family_archive.storage import ArchiveReader
from family_archive.verification import load_manifest, source_number

IndexMediaType = Literal["photo", "video", "audio"]
ReferenceKind = Literal["file_object", "image_entry", "multisession_image_entry"]


@dataclass(frozen=True, slots=True)
class ArchiveReference:
    """Location of the original bytes; this does not imply a library copy exists."""

    kind: ReferenceKind
    object_key: str
    object_size: int
    object_sha256: str
    path: str | None = None
    filesystem: str | None = None
    tracks: tuple[dict, ...] = ()


@dataclass(frozen=True, slots=True)
class LibraryItem:
    source_id: str
    path: str
    filename: str
    media_type: IndexMediaType
    size: int
    sha256: str
    mime_type: str
    mtime_ns: int | None
    original: ArchiveReference


@dataclass(frozen=True, slots=True)
class LibrarySource:
    source_id: str
    source_type: str
    volume_label: str
    completed_at: str
    item_count: int


@dataclass(frozen=True, slots=True)
class LibraryIndex:
    schema_version: int
    sources: tuple[LibrarySource, ...]
    items: tuple[LibraryItem, ...]


def build_library_index(manifests: Iterable[dict]) -> LibraryIndex:
    """Derive a stable media index from verified complete manifests.

    Incomplete manifests are excluded. Every indexed item retains its source ID,
    original path, content hash, and a reference to its archival bytes.
    """
    source_by_id: dict[str, LibrarySource] = {}
    items: list[LibraryItem] = []

    for manifest in manifests:
        if not isinstance(manifest, dict) or manifest.get("archive_status") != "COMPLETE":
            continue
        validate(manifest)

        source_id = manifest["source_id"]
        if source_id in source_by_id:
            raise ValueError(f"Duplicate complete manifest for {source_id}")

        preservation = manifest["preservation"]
        schema_version = manifest["schema_version"]
        direct_files = schema_version == 3
        image_kind = "image_entry" if schema_version == 1 else "multisession_image_entry"
        source_items = tuple(
            item
            for entry in manifest["files"]
            if entry["kind"] == "file"
            if (item := _media_item(manifest, entry, preservation, direct_files, image_kind))
            is not None
        )
        source_by_id[source_id] = LibrarySource(
            source_id=source_id,
            source_type=manifest["source_type"],
            volume_label=manifest.get("inspection", {}).get("volume", ""),
            completed_at=manifest["completed_at"],
            item_count=len(source_items),
        )
        items.extend(source_items)

    ordered_items = tuple(
        sorted(
            items,
            key=lambda item: (
                _source_number(item.source_id),
                item.path.casefold(),
                item.path,
            ),
        )
    )
    ordered_sources = tuple(
        source_by_id[source_id] for source_id in sorted(source_by_id, key=_source_number)
    )
    return LibraryIndex(schema_version=1, sources=ordered_sources, items=ordered_items)


def serialize_library_index(index: LibraryIndex) -> bytes:
    """Serialize an index deterministically for derived storage or API responses."""
    return (json.dumps(asdict(index), sort_keys=True, indent=2) + "\n").encode("utf-8")


def load_library_index(reader: ArchiveReader, config: Config) -> LibraryIndex:
    """Read source manifests from storage and derive an index without reading media."""
    source_prefix = config.key("sources/")
    manifests = []
    for key in sorted(reader.keys(source_prefix)):
        relative_key = key.removeprefix(source_prefix)
        source_id, separator, filename = relative_key.partition("/")
        if not separator or filename != "manifest.json" or "/" in filename:
            continue
        source_number(source_id)
        manifest, _manifest_bytes = load_manifest(reader, config, source_id)
        manifests.append(manifest)
    return build_library_index(manifests)


def _media_item(
    manifest: dict,
    entry: dict,
    preservation: dict,
    direct_files: bool,
    image_kind: ReferenceKind,
) -> LibraryItem | None:
    path = entry["path"]
    saved_mime_type = entry.get("mime_type")
    mime_type = (
        saved_mime_type if isinstance(saved_mime_type, str) else mimetypes.guess_type(path)[0]
    ) or ""
    media_type = _media_type(mime_type)
    if media_type is None:
        return None

    if direct_files:
        original = ArchiveReference(
            kind="file_object",
            object_key=entry["object_key"],
            object_size=entry["size"],
            object_sha256=entry["sha256"],
        )
    else:
        original = ArchiveReference(
            kind=image_kind,
            object_key=preservation["key"],
            object_size=preservation["size"],
            object_sha256=preservation["sha256"],
            path=path,
            filesystem=manifest.get("inspection", {}).get("filesystem"),
            tracks=tuple(preservation.get("tracks", ()))
            if image_kind == "multisession_image_entry"
            else (),
        )
    return LibraryItem(
        source_id=manifest["source_id"],
        path=path,
        filename=entry["filename"],
        media_type=media_type,
        size=entry["size"],
        sha256=entry["sha256"],
        mime_type=mime_type,
        mtime_ns=entry.get("mtime_ns"),
        original=original,
    )


def _media_type(mime_type: str) -> IndexMediaType | None:
    if mime_type.startswith("image/"):
        return "photo"
    if mime_type.startswith("video/"):
        return "video"
    if mime_type.startswith("audio/"):
        return "audio"
    return None


def _source_number(source_id: str) -> int:
    return int(source_id.removeprefix("SRC-"))

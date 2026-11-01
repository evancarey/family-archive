"""Publish verified derived media under immutable, private library object keys."""

import hashlib
import json
from dataclasses import dataclass

from family_archive.config import Config
from family_archive.library import LibraryIndex, LibraryItem
from family_archive.library_assets import MaterializationResult, _require_matching_file
from family_archive.library_thumbnails import THUMBNAIL_VERSION, ThumbnailAsset
from family_archive.storage import Storage

MAX_SINGLE_PUT_SIZE = 5_000_000_000


@dataclass(frozen=True, slots=True)
class PublishedLibraryIndex:
    key: str
    sha256: str
    size: int
    item_count: int
    source_count: int
    media_bytes: int
    thumbnail_bytes: int


def publish_library(
    storage: Storage,
    config: Config,
    index: LibraryIndex,
    materialized: MaterializationResult,
    thumbnails: tuple[ThumbnailAsset, ...],
) -> PublishedLibraryIndex:
    """Verify and conditionally publish derivatives, then publish the index last."""
    assets = {asset.sha256: asset for asset in materialized.assets}
    thumbnails_by_source = {item.source_sha256: item for item in thumbnails}
    supported_items = index.items
    missing = [item for item in supported_items if item.sha256 not in assets]
    if missing:
        item = missing[0]
        raise ValueError(f"No materialized asset for {item.source_id}:{item.path}")

    media_keys = {
        digest: config.key(f"library/media/{digest[:2]}/{digest}")
        for digest in sorted({item.sha256 for item in supported_items})
    }
    thumbnail_keys = {
        item.source_sha256: config.key(
            f"library/thumbnails/v{THUMBNAIL_VERSION}/{item.max_dimension}/{item.source_sha256}.jpg"
        )
        for item in thumbnails
    }

    media_bytes = 0
    for digest, key in media_keys.items():
        asset = assets[digest]
        _require_matching_file(asset.path, asset.size, digest)
        if asset.size > MAX_SINGLE_PUT_SIZE:
            raise ValueError(
                f"Derived asset exceeds the single-PUT S3 limit and needs multipart publishing: "
                f"{key}"
            )
        storage.ensure_content_addressed_file(key, asset.path, digest)
        media_bytes += asset.size

    thumbnail_bytes = 0
    for source_hash, key in thumbnail_keys.items():
        thumbnail = thumbnails_by_source[source_hash]
        _require_matching_file(thumbnail.path, thumbnail.size, thumbnail.sha256)
        storage.ensure_content_addressed_file(key, thumbnail.path, thumbnail.sha256)
        thumbnail_bytes += thumbnail.size

    document = serialize_published_index(
        index, supported_items, media_keys, thumbnails_by_source, thumbnail_keys
    )
    digest = hashlib.sha256(document).hexdigest()
    index_key = config.key(f"library/index/{digest}.json")
    storage.ensure_bytes(index_key, document)
    source_count = len({item.source_id for item in supported_items})
    return PublishedLibraryIndex(
        key=index_key,
        sha256=digest,
        size=len(document),
        item_count=len(supported_items),
        source_count=source_count,
        media_bytes=media_bytes,
        thumbnail_bytes=thumbnail_bytes,
    )


def serialize_published_index(
    index: LibraryIndex,
    items: tuple[LibraryItem, ...],
    media_keys: dict[str, str],
    thumbnails_by_source: dict[str, ThumbnailAsset],
    thumbnail_keys: dict[str, str],
) -> bytes:
    """Create a deterministic public-library document without archive locators."""
    visible_items = tuple(
        _published_item(item, media_keys, thumbnails_by_source, thumbnail_keys) for item in items
    )
    counts = {}
    for item in visible_items:
        counts[item["source_id"]] = counts.get(item["source_id"], 0) + 1
    source_records = [
        {
            "source_id": source.source_id,
            "source_type": source.source_type,
            "volume_label": source.volume_label,
            "completed_at": source.completed_at,
            "item_count": counts[source.source_id],
        }
        for source in index.sources
        if source.source_id in counts
    ]
    return (
        json.dumps(
            {"schema_version": 1, "sources": source_records, "items": visible_items},
            sort_keys=True,
            indent=2,
        )
        + "\n"
    ).encode("utf-8")


def _published_item(item, media_keys, thumbnails_by_source, thumbnail_keys):
    thumbnail = thumbnails_by_source.get(item.sha256)
    return {
        "source_id": item.source_id,
        "path": item.path,
        "filename": item.filename,
        "media_type": item.media_type,
        "size": item.size,
        "sha256": item.sha256,
        "mime_type": item.mime_type,
        "mtime_ns": item.mtime_ns,
        "media_key": media_keys[item.sha256],
        "thumbnail": (
            {
                "key": thumbnail_keys[item.sha256],
                "sha256": thumbnail.sha256,
                "size": thumbnail.size,
                "max_dimension": thumbnail.max_dimension,
            }
            if thumbnail is not None
            else None
        ),
    }

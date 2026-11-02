"""Publish verified derived media under immutable, private library object keys."""

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from family_archive.config import Config
from family_archive.library import LibraryIndex, LibraryItem
from family_archive.library_assets import MaterializationResult, verify_cached_file
from family_archive.library_thumbnails import THUMBNAIL_VERSION, ThumbnailAsset
from family_archive.library_video import PLAYBACK_MIME_TYPE, VideoPlaybackAsset
from family_archive.storage import Storage

MAX_SINGLE_PUT_SIZE = 5_000_000_000
PUBLISH_WORKERS = 4


@dataclass(frozen=True, slots=True)
class PublishedLibraryIndex:
    key: str
    sha256: str
    size: int
    item_count: int
    source_count: int
    media_bytes: int
    thumbnail_bytes: int
    playback_bytes: int = 0
    reused_objects: int = 0


def publish_library(
    storage: Storage,
    config: Config,
    index: LibraryIndex,
    materialized: MaterializationResult,
    thumbnails: tuple[ThumbnailAsset, ...],
    playback_assets: tuple[VideoPlaybackAsset, ...] = (),
    *,
    reuse_published_objects: bool = False,
    progress=None,
) -> PublishedLibraryIndex:
    """Verify and conditionally publish derivatives, then publish the index last."""
    assets = {asset.sha256: asset for asset in materialized.assets}
    thumbnails_by_source = {item.source_sha256: item for item in thumbnails}
    playbacks_by_source = {item.source_sha256: item for item in playback_assets}
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
    playback_keys = {
        item.source_sha256: config.key(
            f"library/playback/v{item.version}/{item.sha256[:2]}/{item.sha256}.mp4"
        )
        for item in playback_assets
    }

    if reuse_published_objects:
        previously_verified, previous_playbacks = _previously_verified_objects(storage, config)
    else:
        previously_verified, previous_playbacks = {}, {}

    def publish_group(label, objects):
        reused = 0
        with ThreadPoolExecutor(max_workers=PUBLISH_WORKERS) as executor:
            futures = {
                executor.submit(publish_one, key, path, size, digest): key
                for key, path, size, digest in objects
            }
            for completed, future in enumerate(as_completed(futures), start=1):
                reused += int(future.result())
                if progress:
                    progress(label, completed, len(futures))
        return reused

    def publish_one(key, path, size, digest):
        verify_cached_file(path, size, digest, materialized.cache_verification)
        if previously_verified.get(key) == (digest, size):
            return True
        storage.ensure_content_addressed_file(key, path, digest)
        return False

    media_objects = [
        (key, assets[digest].path, assets[digest].size, digest)
        for digest, key in media_keys.items()
    ]
    for key, _path, size, _digest in media_objects:
        if size > MAX_SINGLE_PUT_SIZE:
            raise ValueError(
                f"Derived asset exceeds the single-PUT S3 limit and needs multipart publishing: "
                f"{key}"
            )
    thumbnail_objects = [
        (key, thumbnails_by_source[source_hash].path, thumbnails_by_source[source_hash].size,
         thumbnails_by_source[source_hash].sha256)
        for source_hash, key in thumbnail_keys.items()
    ]
    playback_objects = [
        (key, playbacks_by_source[source_hash].path, playbacks_by_source[source_hash].size,
         playbacks_by_source[source_hash].sha256)
        for source_hash, key in playback_keys.items()
    ]
    for key, _path, size, _digest in playback_objects:
        if size > MAX_SINGLE_PUT_SIZE:
            raise ValueError(f"Video playback derivative exceeds the 5 GB publish limit: {key}")

    current_video_hashes = {item.sha256 for item in supported_items if item.media_type == "video"}
    preserved_playbacks = {
        digest: playback
        for digest, playback in previous_playbacks.items()
        if digest in current_video_hashes and digest not in playbacks_by_source
    }

    reused_objects = (
        publish_group("Publish media", media_objects)
        + publish_group("Publish thumbnails", thumbnail_objects)
        + publish_group("Publish video playback", playback_objects)
        + len(preserved_playbacks)
    )
    media_bytes = sum(asset.size for asset in assets.values())
    thumbnail_bytes = sum(item.size for item in thumbnails)
    playback_bytes = sum(item.size for item in playback_assets) + sum(
        item["size"] for item in preserved_playbacks.values()
    )
    if materialized.cache_verification:
        materialized.cache_verification.save()

    document = serialize_published_index(
        index,
        supported_items,
        media_keys,
        thumbnails_by_source,
        thumbnail_keys,
        playbacks_by_source,
        playback_keys,
        preserved_playbacks,
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
        playback_bytes=playback_bytes,
        reused_objects=reused_objects,
    )


def _previously_verified_objects(
    storage: Storage, config: Config
) -> tuple[dict[str, tuple[str, int]], dict[str, dict]]:
    """Use the latest integrity-checked published index as an object checkpoint."""
    index_prefix = config.key("library/index/")
    key = storage.latest_key(index_prefix)
    if key is None or not key.startswith(index_prefix) or not key.endswith(".json"):
        return {}, {}
    filename_hash = key.rsplit("/", 1)[-1].removesuffix(".json")
    if len(filename_hash) != 64 or any(char not in "0123456789abcdef" for char in filename_hash):
        return {}, {}
    with storage.open_read(key) as stream:
        document_bytes = stream.read()
    if hashlib.sha256(document_bytes).hexdigest() != filename_hash:
        return {}, {}
    try:
        document = json.loads(document_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}, {}
    if (
        not isinstance(document, dict)
        or document.get("schema_version") != 1
        or not isinstance(document.get("items"), list)
    ):
        return {}, {}

    verified = {}
    previous_playbacks = {}
    for item in document["items"]:
        if not isinstance(item, dict):
            continue
        digest = item.get("sha256")
        size = item.get("size")
        if not _valid_digest(digest) or not _valid_size(size):
            continue
        expected_media_key = config.key(f"library/media/{digest[:2]}/{digest}")
        if item.get("media_key") == expected_media_key:
            verified[expected_media_key] = (digest, size)

        thumbnail = item.get("thumbnail")
        if isinstance(thumbnail, dict):
            thumb_digest = thumbnail.get("sha256")
            thumb_size = thumbnail.get("size")
            dimension = thumbnail.get("max_dimension")
            expected_key = (
                config.key(f"library/thumbnails/v{THUMBNAIL_VERSION}/{dimension}/{digest}.jpg")
                if _valid_size(dimension) and 64 <= dimension <= 2048
                else None
            )
            if (
                expected_key is not None
                and thumbnail.get("key") == expected_key
                and _valid_digest(thumb_digest)
                and _valid_size(thumb_size)
            ):
                verified[expected_key] = (thumb_digest, thumb_size)

        playback = item.get("playback")
        if isinstance(playback, dict):
            playback_digest = playback.get("sha256")
            playback_size = playback.get("size")
            version = playback.get("version")
            expected_key = (
                config.key(
                    f"library/playback/v{version}/{playback_digest[:2]}/{playback_digest}.mp4"
                )
                if version == 1 and _valid_digest(playback_digest)
                else None
            )
            if (
                expected_key is not None
                and playback.get("key") == expected_key
                and playback.get("mime_type") == PLAYBACK_MIME_TYPE
                and _valid_size(playback_size)
            ):
                verified[expected_key] = (playback_digest, playback_size)
                previous_playbacks[digest] = {
                    "key": expected_key,
                    "sha256": playback_digest,
                    "size": playback_size,
                    "mime_type": PLAYBACK_MIME_TYPE,
                    "version": version,
                }
    return verified, previous_playbacks


def _valid_digest(value) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


def _valid_size(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def serialize_published_index(
    index: LibraryIndex,
    items: tuple[LibraryItem, ...],
    media_keys: dict[str, str],
    thumbnails_by_source: dict[str, ThumbnailAsset],
    thumbnail_keys: dict[str, str],
    playbacks_by_source: dict[str, VideoPlaybackAsset] | None = None,
    playback_keys: dict[str, str] | None = None,
    preserved_playbacks: dict[str, dict] | None = None,
) -> bytes:
    """Create a deterministic public-library document without archive locators."""
    visible_items = tuple(
        _published_item(
            item,
            media_keys,
            thumbnails_by_source,
            thumbnail_keys,
            playbacks_by_source or {},
            playback_keys or {},
            preserved_playbacks or {},
        )
        for item in items
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


def _published_item(
    item,
    media_keys,
    thumbnails_by_source,
    thumbnail_keys,
    playbacks_by_source,
    playback_keys,
    preserved_playbacks,
):
    thumbnail = thumbnails_by_source.get(item.sha256)
    playback = playbacks_by_source.get(item.sha256)
    published = {
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
    if playback is not None:
        published["playback"] = {
            "key": playback_keys[item.sha256],
            "sha256": playback.sha256,
            "size": playback.size,
            "mime_type": PLAYBACK_MIME_TYPE,
            "version": playback.version,
        }
    elif item.sha256 in preserved_playbacks:
        published["playback"] = preserved_playbacks[item.sha256]
    return published

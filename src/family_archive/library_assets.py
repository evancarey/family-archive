"""Create verified, content-addressed local copies for the derived library."""

import hashlib
import io
import os
import stat
import tempfile
from collections import defaultdict
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from family_archive.cache_verification import CacheVerification
from family_archive.hashing import CHUNK_SIZE, hash_stream
from family_archive.image_mount import mount_image
from family_archive.library import LibraryIndex, LibraryItem
from family_archive.multisession import Track, inventory_sessions
from family_archive.storage import ArchiveReader

MATERIALIZE_WORKERS = 4


@dataclass(frozen=True, slots=True)
class DerivedAsset:
    sha256: str
    size: int
    path: Path


@dataclass(frozen=True, slots=True)
class MaterializationResult:
    assets: tuple[DerivedAsset, ...]
    unsupported_items: tuple[str, ...] = ()
    reused_assets: int = 0
    cache_verification: CacheVerification | None = None


def materialize_library(
    reader: ArchiveReader,
    index: LibraryIndex,
    cache_dir: Path,
    *,
    reuse_existing: bool = False,
    progress=None,
) -> MaterializationResult:
    """Copy indexed media locally and verify bytes against their source manifests.

    The cache is content-addressed, so exact duplicates share a derived file while
    the index retains every source path and its provenance.
    """
    cache = _prepare_cache(cache_dir)
    verification = CacheVerification(cache)
    assets: dict[str, DerivedAsset] = {}
    image_items: dict[str, list[LibraryItem]] = defaultdict(list)
    multisession_items: dict[str, list[LibraryItem]] = defaultdict(list)
    unsupported_items = []
    reused_hashes: set[str] = set()
    pending_objects = []
    scheduled_hashes: set[str] = set()

    def report_cache_progress(item_number):
        if progress and (item_number % 100 == 0 or item_number == len(index.items)):
            progress("Check media cache", item_number, len(index.items))

    for item_number, item in enumerate(index.items, start=1):
        reference = item.original
        if reference.kind == "file_object":
            if reuse_existing and item.sha256 in assets:
                report_cache_progress(item_number)
                continue
            if reuse_existing and item.sha256 in scheduled_hashes:
                report_cache_progress(item_number)
                continue
            target = _asset_path(cache, item.sha256) if reuse_existing else None
            if target is not None and (target.exists() or target.is_symlink()):
                verify_cached_file(target, item.size, item.sha256, verification)
                assets[item.sha256] = DerivedAsset(item.sha256, item.size, target)
                reused_hashes.add(item.sha256)
            else:
                pending_objects.append(item)
                if reuse_existing:
                    scheduled_hashes.add(item.sha256)
        elif reference.kind == "image_entry":
            target = _asset_path(cache, item.sha256) if reuse_existing else None
            if target is not None and (target.exists() or target.is_symlink()):
                verify_cached_file(target, item.size, item.sha256, verification)
                assets[item.sha256] = DerivedAsset(item.sha256, item.size, target)
                reused_hashes.add(item.sha256)
            else:
                image_items[reference.object_key].append(item)
        elif reference.kind == "multisession_image_entry":
            target = _asset_path(cache, item.sha256) if reuse_existing else None
            if target is not None and (target.exists() or target.is_symlink()):
                verify_cached_file(target, item.size, item.sha256, verification)
                assets[item.sha256] = DerivedAsset(item.sha256, item.size, target)
                reused_hashes.add(item.sha256)
            else:
                multisession_items[reference.object_key].append(item)
        else:  # guarded above; keep the dispatch exhaustive
            raise ValueError(f"Unsupported archive reference: {reference.kind}")
        report_cache_progress(item_number)

    if pending_objects:
        with ThreadPoolExecutor(max_workers=MATERIALIZE_WORKERS) as executor:
            futures = {
                executor.submit(
                    _materialize_reader_object,
                    reader,
                    item.original.object_key,
                    item.size,
                    item.sha256,
                    cache,
                ): item
                for item in pending_objects
            }
            fetch_error = None
            for completed, future in enumerate(as_completed(futures), start=1):
                item = futures[future]
                try:
                    asset = future.result()
                except Exception as exc:
                    if fetch_error is None:
                        fetch_error = exc
                    if progress:
                        progress("Fetch and verify media", completed, len(pending_objects))
                    continue
                assets[item.sha256] = asset
                verification.remember(asset.path, asset.size, asset.sha256)
                if completed % 25 == 0:
                    verification.save()
                if progress:
                    progress("Fetch and verify media", completed, len(pending_objects))
            verification.save()
            if fetch_error is not None:
                raise fetch_error

    for object_key in sorted(image_items):
        group = [item for item in image_items[object_key] if item.sha256 not in assets]
        if not group:
            continue
        assets.update(_materialize_image(reader, group, cache))

    for object_key in sorted(multisession_items):
        group = [item for item in multisession_items[object_key] if item.sha256 not in assets]
        if group:
            assets.update(_materialize_multisession(reader, group, cache))

    for asset in assets.values():
        verification.remember(asset.path, asset.size, asset.sha256)
    verification.save()

    return MaterializationResult(
        assets=tuple(assets[key] for key in sorted(assets)),
        unsupported_items=tuple(unsupported_items),
        reused_assets=len(reused_hashes),
        cache_verification=verification,
    )


def load_materialized_cache(
    index: LibraryIndex, cache_dir: Path, progress=None
) -> MaterializationResult:
    """Re-hash an existing cache against the current manifest index without S3 reads."""
    cache = _prepare_cache(cache_dir)
    verification = CacheVerification(cache)
    sizes: dict[str, int] = {}
    unsupported_items = []
    for item in index.items:
        if item.original.kind not in {"file_object", "image_entry", "multisession_image_entry"}:
            raise ValueError(f"Unsupported archive reference: {item.original.kind}")
        previous_size = sizes.setdefault(item.sha256, item.size)
        if previous_size != item.size:
            raise ValueError(f"Conflicting file sizes for content hash {item.sha256}")

    assets = []
    for completed, (digest, size) in enumerate(sorted(sizes.items()), start=1):
        path = _asset_path(cache, digest)
        _require_matching_file(path, size, digest)
        verification.remember(path, size, digest)
        assets.append(DerivedAsset(digest, size, path))
        if progress and (completed % 100 == 0 or completed == len(sizes)):
            progress("Audit local media cache", completed, len(sizes))
    verification.save()
    return MaterializationResult(
        tuple(assets), tuple(unsupported_items), cache_verification=verification
    )


def verify_cached_file(
    path: Path,
    expected_size: int,
    expected_hash: str,
    verification: CacheVerification | None,
) -> None:
    """Reuse a prior local hash only while its private file identity is unchanged."""
    if verification and verification.matches(path, expected_size, expected_hash):
        return
    _require_matching_file(path, expected_size, expected_hash)
    if verification:
        verification.remember(path, expected_size, expected_hash)


def _materialize_multisession(
    reader: ArchiveReader, items: list[LibraryItem], cache: Path
) -> dict[str, DerivedAsset]:
    reference = items[0].original
    identity = (
        reference.object_key,
        reference.object_size,
        reference.object_sha256,
        reference.tracks,
    )
    if not reference.tracks or any(
        (
            item.original.object_key,
            item.original.object_size,
            item.original.object_sha256,
            item.original.tracks,
        )
        != identity
        for item in items
    ):
        raise ValueError(f"Conflicting multisession metadata for {reference.object_key}")

    staging = cache / ".staging"
    _ensure_private_directory(staging)
    image = staging / f"{reference.object_sha256}.multisession.img"
    if image.exists() or image.is_symlink():
        _require_matching_file(image, reference.object_size, reference.object_sha256)
        with reader.open_read(reference.object_key) as stream:
            _verify_stream(
                stream,
                None,
                reference.object_size,
                reference.object_sha256,
                reference.object_key,
            )
    else:
        temporary = _new_temporary(staging)
        try:
            with reader.open_read(reference.object_key) as stream:
                _verify_stream(
                    stream,
                    temporary,
                    reference.object_size,
                    reference.object_sha256,
                    reference.object_key,
                )
            _publish_no_replace(temporary, image, reference.object_size, reference.object_sha256)
        finally:
            temporary.unlink(missing_ok=True)

    tracks = []
    for metadata in reference.tracks:
        track_path = staging / f"{reference.object_sha256}.session-{metadata['session']:02d}.bin"
        with image.open("rb") as source:
            source.seek(metadata["offset_bytes"])
            data = source.read(metadata["size"])
        if len(data) != metadata["size"] or hashlib.sha256(data).hexdigest() != metadata["sha256"]:
            raise ValueError(
                f"Session {metadata['session']} differs from the archived track checksum"
            )
        if track_path.exists() or track_path.is_symlink():
            _require_matching_file(track_path, len(data), metadata["sha256"])
        else:
            temporary = _new_temporary(staging)
            try:
                with temporary.open("wb") as output:
                    output.write(data)
                    output.flush()
                    os.fsync(output.fileno())
                _publish_no_replace(temporary, track_path, len(data), metadata["sha256"])
            finally:
                temporary.unlink(missing_ok=True)
        tracks.append(
            Track(
                metadata["session"],
                metadata["track"],
                metadata["start_lba"],
                metadata["sectors"],
                track_path,
                metadata["sha256"],
                metadata["toc"],
            )
        )

    requested = {(item.original.path): item for item in items}
    result: dict[str, DerivedAsset] = {}
    seen = set()

    def save_file(entry, payload):
        item = requested.get(entry["path"])
        if item is None:
            return
        asset = _materialize_stream(
            io.BytesIO(payload), cache, item.size, item.sha256, f"{item.source_id}:{item.path}"
        )
        result[item.sha256] = asset
        seen.add(item.path)

    try:
        inventory_sessions(tracks, on_file=save_file)
        missing = set(requested) - seen
        if missing:
            raise ValueError(
                "Multisession manifest path was not found in the session tree: "
                f"{sorted(missing)[0]}"
            )
    finally:
        image.unlink(missing_ok=True)
        for track in tracks:
            track.path.unlink(missing_ok=True)
    return result


def _prepare_cache(path: Path) -> Path:
    requested = Path(path).expanduser()
    if requested.is_symlink():
        raise ValueError(f"Library cache directory must not be a symlink: {requested}")
    cache = requested.resolve()
    volumes = Path("/Volumes").resolve()
    if cache == volumes or cache.is_relative_to(volumes):
        raise ValueError("Library cache must not be written to mounted source media")
    _ensure_private_directory(cache, parents=True)
    return cache


def _asset_path(cache: Path, digest: str) -> Path:
    directory = cache / digest[:2]
    _ensure_private_directory(directory)
    return directory / digest


def _ensure_private_directory(path: Path, *, parents: bool = False) -> None:
    if path.is_symlink():
        raise ValueError(f"Library directory must not be a symlink: {path}")
    path.mkdir(mode=0o700, parents=parents, exist_ok=True)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError(f"Library cache path is not a directory: {path}")
    if info.st_mode & 0o077:
        raise ValueError(f"Library cache directory must be private (mode 700): {path}")


def _materialize_reader_object(
    reader: ArchiveReader, key: str, expected_size: int, expected_hash: str, cache: Path
) -> DerivedAsset:
    target = _asset_path(cache, expected_hash)
    existing = target.exists() or target.is_symlink()
    if existing:
        _require_matching_file(target, expected_size, expected_hash)
        with reader.open_read(key) as stream:
            _verify_stream(stream, None, expected_size, expected_hash, key)
        return DerivedAsset(expected_hash, expected_size, target)

    temporary = _new_temporary(target.parent)
    try:
        with reader.open_read(key) as stream:
            _verify_stream(stream, temporary, expected_size, expected_hash, key)
        _publish_no_replace(temporary, target, expected_size, expected_hash)
    finally:
        temporary.unlink(missing_ok=True)
    return DerivedAsset(expected_hash, expected_size, target)


def _materialize_image(
    reader: ArchiveReader, items: list[LibraryItem], cache: Path
) -> dict[str, DerivedAsset]:
    reference = items[0].original
    image_identity = (
        reference.kind,
        reference.object_key,
        reference.object_size,
        reference.object_sha256,
        reference.filesystem,
    )
    if any(
        (
            item.original.kind,
            item.original.object_key,
            item.original.object_size,
            item.original.object_sha256,
            item.original.filesystem,
        )
        != image_identity
        for item in items
    ):
        raise ValueError(f"Conflicting image metadata for {reference.object_key}")
    if not reference.filesystem:
        raise ValueError(f"Missing image filesystem metadata for {reference.object_key}")

    staging = cache / ".staging"
    _ensure_private_directory(staging)
    image = staging / f"{reference.object_sha256}.img"
    image_exists = image.exists() or image.is_symlink()

    if image_exists:
        _require_matching_file(image, reference.object_size, reference.object_sha256)
        with reader.open_read(reference.object_key) as stream:
            _verify_stream(
                stream,
                None,
                reference.object_size,
                reference.object_sha256,
                reference.object_key,
            )
    else:
        temporary = _new_temporary(staging)
        try:
            with reader.open_read(reference.object_key) as stream:
                _verify_stream(
                    stream,
                    temporary,
                    reference.object_size,
                    reference.object_sha256,
                    reference.object_key,
                )
            _publish_no_replace(
                temporary,
                image,
                reference.object_size,
                reference.object_sha256,
            )
        finally:
            temporary.unlink(missing_ok=True)

    result = {}
    with mount_image(image, reference.filesystem) as mount_point:
        for item in items:
            with open_manifest_file(mount_point, item.path) as stream:
                asset = _materialize_stream(
                    stream,
                    cache,
                    item.size,
                    item.sha256,
                    f"{item.source_id}:{item.path}",
                )
            result[item.sha256] = asset
    image.unlink()
    return result


def _materialize_stream(
    stream: BinaryIO, cache: Path, expected_size: int, expected_hash: str, label: str
) -> DerivedAsset:
    target = _asset_path(cache, expected_hash)
    if target.exists() or target.is_symlink():
        _require_matching_file(target, expected_size, expected_hash)
        _verify_stream(stream, None, expected_size, expected_hash, label)
        return DerivedAsset(expected_hash, expected_size, target)

    temporary = _new_temporary(target.parent)
    try:
        _verify_stream(stream, temporary, expected_size, expected_hash, label)
        _publish_no_replace(temporary, target, expected_size, expected_hash)
    finally:
        temporary.unlink(missing_ok=True)
    return DerivedAsset(expected_hash, expected_size, target)


def _new_temporary(directory: Path) -> Path:
    descriptor, name = tempfile.mkstemp(prefix=".family-archive-partial-", dir=directory)
    os.fchmod(descriptor, 0o600)
    os.close(descriptor)
    return Path(name)


def _verify_stream(
    stream: BinaryIO,
    output: Path | None,
    expected_size: int,
    expected_hash: str,
    label: str,
) -> None:
    digest = hashlib.sha256()
    size = 0
    destination = output.open("wb") if output is not None else None
    try:
        while chunk := stream.read(CHUNK_SIZE):
            size += len(chunk)
            if size > expected_size:
                raise ValueError(f"{label}: data exceeds the manifest size")
            digest.update(chunk)
            if destination is not None:
                destination.write(chunk)
        if size != expected_size or digest.hexdigest() != expected_hash:
            raise ValueError(f"{label}: SHA-256 or size differs from the manifest")
        if destination is not None:
            destination.flush()
            os.fsync(destination.fileno())
    finally:
        if destination is not None:
            destination.close()


def _publish_no_replace(
    temporary: Path, target: Path, expected_size: int, expected_hash: str
) -> None:
    try:
        os.link(temporary, target, follow_symlinks=False)
    except FileExistsError:
        _require_matching_file(target, expected_size, expected_hash)


def _require_matching_file(path: Path, expected_size: int, expected_hash: str) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise ValueError(f"Refusing unsafe or unreadable derived asset: {path}") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"Refusing non-regular derived asset: {path}")
        if info.st_mode & 0o077:
            raise ValueError(f"Derived asset must be private (mode 600): {path}")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            size, digest = hash_stream(stream)
    finally:
        os.close(descriptor)
    if size != expected_size or digest != expected_hash:
        raise ValueError(f"Existing derived asset differs from manifest; not overwritten: {path}")


@contextmanager
def open_manifest_file(root: Path, relative_path: str) -> Iterator[BinaryIO]:
    """Open one regular file beneath a mounted image without following links."""
    relative = PurePosixPath(relative_path)
    if (
        not relative.parts
        or relative.is_absolute()
        or ".." in relative.parts
        or "\x00" in relative_path
    ):
        raise ValueError(f"Unsafe manifest media path: {relative_path}")

    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    directory = os.open(root, directory_flags)
    try:
        for component in relative.parts[:-1]:
            child = os.open(component, directory_flags, dir_fd=directory)
            os.close(directory)
            directory = child
        descriptor = os.open(relative.parts[-1], file_flags, dir_fd=directory)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            raise ValueError(f"Manifest path is not a regular file: {relative_path}")
        with os.fdopen(descriptor, "rb") as stream:
            yield stream
    finally:
        os.close(directory)

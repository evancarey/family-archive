"""Create small, replaceable JPEG thumbnails with macOS's native sips tool."""

import hashlib
import os
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from family_archive.library import LibraryIndex
from family_archive.library_assets import (
    MaterializationResult,
    _ensure_private_directory,
    _publish_no_replace,
    _require_matching_file,
)

THUMBNAIL_VERSION = 1
DEFAULT_MAX_DIMENSION = 512
SIPS_PATH = "/usr/bin/sips"


@dataclass(frozen=True, slots=True)
class ThumbnailAsset:
    source_sha256: str
    sha256: str
    size: int
    max_dimension: int
    path: Path


@dataclass(frozen=True, slots=True)
class ThumbnailGenerationResult:
    assets: tuple[ThumbnailAsset, ...]
    failed_items: tuple[str, ...] = ()


def generate_thumbnails(
    index: LibraryIndex,
    materialized: MaterializationResult,
    output_dir: Path,
    max_dimension: int = DEFAULT_MAX_DIMENSION,
) -> ThumbnailGenerationResult:
    """Generate one bounded JPEG derivative for every supported unique photo."""
    if type(max_dimension) is not int or not 64 <= max_dimension <= 2048:
        raise ValueError("Thumbnail maximum dimension must be between 64 and 2048 pixels")
    executable = SIPS_PATH
    if not Path(executable).is_file() or shutil.which(executable) is None:
        raise RuntimeError("Thumbnail generation requires macOS /usr/bin/sips")

    assets = {asset.sha256: asset for asset in materialized.assets}
    photos = {item.sha256: item for item in index.items if item.media_type == "photo"}
    output = Path(output_dir).expanduser()
    if output.is_symlink():
        raise ValueError(f"Thumbnail cache directory must not be a symlink: {output}")
    output = output.resolve()
    if output == Path("/Volumes").resolve() or output.is_relative_to(Path("/Volumes").resolve()):
        raise ValueError("Thumbnail cache must not be written to mounted source media")
    version_dir = output / f"v{THUMBNAIL_VERSION}" / str(max_dimension)
    _ensure_private_directory(version_dir, parents=True)

    thumbnails = []
    failed_items = []
    for source_hash in sorted(photos):
        asset = assets.get(source_hash)
        item = photos[source_hash]
        if asset is None:
            raise ValueError(f"No verified local media cache for {item.source_id}:{item.path}")
        _require_matching_file(asset.path, asset.size, asset.sha256)
        target = version_dir / f"{source_hash}.jpg"
        if target.exists() or target.is_symlink():
            size, digest = _hash_thumbnail(target, item.source_id, item.path)
            info = target.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
                raise ValueError(f"Cached thumbnail is not a private regular file: {target}")
            thumbnails.append(ThumbnailAsset(source_hash, digest, size, max_dimension, target))
            continue
        temporary = _new_temporary(version_dir)
        try:
            try:
                _run_sips(
                    executable, asset.path, temporary, max_dimension, item.source_id, item.path
                )
            except RuntimeError as exc:
                failed_items.append(f"{item.source_id}  {item.path}: {exc}")
                continue
            os.chmod(temporary, 0o600)
            size, digest = _hash_thumbnail(temporary, item.source_id, item.path)
            _publish_no_replace(temporary, target, size, digest)
        finally:
            temporary.unlink(missing_ok=True)
        thumbnails.append(ThumbnailAsset(source_hash, digest, size, max_dimension, target))
    return ThumbnailGenerationResult(tuple(thumbnails), tuple(failed_items))


def _new_temporary(directory: Path) -> Path:
    descriptor, name = tempfile.mkstemp(prefix=".family-archive-thumb-", dir=directory)
    os.close(descriptor)
    Path(name).unlink()
    return Path(name)


def _run_sips(executable: str, source: Path, output: Path, dimension: int, source_id, path):
    try:
        result = subprocess.run(
            [
                executable,
                "-s",
                "format",
                "jpeg",
                "-Z",
                str(dimension),
                str(source),
                "--out",
                str(output),
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"{source_id}:{path}: sips thumbnail conversion failed: {exc}") from exc
    if result.returncode:
        detail = result.stderr.decode(errors="replace").strip()
        raise RuntimeError(
            f"{source_id}:{path}: sips could not create a thumbnail: {detail or 'unknown error'}"
        )


def _hash_thumbnail(path: Path, source_id: str, relative_path: str) -> tuple[int, str]:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise RuntimeError(f"{source_id}:{relative_path}: thumbnail output is unsafe") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size < 4:
            raise RuntimeError(f"{source_id}:{relative_path}: sips returned an invalid thumbnail")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            start = stream.read(3)
            stream.seek(-2, os.SEEK_END)
            end = stream.read(2)
            if start != b"\xff\xd8\xff" or end != b"\xff\xd9":
                raise RuntimeError(f"{source_id}:{relative_path}: sips output is not a JPEG")
            stream.seek(0)
            digest = hashlib.sha256()
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
            return info.st_size, digest.hexdigest()
    finally:
        os.close(descriptor)

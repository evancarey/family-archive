"""Create small, replaceable JPEG thumbnails with macOS's native sips tool."""

import hashlib
import os
import shutil
import stat
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from family_archive.library import LibraryIndex
from family_archive.library_assets import (
    MaterializationResult,
    _ensure_private_directory,
    _publish_no_replace,
    verify_cached_file,
)

THUMBNAIL_VERSION = 1
DEFAULT_MAX_DIMENSION = 512
SIPS_PATH = "/usr/bin/sips"
FFMPEG_PATH = "ffmpeg"
THUMBNAIL_WORKERS = 4


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
    *,
    include_videos: bool = False,
    progress=None,
) -> ThumbnailGenerationResult:
    """Generate bounded JPEG derivatives for photos and, optionally, video frames."""
    if type(max_dimension) is not int or not 64 <= max_dimension <= 2048:
        raise ValueError("Thumbnail maximum dimension must be between 64 and 2048 pixels")
    executable = SIPS_PATH
    if not Path(executable).is_file() or shutil.which(executable) is None:
        raise RuntimeError("Thumbnail generation requires macOS /usr/bin/sips")

    assets = {asset.sha256: asset for asset in materialized.assets}
    media = {
        item.sha256: item
        for item in index.items
        if item.media_type == "photo" or (include_videos and item.media_type == "video")
    }
    videos = {digest: item for digest, item in media.items() if item.media_type == "video"}
    ffmpeg = shutil.which(FFMPEG_PATH) if videos else None
    output = Path(output_dir).expanduser()
    if output.is_symlink():
        raise ValueError(f"Thumbnail cache directory must not be a symlink: {output}")
    output = output.resolve()
    if output == Path("/Volumes").resolve() or output.is_relative_to(Path("/Volumes").resolve()):
        raise ValueError("Thumbnail cache must not be written to mounted source media")
    version_dir = output / f"v{THUMBNAIL_VERSION}" / str(max_dimension)
    _ensure_private_directory(version_dir, parents=True)

    work_items = sorted(media.items())

    def generate(source_hash, item):
        asset = assets.get(source_hash)
        if asset is None:
            raise ValueError(f"No verified local media cache for {item.source_id}:{item.path}")
        verification = materialized.cache_verification
        verify_cached_file(asset.path, asset.size, asset.sha256, verification)
        target = version_dir / f"{source_hash}.jpg"
        cached = verification.get(target) if verification and target.exists() else None
        if cached is not None:
            return ThumbnailAsset(source_hash, cached[1], cached[0], max_dimension, target), None
        if target.exists() or target.is_symlink():
            size, digest = _hash_thumbnail(target, item.source_id, item.path)
            info = target.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
                raise ValueError(f"Cached thumbnail is not a private regular file: {target}")
            if verification:
                verification.remember(target, size, digest)
            return ThumbnailAsset(source_hash, digest, size, max_dimension, target), None

        temporary = _new_temporary(version_dir)
        try:
            try:
                if item.media_type == "video":
                    if ffmpeg is None:
                        raise RuntimeError(
                            f"{item.source_id}:{item.path}: video thumbnails require ffmpeg"
                        )
                    _run_ffmpeg_thumbnail(
                        ffmpeg, asset.path, temporary, max_dimension, item.source_id, item.path
                    )
                else:
                    _run_sips(
                        executable, asset.path, temporary, max_dimension, item.source_id, item.path
                    )
            except RuntimeError as exc:
                return None, f"{item.source_id}  {item.path}: {exc}"
            os.chmod(temporary, 0o600)
            size, digest = _hash_thumbnail(temporary, item.source_id, item.path)
            _publish_no_replace(temporary, target, size, digest)
        finally:
            temporary.unlink(missing_ok=True)
        if verification:
            verification.remember(target, size, digest)
        return ThumbnailAsset(source_hash, digest, size, max_dimension, target), None

    thumbnails = []
    failed_items = []
    with ThreadPoolExecutor(max_workers=THUMBNAIL_WORKERS) as executor:
        futures = {
            executor.submit(generate, source_hash, item): (source_hash, item)
            for source_hash, item in work_items
        }
        for completed, future in enumerate(as_completed(futures), start=1):
            asset, failure = future.result()
            if asset is not None:
                thumbnails.append(asset)
            if failure:
                failed_items.append(failure)
            if progress:
                progress("Generate thumbnails", completed, len(work_items))
    verification = materialized.cache_verification
    if verification:
        verification.save()
    return ThumbnailGenerationResult(
        tuple(sorted(thumbnails, key=lambda item: item.source_sha256)), tuple(failed_items)
    )


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


def _run_ffmpeg_thumbnail(
    executable: str, source: Path, output: Path, dimension: int, source_id, path
):
    failures = []
    for seek in ("3", "0"):
        try:
            result = subprocess.run(
                [
                    executable,
                    "-nostdin",
                    "-v",
                    "error",
                    "-y",
                    "-ss",
                    seek,
                    "-i",
                    str(source),
                    "-map",
                    "0:v:0",
                    "-frames:v",
                    "1",
                    "-vf",
                    f"scale={dimension}:{dimension}:force_original_aspect_ratio=decrease",
                    "-q:v",
                    "3",
                    "-f",
                    "image2",
                    "-update",
                    "1",
                    "-vcodec",
                    "mjpeg",
                    str(output),
                ],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=180,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            failures.append(str(exc))
            continue
        if result.returncode == 0 and _is_jpeg(output):
            return
        detail = result.stderr.decode(errors="replace").strip()
        failures.append(detail or "no decodable frame at this timestamp")
    raise RuntimeError(
        f"{source_id}:{path}: ffmpeg could not create a video thumbnail: {failures[-1]}"
    )


def _is_jpeg(path: Path) -> bool:
    try:
        with path.open("rb") as stream:
            if stream.read(3) != b"\xff\xd8\xff":
                return False
            stream.seek(-2, os.SEEK_END)
            return stream.read(2) == b"\xff\xd9"
    except OSError:
        return False


def _hash_thumbnail(path: Path, source_id: str, relative_path: str) -> tuple[int, str]:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise RuntimeError(f"{source_id}:{relative_path}: thumbnail output is unsafe") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size < 4:
            raise RuntimeError(
                f"{source_id}:{relative_path}: converter returned an invalid thumbnail"
            )
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            start = stream.read(3)
            stream.seek(-2, os.SEEK_END)
            end = stream.read(2)
            if start != b"\xff\xd8\xff" or end != b"\xff\xd9":
                raise RuntimeError(f"{source_id}:{relative_path}: thumbnail output is not a JPEG")
            stream.seek(0)
            digest = hashlib.sha256()
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
            return info.st_size, digest.hexdigest()
    finally:
        os.close(descriptor)

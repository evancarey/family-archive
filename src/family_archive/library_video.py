"""Create replaceable MP4 playback copies without changing archived videos."""

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

PLAYBACK_VERSION = 1
PLAYBACK_MIME_TYPE = "video/mp4"
FFMPEG_PATH = "ffmpeg"
FFPROBE_PATH = "ffprobe"
MAX_SINGLE_PUT_SIZE = 5_000_000_000
VIDEO_WORKERS = 2


@dataclass(frozen=True, slots=True)
class VideoPlaybackAsset:
    source_sha256: str
    sha256: str
    size: int
    path: Path
    version: int = PLAYBACK_VERSION


@dataclass(frozen=True, slots=True)
class VideoPlaybackResult:
    assets: tuple[VideoPlaybackAsset, ...]
    failed_items: tuple[str, ...] = ()


def generate_video_playbacks(
    index: LibraryIndex,
    materialized: MaterializationResult,
    output_dir: Path,
    progress=None,
) -> VideoPlaybackResult:
    """Encode unique indexed videos as browser-friendly H.264/AAC MP4 derivatives."""
    ffmpeg = shutil.which(FFMPEG_PATH)
    ffprobe = shutil.which(FFPROBE_PATH)
    if not ffmpeg or not ffprobe:
        raise RuntimeError(
            "Video playback copies require ffmpeg and ffprobe. "
            "Install them with: brew install ffmpeg"
        )

    output = Path(output_dir).expanduser()
    if output.is_symlink():
        raise ValueError(f"Video playback cache directory must not be a symlink: {output}")
    output = output.resolve()
    volumes = Path("/Volumes").resolve()
    if output == volumes or output.is_relative_to(volumes):
        raise ValueError("Video playback cache must not be written to mounted source media")

    assets = {asset.sha256: asset for asset in materialized.assets}
    videos = {item.sha256: item for item in index.items if item.media_type == "video"}
    version_dir = output / f"v{PLAYBACK_VERSION}"
    _ensure_private_directory(version_dir, parents=True)
    work_items = sorted(videos.items())

    def generate(source_hash, item):
        item = videos[source_hash]
        source = assets.get(source_hash)
        if source is None:
            raise ValueError(f"No verified local media cache for {item.source_id}:{item.path}")
        verification = materialized.cache_verification
        verify_cached_file(source.path, item.size, source_hash, verification)
        target = version_dir / f"{source_hash}.mp4"
        cached = verification.get(target) if verification and target.exists() else None
        if cached is not None:
            return VideoPlaybackAsset(source_hash, cached[1], cached[0], target), None
        if target.exists() or target.is_symlink():
            size, digest = _hash_playback(target, ffprobe, item.source_id, item.path)
            info = target.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
                raise ValueError(f"Cached video playback copy is not private: {target}")
            if verification:
                verification.remember(target, size, digest)
            return VideoPlaybackAsset(source_hash, digest, size, target), None

        temporary = _new_temporary(version_dir)
        try:
            try:
                _run_ffmpeg(ffmpeg, source.path, temporary, item.source_id, item.path)
                size, digest = _hash_playback(temporary, ffprobe, item.source_id, item.path)
                if size > MAX_SINGLE_PUT_SIZE:
                    return None, (
                        f"{item.source_id}  {item.path}: playback copy exceeds "
                        "the 5 GB publish limit"
                    )
                os.chmod(temporary, 0o600)
                _publish_no_replace(temporary, target, size, digest)
            except RuntimeError as exc:
                return None, f"{item.source_id}  {item.path}: {exc}"
        finally:
            temporary.unlink(missing_ok=True)
        if verification:
            verification.remember(target, size, digest)
        return VideoPlaybackAsset(source_hash, digest, size, target), None

    results = []
    failures = []
    with ThreadPoolExecutor(max_workers=VIDEO_WORKERS) as executor:
        futures = {
            executor.submit(generate, source_hash, item): (source_hash, item)
            for source_hash, item in work_items
        }
        for completed, future in enumerate(as_completed(futures), start=1):
            asset, failure = future.result()
            if asset is not None:
                results.append(asset)
            if failure:
                failures.append(failure)
            if progress:
                progress("Prepare video playback", completed, len(work_items))
    verification = materialized.cache_verification
    if verification:
        verification.save()
    return VideoPlaybackResult(
        tuple(sorted(results, key=lambda item: item.source_sha256)), tuple(failures)
    )


def _new_temporary(directory: Path) -> Path:
    descriptor, name = tempfile.mkstemp(prefix=".family-archive-video-", dir=directory)
    os.close(descriptor)
    Path(name).unlink()
    return Path(name)


def _run_ffmpeg(executable: str, source: Path, output: Path, source_id: str, path: str) -> None:
    try:
        result = subprocess.run(
            [
                executable,
                "-nostdin",
                "-v",
                "error",
                "-y",
                "-i",
                str(source),
                "-map",
                "0:v:0",
                "-map",
                "0:a?",
                "-vf",
                "scale=out_range=tv",
                "-c:v",
                "libx264",
                "-preset",
                "medium",
                "-crf",
                "23",
                "-pix_fmt",
                "yuv420p",
                "-color_range",
                "tv",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                "-movflags",
                "+faststart",
                "-f",
                "mp4",
                str(output),
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=3600,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"ffmpeg conversion failed: {exc}") from exc
    if result.returncode:
        detail = result.stderr.decode(errors="replace").strip()
        raise RuntimeError(f"ffmpeg could not create an MP4: {detail or 'unknown error'}")


def _hash_playback(path: Path, ffprobe: str, source_id: str, relative_path: str) -> tuple[int, str]:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise RuntimeError(f"{source_id}:{relative_path}: playback output is unsafe") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size < 16:
            raise RuntimeError(f"{source_id}:{relative_path}: ffmpeg returned an invalid MP4")
        try:
            result = subprocess.run(
                [
                    ffprobe,
                    "-v",
                    "error",
                    "-select_streams",
                    "v:0",
                    "-show_entries",
                    "stream=codec_name,pix_fmt,width,height",
                    "-of",
                    "default=noprint_wrappers=1",
                    str(path),
                ],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=60,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"ffprobe could not validate the MP4: {exc}") from exc
        details = result.stdout.decode(errors="replace")
        values = dict(
            line.split("=", 1) for line in details.splitlines() if "=" in line
        )
        if (
            result.returncode
            or values.get("codec_name") != "h264"
            or values.get("pix_fmt") != "yuv420p"
            or not values.get("width", "").isdigit()
            or not values.get("height", "").isdigit()
        ):
            error = result.stderr.decode(errors="replace").strip()
            raise RuntimeError(
                f"{source_id}:{relative_path}: ffmpeg output is not browser-compatible H.264 MP4"
                + (f": {error}" if error else "")
            )
        digest = hashlib.sha256()
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        return info.st_size, digest.hexdigest()
    finally:
        os.close(descriptor)

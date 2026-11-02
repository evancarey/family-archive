import hashlib
from pathlib import Path

import pytest

from family_archive.library import build_library_index
from family_archive.library_assets import DerivedAsset, MaterializationResult
from family_archive.library_video import generate_video_playbacks


def make_video_index(content):
    digest = hashlib.sha256(content).hexdigest()
    manifest = {
        "schema_version": 3,
        "source_id": "SRC-0001",
        "source_type": "data_cd",
        "archive_status": "COMPLETE",
        "started_at": "2026-10-01T00:00:00+00:00",
        "completed_at": "2026-10-01T01:00:00+00:00",
        "inspection": {"volume": "Family CD"},
        "destination": {"bucket": "archive-test", "prefix": ""},
        "preservation": {
            "format": "mounted_files",
            "full_disc_image": False,
            "object_prefix": "sources/SRC-0001/data",
        },
        "file_count": 1,
        "directory_count": 0,
        "logical_file_size": len(content),
        "files": [
            {
                "kind": "file",
                "path": "Movies/clip.avi",
                "filename": "clip.avi",
                "size": len(content),
                "sha256": digest,
                "mtime_ns": 1,
                "mode": 0o444,
                "mime_type": "video/x-msvideo",
                "object_key": "sources/SRC-0001/data/Movies/clip.avi",
            }
        ],
    }
    return build_library_index([manifest]), digest


def hash_file(path, _ffprobe, _source_id, _relative_path):
    content = Path(path).read_bytes()
    return len(content), hashlib.sha256(content).hexdigest()


def test_video_playback_is_a_private_cached_derivative_of_verified_media(tmp_path, monkeypatch):
    source_bytes = b"verified original AVI"
    index, source_hash = make_video_index(source_bytes)
    source = tmp_path / "original.avi"
    source.write_bytes(source_bytes)
    source.chmod(0o600)
    materialized = MaterializationResult(
        (DerivedAsset(source_hash, len(source_bytes), source),)
    )
    calls = []

    def fake_ffmpeg(_executable, _source, output, _source_id, _path):
        calls.append(_source)
        Path(output).write_bytes(b"validated H.264 MP4 derivative")

    monkeypatch.setattr("family_archive.library_video.shutil.which", lambda _name: "/bin/tool")
    monkeypatch.setattr("family_archive.library_video._run_ffmpeg", fake_ffmpeg)
    monkeypatch.setattr("family_archive.library_video._hash_playback", hash_file)

    result = generate_video_playbacks(index, materialized, tmp_path / "playbacks")
    cached = generate_video_playbacks(index, materialized, tmp_path / "playbacks")

    assert len(result.assets) == 1
    asset = result.assets[0]
    assert asset.source_sha256 == source_hash
    assert asset.path.suffix == ".mp4"
    assert asset.path.stat().st_mode & 0o777 == 0o600
    assert asset.sha256 == hashlib.sha256(asset.path.read_bytes()).hexdigest()
    assert result.failed_items == ()
    assert cached.assets == result.assets
    assert len(calls) == 1
    assert source.read_bytes() == source_bytes


def test_video_conversion_failure_keeps_original_and_reports_the_failed_clip(
    tmp_path, monkeypatch
):
    source_bytes = b"verified original AVI"
    index, source_hash = make_video_index(source_bytes)
    source = tmp_path / "original.avi"
    source.write_bytes(source_bytes)
    source.chmod(0o600)

    def failed_ffmpeg(*_arguments):
        raise RuntimeError("unsupported codec")

    monkeypatch.setattr("family_archive.library_video.shutil.which", lambda _name: "/bin/tool")
    monkeypatch.setattr("family_archive.library_video._run_ffmpeg", failed_ffmpeg)

    result = generate_video_playbacks(
        index,
        MaterializationResult((DerivedAsset(source_hash, len(source_bytes), source),)),
        tmp_path / "playbacks",
    )

    assert result.assets == ()
    assert len(result.failed_items) == 1
    assert "unsupported codec" in result.failed_items[0]
    assert source.read_bytes() == source_bytes
    assert not list((tmp_path / "playbacks").rglob(".family-archive-video-*"))


def test_video_conversion_requires_ffmpeg_and_ffprobe(tmp_path, monkeypatch):
    source_bytes = b"verified original AVI"
    index, source_hash = make_video_index(source_bytes)
    source = tmp_path / "original.avi"
    source.write_bytes(source_bytes)
    source.chmod(0o600)
    tools = iter(["/bin/ffmpeg", None])
    monkeypatch.setattr("family_archive.library_video.shutil.which", lambda _name: next(tools))

    with pytest.raises(RuntimeError, match="ffmpeg and ffprobe"):
        generate_video_playbacks(
            index,
            MaterializationResult((DerivedAsset(source_hash, len(source_bytes), source),)),
            tmp_path / "playbacks",
        )

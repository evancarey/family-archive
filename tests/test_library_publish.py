import hashlib
import io
import json
from contextlib import contextmanager
from pathlib import Path

from family_archive.config import Config
from family_archive.library import build_library_index
from family_archive.library_assets import DerivedAsset, MaterializationResult
from family_archive.library_publish import publish_library
from family_archive.library_thumbnails import ThumbnailAsset, generate_thumbnails


class MemoryStorage:
    def __init__(self):
        self.objects = {}
        self.writes = []

    @contextmanager
    def open_read(self, key):
        yield io.BytesIO(self.objects[key])

    def keys(self, prefix):
        return iter(key for key in self.objects if key.startswith(prefix))

    def ensure_small_file(self, key, path, sha256):
        data = Path(path).read_bytes()
        assert hashlib.sha256(data).hexdigest() == sha256
        self._ensure(key, data)

    def ensure_content_addressed_file(self, key, path, sha256):
        self.ensure_small_file(key, path, sha256)

    def ensure_bytes(self, key, data):
        self._ensure(key, data)

    def _ensure(self, key, data):
        if key in self.objects:
            assert self.objects[key] == data
        else:
            self.objects[key] = data
        self.writes.append(key)


def photo_index(content):
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
                "path": "Photos/one.jpg",
                "filename": "one.jpg",
                "size": len(content),
                "sha256": digest,
                "mtime_ns": 1,
                "mode": 0o444,
                "mime_type": "image/jpeg",
                "object_key": "sources/SRC-0001/data/Photos/one.jpg",
            }
        ],
    }
    return build_library_index([manifest])


def test_thumbnail_generation_uses_verified_content_addressed_asset(tmp_path, monkeypatch):
    content = b"verified photo bytes"
    digest = hashlib.sha256(content).hexdigest()
    source = tmp_path / "source"
    source.write_bytes(content)
    source.chmod(0o600)
    index = photo_index(content)

    calls = []

    def fake_sips(_executable, _source, output, _dimension, _source_id, _path):
        calls.append(output)
        Path(output).write_bytes(b"\xff\xd8\xffthumbnail\xff\xd9")

    monkeypatch.setattr("family_archive.library_thumbnails.Path.is_file", lambda _self: True)
    monkeypatch.setattr(
        "family_archive.library_thumbnails.shutil.which", lambda _path: "/usr/bin/sips"
    )
    monkeypatch.setattr("family_archive.library_thumbnails._run_sips", fake_sips)

    result = generate_thumbnails(
        index,
        MaterializationResult((DerivedAsset(digest, len(content), source),)),
        tmp_path / "thumbs",
    )

    assert len(result.assets) == 1
    assert result.assets[0].source_sha256 == digest
    assert result.assets[0].path.read_bytes() == b"\xff\xd8\xffthumbnail\xff\xd9"
    assert result.assets[0].path.stat().st_mode & 0o777 == 0o600
    assert result.failed_items == ()

    cached_result = generate_thumbnails(
        index,
        MaterializationResult((DerivedAsset(digest, len(content), source),)),
        tmp_path / "thumbs",
    )
    assert len(calls) == 1
    assert cached_result.assets == result.assets


def test_thumbnail_conversion_failure_does_not_abort_other_library_work(
    tmp_path, monkeypatch
):
    content = b"verified photo bytes"
    digest = hashlib.sha256(content).hexdigest()
    source = tmp_path / digest
    source.write_bytes(content)
    source.chmod(0o600)
    index = photo_index(content)

    def failed_sips(_executable, _source, _output, _dimension, source_id, path):
        raise RuntimeError(f"{source_id}:{path}: unsupported image encoding")

    monkeypatch.setattr("family_archive.library_thumbnails.Path.is_file", lambda _self: True)
    monkeypatch.setattr(
        "family_archive.library_thumbnails.shutil.which", lambda _path: "/usr/bin/sips"
    )
    monkeypatch.setattr("family_archive.library_thumbnails._run_sips", failed_sips)

    result = generate_thumbnails(
        index,
        MaterializationResult((DerivedAsset(digest, len(content), source),)),
        tmp_path / "thumbs",
    )

    assert result.assets == ()
    assert len(result.failed_items) == 1
    assert "unsupported image encoding" in result.failed_items[0]


def test_publish_writes_media_thumbnail_then_index_without_archive_locator(tmp_path):
    content = b"verified photo bytes"
    digest = hashlib.sha256(content).hexdigest()
    media = tmp_path / digest
    media.write_bytes(content)
    media.chmod(0o600)
    thumb_bytes = b"\xff\xd8\xffthumbnail\xff\xd9"
    thumb_hash = hashlib.sha256(thumb_bytes).hexdigest()
    thumb = tmp_path / "thumb.jpg"
    thumb.write_bytes(thumb_bytes)
    thumb.chmod(0o600)
    index = photo_index(content)
    storage = MemoryStorage()

    result = publish_library(
        storage,
        Config("archive-test", prefix="archive"),
        index,
        MaterializationResult((DerivedAsset(digest, len(content), media),)),
        (ThumbnailAsset(digest, thumb_hash, len(thumb_bytes), 512, thumb),),
    )

    assert storage.writes[-1] == result.key
    assert storage.writes[0] == f"archive/library/media/{digest[:2]}/{digest}"
    document = storage.objects[result.key].decode()
    assert "sources/SRC-0001/data/Photos/one.jpg" not in document
    assert f"archive/library/media/{digest[:2]}/{digest}" in document
    assert result.item_count == result.source_count == 1


def test_publish_keeps_original_when_thumbnail_conversion_failed(tmp_path):
    content = b"verified photo bytes"
    digest = hashlib.sha256(content).hexdigest()
    media = tmp_path / digest
    media.write_bytes(content)
    media.chmod(0o600)
    storage = MemoryStorage()

    result = publish_library(
        storage,
        Config("archive-test"),
        photo_index(content),
        MaterializationResult((DerivedAsset(digest, len(content), media),)),
        (),
    )

    document = json.loads(storage.objects[result.key])
    assert document["items"][0]["media_key"].endswith(digest)
    assert document["items"][0]["thumbnail"] is None
    assert result.thumbnail_bytes == 0


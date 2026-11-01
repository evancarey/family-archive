import hashlib
import io
from contextlib import contextmanager

import pytest

from family_archive.library import build_library_index
from family_archive.library_assets import load_materialized_cache, materialize_library


class MemoryReader:
    def __init__(self, objects):
        self.objects = objects
        self.opened = []

    @contextmanager
    def open_read(self, key):
        self.opened.append(key)
        stream = io.BytesIO(self.objects[key])
        try:
            yield stream
        finally:
            stream.close()

    def keys(self, prefix):
        yield from sorted(key for key in self.objects if key.startswith(prefix))


def file_entry(path, content, mime_type):
    return {
        "kind": "file",
        "path": path,
        "filename": path.rsplit("/", 1)[-1],
        "size": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
        "mtime_ns": 1,
        "mode": 0o444,
        "mime_type": mime_type,
    }


def files_only_manifest(source_id, entries):
    object_prefix = f"sources/{source_id}/data"
    files = [{**entry, "object_key": f"{object_prefix}/{entry['path']}"} for entry in entries]
    return {
        "schema_version": 3,
        "source_id": source_id,
        "source_type": "data_cd",
        "archive_status": "COMPLETE",
        "started_at": "2026-10-01T00:00:00+00:00",
        "completed_at": "2026-10-01T01:00:00+00:00",
        "inspection": {"volume": "Family CD"},
        "destination": {"bucket": "archive-test", "prefix": ""},
        "preservation": {
            "format": "mounted_files",
            "full_disc_image": False,
            "object_prefix": object_prefix,
        },
        "file_count": len(files),
        "directory_count": 0,
        "logical_file_size": sum(entry["size"] for entry in files),
        "files": files,
    }


def image_manifest(source_id, entries, image_bytes):
    return {
        "schema_version": 1,
        "source_id": source_id,
        "source_type": "data_dvd",
        "archive_status": "COMPLETE",
        "started_at": "2026-10-02T00:00:00+00:00",
        "completed_at": "2026-10-02T01:00:00+00:00",
        "inspection": {"volume": "Old Photos", "filesystem": "cd9660"},
        "destination": {"bucket": "archive-test", "prefix": ""},
        "preservation": {
            "format": "raw_logical_sector_image",
            "sector_size": 2048,
            "key": f"sources/{source_id}/image/{source_id}.img",
            "size": len(image_bytes),
            "sha256": hashlib.sha256(image_bytes).hexdigest(),
        },
        "file_count": len(entries),
        "directory_count": 0,
        "logical_file_size": sum(entry["size"] for entry in entries),
        "files": entries,
    }


def multisession_manifest(source_id, content):
    entries = [
        {
            **file_entry("Sessions/01/photo.jpg", content, "image/jpeg"),
            "source_path": "photo.jpg",
            "session": 1,
        }
    ]
    raw_image = b"A" * 2352 + b"B" * 2352
    tracks = []
    for session, start_lba, offset, data in (
        (1, 0, 0, raw_image[:2352]),
        (2, 1, 2352, raw_image[2352:]),
    ):
        tracks.append(
            {
                "session": session,
                "track": session,
                "start_lba": start_lba,
                "sectors": 1,
                "sector_size": 2352,
                "offset_bytes": offset,
                "size": len(data),
                "toc": "CD_ROM_XA\\nTRACK MODE2_RAW\\n",
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    return {
        "schema_version": 2,
        "source_id": source_id,
        "source_type": "multisession_data_cd",
        "archive_status": "COMPLETE",
        "started_at": "2026-10-03T00:00:00+00:00",
        "completed_at": "2026-10-03T01:00:00+00:00",
        "inspection": {"volume": "Multisession", "filesystem": "cd9660"},
        "destination": {"bucket": "archive-test", "prefix": ""},
        "preservation": {
            "format": "concatenated_mode2_raw_session_tracks",
            "sector_size": 2352,
            "key": f"sources/{source_id}/image/{source_id}.bin",
            "size": len(raw_image),
            "sha256": hashlib.sha256(raw_image).hexdigest(),
            "tracks": tracks,
        },
        "file_count": 1,
        "directory_count": 0,
        "logical_file_size": len(content),
        "files": entries,
    }


def test_materialize_files_only_object_and_reuse_by_content_hash(tmp_path):
    content = b"the same photo"
    source_a = files_only_manifest("SRC-0001", [file_entry("one.jpg", content, "image/jpeg")])
    source_b = files_only_manifest("SRC-0002", [file_entry("two.jpg", content, "image/jpeg")])
    objects = {
        "sources/SRC-0001/data/one.jpg": content,
        "sources/SRC-0002/data/two.jpg": content,
    }
    reader = MemoryReader(objects)
    index = build_library_index([source_a, source_b])

    result = materialize_library(reader, index, tmp_path / "cache")

    assert len(result.assets) == 1
    asset = result.assets[0]
    assert asset.path.read_bytes() == content
    assert asset.path.name == hashlib.sha256(content).hexdigest()
    assert reader.opened == list(objects)


def test_load_materialized_cache_rehashes_against_manifest_without_s3_reads(tmp_path):
    content = b"previously verified photo"
    first = files_only_manifest("SRC-0001", [file_entry("one.jpg", content, "image/jpeg")])
    duplicate = files_only_manifest("SRC-0002", [file_entry("two.jpg", content, "image/jpeg")])
    index = build_library_index([first, duplicate])
    digest = hashlib.sha256(content).hexdigest()
    cache = tmp_path / "cache"
    asset = cache / digest[:2] / digest
    asset.parent.mkdir(parents=True)
    cache.chmod(0o700)
    asset.parent.chmod(0o700)
    asset.write_bytes(content)
    asset.chmod(0o600)

    result = load_materialized_cache(index, cache)

    assert len(result.assets) == 1
    assert result.assets[0].sha256 == digest
    assert result.assets[0].path == asset


def test_incremental_materialization_reuses_valid_files_and_fetches_only_new_media(tmp_path):
    old_bytes = b"already materialized photo"
    new_bytes = b"newly archived photo"
    source = files_only_manifest(
        "SRC-0001",
        [
            file_entry("old.jpg", old_bytes, "image/jpeg"),
            file_entry("new.jpg", new_bytes, "image/jpeg"),
        ],
    )
    index = build_library_index([source])
    old_hash = hashlib.sha256(old_bytes).hexdigest()
    old_path = tmp_path / "cache" / old_hash[:2] / old_hash
    old_path.parent.mkdir(parents=True)
    (tmp_path / "cache").chmod(0o700)
    old_path.parent.chmod(0o700)
    old_path.write_bytes(old_bytes)
    old_path.chmod(0o600)
    new_key = source["files"][1]["object_key"]
    reader = MemoryReader({new_key: new_bytes})

    result = materialize_library(reader, index, tmp_path / "cache", reuse_existing=True)

    assert result.reused_assets == 1
    assert len(result.assets) == 2
    assert reader.opened == [new_key]


def test_incremental_materialization_reuses_cached_image_entries_without_reading_image(
    tmp_path, monkeypatch
):
    content = b"photo already extracted from image"
    raw_image = b"image bytes".ljust(2048, b"\x00")
    source = image_manifest(
        "SRC-0001", [file_entry("Photos/photo.jpg", content, "image/jpeg")], raw_image
    )
    index = build_library_index([source])
    digest = hashlib.sha256(content).hexdigest()
    target = tmp_path / "cache" / digest[:2] / digest
    target.parent.mkdir(parents=True)
    (tmp_path / "cache").chmod(0o700)
    target.parent.chmod(0o700)
    target.write_bytes(content)
    target.chmod(0o600)
    reader = MemoryReader({})
    monkeypatch.setattr(
        "family_archive.library_assets.mount_image",
        lambda *_args: (_ for _ in ()).throw(AssertionError("image should not be mounted")),
    )

    result = materialize_library(reader, index, tmp_path / "cache", reuse_existing=True)

    assert result.reused_assets == 1
    assert reader.opened == []


def test_load_materialized_cache_rejects_corrupt_cache_file(tmp_path):
    content = b"expected photo"
    source = files_only_manifest("SRC-0001", [file_entry("photo.jpg", content, "image/jpeg")])
    index = build_library_index([source])
    digest = hashlib.sha256(content).hexdigest()
    asset = tmp_path / "cache" / digest[:2] / digest
    asset.parent.mkdir(parents=True)
    (tmp_path / "cache").chmod(0o700)
    asset.parent.chmod(0o700)
    asset.write_bytes(b"corrupt bytes")
    asset.chmod(0o600)

    with pytest.raises(ValueError, match="Existing derived asset differs from manifest"):
        load_materialized_cache(index, tmp_path / "cache")


def test_materialize_image_entry_from_read_only_mount(tmp_path, monkeypatch):
    content = b"photo in the image"
    raw_image = b"verified image bytes".ljust(2048, b"\x00")
    source = image_manifest(
        "SRC-0001",
        [file_entry("Photos/photo.jpg", content, "image/jpeg")],
        raw_image,
    )
    image_key = source["preservation"]["key"]
    reader = MemoryReader({image_key: raw_image})
    mount = tmp_path / "mounted-image"
    (mount / "Photos").mkdir(parents=True)
    (mount / "Photos" / "photo.jpg").write_bytes(content)
    mounts = []

    @contextmanager
    def fake_mount(image_path, filesystem):
        mounts.append((image_path, filesystem))
        assert image_path.read_bytes() == raw_image
        yield mount

    monkeypatch.setattr("family_archive.library_assets.mount_image", fake_mount)
    index = build_library_index([source])

    result = materialize_library(reader, index, tmp_path / "cache")

    expected_image = tmp_path / "cache" / ".staging" / f"{source['preservation']['sha256']}.img"
    assert mounts == [(expected_image, "cd9660")]
    assert result.assets[0].path.read_bytes() == content
    assert reader.opened == [image_key]
    assert not expected_image.exists()


def test_materialize_refuses_corrupt_archive_bytes_without_publishing(tmp_path):
    content = b"expected photo"
    source = files_only_manifest("SRC-0001", [file_entry("photo.jpg", content, "image/jpeg")])
    reader = MemoryReader({"sources/SRC-0001/data/photo.jpg": b"corrupt bytes"})
    cache = tmp_path / "cache"

    with pytest.raises(ValueError, match="SHA-256 or size differs"):
        materialize_library(reader, build_library_index([source]), cache)

    assert not list(cache.glob("*/*"))


def test_materialize_refuses_to_follow_symlinked_image_path(tmp_path, monkeypatch):
    outside = tmp_path / "outside"
    outside.mkdir()
    content = b"external bytes"
    (outside / "photo.jpg").write_bytes(content)
    raw_image = b"image bytes".ljust(2048, b"\x00")
    source = image_manifest(
        "SRC-0001",
        [file_entry("linked/photo.jpg", content, "image/jpeg")],
        raw_image,
    )
    image_key = source["preservation"]["key"]
    reader = MemoryReader({image_key: raw_image})
    mount = tmp_path / "mounted-image"
    mount.mkdir()
    (mount / "linked").symlink_to(outside, target_is_directory=True)

    @contextmanager
    def fake_mount(_image_path, _filesystem):
        yield mount

    monkeypatch.setattr("family_archive.library_assets.mount_image", fake_mount)

    with pytest.raises(OSError):
        materialize_library(reader, build_library_index([source]), tmp_path / "cache")

    assert (outside / "photo.jpg").read_bytes() == content
    digest = hashlib.sha256(content).hexdigest()
    assert not (tmp_path / "cache" / digest[:2] / digest).exists()


def test_materialize_extracts_multisession_media_and_reuses_verified_tracks(tmp_path, monkeypatch):
    source = multisession_manifest("SRC-0001", b"multisession photo")
    image = b"A" * 2352 + b"B" * 2352
    reader = MemoryReader({"sources/SRC-0001/image/SRC-0001.bin": image})
    index = build_library_index([source])

    def fake_inventory(tracks, on_file=None):
        assert [track.session for track in tracks] == [1, 2]
        on_file({"path": "Sessions/01/photo.jpg"}, b"multisession photo")
        return []

    monkeypatch.setattr("family_archive.library_assets.inventory_sessions", fake_inventory)

    result = materialize_library(reader, index, tmp_path / "cache")

    assert len(result.assets) == 1
    assert result.assets[0].path.read_bytes() == b"multisession photo"
    assert result.unsupported_items == ()
    assert reader.opened == ["sources/SRC-0001/image/SRC-0001.bin"]

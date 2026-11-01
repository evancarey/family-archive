import hashlib
import io
import json
from contextlib import contextmanager

import pytest

from family_archive import cli
from family_archive.config import Config
from family_archive.library import (
    build_library_index,
    load_library_index,
    serialize_library_index,
)
from family_archive.manifest import serialize


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


def file_entry(path, content, mime_type=None, *, object_key=None):
    entry = {
        "kind": "file",
        "path": path,
        "filename": path.rsplit("/", 1)[-1],
        "size": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
        "mtime_ns": 123,
        "mode": 0o444,
    }
    if mime_type is not None:
        entry["mime_type"] = mime_type
    if object_key is not None:
        entry["object_key"] = object_key
    return entry


def files_only_manifest(source_id, entries, *, status="COMPLETE", volume="Family CD"):
    object_prefix = f"sources/{source_id}/data"
    files = [
        {**entry, "object_key": f"{object_prefix}/{entry['path']}"}
        for entry in entries
    ]
    return {
        "schema_version": 3,
        "source_id": source_id,
        "source_type": "data_cd",
        "archive_status": status,
        "started_at": "2026-10-01T00:00:00+00:00",
        "completed_at": "2026-10-01T01:00:00+00:00",
        "inspection": {"volume": volume},
        "destination": {"bucket": "archive-test", "prefix": ""},
        "notes": "",
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


def image_manifest(
    source_id,
    entries,
    *,
    status="COMPLETE",
    image_bytes=b"image archive".ljust(2048, b"\x00"),
):
    files = [
        {key: value for key, value in entry.items() if key != "object_key"}
        for entry in entries
    ]
    return {
        "schema_version": 1,
        "source_id": source_id,
        "source_type": "data_dvd",
        "archive_status": status,
        "started_at": "2026-10-02T00:00:00+00:00",
        "completed_at": "2026-10-02T01:00:00+00:00",
        "inspection": {"volume": "Old Photos", "filesystem": "cd9660"},
        "destination": {"bucket": "archive-test", "prefix": ""},
        "notes": "",
        "preservation": {
            "format": "raw_logical_sector_image",
            "sector_size": 2048,
            "key": f"sources/{source_id}/image/{source_id}.img",
            "size": len(image_bytes),
            "sha256": hashlib.sha256(image_bytes).hexdigest(),
        },
        "file_count": len(files),
        "directory_count": 0,
        "logical_file_size": sum(entry["size"] for entry in files),
        "files": files,
    }


def test_index_preserves_source_paths_and_direct_file_object_references():
    source = files_only_manifest(
        "SRC-0002",
        [file_entry("Photos/Family.JPG", b"photo", "image/jpeg")],
    )

    index = build_library_index([source])

    assert index.sources[0].volume_label == "Family CD"
    assert index.items[0].source_id == "SRC-0002"
    assert index.items[0].path == "Photos/Family.JPG"
    assert index.items[0].media_type == "photo"
    assert index.items[0].original.kind == "file_object"
    assert index.items[0].original.object_key == (
        "sources/SRC-0002/data/Photos/Family.JPG"
    )


def test_index_points_image_mode_items_to_image_and_manifest_path():
    source = image_manifest(
        "SRC-0001",
        [file_entry("Photos/scan.tif", b"image", "image/tiff")],
    )

    item = build_library_index([source]).items[0]

    assert item.original.kind == "image_entry"
    assert item.original.object_key == "sources/SRC-0001/image/SRC-0001.img"
    assert item.original.path == "Photos/scan.tif"


def test_index_excludes_incomplete_and_non_media_entries():
    incomplete = files_only_manifest(
        "SRC-0001", [file_entry("not-ready.jpg", b"x", "image/jpeg")], status="INGESTING"
    )
    complete = files_only_manifest(
        "SRC-0002",
        [
            file_entry("Notes/readme.txt", b"text", "text/plain"),
            file_entry("Audio/voice.mp3", b"audio", "audio/mpeg"),
        ],
    )

    index = build_library_index([incomplete, complete])

    assert [source.source_id for source in index.sources] == ["SRC-0002"]
    assert [(item.path, item.media_type) for item in index.items] == [
        ("Audio/voice.mp3", "audio")
    ]


def test_index_sorts_stably_and_serializes_deterministically():
    later = files_only_manifest(
        "SRC-0010",
        [
            file_entry("z.jpg", b"z", "image/jpeg"),
            file_entry("A.jpg", b"a", "image/jpeg"),
        ],
    )
    earlier = files_only_manifest(
        "SRC-0002", [file_entry("clip.mp4", b"video", "video/mp4")]
    )

    index = build_library_index([later, earlier])
    serialized = serialize_library_index(index)

    assert [source.source_id for source in index.sources] == ["SRC-0002", "SRC-0010"]
    assert [(item.source_id, item.path) for item in index.items] == [
        ("SRC-0002", "clip.mp4"),
        ("SRC-0010", "A.jpg"),
        ("SRC-0010", "z.jpg"),
    ]
    assert json.loads(serialized)["schema_version"] == 1
    assert serialized == serialize_library_index(index)


def test_index_rejects_invalid_complete_manifest_and_duplicate_source():
    source = files_only_manifest(
        "SRC-0001", [file_entry("photo.jpg", b"photo", "image/jpeg")]
    )
    invalid = {**source, "file_count": 2}
    with pytest.raises(ValueError, match="file_count"):
        build_library_index([invalid])
    with pytest.raises(ValueError, match="Duplicate complete manifest"):
        build_library_index([source, source])


def test_index_result_is_immutable():
    source = files_only_manifest(
        "SRC-0001", [file_entry("photo.jpg", b"photo", "image/jpeg")]
    )
    item = build_library_index([source]).items[0]

    with pytest.raises(AttributeError):
        item.path = "other.jpg"


def test_storage_adapter_reads_only_source_manifests():
    config = Config("archive-test")
    manifest = files_only_manifest(
        "SRC-0001", [file_entry("Photos/one.jpg", b"photo", "image/jpeg")]
    )
    manifest_key = config.key("sources/SRC-0001/manifest.json")
    image_key = config.key("sources/SRC-0001/image/SRC-0001.img")
    reader = MemoryReader({manifest_key: serialize(manifest), image_key: b"large raw image"})

    index = load_library_index(reader, config)

    assert [item.path for item in index.items] == ["Photos/one.jpg"]
    assert reader.opened == [manifest_key]


def test_library_index_cli_writes_new_local_index_without_archive_writes(
    tmp_path, monkeypatch, capsys
):
    config = Config("archive-test")
    manifest = files_only_manifest(
        "SRC-0001", [file_entry("Photos/one.jpg", b"photo", "image/jpeg")]
    )
    manifest_key = config.key("sources/SRC-0001/manifest.json")
    reader = MemoryReader({manifest_key: serialize(manifest)})
    monkeypatch.setattr("family_archive.storage.s3.connect", lambda *_args: reader)
    output = tmp_path / "library-index.json"

    assert cli.main(
        ["library", "index", "--bucket", config.bucket, "--output", str(output)]
    ) == 0

    saved = json.loads(output.read_text())
    assert saved["items"][0]["path"] == "Photos/one.jpg"
    assert "1 media records from 1 sources" in capsys.readouterr().out
    assert reader.opened == [manifest_key]


def test_library_index_cli_does_not_overwrite_existing_output(tmp_path, monkeypatch, capsys):
    config = Config("archive-test")
    monkeypatch.setattr("family_archive.storage.s3.connect", lambda *_args: MemoryReader({}))
    output = tmp_path / "library-index.json"
    output.write_text("keep this file")

    assert cli.main(
        ["library", "index", "--bucket", config.bucket, "--output", str(output)]
    ) == 1

    assert output.read_text() == "keep this file"
    assert "File exists" in capsys.readouterr().err


def test_materialize_cli_dry_run_reads_manifests_only(tmp_path, monkeypatch, capsys):
    config = Config("archive-test")
    content = b"photo content"
    source = files_only_manifest(
        "SRC-0001", [file_entry("photo.jpg", content, "image/jpeg")]
    )
    manifest_key = config.key("sources/SRC-0001/manifest.json")
    reader = MemoryReader({manifest_key: serialize(source)})
    monkeypatch.setattr("family_archive.storage.s3.connect", lambda *_args: reader)
    cache = tmp_path / "cache"

    assert cli.main(
        [
            "library",
            "materialize",
            "--bucket",
            config.bucket,
            "--cache-dir",
            str(cache),
            "--dry-run",
        ]
    ) == 0

    output = capsys.readouterr().out
    assert "Materializable records: 1" in output
    assert "Dry run only" in output
    assert reader.opened == [manifest_key]
    assert not cache.exists()


def test_materialize_cli_verifies_file_bytes_into_private_cache(tmp_path, monkeypatch, capsys):
    config = Config("archive-test")
    content = b"photo content"
    source = files_only_manifest(
        "SRC-0001", [file_entry("photo.jpg", content, "image/jpeg")]
    )
    manifest_key = config.key("sources/SRC-0001/manifest.json")
    file_key = source["files"][0]["object_key"]
    reader = MemoryReader({manifest_key: serialize(source), file_key: content})
    monkeypatch.setattr("family_archive.storage.s3.connect", lambda *_args: reader)
    cache = tmp_path / "cache"

    assert cli.main(
        [
            "library",
            "materialize",
            "--bucket",
            config.bucket,
            "--cache-dir",
            str(cache),
        ]
    ) == 0

    digest = hashlib.sha256(content).hexdigest()
    asset = cache / digest[:2] / digest
    assert asset.read_bytes() == content
    assert asset.stat().st_mode & 0o777 == 0o600
    assert "Materialized 1 unique media files" in capsys.readouterr().out
    assert reader.opened == [manifest_key, file_key]


def test_materialize_cli_incremental_fetches_only_missing_cache_files(
    tmp_path, monkeypatch, capsys
):
    config = Config("archive-test")
    old_content = b"existing local photo"
    new_content = b"new archive photo"
    source = files_only_manifest(
        "SRC-0001",
        [
            file_entry("old.jpg", old_content, "image/jpeg"),
            file_entry("new.jpg", new_content, "image/jpeg"),
        ],
    )
    manifest_key = config.key("sources/SRC-0001/manifest.json")
    new_key = source["files"][1]["object_key"]
    reader = MemoryReader(
        {manifest_key: serialize(source), new_key: new_content}
    )
    monkeypatch.setattr("family_archive.storage.s3.connect", lambda *_args: reader)
    cache = tmp_path / "cache"
    old_hash = hashlib.sha256(old_content).hexdigest()
    old_asset = cache / old_hash[:2] / old_hash
    old_asset.parent.mkdir(parents=True)
    cache.chmod(0o700)
    old_asset.parent.chmod(0o700)
    old_asset.write_bytes(old_content)
    old_asset.chmod(0o600)

    assert cli.main(
        [
            "library",
            "materialize",
            "--bucket",
            config.bucket,
            "--cache-dir",
            str(cache),
            "--incremental",
        ]
    ) == 0

    output = capsys.readouterr().out
    assert "Reused 1 locally verified media files; fetched 1 missing files." in output
    assert reader.opened == [manifest_key, new_key]


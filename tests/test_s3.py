import base64
import hashlib
import io
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import boto3
import pytest
from botocore.exceptions import ClientError
from botocore.stub import ANY, Stubber

from family_archive.storage import StorageError
from family_archive.storage.s3 import S3Storage


def error(code):
    return ClientError({"Error": {"Code": code, "Message": "simulated"}}, "test")


class S3Client:
    """Service-shaped fake; no credentials, network, or real bucket."""

    def __init__(self):
        self.objects = {}
        self.uploads = {}
        self.parts_sent = []
        self.fail_part = None
        self.corrupt = False
        self.if_none_match = []
        self.get_calls = 0
        self.head_calls = 0
        self.omit_checksum = False

    def get_object(self, Bucket, Key):
        self.get_calls += 1
        if Key not in self.objects:
            raise error("NoSuchKey")
        data = self.objects[Key]
        return {"Body": io.BytesIO(data + b"corrupt" if self.corrupt else data)}

    def put_object(self, Bucket, Key, Body, IfNoneMatch, **kwargs):
        self.if_none_match.append(IfNoneMatch)
        if Key in self.objects:
            raise error("PreconditionFailed")
        data = Body.read() if hasattr(Body, "read") else Body
        self.objects[Key] = data
        if self.omit_checksum:
            return {}
        return {"ChecksumSHA256": base64.b64encode(hashlib.sha256(data).digest()).decode()}

    def head_object(self, Bucket, Key, ChecksumMode):
        self.head_calls += 1
        if Key not in self.objects:
            raise error("NoSuchKey")
        data = self.objects[Key]
        result = {
            "ContentLength": len(data),
        }
        if not self.omit_checksum:
            result["ChecksumSHA256"] = base64.b64encode(hashlib.sha256(data).digest()).decode()
        return result

    def create_multipart_upload(self, Bucket, Key, **kwargs):
        identifier = str(len(self.uploads) + 1)
        self.uploads[identifier] = {}
        return {"UploadId": identifier}

    def upload_part(self, Bucket, Key, UploadId, PartNumber, Body, **kwargs):
        if PartNumber == self.fail_part:
            raise OSError("network interrupted")
        if UploadId not in self.uploads:
            raise error("NoSuchUpload")
        self.uploads[UploadId][PartNumber] = Body
        self.parts_sent.append(PartNumber)
        return {"ETag": str(PartNumber)}

    def complete_multipart_upload(self, Bucket, Key, UploadId, MultipartUpload, IfNoneMatch):
        self.if_none_match.append(IfNoneMatch)
        if Key in self.objects:
            raise error("PreconditionFailed")
        if UploadId not in self.uploads:
            raise error("NoSuchUpload")
        self.objects[Key] = b"".join(
            self.uploads[UploadId][part["PartNumber"]] for part in MultipartUpload["Parts"]
        )
        return {}


def test_conditional_bytes_and_conflict():
    client = S3Client()
    storage = S3Storage("archive-test", client)
    storage.ensure_bytes("manifest", b"original")
    storage.ensure_bytes("manifest", b"original")
    assert client.if_none_match == ["*"]
    with pytest.raises(StorageError, match="not overwritten"):
        storage.ensure_bytes("manifest", b"different")
    assert client.objects["manifest"] == b"original"


def test_small_file_conditional_upload_verifies_s3_checksum_without_readback(tmp_path):
    path = tmp_path / "photo.jpg"
    path.write_bytes(b"original photo bytes")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    client = S3Client()
    storage = S3Storage("archive-test", client)
    storage.ensure_small_file("sources/SRC-0001/data/photo.jpg", path, digest)
    storage.ensure_small_file("sources/SRC-0001/data/photo.jpg", path, digest)
    assert client.objects["sources/SRC-0001/data/photo.jpg"] == path.read_bytes()
    assert client.if_none_match == ["*", "*"]
    assert client.get_calls == 0
    assert client.head_calls == 1  # only the conflict path needs a HEAD
    path.write_bytes(b"different photo bytes")
    with pytest.raises(StorageError, match="not overwritten"):
        storage.ensure_small_file("sources/SRC-0001/data/photo.jpg", path, digest)


def test_small_file_falls_back_to_full_readback_when_s3_checksum_is_unavailable(tmp_path):
    path = tmp_path / "photo.jpg"
    path.write_bytes(b"photo bytes")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    client = S3Client()
    client.omit_checksum = True
    storage = S3Storage("archive-test", client)

    storage.ensure_small_file("photo", path, digest)

    assert client.objects["photo"] == path.read_bytes()
    assert client.get_calls == 1


def test_content_addressed_file_uses_s3_checksum_for_existing_and_new_objects(tmp_path):
    path = tmp_path / "photo.jpg"
    path.write_bytes(b"original photo bytes")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    client = S3Client()
    storage = S3Storage("archive-test", client)

    storage.ensure_content_addressed_file("library/media/photo", path, digest)
    storage.ensure_content_addressed_file("library/media/photo", path, digest)

    assert client.objects["library/media/photo"] == path.read_bytes()
    assert client.if_none_match == ["*"]
    path.write_bytes(b"different bytes")
    with pytest.raises(StorageError, match="Existing object differs"):
        storage.ensure_content_addressed_file("library/media/photo", path, digest)
    assert client.objects["library/media/photo"] == b"original photo bytes"


def test_small_file_sdk_put_sends_and_checks_sha256_without_a_readback(tmp_path):
    client = boto3.client(
        "s3", region_name="us-east-1", aws_access_key_id="testing", aws_secret_access_key="testing"
    )
    data = b"photo"
    digest = hashlib.sha256(data).hexdigest()
    checksum = base64.b64encode(bytes.fromhex(digest)).decode()
    path = tmp_path / "photo.jpg"
    path.write_bytes(data)
    storage = S3Storage("archive-test", client)

    with Stubber(client) as stub:
        stub.add_response(
            "put_object",
            {"ETag": '"etag"', "ChecksumSHA256": checksum},
            {
                "Bucket": "archive-test",
                "Key": "photo.jpg",
                "Body": ANY,
                "IfNoneMatch": "*",
                "ChecksumSHA256": checksum,
            },
        )
        storage.ensure_small_file("photo.jpg", path, digest)
        stub.assert_no_pending_responses()


def test_latest_key_returns_most_recent_object_across_pages():
    first = datetime(2026, 10, 1, tzinfo=UTC)
    second = first + timedelta(days=1)

    class Paginator:
        def paginate(self, **_parameters):
            return iter(
                [
                    {"Contents": [{"Key": "library/index/old.json", "LastModified": first}]},
                    {"Contents": [{"Key": "library/index/new.json", "LastModified": second}]},
                ]
            )

    class Client:
        def get_paginator(self, operation):
            assert operation == "list_objects_v2"
            return Paginator()

    storage = S3Storage("archive-test", Client())

    assert storage.latest_key("library/index/") == "library/index/new.json"


def test_multipart_resume(tmp_path, monkeypatch):
    monkeypatch.setattr("family_archive.storage.s3.PART_SIZE", 5)
    path = tmp_path / "image"
    path.write_bytes(b"0123456789abcdef")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    client = S3Client()
    client.fail_part = 2
    storage = S3Storage("archive-test", client)
    checkpoint, saved = {}, []

    def save():
        saved.append(deepcopy(checkpoint))

    with pytest.raises(OSError):
        storage.ensure_file("image", path, digest, checkpoint, save)
    assert len(saved[-1]["parts"]) == 1
    client.fail_part = None
    storage.ensure_file("image", path, digest, checkpoint, save)
    assert client.parts_sent == [1, 2, 3, 4]
    assert client.objects["image"] == path.read_bytes()
    assert client.if_none_match == ["*"]
    storage.ensure_file("image", path, digest, checkpoint, save)
    assert client.parts_sent == [1, 2, 3, 4]


def test_corrupt_read_back_never_accepted(tmp_path):
    client = S3Client()
    storage = S3Storage("archive-test", client)
    client.corrupt = True
    with pytest.raises(StorageError, match="verification"):
        storage.ensure_bytes("manifest", b"content")


def test_expired_multipart_clears_checkpoint(tmp_path):
    path = tmp_path / "image"
    path.write_bytes(b"content")
    digest = hashlib.sha256(b"content").hexdigest()
    client = S3Client()
    storage = S3Storage("archive-test", client)
    checkpoint = {
        "key": "image",
        "upload_id": "expired",
        "sha256": digest,
        "size": 7,
        "part_size": 5,
        "parts": [],
    }
    with pytest.raises(StorageError, match="expired"):
        storage.ensure_file("image", path, digest, checkpoint, lambda: None)
    assert checkpoint == {}
    storage.ensure_file("image", path, digest, checkpoint, lambda: None)
    assert client.objects["image"] == b"content"


def test_actual_sdk_request_shapes(tmp_path):
    # Stubber validates API parameters against botocore's real service model.
    client = boto3.client(
        "s3", region_name="us-east-1", aws_access_key_id="testing", aws_secret_access_key="testing"
    )
    storage = S3Storage("archive-test", client)
    data = b"logical sectors"
    digest = hashlib.sha256(data).hexdigest()
    path = tmp_path / "image"
    path.write_bytes(data)
    with Stubber(client) as stub:
        stub.add_client_error("get_object", service_error_code="NoSuchKey")
        stub.add_response("create_multipart_upload", {"UploadId": "upload"})
        stub.add_response("upload_part", {"ETag": "etag"})
        stub.add_response(
            "complete_multipart_upload",
            {},
            {
                "Bucket": "archive-test",
                "Key": "image",
                "UploadId": "upload",
                "IfNoneMatch": "*",
                "MultipartUpload": {"Parts": [{"PartNumber": 1, "ETag": "etag"}]},
            },
        )
        stub.add_response("get_object", {"Body": io.BytesIO(data)})
        storage.ensure_file("image", path, digest, {}, lambda: None)
        stub.add_client_error("get_object", service_error_code="NoSuchKey")
        stub.add_response("put_object", {})
        stub.add_response("get_object", {"Body": io.BytesIO(data)})
        storage.ensure_bytes("manifest", data)
        stub.assert_no_pending_responses()


def test_read_only_reader_lists_all_pages_and_closes_bodies():
    from family_archive.storage import ObjectNotFound

    client = boto3.client(
        "s3", region_name="us-east-1", aws_access_key_id="testing", aws_secret_access_key="testing"
    )
    storage = S3Storage("archive-test", client)
    body = io.BytesIO(b"content")
    with Stubber(client) as stub:
        stub.add_response(
            "list_objects_v2",
            {
                "IsTruncated": True,
                "NextContinuationToken": "next",
                "Contents": [{"Key": "sources/SRC-0001/manifest.json"}],
            },
            {"Bucket": "archive-test", "Prefix": "sources/"},
        )
        stub.add_response(
            "list_objects_v2",
            {"IsTruncated": False, "Contents": [{"Key": "sources/SRC-0002/source.json"}]},
            {"Bucket": "archive-test", "Prefix": "sources/", "ContinuationToken": "next"},
        )
        assert list(storage.keys("sources/")) == [
            "sources/SRC-0001/manifest.json",
            "sources/SRC-0002/source.json",
        ]
        stub.add_response("get_object", {"Body": body}, {"Bucket": "archive-test", "Key": "key"})
        with pytest.raises(RuntimeError), storage.open_read("key") as stream:
            assert stream.read() == b"content"
            raise RuntimeError("caller error")
        assert body.closed
        stub.add_client_error("get_object", service_error_code="NoSuchKey")
        with pytest.raises(ObjectNotFound), storage.open_read("missing"):
            pass
        stub.assert_no_pending_responses()

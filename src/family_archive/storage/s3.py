"""Conditional immutable writes, durable multipart checkpoints, full GET verification."""

import base64
import hashlib
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from botocore.exceptions import ClientError

from family_archive.hashing import hash_stream
from family_archive.storage import ObjectNotFound, StorageError

PART_SIZE = 64 * 1024 * 1024


class S3Storage:
    def __init__(self, bucket: str, client):
        self.bucket = bucket
        self.client = client

    @contextmanager
    def open_read(self, key: str):
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            if exc.response["Error"]["Code"] in {"NoSuchKey", "404"}:
                raise ObjectNotFound(f"Missing archive object: {key}") from exc
            raise
        try:
            yield response["Body"]
        finally:
            response["Body"].close()

    def keys(self, prefix: str) -> Iterator[str]:
        for page in self.client.get_paginator("list_objects_v2").paginate(
            Bucket=self.bucket, Prefix=prefix
        ):
            for item in page.get("Contents", []):
                yield item["Key"]

    def matches(self, key: str, size: int, digest: str) -> bool:
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            if exc.response["Error"]["Code"] in {"NoSuchKey", "404"}:
                return False
            raise
        body = response["Body"]
        try:
            actual_size, actual_hash = hash_stream(body)
        finally:
            body.close()
        if actual_size != size or actual_hash != digest:
            raise StorageError(
                f"Existing object differs or failed verification: {key}; not overwritten"
            )
        return True

    def ensure_bytes(self, key: str, data: bytes) -> None:
        digest = hashlib.sha256(data).hexdigest()
        if self.matches(key, len(data), digest):
            return
        try:
            self.client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=data,
                IfNoneMatch="*",
                ChecksumSHA256=base64.b64encode(bytes.fromhex(digest)).decode(),
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] not in {"PreconditionFailed", "412"}:
                raise
        if not self.matches(key, len(data), digest):
            raise StorageError(f"Object missing after upload: {key}")

    def ensure_small_file(self, key: str, path: Path, sha256: str) -> None:
        """Upload a regular file with one conditional PUT and verify a full read-back."""
        size = path.stat().st_size
        if self.matches(key, size, sha256):
            return
        try:
            with path.open("rb") as stream:
                self.client.put_object(
                    Bucket=self.bucket,
                    Key=key,
                    Body=stream,
                    IfNoneMatch="*",
                    ChecksumSHA256=base64.b64encode(bytes.fromhex(sha256)).decode(),
                )
        except ClientError as exc:
            if exc.response["Error"]["Code"] not in {"PreconditionFailed", "412"}:
                raise
        if not self.matches(key, size, sha256):
            raise StorageError(f"Object missing after upload: {key}")

    def ensure_content_addressed_file(self, key: str, path: Path, sha256: str) -> None:
        """Conditionally upload a derived blob and verify S3's stored SHA-256."""
        size = path.stat().st_size
        try:
            head = self.client.head_object(Bucket=self.bucket, Key=key, ChecksumMode="ENABLED")
        except ClientError as exc:
            if exc.response["Error"]["Code"] not in {"NoSuchKey", "404", "NotFound"}:
                raise
            try:
                with path.open("rb") as stream:
                    self.client.put_object(
                        Bucket=self.bucket,
                        Key=key,
                        Body=stream,
                        IfNoneMatch="*",
                        ChecksumSHA256=base64.b64encode(bytes.fromhex(sha256)).decode(),
                    )
            except ClientError as put_exc:
                if put_exc.response["Error"]["Code"] not in {
                    "PreconditionFailed",
                    "412",
                }:
                    raise
            head = self.client.head_object(Bucket=self.bucket, Key=key, ChecksumMode="ENABLED")

        expected_checksum = base64.b64encode(bytes.fromhex(sha256)).decode()
        stored_checksum = head.get("ChecksumSHA256")
        if head.get("ContentLength") != size:
            raise StorageError(f"Existing object differs; not overwritten: {key}")
        if stored_checksum is None:
            if not self.matches(key, size, sha256):
                raise StorageError(f"Object missing after upload: {key}")
        elif stored_checksum != expected_checksum:
            raise StorageError(f"Existing object differs; not overwritten: {key}")

    def ensure_file(
        self, key: str, path: Path, sha256: str, checkpoint: dict, save: Callable[[], None]
    ) -> None:
        size = path.stat().st_size
        if self.matches(key, size, sha256):
            return
        if not checkpoint:
            upload = self.client.create_multipart_upload(
                Bucket=self.bucket, Key=key, Metadata={"sha256": sha256}
            )
            checkpoint.update(
                key=key,
                upload_id=upload["UploadId"],
                sha256=sha256,
                size=size,
                part_size=PART_SIZE,
                parts=[],
            )
            save()
        if (checkpoint["key"], checkpoint["sha256"], checkpoint["size"]) != (key, sha256, size):
            raise StorageError(f"Multipart checkpoint identity mismatch: {key}")
        try:
            with path.open("rb") as stream:
                number = 0
                while data := stream.read(checkpoint["part_size"]):
                    number += 1
                    digest = hashlib.sha256(data).hexdigest()
                    if number <= len(checkpoint["parts"]):
                        if checkpoint["parts"][number - 1]["sha256"] != digest:
                            raise StorageError(
                                f"Local image changed at upload part {number}: {key}"
                            )
                        continue
                    response = self.client.upload_part(
                        Bucket=self.bucket,
                        Key=key,
                        UploadId=checkpoint["upload_id"],
                        PartNumber=number,
                        Body=data,
                        ContentMD5=base64.b64encode(hashlib.md5(data).digest()).decode(),
                    )
                    checkpoint["parts"].append(
                        {"PartNumber": number, "ETag": response["ETag"], "sha256": digest}
                    )
                    save()
            self.client.complete_multipart_upload(
                Bucket=self.bucket,
                Key=key,
                UploadId=checkpoint["upload_id"],
                IfNoneMatch="*",
                MultipartUpload={
                    "Parts": [
                        {"PartNumber": part["PartNumber"], "ETag": part["ETag"]}
                        for part in checkpoint["parts"]
                    ]
                },
            )
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            if code in {"NoSuchUpload", "ConditionalRequestConflict", "409"}:
                checkpoint.clear()
                save()
                raise StorageError(
                    f"Multipart upload expired or conflicted: {key}; retry to start a new upload"
                ) from exc
            if code not in {"PreconditionFailed", "412"}:
                raise
        if not self.matches(key, size, sha256):
            raise StorageError(f"Object missing after upload: {key}")


def connect(bucket: str, profile: str | None = None) -> S3Storage:
    import boto3
    from botocore.config import Config

    client = boto3.Session(profile_name=profile).client(
        "s3",
        config=Config(
            retries={"mode": "standard", "max_attempts": 5}, connect_timeout=10, read_timeout=60
        ),
    )
    return S3Storage(bucket, client)

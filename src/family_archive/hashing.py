"""Streaming integrity operations; hashes are never inferred from S3 ETags."""

import hashlib
from pathlib import Path
from typing import BinaryIO

CHUNK_SIZE = 8 * 1024 * 1024


def hash_stream(stream: BinaryIO) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    while chunk := stream.read(CHUNK_SIZE):
        digest.update(chunk)
        size += len(chunk)
    return size, digest.hexdigest()


def hash_file(path: Path) -> tuple[int, str]:
    with path.open("rb") as stream:
        return hash_stream(stream)

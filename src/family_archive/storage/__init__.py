"""Archive storage contract; domain code does not depend on S3 responses."""

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from pathlib import Path
from typing import BinaryIO, Protocol


class StorageError(Exception):
    pass


class ObjectNotFound(StorageError):
    pass


class ArchiveReader(Protocol):
    def open_read(self, key: str) -> AbstractContextManager[BinaryIO]: ...

    def keys(self, prefix: str) -> Iterator[str]: ...


class Storage(ArchiveReader, Protocol):
    def latest_key(self, prefix: str) -> str | None: ...

    def ensure_bytes(self, key: str, data: bytes) -> None: ...

    def ensure_small_file(self, key: str, path: Path, sha256: str) -> None: ...

    def ensure_content_addressed_file(self, key: str, path: Path, sha256: str) -> None: ...

    def ensure_file(
        self, key: str, path: Path, sha256: str, checkpoint: dict, save: Callable[[], None]
    ) -> None: ...

"""Strict raw logical-sector capture, without read-error padding or source writes."""

import hashlib
import os
import stat
from pathlib import Path
from typing import BinaryIO

from family_archive.hashing import CHUNK_SIZE


def open_device(device: str) -> BinaryIO:
    import re

    if not re.fullmatch(r"/dev/disk\d+(?:s\d+)?", device):
        raise ValueError(
            "Image capture requires an inspected whole optical device or data-track slice"
        )
    # macOS may refuse the buffered block node while the filesystem is mounted.
    # Its raw character-device counterpart permits read-only sector capture.
    raw_device = device.replace("/dev/disk", "/dev/rdisk", 1)
    descriptor = os.open(raw_device, os.O_RDONLY | os.O_NOFOLLOW)
    if not stat.S_ISCHR(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise ValueError(f"Not a raw character device: {raw_device}")
    return os.fdopen(descriptor, "rb", buffering=0)


def capture(source: BinaryIO, image: Path, expected_size: int, progress=None) -> str:
    """Check every existing byte against the inserted source before appending.

    A crash may leave a partial chunk; it is still verified, never discarded.
    A short read before the declared end is an error, never zero-filled.
    """
    if image.is_symlink():
        raise ValueError(f"Cached image must not be a symlink: {image}")
    existing = image.stat().st_size if image.exists() else 0
    if existing > expected_size:
        raise ValueError(f"Cached image exceeds raw media size: {image}")
    digest = hashlib.sha256()
    with image.open("r+b" if image.exists() else "x+b", buffering=0) as output:
        offset = 0
        while offset < expected_size:
            length = min(CHUNK_SIZE, expected_size - offset)
            data = source.read(length)
            if not data:
                raise OSError(
                    f"Unreadable or truncated source at byte {offset}; expected {expected_size}"
                )
            preserved = min(len(data), max(0, existing - offset))
            if preserved and output.read(preserved) != data[:preserved]:
                raise ValueError(f"Inserted source differs from cached image at byte {offset}")
            if preserved < len(data):
                remaining = memoryview(data)[preserved:]
                while remaining:
                    written = output.write(remaining)
                    if not written:
                        raise OSError(f"Unable to write cached image at byte {offset}")
                    remaining = remaining[written:]
                output.flush()
                os.fsync(output.fileno())
            digest.update(data)
            offset += len(data)
            if progress:
                progress(offset, expected_size)
        if source.read(2048):
            raise ValueError("Source exceeds reported raw media size; review size metadata")
    return digest.hexdigest()

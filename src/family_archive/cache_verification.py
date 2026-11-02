"""Private local checkpoints for content hashes already verified in the library cache."""

import json
import os
import stat
import tempfile
from pathlib import Path
from threading import RLock


class CacheVerification:
    """Skip repeat hashes only while a cached file's local identity is unchanged.

    This is an acceleration checkpoint, not a substitute for the explicit full-cache
    audit. A file whose inode, size, timestamps, or mode changes is hashed again.
    """

    filename = ".family-archive-verification.json"

    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.path = self.root / self.filename
        self._lock = RLock()
        self._records = self._load()
        self._dirty = False

    def get(self, path: Path) -> tuple[int, str] | None:
        key, signature = self._file_identity(path)
        if key is None:
            return None
        with self._lock:
            record = self._records.get(key)
            if not isinstance(record, dict) or record.get("identity") != signature:
                return None
            size, digest = record.get("size"), record.get("sha256")
            if type(size) is not int or not isinstance(digest, str):
                return None
            return size, digest

    def matches(self, path: Path, size: int, digest: str) -> bool:
        return self.get(path) == (size, digest)

    def remember(self, path: Path, size: int, digest: str) -> None:
        key, identity = self._file_identity(path)
        if key is None:
            raise ValueError(f"Verified library file is outside its cache: {path}")
        with self._lock:
            self._records[key] = {"size": size, "sha256": digest, "identity": identity}
            self._dirty = True

    def save(self) -> None:
        with self._lock:
            if not self._dirty:
                return
            document = json.dumps(
                {"schema_version": 1, "files": self._records},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            descriptor, name = tempfile.mkstemp(prefix=".verification-", dir=self.root)
            temporary = Path(name)
            try:
                os.fchmod(descriptor, 0o600)
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(document)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
                self._dirty = False
            finally:
                temporary.unlink(missing_ok=True)

    def _load(self) -> dict:
        if self.path.is_symlink():
            raise ValueError(f"Library verification checkpoint must not be a symlink: {self.path}")
        try:
            info = self.path.lstat()
        except FileNotFoundError:
            return {}
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise ValueError(f"Library verification checkpoint must be a private file: {self.path}")
        try:
            document = json.loads(self.path.read_bytes())
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return {}
        if (
            not isinstance(document, dict)
            or document.get("schema_version") != 1
            or not isinstance(document.get("files"), dict)
        ):
            return {}
        return document["files"]

    def _file_identity(self, path: Path) -> tuple[str | None, dict | None]:
        try:
            resolved = Path(path).resolve(strict=True)
            key = resolved.relative_to(self.root).as_posix()
            info = Path(path).lstat()
        except (OSError, ValueError):
            return None, None
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            return None, None
        return key, {
            "device": info.st_dev,
            "inode": info.st_ino,
            "size": info.st_size,
            "mtime_ns": info.st_mtime_ns,
            "ctime_ns": info.st_ctime_ns,
            "mode": stat.S_IMODE(info.st_mode),
        }

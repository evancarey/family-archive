"""Mount only a local archival image, read-only, without recursive cleanup."""

import plistlib
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from family_archive.media import diskutil


class ImageMountError(OSError):
    pass


def require_detached(image: Path) -> None:
    mount = image.absolute().parent / ".image-mount"
    if mount.is_symlink():
        raise ImageMountError(f"Image mount directory must not be a symlink: {mount}")
    if mount.is_mount():
        raise ImageMountError(
            f"A previous image mount is still attached: {mount}. "
            f'Run hdiutil detach "{mount}" before resuming.'
        )


def hdiutil(*arguments: str) -> bytes:
    try:
        result = subprocess.run(
            ["/usr/bin/hdiutil", *arguments],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ImageMountError(f"hdiutil {arguments[0]} failed: {exc}") from exc
    if result.returncode:
        raise ImageMountError(
            f"hdiutil {arguments[0]} failed: {result.stderr.decode(errors='replace').strip()}"
        )
    return result.stdout


@contextmanager
def mount_image(image: Path, filesystem: str) -> Iterator[Path]:
    """A fixed private mount directory makes an unclean exit visible on resume.

    Never delete recursively or force-detach: a mounted filesystem must never be
    traversed by cleanup. A stale mount requires explicit detach before retrying.
    """
    image = image.absolute()
    mount = image.parent / ".image-mount"
    require_detached(image)
    mount.mkdir(mode=0o700, exist_ok=True)
    if any(mount.iterdir()):
        raise ImageMountError(f"Image mount directory must be empty: {mount}")
    attached = False
    try:
        output = hdiutil(
            "attach",
            "-readonly",
            "-nobrowse",
            "-noautoopen",
            "-mountpoint",
            str(mount),
            "-plist",
            str(image),
        )
        attached = True
        try:
            response = plistlib.loads(output)
            mounted = [
                entity["mount-point"]
                for entity in response["system-entities"]
                if entity.get("mount-point")
            ]
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise ImageMountError("Invalid hdiutil attach property list") from exc
        if mounted != [str(mount)] or not mount.is_mount():
            raise ImageMountError(
                f"Expected exactly one filesystem mounted at {mount}; got {mounted}"
            )
        info = diskutil("info", "-plist", str(mount))
        readonly = info.get("WritableVolume") is False or (
            "WritableVolume" not in info and info.get("ReadOnlyVolume") is True
        )
        expected = filesystem.lower().replace("iso9660", "cd9660")
        actual = str(info.get("FilesystemType", "")).lower().replace("iso9660", "cd9660")
        if not readonly or info.get("MountPoint") != str(mount) or actual != expected:
            raise ImageMountError(
                "Local image mount is not confirmed read-only with the expected filesystem"
            )
        yield mount
    finally:
        # Also handle a failed/timed-out attach that nevertheless left this path mounted.
        if attached or mount.is_mount():
            try:
                hdiutil("detach", str(mount))
            except ImageMountError as exc:
                raise ImageMountError(
                    f"Image detach failed; local mount retained at {mount}. "
                    f'Run hdiutil detach "{mount}" before resuming. {exc}'
                ) from exc
        # rmdir only removes an empty directory; it can never walk into a volume.
        if mount.is_mount():
            raise ImageMountError(f"Image is still mounted at {mount}; directory retained")
        mount.rmdir()

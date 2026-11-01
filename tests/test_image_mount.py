import plistlib
import subprocess
from pathlib import Path

import pytest

from family_archive import image_mount


@pytest.fixture
def mounting(tmp_path, monkeypatch):
    image = tmp_path / "archive.img"
    image.write_bytes(b"image")
    mount = tmp_path / ".image-mount"
    state = {
        "mounted": False,
        "readonly": False,
        "filesystem": "udf",
        "commands": [],
        "bad_plist": False,
        "detach_error": False,
        "attach_error": False,
    }

    def run(command, **kwargs):
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert kwargs["timeout"] == 60
        state["commands"].append(command)
        if command[1] == "attach":
            if state["attach_error"]:
                return subprocess.CompletedProcess(command, 1, b"", b"no mountable filesystem")
            state["mounted"] = True
            output = (
                b"invalid"
                if state["bad_plist"]
                else plistlib.dumps(
                    {"system-entities": [{"dev-entry": "/dev/disk9", "mount-point": str(mount)}]}
                )
            )
            return subprocess.CompletedProcess(command, 0, output, b"")
        if state["detach_error"]:
            return subprocess.CompletedProcess(command, 1, b"", b"resource busy")
        state["mounted"] = False
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(image_mount.subprocess, "run", run)
    original_is_mount = Path.is_mount
    monkeypatch.setattr(
        Path,
        "is_mount",
        lambda path: state["mounted"] if path == mount else original_is_mount(path),
    )
    monkeypatch.setattr(
        image_mount,
        "diskutil",
        lambda *args: {
            "WritableVolume": state["readonly"],
            "FilesystemType": state["filesystem"],
            "MountPoint": str(mount),
        },
    )
    return image, mount, state


def test_readonly_mount_and_detach(mounting):
    image, mount, state = mounting
    with image_mount.mount_image(image, "udf") as root:
        assert root == mount
        assert state["mounted"]
    assert not state["mounted"]
    assert not mount.exists()
    assert state["commands"] == [
        [
            "/usr/bin/hdiutil",
            "attach",
            "-readonly",
            "-nobrowse",
            "-noautoopen",
            "-mountpoint",
            str(mount),
            "-plist",
            str(image),
        ],
        ["/usr/bin/hdiutil", "detach", str(mount)],
    ]


@pytest.mark.parametrize("exception", [KeyboardInterrupt, OSError])
def test_cleanup_on_inventory_interruption(mounting, exception):
    image, mount, state = mounting
    with pytest.raises(exception), image_mount.mount_image(image, "udf"):
        raise exception()
    assert not state["mounted"]
    assert not mount.exists()


@pytest.mark.parametrize("change", [{"readonly": True}, {"filesystem": "hfs"}, {"bad_plist": True}])
def test_untrusted_mount_is_detached(mounting, change):
    image, mount, state = mounting
    state.update(change)
    with pytest.raises(image_mount.ImageMountError), image_mount.mount_image(image, "udf"):
        pytest.fail("Unvalidated mount must not be inventoried")
    assert not state["mounted"]
    assert not mount.exists()


def test_failed_attach_removes_only_empty_directory(mounting):
    image, mount, state = mounting
    state["attach_error"] = True
    with (
        pytest.raises(image_mount.ImageMountError, match="no mountable"),
        image_mount.mount_image(image, "udf"),
    ):
        pass
    assert not mount.exists()
    assert len(state["commands"]) == 1


def test_failed_detach_keeps_directory(mounting):
    image, mount, state = mounting
    state["detach_error"] = True
    with (
        pytest.raises(image_mount.ImageMountError, match="mount retained"),
        image_mount.mount_image(image, "udf"),
    ):
        pass
    assert state["mounted"]
    assert mount.is_dir()
    assert "-force" not in state["commands"][-1]


def test_stale_mount_and_symlink_refused(mounting, tmp_path):
    image, mount, state = mounting
    state["mounted"] = True
    with pytest.raises(image_mount.ImageMountError, match="before resuming"):
        image_mount.require_detached(image)
    state["mounted"] = False
    mount.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(image_mount.ImageMountError, match="symlink"):
        image_mount.require_detached(image)
    assert not state["commands"]


def test_nonempty_directory_is_never_cleaned(mounting):
    image, mount, state = mounting
    mount.mkdir()
    (mount / "keep").write_text("untouched")
    with (
        pytest.raises(image_mount.ImageMountError, match="empty"),
        image_mount.mount_image(image, "udf"),
    ):
        pass
    assert (mount / "keep").read_text() == "untouched"
    assert not state["commands"]


def test_timeout_after_attachment_still_detaches(mounting, monkeypatch):
    image, mount, state = mounting
    original = image_mount.subprocess.run

    def timeout_on_attach(command, **kwargs):
        if command[1] == "attach":
            state["mounted"] = True
            raise subprocess.TimeoutExpired(command, 60)
        return original(command, **kwargs)

    monkeypatch.setattr(image_mount.subprocess, "run", timeout_on_attach)
    with pytest.raises(image_mount.ImageMountError), image_mount.mount_image(image, "udf"):
        pass
    assert not state["mounted"]
    assert not mount.exists()


def test_missing_hdiutil_has_useful_error(monkeypatch):
    def missing(*args, **kwargs):
        raise FileNotFoundError("/usr/bin/hdiutil")

    monkeypatch.setattr(image_mount.subprocess, "run", missing)
    with pytest.raises(image_mount.ImageMountError, match="hdiutil attach failed"):
        image_mount.hdiutil("attach", "-readonly", "image.img")

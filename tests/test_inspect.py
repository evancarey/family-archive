import plistlib
import subprocess
from pathlib import Path

import pytest

from family_archive import cli, media


def test_reported_macos_dvd_is_detected(monkeypatch, capsys):
    # Relevant fields from a real macOS diskutil info response, not inferred keys.
    info = plistlib.loads(
        (Path(__file__).parent / "fixtures" / "dvd_diskutil_info.plist").read_bytes()
    )
    monkeypatch.setattr(media.platform, "system", lambda: "Darwin")

    def run(command, **kwargs):
        response = {"AllDisks": ["disk5"]} if command[1] == "list" else info
        return subprocess.CompletedProcess(command, 0, plistlib.dumps(response), b"")

    monkeypatch.setattr(media.subprocess, "run", run)
    assert cli.main(["inspect"]) == 2
    output = capsys.readouterr().out
    for expected in ("/dev/disk5", "DVD+R", "070307_2229", "udf", "3,713,744,896"):
        assert expected in output
    assert "NEEDS_REVIEW" in output
    assert "layout is unusual or unknown" in output
    assert "Multiple mounted volumes" not in output


@pytest.mark.parametrize(
    "writable, expected", [(False, "DATA CD"), (True, "NEEDS_REVIEW"), (None, "NEEDS_REVIEW")]
)
def test_whole_disk_mounted_cd_with_native_keys(monkeypatch, disc, writable, expected):
    whole, volume = disc
    whole.update(volume)
    whole.update(
        DeviceIdentifier="disk4", ParentWholeDisk="disk4", WholeDisk=True, WritableVolume=writable
    )
    whole.pop("Whole")
    whole.pop("ReadOnlyVolume")
    monkeypatch.setattr(media.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        media, "diskutil", lambda *args: {"AllDisks": ["disk4"]} if args[0] == "list" else whole
    )
    reports = media.inspect_media()
    assert len(reports) == 1
    assert reports[0].classification == expected
    assert reports[0].volume == "Family Photos"


@pytest.fixture
def disc(tmp_path):
    whole = {
        "DeviceIdentifier": "disk4",
        "DeviceNode": "/dev/disk4",
        "Whole": True,
        "OpticalDeviceType": "CD-ROM",
        "OpticalMediaType": "CD-ROM",
        "OpticalMediaNumTracks": 1,
        "OpticalMediaNumSessions": 1,
        "TotalSize": 650000000,
    }
    volume = {
        "DeviceIdentifier": "disk4s1",
        "ParentWholeDisk": "disk4",
        "Whole": False,
        "MountPoint": str(tmp_path),
        "FilesystemType": "cd9660",
        "ReadOnlyVolume": True,
        "VolumeName": "Family Photos",
    }
    return whole, volume


def test_ordinary_cd(disc):
    report = media.classify(*disc)
    assert report.classification == "DATA CD"
    assert report.volume == "Family Photos"
    assert report.capacity == 650000000


@pytest.mark.parametrize(
    "key,value",
    [
        ("OpticalMediaType", "Unknown"),
        ("OpticalMediaNumTracks", 2),
        ("OpticalMediaNumTracks", None),
        ("OpticalMediaNumSessions", 2),
        ("OpticalMediaNumSessions", None),
        ("OpticalMediaNumAudioTracks", 1),
    ],
)
def test_unusual_layout_requires_review(disc, key, value):
    whole, volume = disc
    whole[key] = value
    assert media.classify(whole, volume).classification == "NEEDS_REVIEW"


@pytest.mark.parametrize(
    "key,value",
    [
        ("FilesystemType", "udf"),
        ("FilesystemType", "hfs"),
        ("MountPoint", ""),
        ("ReadOnlyVolume", False),
        ("ReadOnlyVolume", None),
    ],
)
def test_unsupported_volume(disc, key, value):
    whole, volume = disc
    volume[key] = value
    assert media.classify(whole, volume).classification == "NEEDS_REVIEW"


def test_video_and_unreadable_volume(disc, tmp_path):
    (tmp_path / "mPeGaV").mkdir()
    assert "Video-disc" in media.classify(*disc).reason
    disc[1]["MountPoint"] = str(tmp_path / "missing")
    assert "Unable to read" in media.classify(*disc).reason


def test_discovery_uses_only_read_commands(monkeypatch, disc):
    monkeypatch.setattr(media.platform, "system", lambda: "Darwin")
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        assert kwargs == {"capture_output": True, "timeout": 30, "check": False}
        responses = {
            ("list", "-plist"): {"AllDisks": ["disk0", "disk4", "disk4s1"]},
            ("info", "-plist", "disk0"): {"Whole": True},
            ("info", "-plist", "disk4"): disc[0],
            ("info", "-plist", "disk4s1"): disc[1],
        }
        return subprocess.CompletedProcess(
            command, 0, plistlib.dumps(responses[tuple(command[1:])]), b""
        )

    monkeypatch.setattr(media.subprocess, "run", run)
    reports = media.inspect_media()
    assert len(reports) == 1
    assert reports[0].classification == "DATA CD"
    assert len(calls) == 4


@pytest.mark.parametrize(
    "result",
    [
        subprocess.CompletedProcess([], 1, b"", b"Device disconnected"),
        subprocess.CompletedProcess([], 0, b"invalid plist", b""),
        subprocess.CompletedProcess([], 0, plistlib.dumps({"ErrorMessage": "failed"}), b""),
    ],
)
def test_command_errors(monkeypatch, result):
    monkeypatch.setattr(media.subprocess, "run", lambda *a, **k: result)
    with pytest.raises(media.InspectionError):
        media.diskutil("list", "-plist")


@pytest.mark.parametrize(
    "error", [FileNotFoundError("missing"), subprocess.TimeoutExpired("diskutil", 30)]
)
def test_unavailable_command(monkeypatch, error):
    def run(*args, **kwargs):
        raise error

    monkeypatch.setattr(media.subprocess, "run", run)
    with pytest.raises(media.InspectionError):
        media.diskutil("list", "-plist")


def test_platform_guard(monkeypatch):
    monkeypatch.setattr(media.platform, "system", lambda: "Linux")
    with pytest.raises(media.InspectionError, match="requires macOS"):
        media.inspect_media()


def test_cli(monkeypatch, capsys, disc):
    monkeypatch.setattr(cli, "inspect_media", lambda: [media.classify(*disc)])
    assert cli.main(["inspect"]) == 0
    assert "DATA CD" in capsys.readouterr().out
    disc[0]["OpticalMediaNumTracks"] = 2
    assert cli.main(["inspect"]) == 2
    assert "NEEDS_REVIEW" in capsys.readouterr().out
    monkeypatch.setattr(cli, "inspect_media", lambda: [])
    assert cli.main(["inspect"]) == 0
    assert "No optical media" in capsys.readouterr().out


def test_cli_failure(monkeypatch, capsys):
    def fail():
        raise media.InspectionError("Device disconnected")

    monkeypatch.setattr(cli, "inspect_media", fail)
    assert cli.main(["inspect"]) == 1
    assert "Device disconnected" in capsys.readouterr().err


@pytest.mark.parametrize("devices", [None, "disk4", ["/dev/disk4"], [123]])
def test_invalid_discovery(monkeypatch, devices):
    monkeypatch.setattr(media.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(media, "diskutil", lambda *args: {"AllDisks": devices})
    with pytest.raises(media.InspectionError, match="AllDisks"):
        media.inspect_media()


@pytest.mark.parametrize("mounted_volumes", [0, 2])
def test_ambiguous_or_unmounted_disc(monkeypatch, disc, mounted_volumes):
    monkeypatch.setattr(media.platform, "system", lambda: "Darwin")
    responses = {"disk4": disc[0]}
    for index in range(mounted_volumes):
        responses[f"disk4s{index + 1}"] = dict(disc[1])

    def query(*args):
        return {"AllDisks": list(responses)} if args[0] == "list" else responses[args[-1]]

    monkeypatch.setattr(media, "diskutil", query)
    reports = media.inspect_media()
    assert len(reports) == 1
    assert reports[0].classification == "NEEDS_REVIEW"


@pytest.fixture(autouse=True)
def no_hardware_drutil(monkeypatch):
    monkeypatch.setattr(media, "drutil_status", lambda: ({}, "No layout available."))

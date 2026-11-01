import plistlib
import subprocess
from pathlib import Path

import pytest

from family_archive import cli, media

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def dvd(tmp_path):
    info = plistlib.loads((FIXTURES / "dvd_diskutil_info.plist").read_bytes())
    info["MountPoint"] = str(tmp_path)
    return info


def test_real_status():
    layouts = media.parse_drutil_status((FIXTURES / "dvd_drutil_status.txt").read_text())
    assert layouts == {"/dev/disk5": media.OpticalLayout("DVD+R", 1, 1)}


@pytest.mark.parametrize("disc_type", sorted(media.DVD_TYPES))
@pytest.mark.parametrize("filesystem", ["udf", "cd9660", "iso9660"])
def test_data_dvd_types(dvd, disc_type, filesystem):
    dvd.update(OpticalMediaType=disc_type, FilesystemType=filesystem)
    report = media.classify(dvd, dvd, media.OpticalLayout(disc_type, 1, 1))
    assert report.classification == "DATA DVD"
    assert report.inspection_supported
    assert report.sessions == report.tracks == 1
    assert report.image_size == 3714285568


def test_single_session_cd_uses_2048_byte_logical_track_slice(dvd, tmp_path):
    whole = dict(dvd)
    whole.update(
        DeviceNode="/dev/disk5",
        DeviceIdentifier="disk5",
        DeviceBlockSize=2352,
        OpticalMediaType="CD-RW",
        OpticalMediaNumTracks=1,
        OpticalMediaNumSessions=1,
        OpticalMediaNumAudioTracks=0,
        Size=846367200,
        IOKitSize=846367200,
        TotalSize=846367200,
    )
    volume = {
        "DeviceNode": "/dev/disk5s0",
        "ParentWholeDisk": "disk5",
        "DeviceBlockSize": 2048,
        "Content": "CD_ROM_Mode_2_Form_1",
        "Size": 658667520,
        "FilesystemType": "cd9660",
        "MountPoint": str(tmp_path),
        "WritableVolume": False,
        "VolumeName": "Ordinary CD",
    }
    report = media.classify(whole, volume, media.OpticalLayout("CD-RW", 1, 1))
    assert report.classification == "DATA CD"
    assert report.image_device == "/dev/disk5s0"
    assert report.image_size == 658667520


def test_single_session_raw_cd_without_confirmed_logical_track_needs_review(dvd, tmp_path):
    whole = dict(dvd)
    whole.update(
        DeviceNode="/dev/disk5",
        DeviceIdentifier="disk5",
        DeviceBlockSize=2352,
        OpticalMediaType="CD-RW",
        OpticalMediaNumTracks=1,
        OpticalMediaNumSessions=1,
        OpticalMediaNumAudioTracks=0,
        Size=846367200,
        IOKitSize=846367200,
    )
    volume = {
        "DeviceNode": "/dev/disk5s0",
        "ParentWholeDisk": "disk5",
        "DeviceBlockSize": 2048,
        "Content": "CD_ROM_Mode_2_Form_2",
        "Size": 658667520,
        "FilesystemType": "cd9660",
        "MountPoint": str(tmp_path),
        "WritableVolume": False,
    }
    report = media.classify(whole, volume, media.OpticalLayout("CD-RW", 1, 1))
    assert report.classification == "NEEDS_REVIEW"
    assert "2048-byte logical data-track" in report.reason


@pytest.mark.parametrize(
    "markers, classification",
    [
        (["vIdEo_Ts"], "LIKELY VIDEO DVD"),
        (["AUDIO_TS"], "LIKELY AUDIO DVD"),
        (["VIDEO_TS", "AUDIO_TS"], "LIKELY VIDEO DVD"),
        (["VCD"], "NEEDS_REVIEW"),
        (["DVD_RTAV"], "NEEDS_REVIEW"),
        (["BDMV"], "NEEDS_REVIEW"),
    ],
)
def test_markers(dvd, markers, classification):
    for marker in markers:
        (Path(dvd["MountPoint"]) / marker).mkdir()
    report = media.classify(dvd, dvd, media.OpticalLayout("DVD+R", 1, 1))
    assert report.classification == classification
    assert report.inspection_supported == (classification != "NEEDS_REVIEW")


@pytest.mark.parametrize(
    "changes",
    [
        {"MountPoint": ""},
        {"MountPoint": "/nonexistent/family-archive-test"},
        {"WritableVolume": True},
        {"FilesystemType": "hfs"},
        {"OpticalMediaNumTracks": 2},
        {"OpticalMediaNumSessions": 2},
        {"OpticalMediaType": "BD-ROM"},
    ],
)
def test_review_conditions(dvd, changes):
    dvd.update(changes)
    assert (
        media.classify(dvd, dvd, media.OpticalLayout("DVD+R", 1, 1)).classification
        == "NEEDS_REVIEW"
    )


def test_conflicts_and_missing_layout(dvd):
    dvd["OpticalMediaNumTracks"] = 2
    assert "Conflicting" in media.classify(dvd, dvd, media.OpticalLayout("DVD+R", 1, 1)).reason
    dvd.pop("OpticalMediaNumTracks")
    report = media.classify(dvd, dvd, layout_error="drutil unavailable")
    assert report.classification == "NEEDS_REVIEW"
    assert "drutil unavailable" in report.reason


def test_multiple_drive_correlation():
    text = "Type: CD-ROM Name: /dev/disk9\nSessions: 2 Tracks: 3\n"
    text += (FIXTURES / "dvd_drutil_status.txt").read_text()
    parsed = media.parse_drutil_status(text)
    assert parsed["/dev/disk9"] == media.OpticalLayout("CD-ROM", 2, 3)
    assert parsed["/dev/disk5"] == media.OpticalLayout("DVD+R", 1, 1)
    with pytest.raises(media.InspectionError, match="duplicate"):
        media.parse_drutil_status(text + text)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "No media",
        "Type: DVD+R Name: /dev/disk5\n",
        "Type: DVD+R Name: /dev/disk5\nSessions: 0 Tracks: 0",
    ],
)
def test_missing_or_changed_status(text):
    assert media.parse_drutil_status(text) == {}


@pytest.mark.parametrize(
    "error", [FileNotFoundError("missing"), subprocess.TimeoutExpired("drutil", 30)]
)
def test_drutil_unavailable(monkeypatch, error):
    def run(*args, **kwargs):
        raise error

    monkeypatch.setattr(media.subprocess, "run", run)
    layouts, reason = media.drutil_status()
    assert layouts == {}
    assert "/usr/bin/drutil" in reason


def test_end_to_end_dvd(dvd, monkeypatch, capsys):
    monkeypatch.setattr(media.platform, "system", lambda: "Darwin")
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        if command == ["/usr/bin/drutil", "status"]:
            output = (FIXTURES / "dvd_drutil_status.txt").read_bytes()
        else:
            output = plistlib.dumps({"AllDisks": ["disk5"]} if command[1] == "list" else dvd)
        return subprocess.CompletedProcess(command, 0, output, b"")

    monkeypatch.setattr(media.subprocess, "run", run)
    assert cli.main(["inspect"]) == 0
    output = capsys.readouterr().out
    assert "DATA DVD" in output
    assert "Supported" in output
    assert "Sessions:       " in output
    assert commands == [
        ["/usr/sbin/diskutil", "list", "-plist"],
        ["/usr/sbin/diskutil", "info", "-plist", "disk5"],
        ["/usr/bin/drutil", "status"],
    ]


def test_status_other_disc_not_used(dvd, monkeypatch):
    monkeypatch.setattr(media.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        media, "diskutil", lambda *args: {"AllDisks": ["disk5"]} if args[0] == "list" else dvd
    )
    monkeypatch.setattr(
        media,
        "drutil_status",
        lambda: ({"/dev/disk9": media.OpticalLayout("DVD+R", 1, 1)}, "No matching metadata"),
    )
    (report,) = media.inspect_media()
    assert report.classification == "NEEDS_REVIEW"
    assert report.tracks is None

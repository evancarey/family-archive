"""Read-only macOS discovery; never mount, repair, or write to a source."""

import platform
import plistlib
import re
import subprocess
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any


class InspectionError(Exception):
    """Inspection could not obtain trustworthy information."""


def diskutil(*arguments: str) -> dict[str, Any]:
    command = ["/usr/sbin/diskutil", *arguments]
    try:
        result = subprocess.run(command, capture_output=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InspectionError(f"diskutil {' '.join(arguments)}: {exc}") from exc
    if result.returncode:
        detail = result.stderr.decode(errors="replace").strip()
        raise InspectionError(f"diskutil {' '.join(arguments)} failed: {detail or 'unknown error'}")
    try:
        value = plistlib.loads(result.stdout)
    except (ValueError, plistlib.InvalidFileException) as exc:
        raise InspectionError("diskutil returned an invalid property list") from exc
    if not isinstance(value, dict) or value.get("Error") or value.get("ErrorMessage"):
        raise InspectionError(f"diskutil returned an error or unexpected response: {value!r}")
    return value


@dataclass(frozen=True)
class Inspection:
    device: str
    media: str
    volume: str
    mount_point: str
    filesystem: str
    capacity: int | None
    tracks: int | None
    classification: str
    reason: str
    sessions: int | None = None
    inspection_supported: bool = False
    image_size: int | None = None
    image_device: str = ""
    hardware: dict | None = None
    volume_device: str = ""
    multisession_candidate: bool = False
    session_layout: list[dict[str, int]] = field(default_factory=list)
    archive_image_size: int | None = None
    archive_sector_count: int | None = None
    excluded_track_sectors: int = 0
    raw_sector_mode: int | None = None


def positive_integer(value: Any) -> int | None:
    return value if type(value) is int and value > 0 else None


CD_TYPES = {"CD-ROM", "CD-R", "CD-RW"}
DVD_TYPES = {
    "DVD-ROM",
    "DVD-R",
    "DVD-R DL",
    "DVD-RW",
    "DVD+R",
    "DVD+R DL",
    "DVD+RW",
    "DVD+RW DL",
    "DVD-RAM",
}


@dataclass(frozen=True)
class OpticalLayout:
    media: str
    sessions: int
    tracks: int


def parse_drutil_status(output: str) -> dict[str, OpticalLayout]:
    """Match each status block to its explicit device node, never drive order."""
    layouts = {}
    for block in re.split(r"(?=Type:)", output)[1:]:
        header = re.match(r"Type:\s*(.*?)\s+Name:\s*(/dev/disk\d+)\s*\n", block)
        counts = re.search(r"Sessions:\s*(\d+)\s+Tracks:\s*(\d+)", block)
        if not header or not counts:
            continue
        node = header[2]
        if node in layouts:
            raise InspectionError(f"drutil status returned duplicate records for {node}")
        sessions, tracks = map(int, counts.groups())
        if sessions > 0 and tracks > 0:
            layouts[node] = OpticalLayout(header[1].strip().upper(), sessions, tracks)
    return layouts


def drutil_status() -> tuple[dict[str, OpticalLayout], str]:
    try:
        result = subprocess.run(
            ["/usr/bin/drutil", "status"], capture_output=True, timeout=30, check=False
        )
        if result.returncode:
            raise InspectionError(
                result.stderr.decode(errors="replace").strip() or "command failed"
            )
        layouts = parse_drutil_status(result.stdout.decode(errors="replace"))
        return layouts, "No matching track/session metadata from drutil status."
    except (OSError, subprocess.TimeoutExpired, InspectionError) as exc:
        return {}, f"Unable to query drutil status: {exc}. Requires macOS native /usr/bin/drutil."


def parse_drutil_toc(output: str) -> tuple[dict[str, int], ...]:
    """Parse a single data track and lead-out for every session."""
    records = []
    for block in re.split(r"(?=First track:)", output)[1:]:
        match = re.search(
            r"Lead-out:\s*(\d+:\d+\.\d+).*?Session\s+(\d+),\s*Track\s+(\d+):"
            r"\s*(\d+:\d+\.\d+)\s+(.*)",
            block,
            re.S,
        )
        if not match:
            raise InspectionError("drutil toc omitted a session track or lead-out")
        leadout, session, track, start, description = match.groups()
        if "data track" not in description.lower() or "audio track" in description.lower():
            raise InspectionError("drutil toc reports an audio or unknown track type")

        def frames(value):
            minute, rest = value.split(":")
            second, frame = rest.split(".")
            if int(second) >= 60 or int(frame) >= 75:
                raise InspectionError("drutil toc contains invalid MSF time")
            return (int(minute) * 60 + int(second)) * 75 + int(frame)

        start_frames, end_frames = frames(start), frames(leadout)
        sectors = end_frames - start_frames
        if sectors <= 0:
            raise InspectionError("drutil toc contains an invalid session length")
        records.append(
            {
                "session": int(session),
                "track": int(track),
                "start_lba": start_frames - 150,
                "sectors": sectors,
            }
        )
    if not records or [item["session"] for item in records] != list(range(1, len(records) + 1)):
        raise InspectionError("drutil toc session numbering is incomplete or ambiguous")
    if [item["track"] for item in records] != list(range(1, len(records) + 1)):
        raise InspectionError("Expected one sequential data track per session")
    return tuple(records)


def drutil_toc() -> tuple[tuple[dict[str, int], ...], str]:
    try:
        result = subprocess.run(
            ["/usr/bin/drutil", "toc"], capture_output=True, timeout=30, check=False
        )
        if result.returncode:
            detail = result.stderr.decode(errors="replace").strip()
            raise InspectionError(detail or "drutil toc failed")
        return parse_drutil_toc(result.stdout.decode(errors="replace")), ""
    except (OSError, subprocess.TimeoutExpired, InspectionError) as exc:
        return (), f"Unable to query CD session layout: {exc}"


def classify(
    whole: dict[str, Any],
    volume: dict[str, Any],
    layout: OpticalLayout | None = None,
    layout_error: str = "",
) -> Inspection:
    media = str(whole.get("OpticalMediaType", "Unknown"))
    filesystem = str(volume.get("FilesystemType", "Unknown"))
    mount = volume.get("MountPoint")
    tracks = positive_integer(whole.get("OpticalMediaNumTracks"))
    sessions = positive_integer(whole.get("OpticalMediaNumSessions"))
    reason = ""
    classification = "NEEDS_REVIEW"
    is_dvd = media.upper() in DVD_TYPES
    image_device = str(whole.get("DeviceNode", whole.get("DeviceIdentifier", "")))
    image_size = positive_integer(whole.get("Size")) or positive_integer(whole.get("IOKitSize"))
    logical_cd_track = False
    if media.upper() in CD_TYPES and whole.get("DeviceBlockSize") == 2352:
        # macOS exposes a CD's whole-disc node in raw 2352-byte sectors, while
        # its mounted data track is a separate 2048-byte logical-sector slice.
        volume_node = volume.get("DeviceNode")
        volume_size = positive_integer(volume.get("Size")) or positive_integer(
            volume.get("IOKitSize")
        )
        valid_cd_track = (
            volume.get("ParentWholeDisk") == whole.get("DeviceIdentifier")
            and isinstance(volume_node, str)
            and re.fullmatch(r"/dev/disk\d+s\d+", volume_node)
            and volume.get("DeviceBlockSize") == 2048
            and volume.get("Content") in {"CD_ROM_Mode_1", "CD_ROM_Mode_2_Form_1"}
            and volume_size is not None
            and volume_size % 2048 == 0
        )
        if valid_cd_track:
            image_device = volume_node
            image_size = volume_size
            logical_cd_track = True
    if layout:
        if (
            layout.media != media.upper()
            or (tracks is not None and tracks != layout.tracks)
            or (sessions is not None and sessions != layout.sessions)
        ):
            reason = (
                "Conflicting diskutil and drutil metadata; "
                "retry inspection with the disc unchanged."
            )
        else:
            tracks = tracks or layout.tracks
            sessions = sessions or layout.sessions
    multisession_candidate = False
    if reason:
        pass
    elif media.upper() not in CD_TYPES | DVD_TYPES:
        reason = "Media is not a positively identified supported CD or DVD type."
    elif tracks != 1 or sessions != 1:
        if (
            media.upper() in {"CD-R", "CD-RW"}
            and tracks is not None
            and sessions is not None
            and tracks == sessions
            and tracks > 1
            and filesystem.lower() in {"cd9660", "iso9660"}
            and isinstance(mount, str)
            and mount
            and volume.get("WritableVolume") is False
        ):
            multisession_candidate = True
            try:
                names = {entry.name.upper() for entry in Path(mount).iterdir()}
                if names & {
                    "DVD_RTAV",
                    "BDMV",
                    "HVDVD_TS",
                    "AACS",
                    "VCD",
                    "SVCD",
                    "MPEGAV",
                    "MPEG2",
                }:
                    multisession_candidate = False
                    reason = "Video or specialized media marker found; layout requires review."
                else:
                    reason = (
                        "Possible one-data-track-per-session CD. Ingestion will proceed only "
                        "if cdrdao confirms every session is Mode 2 Form 1 data."
                    )
            except OSError as exc:
                multisession_candidate = False
                reason = f"Unable to read mounted volume {mount}: {exc}"
        else:
            reason = (
                "Requires exactly one reported track and session; layout is unusual or unknown."
            )
            if layout_error and (tracks is None or sessions is None):
                reason += " " + layout_error
    elif whole.get("OpticalMediaNumAudioTracks", 0) != 0:
        reason = "Audio tracks require a different preservation strategy."
    elif filesystem.lower() not in (
        {"cd9660", "iso9660", "udf"} if is_dvd else {"cd9660", "iso9660"}
    ):
        reason = "Requires ISO 9660 on CDs, or ISO 9660/UDF on DVDs."
    elif not isinstance(mount, str) or not mount:
        reason = "No mounted filesystem is available; mount the disc read-only before retrying."
    elif not (
        volume.get("WritableVolume") is False
        or ("WritableVolume" not in volume and volume.get("ReadOnlyVolume") is True)
    ):
        reason = "The volume is not confirmed read-only."
    else:
        try:
            # Only examine names at the root. Never follow links or traverse content.
            names = {entry.name.upper() for entry in Path(mount).iterdir()}
            if names & {"DVD_RTAV", "BDMV", "HVDVD_TS", "AACS"}:
                reason = "Specialized video-disc markers found; layout requires review."
            elif names & {"VCD", "SVCD", "MPEGAV", "MPEG2"}:
                reason = "Video-disc markers found; layout requires review."
            elif "VIDEO_TS" in names and is_dvd:
                classification = "LIKELY VIDEO DVD"
                reason = (
                    "VIDEO_TS marker found; format and full readability have not been verified."
                )
            elif "AUDIO_TS" in names and is_dvd:
                classification = "LIKELY AUDIO DVD"
                reason = "AUDIO_TS marker found; it may be empty. DVD audio format is unverified."
            elif names & {"VIDEO_TS", "AUDIO_TS"}:
                reason = "Video-disc markers found on a CD; layout requires review."
            else:
                classification = "DATA DVD" if is_dvd else "DATA CD"
                reason = "Appears to be an ordinary single-track, single-session data disc."
        except OSError as exc:
            reason = f"Unable to read mounted volume {mount}: {exc}"
    if (
        classification == "DATA CD"
        and whole.get("DeviceBlockSize") == 2352
        and not logical_cd_track
    ):
        classification = "NEEDS_REVIEW"
        reason = "Could not confirm a mounted 2048-byte logical data-track device."
    return Inspection(
        device=str(whole.get("DeviceNode", whole.get("DeviceIdentifier", "Unknown"))),
        media=media,
        volume=str(volume.get("VolumeName", "Unknown")),
        mount_point=mount if isinstance(mount, str) else "Unknown",
        filesystem=filesystem,
        capacity=positive_integer(whole.get("TotalSize")),
        tracks=tracks,
        classification=classification,
        reason=reason,
        sessions=sessions,
        inspection_supported=classification != "NEEDS_REVIEW",
        image_size=image_size,
        image_device=image_device,
        hardware={
            **{
                key: whole[key]
                for key in (
                "MediaName",
                "IORegistryEntryName",
                "BusProtocol",
                "DeviceBlockSize",
                "OpticalMediaType",
                "WritableMedia",
                "Content",
                "OpticalMediaNumTracks",
                "OpticalMediaNumSessions",
                "OpticalMediaNumAudioTracks",
                "VolumeUUID",
                "DiskUUID",
                )
                if key in whole
            },
            "VolumeContent": volume.get("Content"),
        },
        volume_device=str(volume.get("DeviceNode", "")),
        multisession_candidate=multisession_candidate,
    )


def inspect_media() -> list[Inspection]:
    if platform.system() != "Darwin":
        raise InspectionError("Media inspection requires macOS and its native diskutil utility.")
    listing = diskutil("list", "-plist")
    devices = listing.get("AllDisks")
    if not isinstance(devices, list) or not all(
        isinstance(item, str) and re.fullmatch(r"disk\d+(?:s\d+)*", item) for item in devices
    ):
        raise InspectionError("diskutil list did not return a valid AllDisks list")
    # Query every node so child volumes retain their own mount/filesystem metadata.
    infos = [diskutil("info", "-plist", device) for device in devices]
    reports = []
    optical = [
        info
        for info in infos
        if info.get("WholeDisk", info.get("Whole")) is True
        and (info.get("OpticalDeviceType") or info.get("OpticalMediaType"))
    ]
    layouts, layout_error = drutil_status() if optical else ({}, "")
    cd_whole_count = sum(
        str(item.get("OpticalMediaType", "")).upper() in CD_TYPES for item in optical
    )
    for whole in infos:
        if whole.get("WholeDisk", whole.get("Whole")) is not True or not (
            whole.get("OpticalDeviceType") or whole.get("OpticalMediaType")
        ):
            continue
        identifier = whole.get("DeviceIdentifier")
        volumes = [
            info
            for info in infos
            if info is not whole
            and info.get("ParentWholeDisk") == identifier
            and info.get("MountPoint")
        ]
        if whole.get("MountPoint"):
            volumes.insert(0, whole)
        layout = layouts.get(str(whole.get("DeviceNode", f"/dev/{identifier}")))
        if len(volumes) > 1:
            report = classify(whole, {}, layout, layout_error)
            reports.append(
                replace(
                    report,
                    classification="NEEDS_REVIEW",
                    reason="Multiple mounted volumes found; layout requires review.",
                    inspection_supported=False,
                )
            )
        else:
            report = classify(whole, volumes[0] if volumes else {}, layout, layout_error)
            if report.multisession_candidate and cd_whole_count == 1:
                session_layout, detail = drutil_toc()
                if len(session_layout) == report.sessions:
                    report = replace(report, session_layout=list(session_layout))
                else:
                    report = replace(
                        report,
                        multisession_candidate=False,
                        reason=detail or "Could not confirm one data track per session.",
                    )
            elif (
                report.classification == "DATA CD"
                and whole.get("DeviceBlockSize") == 2352
                and cd_whole_count == 1
            ):
                session_layout, detail = drutil_toc()
                if (
                    len(session_layout) == 1
                    and session_layout[0]["session"] == 1
                    and session_layout[0]["track"] == 1
                    and session_layout[0]["sectors"] * 2048 == report.image_size
                ):
                    report = replace(report, session_layout=list(session_layout))
                else:
                    report = replace(
                        report,
                        classification="NEEDS_REVIEW",
                        inspection_supported=False,
                        reason=detail or "Could not match the data track TOC to its logical size.",
                    )
            reports.append(report)
    return reports

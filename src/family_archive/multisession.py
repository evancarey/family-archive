"""Read-only capture and strict Mode 2/Joliet handling for data multisession CDs."""

import hashlib
import os
import plistlib
import re
import shutil
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

SECTOR_SIZE = 2352
USER_DATA_SIZE = 2048
RAW_READ_BATCH_SECTORS = 512


class MultisessionError(ValueError):
    """The disc or one of its captured tracks cannot be handled safely."""


@dataclass(frozen=True)
class Track:
    session: int
    track: int
    start_lba: int
    sectors: int
    path: Path
    sha256: str
    toc: str


def _run(command: list[str], timeout: int = 3600) -> str:
    try:
        result = subprocess.run(command, capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MultisessionError(f"{command[0]} failed: {exc}") from exc
    if result.returncode:
        detail = result.stderr.decode(errors="replace").strip()
        raise MultisessionError(f"{command[0]} failed: {detail or 'read failed'}")
    return result.stdout.decode(errors="replace")


def _device(executable: str) -> str:
    try:
        result = subprocess.run(
            [executable, "scanbus"], capture_output=True, timeout=30, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MultisessionError(f"{executable} scanbus failed: {exc}") from exc
    output = (result.stdout + b"\n" + result.stderr).decode(errors="replace")
    if result.returncode:
        raise MultisessionError(f"{executable} scanbus failed: {output.strip() or 'unknown error'}")
    devices = re.findall(r"^(IOService:.*?) : [^,]+, .+$", output, re.M)
    if len(devices) != 1:
        raise MultisessionError(
            f"Could not uniquely identify the optical drive for cdrdao "
            f"({len(devices)} candidates). scanbus output: {output.strip()[:1000] or '(empty)'}"
        )
    return devices[0]


def _toc(command: list[str], session: int, start_lba: int) -> tuple[int, str]:
    output = _run(command)
    tracks = re.findall(r"^TRACK\s+(\S+)", output, re.M)
    lengths = re.findall(r'^DATAFILE\s+".*"\s+(\d+):(\d+):(\d+)', output, re.M)
    if tracks != ["MODE2_RAW"] or len(lengths) != 1 or "CD_ROM_XA" not in output:
        raise MultisessionError(
            f"Session {session} is not exactly one Mode 2 data track; "
            "audio/mixed layouts need review"
        )
    minute, second, frame = map(int, lengths[0])
    sectors = (minute * 60 + second) * 75 + frame
    return sectors, output


def _canonical_toc(toc: str, session: int) -> str:
    return re.sub(
        r'DATAFILE\s+".*?"',
        f'DATAFILE "session-{session:02d}.bin"',
        toc,
        count=1,
    )


def _restore_readonly_mount(device: str, volume_device: str, mount_point: Path) -> None:
    last_error = ""
    for attempt in range(5):
        result = subprocess.run(
            ["/usr/sbin/diskutil", "info", "-plist", volume_device],
            capture_output=True,
            timeout=30,
            check=False,
        )
        if result.returncode == 0:
            try:
                info = plistlib.loads(result.stdout)
            except (ValueError, plistlib.InvalidFileException) as exc:
                raise MultisessionError(
                    "diskutil returned invalid mount metadata after capture"
                ) from exc
            existing = info.get("MountPoint")
            if existing:
                if Path(existing) != mount_point:
                    raise MultisessionError(
                        f"Optical volume remounted at unexpected path: {existing}"
                    )
                if not (info.get("WritableVolume") is False or info.get("ReadOnlyVolume") is True):
                    raise MultisessionError("Optical volume is mounted but not confirmed read-only")
                return
        else:
            last_error = result.stderr.decode(errors="replace").strip()
        try:
            # Disk Arbitration can drop the volume slice after cdrdao releases it.
            _run(["/usr/sbin/diskutil", "mountDisk", "readOnly", device], timeout=60)
        except MultisessionError as exc:
            last_error = str(exc)
        if attempt < 4:
            time.sleep(2)
    raise MultisessionError(
        "Could not verify the read-only optical volume mount after capture"
        + (f": {last_error}" if last_error else "")
    )


def _unmount_device(device: str) -> None:
    """Retry transient Disk Arbitration busy errors before the raw drive read."""
    error = None
    for attempt in range(4):
        try:
            _run(["/usr/sbin/diskutil", "unmountDisk", device], timeout=60)
            return
        except MultisessionError as exc:
            error = exc
            if attempt < 3:
                time.sleep(attempt + 1)
    raise error


def capture_tracks(
    device: str,
    volume_device: str,
    mount_point: Path,
    start_lbas: list[int],
    track_lengths: list[int],
    directory: Path,
    previous: list[dict] | None = None,
    checkpoint=None,
) -> list[Track]:
    """Capture/recheck one track per session, always restoring a read-only mount.

    Complete checkpoints are reused only after a fresh physical read matches
    their byte count and SHA-256. A failed track is retried from its beginning.
    """
    if not re.fullmatch(r"/dev/disk\d+s\d+", volume_device):
        raise MultisessionError("Cannot identify the mounted optical volume for safe remount")
    if len(start_lbas) < 2 or len(start_lbas) != len(track_lengths):
        raise MultisessionError("Invalid multisession track layout")
    executable = shutil.which("cdrdao")
    if executable is None:
        raise MultisessionError("cdrdao is required; install it with `brew install cdrdao`")
    directory.mkdir(parents=True, exist_ok=True)
    for path in directory.iterdir():
        if path.is_symlink() or (path.exists() and not stat.S_ISREG(path.stat().st_mode)):
            raise MultisessionError(f"Unsafe capture path: {path}")
    old = {item["session"]: item for item in (previous or [])}
    tracks = []
    unmounted = False
    try:
        unmounted = True
        _unmount_device(device)
        cdrdao_device = _device(executable)
        for session, (start, expected_sectors) in enumerate(
            zip(start_lbas, track_lengths, strict=True), start=1
        ):
            target = directory / f"session-{session:02d}.bin"
            toc_path = directory / f"session-{session:02d}.toc"
            saved = old.get(session)
            if (target.exists() or toc_path.exists()) and not saved:
                raise MultisessionError(
                    f"Session {session} has local data without a catalog checkpoint; "
                    f"review {target}"
                )
            if saved and (not target.is_file() or not toc_path.is_file()):
                raise MultisessionError(f"Session {session} checkpoint files are missing")
            candidate = directory / f"session-{session:02d}.candidate.bin"
            candidate_toc = directory / f"session-{session:02d}.candidate.toc"
            for path in (candidate, candidate_toc):
                if path.is_symlink() or (path.exists() and not path.is_file()):
                    raise MultisessionError(f"Unsafe temporary capture path: {path}")
                path.unlink(missing_ok=True)
            args = [
                executable,
                "read-cd",
                "--device",
                cdrdao_device,
                "--read-raw",
                "--tao-source",
                "--tao-source-adjust",
                "2",
                "--session",
                str(session),
                "--datafile",
                str(candidate),
                str(candidate_toc),
            ]
            _run(args)
            if candidate.is_symlink() or candidate_toc.is_symlink():
                raise MultisessionError("cdrdao replaced a capture path with a symlink")
            size = candidate.stat().st_size
            expected_size = (expected_sectors - 2) * SECTOR_SIZE
            if size != expected_size or candidate_toc.stat().st_size > 1024 * 1024:
                raise MultisessionError(
                    f"Session {session} captured {size} bytes; expected {expected_size}. "
                    "Disc recording mode or session layout needs review"
                )
            toc = candidate_toc.read_text(errors="strict")
            count, _ = _toc(["/bin/cat", str(candidate_toc)], session, start)
            if count != expected_sectors - 2:
                raise MultisessionError(f"Session {session} TAO-adjusted track length changed")
            toc = _canonical_toc(toc, session)
            digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
            if saved:
                if (
                    saved.get("sha256") != digest
                    or saved.get("size") != size
                    or saved.get("start_lba") != start
                    or saved.get("sectors") != expected_sectors - 2
                    or saved.get("toc") != toc
                ):
                    raise MultisessionError(
                        f"Session {session} differs from its prior checkpoint; "
                        "keep both media copies"
                    )
                candidate.unlink()
                candidate_toc.unlink()
                existing_toc = toc_path.read_text(errors="strict")
                if _canonical_toc(existing_toc, session) != toc:
                    raise MultisessionError(f"Session {session} TOC checkpoint changed")
                if existing_toc != toc:
                    toc_path.write_text(toc)
            else:
                candidate_toc.write_text(toc)
                os.replace(candidate, target)
                os.replace(candidate_toc, toc_path)
            track = Track(session, session, start, expected_sectors - 2, target, digest, toc)
            tracks.append(track)
            if checkpoint:
                checkpoint(tracks)
    except BaseException as operation_error:
        if unmounted:
            try:
                _restore_readonly_mount(device, volume_device, mount_point)
            except Exception as mount_error:
                operation_error.add_note(f"Read-only remount also failed: {mount_error}")
        raise
    else:
        if unmounted:
            _restore_readonly_mount(device, volume_device, mount_point)
    return tracks


def track_metadata(track: Track, offset: int) -> dict:
    return {
        "session": track.session,
        "track": track.track,
        "start_lba": track.start_lba,
        "sectors": track.sectors,
        "sector_size": SECTOR_SIZE,
        "offset_bytes": offset,
        "size": track.sectors * SECTOR_SIZE,
        "sha256": track.sha256,
        "toc": track.toc,
    }


def build_composite(tracks: list[Track], image: Path) -> tuple[int, str, list[dict]]:
    candidate = image.with_name(image.name + ".candidate")
    if image.is_symlink() or candidate.is_symlink():
        raise MultisessionError("Composite image paths must not be symlinks")
    if candidate.exists():
        if not candidate.is_file():
            raise MultisessionError("Composite candidate is not a regular file")
        candidate.unlink()
    digest = hashlib.sha256()
    metadata = []
    offset = 0
    with candidate.open("xb") as output:
        for track in tracks:
            if track.path.is_symlink() or track.path.stat().st_size != track.sectors * SECTOR_SIZE:
                raise MultisessionError(f"Session {track.session} image changed")
            metadata.append(track_metadata(track, offset))
            with track.path.open("rb") as source:
                while chunk := source.read(1024 * 1024):
                    digest.update(chunk)
                    output.write(chunk)
                    offset += len(chunk)
        output.flush()
        os.fsync(output.fileno())
    checksum = digest.hexdigest()
    if image.exists():
        if (
            image.stat().st_size != offset
            or hashlib.sha256(image.read_bytes()).hexdigest() != checksum
        ):
            candidate.unlink()
            raise MultisessionError("Composite image differs from the verified session tracks")
        candidate.unlink()
    else:
        os.replace(candidate, image)
    return offset, checksum, metadata


class TrackReader:
    """Read logical 2048-byte Mode 2 sectors across absolute track extents."""

    def __init__(self, tracks: list[Track]):
        self.data = []
        for track in tracks:
            raw = track.path.read_bytes()
            if len(raw) != track.sectors * SECTOR_SIZE:
                raise MultisessionError(f"Session {track.session} raw image size changed")
            if hashlib.sha256(raw).hexdigest() != track.sha256:
                raise MultisessionError(f"Session {track.session} raw image hash changed")
            self.data.append((track.start_lba, track.start_lba + track.sectors, raw))

    def sector(self, lba: int) -> bytes:
        for start, end, raw in self.data:
            if start <= lba < end:
                offset = (lba - start) * SECTOR_SIZE
                sector = raw[offset : offset + SECTOR_SIZE]
                validate_sector(sector, lba)
                return sector[24:2072]
        raise MultisessionError(f"Referenced sector {lba} was not captured")


_EDC_TABLE = []
for _value in range(256):
    _crc = _value
    for _ in range(8):
        _crc = (_crc >> 1) ^ (0xD8018001 if _crc & 1 else 0)
    _EDC_TABLE.append(_crc)


def _edc(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc = (crc >> 8) ^ _EDC_TABLE[(crc ^ byte) & 0xFF]
    return crc


_ECC_F = [((i << 1) ^ (0x11D if i & 0x80 else 0)) & 0xFF for i in range(256)]
_ECC_B = [0] * 256
for _index in range(256):
    _ECC_B[_index ^ _ECC_F[_index]] = _index


def _ecc(data: bytes, major_count: int, minor_count: int, major_mult: int, minor_inc: int):
    size = major_count * minor_count
    if len(data) != size:
        raise MultisessionError("Invalid ECC source length")
    output = bytearray(major_count * 2)
    for major in range(major_count):
        index = (major >> 1) * major_mult + (major & 1)
        a = b = 0
        for _ in range(minor_count):
            value = data[index]
            index = (index + minor_inc) % size
            a ^= value
            b ^= value
            a = _ECC_F[a]
        a = _ECC_B[_ECC_F[a] ^ b]
        output[major] = a
        output[major + major_count] = a ^ b
    return output


def validate_sector(sector: bytes, lba: int) -> None:
    if len(sector) != SECTOR_SIZE or sector[:12] != b"\0" + b"\xff" * 10 + b"\0":
        raise MultisessionError(f"Invalid raw sector sync at LBA {lba}")
    header = sector[12:16]
    if header[3] != 2:
        raise MultisessionError(f"Expected Mode 2 sector at LBA {lba}")

    def bcd(value):
        return (value >> 4) * 10 + (value & 15)

    if any((value >> 4) > 9 or (value & 15) > 9 for value in header[:3]):
        raise MultisessionError(f"Invalid BCD sector address at LBA {lba}")
    minute, second, frame = map(bcd, header[:3])
    if (minute * 60 + second) * 75 + frame - 150 != lba:
        raise MultisessionError(f"Sector address mismatch at LBA {lba}")
    if sector[16:20] != sector[20:24] or sector[18] & 0x20:
        raise MultisessionError(f"Expected Mode 2 Form 1 sector at LBA {lba}")
    if int.from_bytes(sector[2072:2076], "little") != _edc(sector[16:2072]):
        raise MultisessionError(f"Mode 2 EDC failure at LBA {lba}")
    check = bytearray(sector)
    check[12:16] = b"\0" * 4
    if (
        _ecc(check[12:2076], 86, 24, 2, 86) != sector[2076:2248]
        or _ecc(check[12:2248], 52, 43, 86, 88) != sector[2248:2352]
    ):
        raise MultisessionError(f"Mode 2 ECC failure at LBA {lba}")


def validate_mode1_sector(sector: bytes, lba: int) -> None:
    """Validate a raw Mode 1 CD sector, including address, EDC, and ECC."""
    if len(sector) != SECTOR_SIZE or sector[:12] != b"\0" + b"\xff" * 10 + b"\0":
        raise MultisessionError(f"Invalid raw sector sync at LBA {lba}")
    header = sector[12:16]
    if header[3] != 1:
        raise MultisessionError(f"Expected Mode 1 sector at LBA {lba}")

    def bcd(value):
        if (value >> 4) > 9 or (value & 15) > 9:
            raise MultisessionError(f"Invalid BCD sector address at LBA {lba}")
        return (value >> 4) * 10 + (value & 15)

    minute, second, frame = map(bcd, header[:3])
    if (minute * 60 + second) * 75 + frame - 150 != lba:
        raise MultisessionError(f"Sector address mismatch at LBA {lba}")
    if int.from_bytes(sector[2064:2068], "little") != _edc(sector[:2064]):
        raise MultisessionError(f"Mode 1 EDC failure at LBA {lba}")
    if sector[2068:2076] != bytes(8):
        raise MultisessionError(f"Mode 1 reserved bytes are nonzero at LBA {lba}")
    if (
        _ecc(sector[12:2076], 86, 24, 2, 86) != sector[2076:2248]
        or _ecc(sector[12:2248], 52, 43, 86, 88) != sector[2248:2352]
    ):
        raise MultisessionError(f"Mode 1 ECC failure at LBA {lba}")


def detect_unreadable_iso_tao_tail(source, start_lba: int, track_sectors: int):
    """Confirm a two-sector unreadable tail lies outside an ISO volume."""
    if track_sectors < 19:
        return None
    pvd_lba = start_lba + 16
    pvd_sector = os.pread(source.fileno(), SECTOR_SIZE, pvd_lba * SECTOR_SIZE)
    if len(pvd_sector) != SECTOR_SIZE:
        return None
    mode = pvd_sector[15]
    if mode == 1:
        validate_mode1_sector(pvd_sector, pvd_lba)
        payload = pvd_sector[16:2064]
    elif mode == 2:
        validate_sector(pvd_sector, pvd_lba)
        payload = pvd_sector[24:2072]
    else:
        return None
    if payload[0] != 1 or payload[1:6] != b"CD001" or payload[6] != 1:
        return None
    volume_sectors = int.from_bytes(payload[80:84], "little")
    if volume_sectors != int.from_bytes(payload[84:88], "big"):
        raise MultisessionError("ISO volume-space-size copies disagree")
    block_size = int.from_bytes(payload[128:130], "little")
    if block_size != int.from_bytes(payload[130:132], "big") or block_size != USER_DATA_SIZE:
        raise MultisessionError("ISO logical-block-size copies disagree or are unsupported")
    if volume_sectors != track_sectors - 2:
        return None

    unreadable = 0
    for lba in range(start_lba + track_sectors - 2, start_lba + track_sectors):
        try:
            sector = os.pread(source.fileno(), SECTOR_SIZE, lba * SECTOR_SIZE)
        except OSError:
            unreadable += 1
            continue
        if len(sector) != SECTOR_SIZE:
            unreadable += 1
            continue
        if mode == 1:
            validate_mode1_sector(sector, lba)
        else:
            validate_sector(sector, lba)
    if unreadable == 2:
        return mode, volume_sectors
    return None


class RawCdDataReader:
    """Expose a single-mode raw CD data track as verified 2048-byte sectors."""

    expected_mode = None

    def __init__(self, source, start_lba: int, sectors: int):
        if start_lba < 0 or sectors < 1:
            raise MultisessionError("Invalid Mode 2 track extent")
        self.fd = source.fileno()
        self.next_lba = start_lba
        self.end_lba = start_lba + sectors
        self.pending = bytearray()
        self.mode = None

    def _payload(self, sector: bytes, lba: int) -> bytes:
        mode = sector[15] if len(sector) > 15 else None
        if self.expected_mode is not None and mode != self.expected_mode:
            raise MultisessionError(f"Expected Mode {self.expected_mode} sector at LBA {lba}")
        if self.mode is not None and mode != self.mode:
            raise MultisessionError(f"CD track sector mode changed at LBA {lba}")
        if mode == 1:
            validate_mode1_sector(sector, lba)
            self.mode = mode
            return sector[16:2064]
        if mode == 2:
            validate_sector(sector, lba)
            self.mode = mode
            return sector[24:2072]
        raise MultisessionError(f"Unsupported raw CD sector mode at LBA {lba}")

    @property
    def sector_label(self):
        return "raw Mode 2" if self.expected_mode == 2 else "raw CD data"

    def read(self, size: int = -1) -> bytes:
        if size == 0:
            return b""
        if size < 0:
            size = (self.end_lba - self.next_lba) * USER_DATA_SIZE + len(self.pending)
        while len(self.pending) < size and self.next_lba < self.end_lba:
            needed = (size - len(self.pending) + USER_DATA_SIZE - 1) // USER_DATA_SIZE
            sectors = min(
                needed,
                RAW_READ_BATCH_SECTORS,
                self.end_lba - self.next_lba,
            )
            first_lba = self.next_lba
            try:
                raw = os.pread(
                    self.fd,
                    sectors * SECTOR_SIZE,
                    first_lba * SECTOR_SIZE,
                )
            except OSError:
                # A failed block read does not identify which sector failed.
                # Retry it sector-by-sector so the error names the exact LBA.
                for index in range(sectors):
                    lba = first_lba + index
                    try:
                        sector = os.pread(self.fd, SECTOR_SIZE, lba * SECTOR_SIZE)
                    except OSError as sector_error:
                        raise OSError(
                            sector_error.errno,
                            f"Unable to read {self.sector_label} sector at LBA {lba}",
                        ) from sector_error
                    if len(sector) != SECTOR_SIZE:
                        raise OSError(
                            f"Short {self.sector_label} read at LBA {lba}: {len(sector)} bytes"
                        )
                    self.pending.extend(self._payload(sector, lba))
                    self.next_lba += 1
                continue
            complete_bytes = len(raw) - len(raw) % SECTOR_SIZE
            for offset in range(0, complete_bytes, SECTOR_SIZE):
                lba = first_lba + offset // SECTOR_SIZE
                sector = raw[offset : offset + SECTOR_SIZE]
                self.pending.extend(self._payload(sector, lba))
                self.next_lba += 1
            if complete_bytes != len(raw):
                lba = first_lba + complete_bytes // SECTOR_SIZE
                raise OSError(
                    f"Short {self.sector_label} read at LBA {lba}: "
                    f"{len(raw) - complete_bytes} bytes of a sector"
                )
            if not raw and self.next_lba < self.end_lba:
                raise OSError(f"Short {self.sector_label} read at LBA {self.next_lba}: 0 bytes")
        length = min(size, len(self.pending))
        result = bytes(self.pending[:length])
        del self.pending[:length]
        return result


class Mode2Form1Reader(RawCdDataReader):
    """Expose a confirmed raw Mode 2 Form 1 track as verified data sectors."""

    expected_mode = 2


def _both_endian(data: bytes, width: int = 4) -> int:
    little = int.from_bytes(data[:width], "little")
    big = int.from_bytes(data[width : width * 2], "big")
    if little != big:
        raise MultisessionError("ISO both-endian integer copies disagree")
    return little


def _record_location(record: bytes) -> tuple[int, int]:
    if len(record) < 34 or record[1] or record[26] or record[27] or record[25] & 0x80:
        raise MultisessionError("Unsupported ISO extended attribute/interleaved/multi-extent file")
    return _both_endian(record[2:10]), _both_endian(record[10:18])


def inventory_sessions(tracks: list[Track], on_file=None) -> list[dict]:
    """Hash regular files in every session's ISO/Joliet view, retaining provenance."""
    reader = TrackReader(tracks)
    entries = []

    def read_extent(lba: int, size: int) -> bytes:
        if size < 0 or size > 4 * 1024 * 1024 * 1024:
            raise MultisessionError("ISO extent exceeds the 4 GiB safety limit")
        data = bytearray()
        for block in range((size + USER_DATA_SIZE - 1) // USER_DATA_SIZE):
            data.extend(reader.sector(lba + block))
        return bytes(data[:size])

    for track in tracks:
        descriptors = []
        for block in range(track.start_lba + 16, track.start_lba + 80):
            descriptor = reader.sector(block)
            if descriptor[1:6] != b"CD001" or descriptor[6] != 1:
                raise MultisessionError(f"Session {track.session} has an invalid ISO descriptor")
            descriptors.append(descriptor)
            if descriptor[0] == 255:
                break
        else:
            raise MultisessionError(f"Session {track.session} has no ISO descriptor terminator")
        joliet = [
            descriptor
            for descriptor in descriptors
            if descriptor[0] == 2 and descriptor[88:91] in {b"%/@", b"%/C", b"%/E"}
        ]
        descriptor = (
            joliet[-1] if joliet else next((item for item in descriptors if item[0] == 1), None)
        )
        if descriptor is None:
            raise MultisessionError(f"Session {track.session} has no supported ISO/Joliet tree")
        if _both_endian(descriptor[128:132], 2) != USER_DATA_SIZE:
            raise MultisessionError("Only 2048-byte ISO logical blocks are supported")
        encoding = "utf-16-be" if joliet else "ascii"
        root_record = descriptor[156 : 156 + descriptor[156]]
        active = set()
        prefix = f"Sessions/{track.session:02d}"

        def walk(record: bytes, parent: str, depth: int):
            if depth > 100:
                raise MultisessionError("ISO directory nesting exceeds 100 levels")
            lba, size = _record_location(record)
            if lba in active:
                raise MultisessionError("ISO directory cycle detected")
            active.add(lba)
            content = read_extent(lba, size)
            offset = 0
            while offset < len(content):
                length = content[offset]
                if length == 0:
                    offset = (offset // USER_DATA_SIZE + 1) * USER_DATA_SIZE
                    continue
                child = content[offset : offset + length]
                if len(child) != length or (offset % USER_DATA_SIZE) + length > USER_DATA_SIZE:
                    raise MultisessionError("Malformed ISO directory record")
                offset += length
                child_lba, child_size = _record_location(child)
                raw_name = child[33 : 33 + child[32]]
                if raw_name in {b"\0", b"\1"}:
                    continue
                name = raw_name.decode(encoding, errors="strict")
                if name.endswith(";1"):
                    name = name[:-2]
                if not name or name in {".", ".."} or any(c in name for c in ("/", "\0")):
                    raise MultisessionError("Unsafe or malformed ISO filename")
                original_path = f"{parent}/{name}" if parent else name
                archive_path = f"{prefix}/{original_path}"
                is_dir = bool(child[25] & 2)
                item = {
                    "path": archive_path,
                    "source_path": original_path,
                    "session": track.session,
                    "filename": name,
                    "kind": "directory" if is_dir else "file",
                    "iso_timestamp": child[18:25].hex(),
                }
                if is_dir:
                    entries.append(item)
                    walk(child, original_path, depth + 1)
                else:
                    if child[25] & 0x80:
                        raise MultisessionError("Multi-extent ISO files require review")
                    payload = read_extent(child_lba, child_size)
                    if on_file is not None:
                        on_file(item, payload)
                    item.update(
                        size=child_size,
                        sha256=hashlib.sha256(payload).hexdigest(),
                    )
                    entries.append(item)
            active.remove(lba)

        walk(root_record, "", 0)
    return entries

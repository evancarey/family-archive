import copy
import hashlib
import io
import os
import plistlib
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from family_archive import media
from family_archive.config import Config
from family_archive.manifest import deserialize, serialize
from family_archive.multisession import (
    Mode2Form1Reader,
    MultisessionError,
    RawCdDataReader,
    Track,
    _canonical_toc,
    _device,
    _ecc,
    _edc,
    _restore_readonly_mount,
    _unmount_device,
    detect_unreadable_iso_tao_tail,
    inventory_sessions,
    validate_mode1_sector,
    validate_sector,
)
from family_archive.verification import verify_source

FIXTURES = Path(__file__).parent / "fixtures"


def raw_mode2_sector(lba):
    frames = lba + 150
    minute, remainder = divmod(frames, 60 * 75)
    second, frame = divmod(remainder, 75)

    def bcd(value):
        return (value // 10) * 16 + value % 10

    sector = bytearray(2352)
    sector[:12] = b"\0" + b"\xff" * 10 + b"\0"
    sector[12:16] = bytes((bcd(minute), bcd(second), bcd(frame), 2))
    sector[16:20] = b"\x01\x02\x00\x00"
    sector[20:24] = sector[16:20]
    sector[24:2072] = bytes((index * 17) % 256 for index in range(2048))
    sector[2072:2076] = _edc(sector[16:2072]).to_bytes(4, "little")
    check = bytearray(sector)
    check[12:16] = b"\0" * 4
    parity = _ecc(check[12:2076], 86, 24, 2, 86)
    sector[2076:2248] = parity
    check[2076:2248] = parity
    sector[2248:2352] = _ecc(check[12:2248], 52, 43, 86, 88)
    return bytes(sector)


def mode2_sector(lba, payload):
    sector = bytearray(raw_mode2_sector(lba))
    sector[24:2072] = payload.ljust(2048, b"\0")
    sector[2072:2076] = _edc(sector[16:2072]).to_bytes(4, "little")
    check = bytearray(sector)
    check[12:16] = b"\0" * 4
    parity = _ecc(check[12:2076], 86, 24, 2, 86)
    sector[2076:2248] = parity
    check[2076:2248] = parity
    sector[2248:2352] = _ecc(check[12:2248], 52, 43, 86, 88)
    return bytes(sector)


def mode1_sector(lba, payload):
    frames = lba + 150
    minute, remainder = divmod(frames, 60 * 75)
    second, frame = divmod(remainder, 75)

    def bcd(value):
        return (value // 10) * 16 + value % 10

    sector = bytearray(2352)
    sector[:12] = b"\0" + b"\xff" * 10 + b"\0"
    sector[12:16] = bytes((bcd(minute), bcd(second), bcd(frame), 1))
    sector[16:2064] = payload.ljust(2048, b"\0")
    sector[2064:2068] = _edc(sector[:2064]).to_bytes(4, "little")
    sector[2076:2248] = _ecc(sector[12:2076], 86, 24, 2, 86)
    sector[2248:2352] = _ecc(sector[12:2248], 52, 43, 86, 88)
    return bytes(sector)


def iso_record(lba, size, name, directory=False):
    encoded = name if isinstance(name, bytes) else name.encode("ascii")
    record = bytearray(33 + len(encoded) + (len(encoded) % 2 == 0))
    record[0] = len(record)
    record[2:6] = lba.to_bytes(4, "little")
    record[6:10] = lba.to_bytes(4, "big")
    record[10:14] = size.to_bytes(4, "little")
    record[14:18] = size.to_bytes(4, "big")
    record[25] = 2 if directory else 0
    record[28:30] = (1).to_bytes(2, "little")
    record[30:32] = (1).to_bytes(2, "big")
    record[32] = len(encoded)
    record[33 : 33 + len(encoded)] = encoded
    return bytes(record)


def test_parse_multisession_drutil_toc():
    toc = (FIXTURES / "multisession_drutil_toc.txt").read_text()
    assert media.parse_drutil_toc(toc) == (
        {"session": 1, "track": 1, "start_lba": 0, "sectors": 10254},
        {"session": 2, "track": 2, "start_lba": 21654, "sectors": 10450},
        {"session": 3, "track": 3, "start_lba": 39004, "sectors": 18895},
    )


def test_canonical_toc_removes_temporary_capture_path():
    raw = 'DATAFILE "/tmp/session-01.candidate.bin" 01:00:00\n'
    assert _canonical_toc(raw, 1) == 'DATAFILE "session-01.bin" 01:00:00\n'


def test_mount_restore_recreates_volume_slice_when_initial_info_fails(monkeypatch, tmp_path):
    calls = []
    responses = [
        SimpleNamespace(returncode=1, stdout=b"", stderr=b"volume not found"),
        SimpleNamespace(
            returncode=0,
            stdout=plistlib.dumps({"MountPoint": str(tmp_path / "disc"), "WritableVolume": False}),
            stderr=b"",
        ),
    ]
    monkeypatch.setattr(
        "family_archive.multisession.subprocess.run", lambda *a, **k: responses.pop(0)
    )
    monkeypatch.setattr(
        "family_archive.multisession._run", lambda command, **kwargs: calls.append(command)
    )

    _restore_readonly_mount("/dev/disk5", "/dev/disk5s0", tmp_path / "disc")

    assert calls == [["/usr/sbin/diskutil", "mountDisk", "readOnly", "/dev/disk5"]]


def test_unmount_retries_transient_busy_errors(monkeypatch):
    calls = []

    def run(command, timeout=3600):
        calls.append(command)
        if len(calls) < 3:
            raise MultisessionError("busy")

    monkeypatch.setattr("family_archive.multisession._run", run)
    monkeypatch.setattr("family_archive.multisession.time.sleep", lambda seconds: None)

    _unmount_device("/dev/disk5")

    assert len(calls) == 3


def test_cdrdao_device_scan_reads_device_list_from_stderr(monkeypatch):
    device = (
        "IOService:/usb/drive/IOSCSILogicalUnitNub@0/"
        "com_apple_driver_AppleUSBODDType05/IODVDServices : HL-DT-ST, DVDRW GX40N, RQ00"
    )
    monkeypatch.setattr(
        "family_archive.multisession.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout=b"", stderr=(device + "\n").encode()
        ),
    )

    assert _device("cdrdao") == device.split(" : ", maxsplit=1)[0]


def test_mode2_form1_sector_checks_address_edc_and_ecc():
    sector = raw_mode2_sector(21654)
    validate_sector(sector, 21654)
    with pytest.raises(ValueError, match="address mismatch"):
        validate_sector(sector, 21655)
    changed_edc = bytearray(sector)
    changed_edc[100] ^= 1
    with pytest.raises(ValueError, match="EDC"):
        validate_sector(bytes(changed_edc), 21654)
    changed_ecc = bytearray(sector)
    changed_ecc[2080] ^= 1
    with pytest.raises(ValueError, match="ECC"):
        validate_sector(bytes(changed_ecc), 21654)


def test_mode1_sector_checks_address_edc_and_ecc():
    sector = mode1_sector(21654, b"mode one data")
    validate_mode1_sector(sector, 21654)
    with pytest.raises(ValueError, match="address mismatch"):
        validate_mode1_sector(sector, 21655)
    changed_edc = bytearray(sector)
    changed_edc[100] ^= 1
    with pytest.raises(ValueError, match="EDC"):
        validate_mode1_sector(bytes(changed_edc), 21654)
    changed_ecc = bytearray(sector)
    changed_ecc[2080] ^= 1
    with pytest.raises(ValueError, match="ECC"):
        validate_mode1_sector(bytes(changed_ecc), 21654)


def test_raw_cd_data_reader_extracts_mode1_payload(tmp_path):
    payloads = [b"mode one first", b"mode one second"]
    path = tmp_path / "raw-mode1-disc.bin"
    path.write_bytes(
        bytes(8 * 2352)
        + b"".join(mode1_sector(8 + i, payload) for i, payload in enumerate(payloads))
    )
    expected = b"".join(payload.ljust(2048, b"\0") for payload in payloads)
    with path.open("rb", buffering=0) as source:
        reader = RawCdDataReader(source, 8, 2)
        assert reader.read() == expected
        assert reader.mode == 1


def test_detect_unreadable_tao_tail_only_when_iso_volume_matches(tmp_path):
    sectors = [mode1_sector(lba, bytes(2048)) for lba in range(20)]
    pvd = bytearray(2048)
    pvd[0] = 1
    pvd[1:6] = b"CD001"
    pvd[6] = 1
    pvd[80:84] = (20).to_bytes(4, "little")
    pvd[84:88] = (20).to_bytes(4, "big")
    pvd[128:130] = (2048).to_bytes(2, "little")
    pvd[130:132] = (2048).to_bytes(2, "big")
    sectors[16] = mode1_sector(16, bytes(pvd))
    path = tmp_path / "iso-with-unreadable-tail.bin"
    path.write_bytes(b"".join(sectors))
    with path.open("rb", buffering=0) as source:
        assert detect_unreadable_iso_tao_tail(source, 0, 22) == (1, 20)

    pvd[80:84] = (19).to_bytes(4, "little")
    pvd[84:88] = (19).to_bytes(4, "big")
    sectors[16] = mode1_sector(16, bytes(pvd))
    path.write_bytes(b"".join(sectors))
    with path.open("rb", buffering=0) as source:
        assert detect_unreadable_iso_tao_tail(source, 0, 22) is None


def test_detect_unreadable_mode2_iso_tao_tail_when_volume_matches(tmp_path):
    sectors = [mode2_sector(lba, bytes(2048)) for lba in range(20)]
    pvd = bytearray(2048)
    pvd[0] = 1
    pvd[1:6] = b"CD001"
    pvd[6] = 1
    pvd[80:84] = (20).to_bytes(4, "little")
    pvd[84:88] = (20).to_bytes(4, "big")
    pvd[128:130] = (2048).to_bytes(2, "little")
    pvd[130:132] = (2048).to_bytes(2, "big")
    sectors[16] = mode2_sector(16, bytes(pvd))
    path = tmp_path / "mode2-iso-with-unreadable-tail.bin"
    path.write_bytes(b"".join(sectors))
    with path.open("r+b", buffering=0) as source:
        source.truncate(20 * 2352)
        assert detect_unreadable_iso_tao_tail(source, 0, 22) == (2, 20)


def test_mode2_form1_reader_extracts_verified_payload_in_partial_reads(tmp_path):
    payloads = [b"first sector", b"second sector"]
    raw = b"".join(mode2_sector(100 + index, payload) for index, payload in enumerate(payloads))
    path = tmp_path / "raw-disc.bin"
    path.write_bytes(bytes(100 * 2352) + raw)
    expected = b"".join(payload.ljust(2048, b"\0") for payload in payloads)
    with path.open("rb", buffering=0) as source:
        reader = Mode2Form1Reader(source, 100, 2)
        assert reader.read(13) == expected[:13]
        remainder = reader.read()
    assert expected[:13] + remainder == expected
    assert reader.read(1) == b""


def test_mode2_form1_reader_rejects_corrupt_sector(tmp_path):
    sector = bytearray(mode2_sector(5, b"payload"))
    sector[60] ^= 1
    path = tmp_path / "raw-disc.bin"
    path.write_bytes(bytes(5 * 2352) + sector)
    with path.open("rb", buffering=0) as source:
        reader = Mode2Form1Reader(source, 5, 1)
        with pytest.raises(MultisessionError, match="EDC failure at LBA 5"):
            reader.read(1)


def test_mode2_form1_reader_falls_back_to_sector_reads_for_exact_error_lba(tmp_path, monkeypatch):
    path = tmp_path / "raw-disc.bin"
    path.write_bytes(b"".join(mode2_sector(lba, b"payload") for lba in range(3)))
    real_pread = os.pread

    def pread(fd, size, offset):
        if size > 2352:
            raise OSError(5, "simulated multi-sector read failure")
        if offset == 2352:
            raise OSError(5, "simulated unreadable sector")
        return real_pread(fd, size, offset)

    monkeypatch.setattr("family_archive.multisession.os.pread", pread)
    with path.open("rb", buffering=0) as source:
        reader = Mode2Form1Reader(source, 0, 3)
        with pytest.raises(OSError, match="raw Mode 2 sector at LBA 1"):
            reader.read(3 * 2048)


def test_inventory_sessions_hashes_files_under_session_scoped_paths(tmp_path):
    sectors = [bytes(2352) for _ in range(21)]
    pvd = bytearray(2048)
    pvd[0] = 1
    pvd[1:6] = b"CD001"
    pvd[6] = 1
    pvd[128:132] = (2048).to_bytes(2, "little") + (2048).to_bytes(2, "big")
    root = iso_record(19, 2048, b"\0", directory=True)
    pvd[156 : 156 + len(root)] = root
    terminator = bytearray(2048)
    terminator[0] = 255
    terminator[1:6] = b"CD001"
    terminator[6] = 1
    directory = (
        iso_record(19, 2048, b"\0", directory=True)
        + iso_record(19, 2048, b"\1", directory=True)
        + iso_record(20, 5, "HELLO.TXT;1")
    )
    sectors[16] = mode2_sector(16, bytes(pvd))
    sectors[17] = mode2_sector(17, bytes(terminator))
    sectors[19] = mode2_sector(19, directory)
    sectors[20] = mode2_sector(20, b"hello")
    capture = b"".join(sectors)
    track_path = tmp_path / "session.bin"
    track_path.write_bytes(capture)
    track = Track(
        1,
        1,
        0,
        len(sectors),
        track_path,
        hashlib.sha256(capture).hexdigest(),
        "fixture",
    )

    extracted = {}
    entries = inventory_sessions(
        [track], on_file=lambda entry, payload: extracted.update({entry["path"]: payload})
    )

    assert entries == [
        {
            "path": "Sessions/01/HELLO.TXT",
            "source_path": "HELLO.TXT",
            "session": 1,
            "filename": "HELLO.TXT",
            "kind": "file",
            "iso_timestamp": "00000000000000",
            "size": 5,
            "sha256": hashlib.sha256(b"hello").hexdigest(),
        }
    ]
    assert extracted == {"Sessions/01/HELLO.TXT": b"hello"}


def multisession_manifest():
    first = b"a" * 2352
    second = b"b" * 2352
    data = first + second
    digest = hashlib.sha256(data).hexdigest()
    file = {
        "kind": "file",
        "path": "Sessions/01/hello.txt",
        "source_path": "hello.txt",
        "session": 1,
        "filename": "hello.txt",
        "size": 5,
        "sha256": hashlib.sha256(b"hello").hexdigest(),
    }
    track = {
        "session": 1,
        "track": 1,
        "start_lba": 0,
        "sectors": 1,
        "sector_size": 2352,
        "offset_bytes": 0,
        "size": 2352,
        "sha256": hashlib.sha256(first).hexdigest(),
        "toc": "CD_ROM_XA\nTRACK MODE2_RAW\n",
    }
    second_track = {
        **track,
        "session": 2,
        "track": 2,
        "start_lba": 100,
        "offset_bytes": 2352,
        "sha256": hashlib.sha256(second).hexdigest(),
    }
    preservation = {
        "format": "concatenated_mode2_raw_session_tracks",
        "sector_size": 2352,
        "key": "sources/SRC-0001/image/SRC-0001.bin",
        "size": 4704,
        "sha256": digest,
        "tracks": [track, second_track],
        "scope": "test",
    }
    return {
        "schema_version": 2,
        "source_id": "SRC-0001",
        "source_type": "multisession_data_cd",
        "archive_status": "COMPLETE",
        "started_at": "2026-01-01T00:00:00+00:00",
        "completed_at": "2026-01-01T01:00:00+00:00",
        "inspection": {"volume": "TEST"},
        "destination": {"bucket": "test", "prefix": ""},
        "preservation": preservation,
        "inventory_origin": "multisession_tracks",
        "file_count": 1,
        "directory_count": 0,
        "logical_file_size": 5,
        "files": [file],
    }


def test_multisession_manifest_round_trip():
    manifest = multisession_manifest()
    assert deserialize(serialize(manifest)) == manifest


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda m: m["preservation"].update(size=4705), "track image metadata"),
        (lambda m: m["preservation"]["tracks"][1].update(offset_bytes=2353), "track"),
        (lambda m: m["files"][0].update(path="../hello.txt"), "Unsafe"),
        (lambda m: m["files"][0].update(source_path="../hello.txt"), "session-scoped"),
    ],
)
def test_multisession_manifest_rejects_invalid_layout(mutate, message):
    manifest = copy.deepcopy(multisession_manifest())
    mutate(manifest)
    with pytest.raises(ValueError, match=message):
        serialize(manifest)


class MemoryReader:
    def __init__(self, objects):
        self.objects = objects

    @contextmanager
    def open_read(self, key):
        yield io.BytesIO(self.objects[key])


def test_multisession_verification_checks_each_track(tmp_path):
    manifest = multisession_manifest()
    config = Config("archive-test", database=tmp_path / "catalog.db")
    manifest["destination"] = {"bucket": config.bucket, "prefix": config.prefix}
    payload = b"a" * 2352 + b"b" * 2352
    manifest_key = config.key("sources/SRC-0001/manifest.json")
    image_key = manifest["preservation"]["key"]
    objects = {manifest_key: serialize(manifest), image_key: payload}
    reader = MemoryReader(objects)

    verified = verify_source(reader, config, "SRC-0001", announce=lambda message: None)
    assert verified.image_sha256 == hashlib.sha256(payload).hexdigest()

    changed = b"a" * 2352 + b"c" * 2352
    manifest["preservation"]["sha256"] = hashlib.sha256(changed).hexdigest()
    objects[manifest_key] = serialize(manifest)
    objects[image_key] = changed
    with pytest.raises(ValueError, match="session 2 track image verification FAILED"):
        verify_source(reader, config, "SRC-0001", announce=lambda message: None)

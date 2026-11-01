# Multisession data CD support

The implementation supports one narrow multisession layout: a CD-R/CD-RW with
one CD-ROM XA Mode 2 raw data track in each session, where every session exposes
a readable ISO 9660/Joliet filesystem. Audio, mixed-mode, video, Mode 1, multiple
tracks per session, and unsupported ISO features are rejected for review.

## Preservation format

The application captures each session separately with cdrdao and stores their
raw 2352-byte sectors concatenated in track order. Schema-v2 `manifest.json`
records the absolute start LBA, captured sector count, offset, size, TOC, and
SHA-256 of each session track, as well as the total image hash. Pregaps, lead-in,
lead-out, and subchannels are outside this representation.

On the tested CD-RW, cdrdao could not read the final two blocks at each reported
track length. Captures use cdrdao's TAO source adjustment to omit two link blocks
per session; the readable sector ranges matched independent read-only raw-device
reads. The TOC does not prove that any other disc was recorded in TAO mode, and
cdrdao warns the adjustment is wrong for DAO media. This handling is therefore
experimental and limited to this tested behavior. Any TOC, capture size, track
type, address, Mode 2 Form 1, EDC, ECC, or filesystem inconsistency fails safely.

Each session's filesystem tree is inventoried independently. Paths are prefixed
with `Sessions/NN/`, preserving prior versions as separate records. The raw
image remains the preservation master; the file inventory is descriptive.
Resume keeps completed session captures, rereads each track and compares its
hash before reuse, then independently rereads all sessions before upload. Remote
verification checks both the complete image hash and each manifest track hash.
Recovery consumes the same versioned manifest without rewriting existing
schema-v1 sources.

## Hardware evidence and limits

The current three-session `August 12, 2005` CD-RW was read-only captured. Each
session contains one Mode 2 track, and the newest mounted filesystem's 178 files
matched path, size, and SHA-256. The earlier session trees contain 96 and 96
files; two paths have earlier versions whose content differs in the latest
session. Sector EDC/ECC passed across the 39,593 captured sectors. Capture
comparisons, hashes, TOCs, and raw command output remain in ignored
`diagnostics/multisession-cdrw/`.

The physical disc was ingested as SRC-0002. All three saved session captures
matched fresh physical rereads, sector EDC/ECC and all 370 file records passed,
and the 93,122,736-byte combined image, schema-v2 manifest, and catalog backups
were uploaded and verified. The standalone verifier independently streamed the
complete S3 image and passed. A separate recovery rebuilt the catalog from S3;
all 370 file hashes, 412 total entries, and the image hash matched the working
catalog. The drive ended mounted read-only.

During this run, production testing found and fixed three issues: cdrdao writes
its scanbus listing to stderr; temporary file paths in saved TOC sidecars must be
normalized to resume; and macOS may temporarily remove the volume slice after
cdrdao releases the drive, requiring a whole-device read-only remount with
retries. Automated tests cover these paths. A physical capture-layer interruption
drill stopped after session 1 and resumed by rereading all three tracks against
the saved hashes. A live capture worker was also killed after the
session-1 checkpoint; a fresh capture process resumed from that checkpoint and
reread all three session tracks, matching the SRC-0002 hashes. This used temporary
local test files and made no S3 or production-catalog writes. The kill occurred
between sessions. A second live drill killed capture partway through session 3;
resume discarded the partial candidate, recaptured the track, and matched all
three SRC-0002 hashes. Both drills used temporary local files and no S3 or
production-catalog writes. The fake-media/storage ingestion test now terminates
the worker abruptly with `os._exit` after the first durable SQLite checkpoint,
then resumes in a fresh process and verifies completion and run history. A full
CLI-level test also invokes the real argument parser and ingest command, sends
SIGKILL after the first durable checkpoint, then resumes through `--resume` with
local fake storage. Install the runtime dependency with `brew install cdrdao`.

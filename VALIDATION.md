# First real DVD ingestion validation

One mounted DVD+R with UDF, one track, and one session was archived as `SRC-0001`.
The source remained mounted read-only; the application opened its raw character
device with `O_RDONLY`. No source data was written or reorganized.

Results:

- Complete raw logical-sector image: **3,714,285,568 bytes**. This uses raw media
  size, not the smaller mounted-filesystem size.
- Image SHA-256: `d94bae87009fe5e8f88cf0d763a99fc65dae864f4535e030b1ef4359f408f854`.
- Inventory: **2,141 regular files**, **214 directories**, **3,705,172,862 logical
  file bytes**. Every regular file has a SHA-256.
- Ctrl-C exited with status 130 and recorded `INTERRUPTED`. Resume verified all
  **687,865,856 cached bytes** against the DVD before appending new image data.
- Local image read-back and a full post-inventory source comparison passed.
- All **56 multipart image parts** uploaded. The entire stored S3 image was
  downloaded and its size and SHA-256 matched before the complete manifest was
  published.
- Manifest, SQLite checkpoint backup, and CSV backup passed S3 read-back checks.
  Only after those checks did the local source become `COMPLETE`.
- A separate read-only check downloaded the manifest and backups again, matched
  their local bytes, opened a downloaded SQLite backup in a temporary directory,
  passed `PRAGMA integrity_check`, and confirmed all 2,141 file hashes were present.
- Resuming the completed source was a no-op: no new run or replacement object
  versions were created. Each of the three source objects has one version.
- The original DVD, local cache, catalog, logs, and S3 objects were retained.

As designed, the immutable SQLite backup records the source at `VERIFYING`, just
before the final local completion transition, and contains its verified complete
manifest. This backup was checked separately without replacing the working catalog.

Hardware testing identified two setup/implementation issues. The existing virtual
environment needed the newly declared boto3 dependency installed. macOS also
returned `Resource busy` for the mounted buffered `/dev/diskN` node; using the
corresponding read-only `/dev/rdiskN` raw node resolved it without unmounting media.
Regression tests cover raw-device selection and the missing-dependency diagnostic.
The resulting suite has **119 passing tests**, with Ruff checks passing.

This establishes one successful data-DVD archival run. It does not validate every
drive/disc combination, damaged media, ordinary CDs on hardware, power loss,
physical disconnection, or interruption during a real multipart upload. Automated
tests cover upload interruption, verification failures, wrong-disc resume, and
other failure paths; additional hardware/recovery trials remain separate work.
No audio/video/mixed-mode archival support was added by this validation.

## Ordinary single-session CD attempt

An inserted CD-RW was inspected as a supported one-track, one-session Mode 2 Form
1 data CD. macOS reports the whole disc using 2352-byte sectors and exposes its
mounted data track as a 2048-byte logical slice. The first attempt reserved
SRC-0003 but could not open `/dev/rdisk5s0`, whose owner reverted to
`root:operator` after macOS recreated the device node. A later slice-based attempt
read 654,311,424 bytes before an I/O error. No source data was uploaded; the S3
reservation marker remains.

Inspection and capture now correlate the single-track TOC with the mounted logical
size and read the user-owned whole-disc raw node. The capture validates every raw
sector's address, EDC, and ECC, then extracts its 2048-byte Mode 2 Form 1 payload.
It re-read and matched the retained 654,311,424-byte prefix, then encountered
repeatable I/O errors at LBAs 321613 and 321614 (including three independent
read-only retries of each). Those are the final two declared track sectors. The
capture did not append data to the cached image or upload an image. SRC-0003 is
`FAILED` locally and remains resumable with `--resume SRC-0003`; the inserted disc
or drive needs attention before retrying. Do not treat this partial capture as a
complete archive. Automated tests cover TOC extent matching, verified sector
extraction, corrupt-sector rejection, batched reads, and exact-LBA fallback.
Ordinary CD hardware ingestion remains unvalidated.

A machine-readable copy of the live check results is kept locally at
`diagnostics/live-validation.json` (ignored by Git).

## Inventory from the local image

The subsequent ingestion optimization inventories files from a read-only mount
of the verified cached image, retaining the second complete optical read. It was
checked against the same archived DVD image without rereading the physical disc
or uploading another archive.

Mounting, inventorying/hashing all **2,141 files and 214 directories**, and detaching
took **11.194 seconds** in the local-image check. Every path, file size, SHA-256,
modification timestamp, and symlink target matched the original disc inventory.
The image's complete SHA-256 was checked before and after, and stayed unchanged;
the original manifest and completed catalog state were also retained.

This timing measures the mount/inventory/detach check, not a complete ingestion:
it excludes SQLite checkpoints, disc capture, the second optical read, and S3
upload/download. Those stages still take time. No archived objects were replaced.
Local check results are in `diagnostics/image-inventory-validation.json`.

Automated tests also check that the pipeline uses the image's filesystem instead
of the physical mount, detaches before uploading, refuses unconfirmed writable or
wrong-filesystem mounts, cleans up on interruption, preserves a failed/stale mount
for explicit cleanup, and resumes older unfinished ingestions without replacing
published manifests.
The updated suite has **134 passing tests**, with Ruff checks passing.

## Independent verification and catalog recovery

The Milestone 3 commands were exercised against the existing S3 archive without
reading the physical disc or changing source objects:

- `verify SRC-0001` streamed all **3,714,285,568 bytes**, verified SHA-256 against
  the archived manifest and the existing local catalog's copy, and exited 0.
- Manifest recovery built `diagnostics/recovered-from-manifests.db` using only
  archived manifests/reservations and full image read-back, without depending on
  the working catalog or cache image.
- Backup recovery built `diagnostics/recovered-from-backup.db` from the archived
  schema-v1 SQLite checkpoint, checked its integrity/schema/reservation identity,
  and re-verified the source image against the published manifest and checkpoint.
  The snapshot's `VERIFYING` source became `COMPLETE` only after this check.
- Both recovered databases passed `PRAGMA integrity_check`. Every file/directory
  record and original provenance matched the working database, including the
  image hash, source token, manifest, paths, sizes, modes, and timestamps.
- Both recovered ID sequences would allocate `SRC-0002` next. No test source ID
  was actually allocated in these recovered databases.
- A before/after SHA-256 of the working database matched. No original catalog was
  replaced. The recovered files are separate local artifacts, not automatically
  adopted working catalogs.

An additional real multipart trial used a separate `validation/multipart-resume/`
prefix and **12,582,912 bytes of synthetic A/B/C data**, not family content. The
first process exited with code 130 immediately after S3 acknowledged its first
5 MiB part and its checkpoint was saved. A new process confirmed that server part,
uploaded **only parts 2 and 3**, completed conditionally, and verified the entire
stored object's SHA-256 by download. The synthetic object and local checkpoint
were retained; no objects were deleted and no source objects were modified.
This tests process interruption, not physical network disconnection.

Results are stored locally in `diagnostics/recovery-validation.json` and
`diagnostics/multipart-resume-validation.json`. The expanded automated suite has
**182 passing tests**, with Ruff checks passing. Corrupt/missing objects, malformed
manifests, unexpected SQLite schemas, stale cached hashes, ID retention, existing
output paths, concurrent output creation, paginated listings, and interrupted
reads are covered without access to the real archive bucket.

## Multisession CD-RW support

The three-session `August 12, 2005` CD-RW was read without source writes. Default
raw cdrdao reads failed near track ends; independent repeats matched every
returned byte. With cdrdao's documented two-link-block TAO adjustment, all three
session captures completed and matched the independently assembled raw-sector
ranges byte for byte. The captures retain 39,593 sectors with validated absolute
addresses. This was followed by production ingestion as SRC-0002.

Experimental offline Joliet inventories hashed 96, 96, and 178 files respectively.
Two earlier file versions differ from their counterparts in the latest session.
Production ingestion reread and matched all three checkpointed tracks, validated
EDC/ECC, and recorded 370 file entries. It uploaded and verified the
93,122,736-byte image, schema-v2 manifest, SQLite backup, and CSV backup. A
separate `verify SRC-0002` run streamed the complete S3 image and passed. A
separate recovery verified both archived images, rebuilt two complete sources
and zero incomplete sources, and restored SRC-0002's 370 files (412 total
entries). Its paths, hashes, and image SHA-256 match the working catalog. The CD
ended mounted read-only. Resume was exercised across multiple safe failures,
which exposed and fixed scanbus stderr parsing, normalized TOC checkpoints, and
delayed macOS volume-slice remounting. A physical capture-layer interruption
drill stopped after session 1, then resumed and reread all three sessions against
their saved hashes. A live capture worker was killed after its session-1
checkpoint; a fresh process
resumed from temporary local checkpoint files and reread all three tracks with
hashes matching SRC-0002. This made no S3 or production-catalog writes. The kill
was between sessions. A separate live drill killed capture during session 3;
resume discarded the partial candidate, recaptured it, and matched all three
hashes. Both used temporary local files with no S3 or production-catalog writes.
A fake-media/storage ingestion test now hard-terminates its worker immediately
after the first durable SQLite checkpoint, resumes in a fresh process, and checks
the completed archive plus interrupted/completed run history. A full CLI
integration test invokes the argument parser and ingest command, receives SIGKILL
after the first durable checkpoint, then resumes through `--resume` with local
fake storage and confirms completion. The recording mode remains inferred;
support is limited to the tested TAO-style layout. See
[MULTISESSION.md](MULTISESSION.md) for detail.

## Four-session CD-RW production ingestion

A second multisession CD-RW (volume `20050702172941`) was inspected as a possible
four-session, one-data-track-per-session disc. Production capture confirmed each
session as Mode 2 Form 1, retained the supported TAO-adjusted raw tracks, validated
sector EDC/ECC, inventoried the Joliet views, and performed a second physical
capture to confirm the source identity. The source remained read-only.

SRC-0004 completed with a **208,328,400-byte** composite raw-track image and
**150 files across 53 directories**. The S3 image, portable manifest, SQLite
checkpoint, and CSV checkpoint were uploaded and verified. A separate
`verify SRC-0004` streamed the full image and passed its SHA-256 and manifest
checks. File hashes are recorded in the manifest; the standalone verification
does not extract and re-hash individual files. This validates another four-session
disc on this drive, not all multisession recording modes or damaged media.

## Single-session UDF DVD+R

The DVD+R labeled `070613_1549` was inspected as a one-track, one-session UDF
data DVD. SRC-0010 captured the **2,644,508,672-byte** logical-device image,
inventoried **319 file records**, and completed a second physical read. The image,
manifest, SQLite checkpoint, and CSV checkpoint were uploaded and verified.
Independent `verify SRC-0010` streamed the full image and passed its SHA-256 and
manifest checks. File hashes remain inventory metadata; standalone verification
does not extract and re-hash individual files.

## Single-session Mode 1 CD with unreadable tail

The CD-RW labeled `Personal Data, A` was inspected as a one-track, one-session
Mode 1 data disc. Its TOC declared 1,298 sectors, but the final two raw-sector
reads failed. The ISO primary volume descriptor declares 1,296 2048-byte sectors.
A separate read-only cdrdao diagnostic using its two-sector TAO adjustment
produced 1,296 Mode 1 raw sectors, matching that filesystem boundary. The
recording mode is inferred from this evidence; the two trailing TOC sectors are
not included in the archive.

SRC-0005 captured the full 1,296-sector ISO volume (**2,654,208 bytes**),
validated Mode 1 sector addresses, EDC, and ECC, inventoried **167 files and 9
directories**, and completed a second physical read. Its manifest records the
declared 1,298-sector track, captured 1,296 sectors, and the two unreadable
excluded sectors. The image, manifest, SQLite checkpoint, and CSV checkpoint
were uploaded and verified. Independent `verify SRC-0005` streamed the complete
image and passed its SHA-256 and manifest checks. The file hashes are recorded as
inventory metadata; standalone verification does not extract and re-hash the
individual files.

## Single-session Mode 2 CD with unreadable tail

The CD-R labeled `BIRTHDAY DEC03` was identified by macOS as a single-track,
single-session CD-ROM XA disc. Its TOC identified Mode 2, and a read-only raw
sector probe confirmed Mode 2 Form 1. The initial SRC-0008 capture reached the
final two sectors and failed with a raw read error at LBA 13,515. The ISO primary
volume descriptor declares 13,515 logical sectors while the track declares
13,517; both 2048-byte block-size copies and both volume-size copies agree. A
read-only probe confirmed both final track sectors are unreadable, so the captured
image ends at the exact filesystem boundary and records the two excluded sectors.

SRC-0008 completed with a **27,678,720-byte** image and **36 file records**.
Capture validated the Mode 2 Form 1 sectors, inventoried from a read-only image
mount, and confirmed the source with a second physical read. The image, manifest,
SQLite checkpoint, and CSV checkpoint were uploaded and verified. Independent
`verify SRC-0008` streamed the image, checked its SHA-256 and manifest, and passed.
The manifest records the 13,517-sector track, 13,515 captured sectors, and the
two excluded unreadable trailing sectors. Standalone verification does not
extract and re-hash individual files.

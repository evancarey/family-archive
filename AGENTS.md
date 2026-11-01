# Family Archive

## Project Purpose

`family-archive` is a command-line application for safely archiving aging physical digital media to Amazon S3.

The initial use case is archiving a collection of old CDs whose contents are not necessarily known in advance. Future versions must support additional source media including:

- Data CDs
- DVDs
- Audio CDs
- Video CDs
- Mixed-mode CDs
- External hard drives
- Internal hard drives connected through adapters
- USB flash drives
- SD cards
- Directories from computers or other storage devices

The primary goal is preservation.

The application must preserve the original source as faithfully as practical, verify that archived content was transferred correctly, maintain provenance, and create a searchable catalog.

A future project or later phase will provide a simple web-based photo/video/document library for nontechnical family members. Do not optimize the initial ingestion system around the web UI.

---

# Core Principles

## 1. Never Modify Source Media

Treat all source media as read-only.

The application must never:

- Rename source files
- Delete source files
- Modify source metadata
- Write files to source media
- Reorganize the source
- Deduplicate the source in place

Whenever practical, mount source media read-only.

Preservation is more important than cleanup.

---

## 2. Preserve Before Organizing

The archive layer represents:

> What existed on the original physical media.

A later library layer will represent:

> A convenient way for people to browse the useful content.

These are separate concepts.

Do not reorganize, rename, transcode, deduplicate, or otherwise transform original content during archival ingestion unless required to preserve the media.

---

## 3. Every Source Has Provenance

Every physical source receives a permanent archive identifier:

```text
SRC-0001
SRC-0002
SRC-0003
...
```

A source may be a CD, DVD, HDD, USB device, SD card, directory, or another supported medium.

Record as much source information as practical, including:

- Source ID
- Media type
- Volume label
- Filesystem
- Device identifier
- Manufacturer
- Model
- Serial number, when available
- Capacity
- Number of files
- Number of directories
- Total logical file size
- Date ingestion started
- Date ingestion completed
- Archive status
- Notes
- Relevant hardware/media metadata

Not every field will apply to every source.

Never use a hardware serial number as the archive's primary identifier.

---

## 4. Integrity Is Critical

Use SHA-256 to identify and verify files whenever practical.

For each regular file, record at minimum:

- Original relative path
- Filename
- Size
- SHA-256
- Modification timestamp, when available
- Detected MIME/media type, when practical

Additional metadata may be added later.

Hashes will eventually be used for:

- Integrity verification
- Exact duplicate detection
- Catalog reconstruction
- Provenance
- Migration verification

Do not delete duplicate files during initial archival ingestion.

---

## 5. Ingestion Must Be Resumable

Archiving may take place over days, weeks, or months.

Large hard drives may take many hours.

The user must be able to:

- Press Ctrl-C
- Quit the application
- Shut down the computer
- Disconnect the source
- Lose network connectivity
- Resume later

without restarting completed work.

A source must not be marked `COMPLETE` until all required archival data has been successfully uploaded and verified.

Possible source states should include concepts such as:

```text
NEW
INSPECTED
INGESTING
INTERRUPTED
VERIFYING
COMPLETE
FAILED
NEEDS_REVIEW
```

Exact implementation may evolve.

---

# Storage Architecture

Amazon S3 is the initial archive storage backend.

Do not tightly couple the core archive model to S3.

The archive may eventually migrate to:

- Backblaze B2
- Cloudflare R2
- Local/NAS storage
- Another object-storage provider

Storage operations should therefore be abstracted behind a storage interface when doing so remains reasonably simple.

Avoid premature abstraction, but do not scatter S3-specific assumptions throughout the domain model.

---

# S3 Layout

Initial conceptual layout:

```text
s3://<archive-bucket>/

    sources/

        SRC-0001/
            manifest.json
            data/
                ...

        SRC-0002/
            manifest.json
            image/
                SRC-0002.iso

        SRC-0003/
            manifest.json
            ...

    catalog/
        archive.db
        archive.csv
```

The exact implementation can evolve, but existing archived objects should not require unnecessary movement or renaming.

---

# Source Manifests

Every successfully ingested source must have a portable manifest.

Preferred format:

```text
JSON
```

The manifest must contain sufficient information to understand the archived source independently of the SQLite database.

Example conceptual structure:

```json
{
  "schema_version": 1,
  "source_id": "SRC-0001",
  "source_type": "data_cd",
  "volume_label": "FAMILY_PHOTOS",
  "filesystem": "iso9660",
  "archive_status": "complete",
  "files": [
    {
      "path": "Photos/Christmas 1998/IMG001.JPG",
      "size": 1837291,
      "sha256": "..."
    }
  ]
}
```

Do not consider this example a fixed schema. Design and version the real schema deliberately.

Include a manifest schema version so future versions of the application can evolve without invalidating old archives.

The SQLite catalog must be reconstructable from manifests and archived objects wherever practical.

---

# SQLite Catalog

Use SQLite as the initial local catalog.

Default location should be somewhere similar to:

```text
~/.family-archive/archive.db
```

Configuration should allow this location to change.

SQLite is the working catalog.

Do NOT attempt to use a SQLite database directly from S3.

Periodically back up the database to:

```text
s3://<archive-bucket>/catalog/archive.db
```

Also generate a human-readable/exportable catalog format such as:

```text
s3://<archive-bucket>/catalog/archive.csv
```

The SQLite database must not contain the only copy of information required to reconstruct the archive.

---

# Initial Database Concepts

Likely tables include:

```text
sources
files
ingestion_runs
```

Potential later tables include:

```text
media_metadata
duplicate_groups
tags
albums
people
locations
```

Do not create future tables until needed.

Keep the initial schema simple and normalized enough to query reliably.

Use schema migrations from the beginning.

---

# CLI

The application name is:

```text
family-archive
```

Initial commands should evolve toward:

```text
family-archive inspect
family-archive ingest
family-archive status
family-archive list
family-archive search
```

Additional commands may be introduced when justified.

---

# inspect

Example:

```bash
family-archive inspect
```

Purpose:

- Detect connected/mounted media
- Identify likely media type
- Determine filesystem
- Determine capacity
- Determine volume label
- Inspect optical-disc tracks where possible
- Report whether the media appears supported
- Make no changes

Example conceptual output:

```text
Device:       /dev/disk4
Media:        CD-ROM
Volume:       FAMILY_1999
Filesystem:   ISO 9660
Capacity:     648 MB
Tracks:       1

Classification: DATA CD
```

Unknown or unusual media should result in a clear report rather than an unsafe assumption.

---

# ingest

Example:

```bash
family-archive ingest
```

The command should:

1. Identify the source.
2. Inspect/classify it.
3. Assign or recover its `SRC-####` identifier.
4. Inventory its contents.
5. Calculate required hashes.
6. Create/update its manifest.
7. Upload archival content.
8. Resume existing uploads where possible.
9. Verify successful storage.
10. Update SQLite.
11. Back up relevant catalog information.
12. Mark the source complete only after verification.

Never silently proceed when the media type cannot be handled safely.

---

# status

Example:

```bash
family-archive status
```

Conceptual output:

```text
Archive

Sources:             27
Complete:            25
Incomplete:           2

Files discovered:    82,417
Logical data:        436.7 GB

Current source:

SRC-0027
Type:                HDD
Capacity:            1 TB
Status:              INGESTING
Progress:            63%
```

Output should be readable by a person in a terminal.

---

# list

Example:

```bash
family-archive list
```

Conceptual output:

```text
SOURCE     TYPE       LABEL               STATUS
SRC-0001  DATA_CD    FAMILY_1998         COMPLETE
SRC-0002  DATA_CD    BACKUP_1999         COMPLETE
SRC-0003  AUDIO_CD                       COMPLETE
SRC-0004  HDD        OLD_DELL_DRIVE      INGESTING
```

---

# search

Example:

```bash
family-archive search christmas
```

Conceptual output:

```text
SRC-0004  Photos/Christmas 1997/001.jpg
SRC-0004  Photos/Christmas 1997/002.jpg
SRC-0019  Xmas98/family.jpg
SRC-0027  Videos/christmas2001.avi
```

Initial search can use:

- Filename
- Relative path
- Source label
- Source ID

Future versions may search extracted metadata, tags, EXIF data, dates, people, objects, locations, OCR text, etc.

Do not implement advanced search until the archive foundation is stable.

---

# Optical Media

Old optical media may contain unknown content.

Do not assume every inserted CD is an ordinary data CD.

Potential types include:

- ISO 9660 data CD
- UDF media
- Audio CD
- Video CD
- Mixed-mode CD
- Bootable CD
- Data DVD
- Video DVD
- Damaged or partially readable media

The application should identify the media before selecting an archival strategy.

---

# Disc Images

For relatively small optical media, preserving a complete disc image is often preferable to merely copying visible files.

For ordinary data CDs, an ISO image may be appropriate.

However:

> ISO is not a universal archival representation for every optical-disc format.

Audio CDs and mixed-mode discs require different handling.

Never force unsupported media into an ISO representation merely for consistency.

If the program cannot confidently preserve a disc format, classify it as `NEEDS_REVIEW` and explain why.

---

# Audio CDs

Audio CDs do not contain ordinary filesystem files.

They must not be processed as ordinary data CDs.

Future support should consider lossless extraction, likely using FLAC, while retaining sufficient disc/track metadata for preservation.

Do not implement lossy-only archival of audio CDs.

---

# Hard Drives

Hard-drive support will be added after basic optical-media support is stable.

Hard drives introduce two distinct archival modes.

## File Archive

Appropriate for ordinary data drives.

Archive visible files while preserving:

- Relative paths
- Timestamps where practical
- File sizes
- Hashes
- Relevant metadata

## Disk Image

Appropriate when preserving the entire disk is important, such as:

- Old boot/system disks
- Unusual filesystems
- Partition information that matters
- Forensic/recovery scenarios

Do not automatically create a full image of every hard drive.

A 2 TB disk should not automatically result in a 2 TB archival image without an explicit reason.

---

# Duplicate Handling

Calculate SHA-256 hashes during ingestion.

Identical hashes can later identify exact duplicates.

Example:

```text
SRC-0007/Photos/Christmas.jpg
SRC-0042/Old PC/My Pictures/Christmas.jpg
SRC-0051/Backup/Christmas-copy.jpg
```

If all have the same SHA-256, the catalog may identify them as duplicates.

However:

> Never delete, omit, or consolidate source files during initial archival ingestion merely because another source contains the same bytes.

The archive preserves provenance.

Deduplication may later occur in the derived family library.

---

# Archive vs Library

Maintain a strict conceptual distinction.

## Archive

Purpose:

> Preserve what existed.

Characteristics:

- Source-oriented
- Immutable or effectively immutable
- Provenance retained
- Original filenames retained
- Original directory structures retained
- Original formats retained
- Duplicates retained

## Library

Purpose:

> Make family content pleasant to browse.

Future library categories may include:

```text
Photos
Videos
Documents
Audio
```

The library may:

- Deduplicate
- Generate thumbnails
- Transcode old video
- Extract EXIF
- Create albums
- Add tags
- Perform facial recognition
- Perform object recognition
- Provide full-text search
- Normalize dates
- Present a web interface

The library is derived from the archive.

Do not allow library processing to modify archival masters.

---

# Future Family Web Application

A future application should make content easily accessible to a nontechnical family member.

The user should never need to interact with:

- S3
- AWS Console
- SQLite
- Filesystem paths
- Archive source identifiers

A future interface may resemble a simple photo service with:

```text
Photos
Videos
Albums
Years
Files
Search
```

This is explicitly not a requirement for the first version of `family-archive`.

---

# Metadata

Initial ingestion should capture inexpensive, deterministic metadata.

Examples:

- Filename
- Path
- Extension
- Size
- SHA-256
- Filesystem timestamps
- Source ID

Where straightforward, later add:

### Images

- EXIF capture date
- Camera manufacturer/model
- Dimensions
- GPS coordinates
- Orientation

### Video

- Duration
- Codec
- Resolution
- Creation timestamp

### Audio

- Duration
- Codec
- Embedded tags

Metadata extraction must never alter the original file.

---

# AI Analysis

AI-based content recognition is a future enhancement.

Possible future searches include:

```text
beach
dog
birthday cake
snow
Eiffel Tower
```

Do not perform AI analysis during initial archival ingestion.

Preservation must not depend on AI services.

---

# Error Handling

Archival software should fail conservatively.

Prefer:

```text
Unable to classify this disc safely.
Source marked NEEDS_REVIEW.
No archival data was modified.
```

over guessing.

Errors should clearly identify:

- Source
- Operation
- File when applicable
- Error
- Whether retry is safe
- Whether anything was successfully uploaded

Do not hide failed files in summary output.

---

# Logging

Maintain persistent logs.

Default location may be:

```text
~/.family-archive/logs/
```

Logs should make it possible to understand what happened during previous ingestion attempts.

Do not log AWS secrets or credentials.

---

# AWS Credentials

Use normal AWS credential mechanisms.

Prefer:

- AWS profiles
- Environment-based credentials
- Standard AWS SDK credential resolution

Never store AWS access keys in:

- Source code
- SQLite
- Configuration committed to Git
- Manifests

The user's AWS profile should be configurable.

---

# Configuration

Configuration should support at least:

```text
S3 bucket
S3 prefix
AWS profile
Local database path
Local working/cache directory
```

Do not hardcode user-specific paths, usernames, AWS account IDs, bucket names, or credentials.

Provide sensible defaults where possible.

---

# macOS

The initial development and execution environment is macOS.

The application should use native macOS utilities when they provide reliable media information, including tools such as:

```text
diskutil
hdiutil
```

External open-source utilities may be introduced when necessary, particularly for optical-media formats.

Any external dependency must:

- Have a clear purpose
- Be documented
- Be detectable at runtime
- Produce a useful error if missing

Homebrew installation instructions may be provided for command-line dependencies.

---

# Python

Implement the application in Python.

Favor:

- Standard library where reasonable
- boto3 for AWS operations
- sqlite3 initially unless a stronger need emerges
- pathlib for paths
- dataclasses or similarly simple typed models
- type hints
- pytest
- clear separation between CLI, domain logic, persistence, media inspection, and storage

Avoid large frameworks unless justified.

Keep dependencies minimal.

Where practical, use a functional style: keep domain transformations pure,
make inputs and outputs explicit, and prefer immutable values. Isolate filesystem,
SQLite, network, S3, and AWS effects at the application boundaries. Avoid
introducing functional abstractions when a simple function is clearer.

---

# Suggested Package Structure

A reasonable starting point is:

```text
family-archive/
├── AGENTS.md
├── README.md
├── pyproject.toml
├── src/
│   └── family_archive/
│       ├── __init__.py
│       ├── cli.py
│       ├── config.py
│       ├── models.py
│       ├── catalog.py
│       ├── manifest.py
│       ├── hashing.py
│       ├── media.py
│       └── storage/
│           ├── __init__.py
│           └── s3.py
└── tests/
```

Treat this as guidance, not an immutable structure.

Do not create modules solely to match this tree if they do not yet have a useful responsibility.

---

# Testing

Core archive behavior requires automated tests.

Prioritize tests for:

- SHA-256 calculation
- Manifest serialization/deserialization
- Manifest schema versioning
- Source ID generation
- Database migrations
- Resume behavior
- Interrupted uploads
- Verification behavior
- Duplicate hash detection
- Path handling
- Source-state transitions

AWS tests should not require access to the production archive bucket.

Use mocks/fakes or an isolated test strategy.

Never run destructive tests against the real archive.

---

# Safety Rules

The following rules are especially important.

1. Never delete source files.
2. Never modify source files.
3. Never format or repartition media.
4. Never automatically delete archived S3 objects.
5. Never overwrite archival masters without an explicit, validated reason.
6. Never mark ingestion complete before verification.
7. Never silently skip unreadable files.
8. Never assume unknown media is an ordinary data disc.
9. Never store AWS credentials in the project.
10. Never make deduplication destructive during archival ingestion.

When uncertain, stop and ask the user.

---

# Development Philosophy

Build incrementally.

The first useful milestone is not a complete family photo service.

The first useful milestone is:

> Reliably inspect and archive one ordinary data CD to S3 with SHA-256 verification, a versioned manifest, SQLite catalog entry, and resumable behavior.

After that works reliably, expand one capability at a time.

Suggested progression:

```text
Milestone 1
Read-only data CD and DVD inspection

Milestone 2
Data CD and ordinary data DVD ingestion + S3, including minimum verification, resume, and catalog foundations

Milestone 3
Recovery/import and independent archive verification

Milestone 4
Catalog status/list/search usability

Milestone 5
Robust optical-media classification

Milestone 6
Other optical formats

Milestone 7
Hard-drive file archival

Milestone 8
Derived photo/video library

Milestone 9
Family web interface
```

The adjusted scope and validation status are recorded in `MILESTONES.md`.

Do not implement later milestones merely because they are described here.

Prefer small, reviewable changes.

---

# Codex Working Instructions

When implementing changes:

1. Read this file before making architectural decisions.
2. Inspect the existing implementation and tests before editing.
3. Preserve backward compatibility with already archived sources whenever practical.
4. Do not make destructive archive-format changes without explicitly calling them out.
5. Add or update tests for meaningful behavior changes.
6. Run the relevant tests after changes.
7. Run formatting/lint/type-checking tools configured by the project.
8. Keep commits/changes focused on the requested task.
9. Do not introduce speculative infrastructure.
10. Explain any decision that could affect long-term archive recoverability.

When a requested change conflicts with preservation or integrity guarantees, favor preservation and point out the conflict.

The physical media may contain irreplaceable family data. Treat archive correctness and recoverability as more important than convenience or performance.

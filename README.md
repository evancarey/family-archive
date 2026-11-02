# Family Archive

Preservation-first command-line tools for aging family media. This initial milestone
supports read-only inspection and verified S3 ingestion of ordinary mounted data CDs
and DVDs on macOS, plus a deliberately narrow multisession CD format described below.
See [the milestone plan](MILESTONES.md) for scope and validation status.

## Setup

Requires Python 3.11+ and macOS with native `diskutil`, `drutil`, and `hdiutil`.
Ordinary CD/DVD inspection and ingestion need no additional media utilities.
Multisession CD capture also requires cdrdao (`brew install cdrdao`). Ingestion uses boto3.

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
family-archive inspect
```

Insert a CD or DVD and allow macOS to mount it before running inspection. The command
discovers optical media, including unmounted discs when reported by diskutil,
and prints device, media type, volume label, mount point, filesystem, capacity
in bytes, track/session counts, classification, inspection support, and an explanation.

The `inspect` command uses `diskutil list -plist`, `diskutil info -plist`,
`drutil status`, and a listing of names at each candidate volume's root. For a
single attached multi-session CD candidate, it also reads `drutil toc` to confirm
track layout.
It never mounts, unmounts, repairs,
reads file contents, writes to source media, or uploads anything.

`DATA CD` means the media appears to be a CD-ROM/CD-R/CD-RW with exactly one
reported track and session, a mounted read-only ISO 9660 filesystem, and no
recognized video-disc directory markers. This is a limited inspection result,
not a verification of readability, a complete format analysis, or a guarantee
that a future archival strategy can preserve every feature (such as boot data).

A mounted multisession CD-R/CD-RW may be reported as `NEEDS_REVIEW` while also
marked as a supported multisession candidate. Ingestion is limited to discs with
one Mode 2 data track per session and a readable ISO 9660/Joliet view in every
session. cdrdao must confirm each track as CD-ROM XA Mode 2 raw. Capture applies
the two-sector TAO link-block adjustment validated on the current CD-RW; because
the recording mode cannot be independently proven from the TOC, this support is
experimental and DAO or otherwise unusual discs will need review. Audio, mixed
mode, video, Mode 1, multi-track sessions, and unsupported ISO layouts are refused.
The archive keeps the raw 2352-byte sectors for each session in order, excluding
pregap/lead-in/lead-out, subchannels, and two presumed TAO link blocks per session.
Its schema-v2 manifest records each track and the combined image. Every sector's
address, Mode 2 Form 1 structure, EDC, and ECC are checked; uploaded track ranges
and the combined image are independently SHA-256 verified. Inventory paths are
session-scoped (`Sessions/01/...`) so earlier versions of a file remain distinct.

`DATA DVD` accepts DVD-ROM, DVD±R, DVD±RW, their listed dual-layer variants,
and DVD-RAM with one track/session and a mounted read-only UDF or ISO 9660
filesystem. No capacity-based media guesses are made.

DVD root markers produce `LIKELY VIDEO DVD` (`VIDEO_TS`) or `LIKELY AUDIO DVD`
(`AUDIO_TS` alone). Video takes precedence when both markers occur: video discs
can also contain an empty AUDIO_TS directory. Names alone are heuristic;
an AUDIO_TS marker may also be empty. Files are not opened to validate formats.
These classifications have supported **inspection**, but ingestion accepts only
`DATA CD` and `DATA DVD`. Inspection does not assess encryption, boot data, recording
completeness, and full readability are not assessed.

Track and session counts from `drutil status` are correlated by explicit device
node, never drive order. Matching counts fill gaps in diskutil metadata;
conflicting counts or media types require review. If drutil is missing, fails,
or changes its output format, missing layout information remains unknown and
the report explains why. Known diskutil counts remain usable without drutil.
No `drutil toc` query is needed for DVDs.

Missing layout metadata, unreadable roots, multiple volumes, audio/mixed-mode
CDs, unsupported filesystems, and other ambiguous media report `NEEDS_REVIEW`
with a reason. The `inspect` command persists nothing and creates no catalog.
Unknown fields are never inferred from disc size or volume names. macOS metadata
varies by OS and drive; a valid data CD may conservatively require review.

Exit codes: `0` inspection succeeded, including likely video/audio DVDs
(also when no optical media was detected),
`1` inspection failed, `2` at least one disc needs review, `130` interrupted.
Command failures are safe to retry and perform no uploads.

## Ingest an ordinary data CD or DVD

### Create a dedicated archive bucket

Run the provisioner from a terminal with access to your personal AWS account:

```sh
python scripts/create_archive_bucket.py --profile YOUR_PERSONAL_PROFILE
```

It uses the profile's region (or `--region`), checks the STS caller identity, and
creates a uniquely named new bucket. `--expected-account ACCOUNT_ID` adds an
explicit account check. It never adopts an existing bucket or changes IAM.
Use `aws sso login --profile YOUR_PERSONAL_PROFILE` first if your SSO session is expired.

The bucket has all four public-access blocks enabled, ACLs disabled, explicit
SSE-S3 encryption, versioning enabled, and a policy denying non-HTTPS access.
It has no expiration, version deletion, archival-tier transitions, or automatic
multipart cleanup. Versioning preserves older versions; it is not Object Lock
or protection against an administrator deliberately deleting versions.

The script reads back and checks the security settings, then writes ignored local
files `archive-bucket.local.json` and `archive-config.local.toml`. Neither contains
credentials. These files are never overwritten. On a partial provisioning failure,
the bucket name is printed and nothing is deleted; inspect that bucket before
running the script again, since another invocation generates a new name.

Then preview ingestion with the generated configuration:

```sh
family-archive ingest --device /dev/disk5 --config archive-config.local.toml --dry-run
```

AWS guidance: [S3 security best practices](https://docs.aws.amazon.com/AmazonS3/latest/userguide/security-best-practices.html)
and [S3 versioning](https://docs.aws.amazon.com/AmazonS3/latest/userguide/Versioning.html).

### Configure and run ingestion

The ingestion implementation is covered by simulated devices and S3 service
fakes, plus a successful end-to-end run on one real DVD+R. That run included
Ctrl-C/resume during capture, full S3 SHA-256 read-back, and backup validation.
See [VALIDATION.md](VALIDATION.md) for results and the remaining validation limits.

Ingestion runs from your own terminal, where the drive is accessible. It does not
need broader permissions for the coding agent. It opens the selected block device
read-only through the corresponding `/dev/rdiskN` raw node. It does not change
the physical disc's mount state, eject, repair, or write to media. It temporarily
mounts only the verified local image, explicitly read-only, using native `hdiutil`.
If normal device
permissions deny access, it fails with retained checkpoints; do not run the whole
application as root just to bypass an error.

Configure a bucket you already own, using normal AWS credential resolution or a
named profile. No bucket is created and no production AWS calls run during tests.
Store configuration at `~/.family-archive/config.toml` or pass `--config PATH`:

```toml
bucket = "your-archive-bucket"
prefix = "optical-test"
profile = "your-aws-profile"  # optional; omit to use standard AWS resolution
# Defaults; all may be changed:
database = "~/.family-archive/archive.db"
work_dir = "~/.family-archive/work"
log_dir = "~/.family-archive/logs"
```

CLI options override configuration. Never put access keys in this file. Provide
local cache space for at least the entire raw image plus manifests/catalog backups.
The raw image can exceed the mounted filesystem's reported capacity.

```sh
# Query the drive and preview the plan; no local archive or S3 writes:
family-archive ingest --device /dev/disk5 --dry-run

# This command creates local checkpoints and uploads to the configured bucket:
family-archive ingest --device /dev/disk5 --notes "Disc label: 070307_2229"

# After interruption, use the printed permanent ID and the currently inspected device:
family-archive ingest --device /dev/disk5 --resume SRC-0001
```

For the goal of making readable pictures, videos, and audio files searchable,
use files-only mode:

```sh
family-archive ingest --device /dev/disk5 --files-only --dry-run
family-archive ingest --device /dev/disk5 --files-only
```

Files-only mode inventories regular files exposed by the mounted, read-only
filesystem, then stores each file under `sources/SRC-####/data/<original path>`.
It hashes each file, verifies the S3 read-back, and publishes a versioned
manifest and catalog backup. Symlinks are recorded but not followed. This mode
does not need to read every raw disc sector, so an unreadable unused tail can be
bypassed when the filesystem still exposes the files. It does not preserve boot
structures, hidden/unmounted sessions, filesystem slack, or other raw-disc
details; choose image mode when preserving the disc itself matters. `--files-only`
currently supports confirmed ordinary mounted data CDs and DVDs only.

To archive a folder from your Mac, use directory source mode. It archives all
regular files in the tree (not only images), keeps their relative paths, records
empty directories and symlink targets without following symlinks, and verifies
each uploaded file by SHA-256. The source folder is only read; keep it and the
configured local catalog/work/log directories separate. Preview first:

```sh
family-archive ingest --directory "/path/to/family photos" --dry-run
family-archive ingest --directory "/path/to/family photos" --notes "Scanned family photos"
# If interrupted, resume with the permanent source ID printed by the command:
family-archive ingest --directory "/path/to/family photos" --resume SRC-####
```

Directory ingestion uploads files under `sources/SRC-####/data/` and publishes a
versioned manifest and catalog backups. It does not create a disk image or capture
filesystem features outside the visible directory entries and recorded metadata.
Files up to 64 MiB upload with at most four concurrent S3 requests. S3's
server-validated SHA-256 checksum is checked from the successful response; if
the response omits it, the program checks the stored checksum or falls back to a
full read-back. Larger files retain the resumable multipart path. The source is
still re-inventoried and re-hashed after upload to detect changes during capture.

If an image-mode attempt failed before any image data or manifest was saved,
`--resume SRC-#### --files-only` can continue that source in files-only mode.
The command refuses this switch once an image checkpoint or upload exists.

`--bucket`, `--prefix`, `--profile`, `--database`, `--work-dir`, and `--log-dir` are
also available directly. Preview requires a configured destination. A device must
be explicitly selected; video/audio/ambiguous media are refused. Dual-layer data
DVDs use the same image strategy with multipart uploads.

The pipeline:

1. Assigns `SRC-####` in SQLite and reserves its S3 namespace with a unique token.
   A different existing reservation fails without replacing it. Keep one working
   catalog per archive; starting a fresh database against an existing prefix can
   collide with existing IDs. IDs are never inferred from hardware serials/labels.
2. Copies exactly the whole device's reported `Size` (or `IOKitSize`) bytes to a
   local `.img` file. This preserves 2048-byte logical sectors, including filesystem
   structures and boot sectors, rather than just visible files. It does not capture
   physical optical subchannels. Read errors fail without padding or silently
   skipping bytes; damaged discs need a separate recovery strategy.
3. Verifies local image read-back with SHA-256, attaches that local image read-only,
   confirms the mounted filesystem matches the inspected source, inventories every
   visible entry from the image, hashes every regular file, and records paths, timestamps, modes,
   sizes, MIME guesses, directory entries, and symlink targets. It never follows
   symlinks or drops duplicates. File metadata is descriptive, not an extraction
   recipe. OS-visible metadata may not expose all original filesystem attributes;
   the image is the archival master. The image is detached before uploading.
   File hashing now reads local storage rather than seeking among files on the
   optical disc. The second full optical read in the next step is retained.
4. Rechecks the raw source against the image after inventory, uploads/resumes
   64 MiB multipart image parts, then downloads the entire stored object and
   compares its byte count and SHA-256. This consumes time and download bandwidth;
   neither an ETag nor user-supplied object metadata counts as verification.
5. Publishes and reads back the versioned complete manifest, backs up the live
   SQLite catalog with SQLite's backup API and exports a source CSV, uploads and
   verifies both, then marks the local source `COMPLETE`.

Object layout under the configured prefix:

```text
sources/SRC-0001/source.json
sources/SRC-0001/image/SRC-0001.img
sources/SRC-0001/manifest.json
catalog/backups/SRC-0001/<run-uuid>/archive.db
catalog/backups/SRC-0001/<run-uuid>/archive.csv
```

**Recoverability decision:** backups use immutable per-run names instead of
replacing `catalog/archive.db` and `catalog/archive.csv`. This retains prior
checkpoints and prevents concurrent/local-state mistakes from overwriting an
archive-wide backup. The snapshot reflects `VERIFYING` just before the final local
completion transition; the verified manifest independently records completed
source preservation. Use S3 object timestamps to find backup attempts. Read-only
catalog recovery and standalone image verification are available through the
`recover` and `verify` commands below.
The manifest contains enough source, image, and file metadata to rebuild the
working source/file catalog without the original local database.

## Browse the local catalog

The read-only catalog commands use `database` from the selected config file or
`~/.family-archive/archive.db` by default. Pass `--database PATH` to select a
different working catalog:

```sh
family-archive status
family-archive list
family-archive search christmas
family-archive search --media photo
family-archive search --media video christmas
```

`status` summarizes source states and inventoried file totals. `list` shows each
source ID, type, volume label, and state. `search` matches source IDs, volume
labels, relative paths, and filenames without regard to case. `--media photo`,
`--media video`, and `--media audio` filter by filename-derived MIME type. These
commands only read an existing catalog; they do not create or migrate one. CSV catalog
exports continue to be generated with verified ingestion backups.

## Build a derived family library cache

The presentation-side prototype reads the configured S3 archive's validated
complete manifests and writes a metadata-only JSON index locally:

```sh
family-archive library index \
  --config archive-config.local.toml \
  --output ~/family-library-index.json
```

The output path must be new and outside mounted source media. The command reads
manifests only; it does not download media, write to S3, or modify the archive.
The index includes filenames, paths, hashes, source IDs, media types, and
archive object references, so keep it private. Image files stored inside
disc-image objects are indexed by their original path.

To build a private local cache for indexed photos, videos, and audio, preview
the transfer and space requirements first:

```sh
family-archive library materialize \
  --config archive-config.local.toml \
  --cache-dir ~/.family-archive/library-cache \
  --dry-run
```

Remove `--dry-run` to download and verify media. Direct files are checked
against their manifest SHA-256. For image-mode sources the command downloads
and checks the complete archived image, mounts it read-only, then extracts and
checks only manifest-listed media files. The image may be large, so the dry-run
reports source bytes and unique derived-cache bytes. The cache is private and
content-addressed; duplicate bytes share a cache file while the index retains
each source and path. Multisession raw images are listed as unsupported and
skipped.

After reviewing the local result, `family-archive library materialize
--publish` also generates bounded JPEG thumbnails with macOS `/usr/bin/sips`
and publishes verified media copies and thumbnails under the configured
private `library/` prefix. It uses content-addressed keys and conditional
writes, then publishes a content-addressed JSON index last. A failed attempt
can leave verified library objects behind; rerunning safely reuses them. The
source archive is never changed. This publishing option has automated coverage
and has been run against the live family archive. S3 stores a SHA-256 checksum
for each content-addressed object; retries verify that checksum without
downloading existing objects again. Existing thumbnails are reused only after
validating them as private JPEG files. Files larger than 5 GB are rejected
pending a multipart publishing implementation.

Some original video codecs are not playable in web browsers (for example AVI,
or MOV files whose video stream uses an older codec). Install FFmpeg with
`brew install ffmpeg`, then add `--video-playback` when materializing to create
replaceable H.264/AAC MP4 copies under the private derived-library area. This
never changes the archived videos; the website uses the MP4 when available and
keeps the preserved original as its download. Failed conversions leave that
video's original playable through a compatible desktop application or
downloadable from the site. The same flag also creates a bounded JPEG thumbnail
from a frame about three seconds into each video, falling back to the first
frame for short clips. The Videos grid uses this preview. Use the flag on each
publish that should include playback copies and thumbnails. Both derivatives
are cached locally and reused after verification.
For photo-only incremental updates, the `--video-playback` flag can be omitted:
playback references from the last verified library index are retained for
existing videos, while newly added videos remain downloadable in their original
format until playback derivatives are requested.

For routine updates when the local cache is available, add `--incremental`:

```sh
family-archive library materialize \
  --config archive-config.local.toml \
  --cache-dir ~/.family-archive/library-cache \
  --incremental --publish --video-playback
```

Incremental mode uses a private local verification checkpoint at
`<cache-dir>/.family-archive-verification.json`. After a file has been checked
against its manifest, unchanged file identity (device, inode, size, timestamps,
and permissions) lets later runs reuse that verification without reading all
its bytes again. Changed, missing, or uncheckpointed files are fully hashed;
missing media is fetched from the archive. This trusts the local checkpoint and
filesystem metadata to detect ordinary changes; use `--reuse-verified-cache`
to re-hash every cached media file, or omit `--incremental` for a full source
and S3 audit. Thumbnail and playback derivatives use the same local checkpoint
after their first successful validation. Direct media fetches, thumbnail work,
and S3 publishing use bounded worker pools; the CLI reports stage progress as
work completes. `--video-playback` creates missing video copies and frame
thumbnails and includes them in the index.
If the cache is empty, this naturally performs a full materialization. Without
`--incremental`, materialization continues to read and verify source objects
and checks all published S3 objects for a full reconciliation. Incremental mode
trusts objects referenced by the latest verified index; it does not detect an
object later removed or changed outside this application. Run without
`--incremental` for a complete S3 object audit. If a local cache is lost, a
future cache-restore command could download the published library objects
directly from S3 instead of rereading disc images and regenerating thumbnails.

Preview publishing without downloading or writing anything by adding
`--publish --dry-run` to the materialize command. To materialize locally and
publish the derived library, use `--publish` without `--dry-run`.

The current archive's dry-run estimate is 1,347 materializable media records,
about 3.75 GB streamed from S3, and up to 1.48 GB of unique local cache files.
The remaining 44 records reference multisession raw images and are not extracted
by this version. The estimate read only manifests, not media payloads.

## Presentation prototype

`web/` contains a Vite/Lit static family-facing sign-in page. It uses Cognito's
hosted sign-in with authorization code and PKCE, stores the PKCE transaction
only for the active browser tab, and keeps the token session in tab-scoped
`sessionStorage` so reloads restore sign-in. Expired access tokens are renewed
with Cognito's refresh-token grant; closing the tab clears the session. With a
published derived index, it can
show searchable photos in a paginated grid or list, choose 10–100 photos per
page, add shared tags to files, rotate thumbnail previews, and open originals
through short-lived links issued by the authenticated API. Search includes file
tags. Photos and videos can be sorted by recorded
file modification date, newest or oldest; items without a date follow dated
items. The current page, page size, search, selected discs, sort order, and layout are
restored after reload in the same tab. Rotations affect only the browser preview
and are saved locally in that browser for future visits.
Copy
`web/config.example.json` to `web/public/config.json` and fill in the
Terraform outputs before hosting; that file is ignored by Git. Run these from
the repository root to create `web/dist/`:

```sh
cd web
npm ci
npm run build
```

The static site is deployed at
https://d1nbk1tlbv97a1.cloudfront.net/. The first library index is published
with 1,347 media records from six sources (1,480,159,492 unique media bytes;
57,907,498 thumbnail bytes). Forty-four multisession records are excluded. One
JPEG could not be decoded by `sips`; its verified original is published without
a thumbnail. A family account has been created and signed in; an authenticated
end-to-end library browse remains to be confirmed.

Ctrl-C, a failed command, or a killed process leaves local work for an explicit
`--resume`. Resume compares **every existing image byte** with the inserted disc,
validates its full hash, reuses completed file hashes only for that same image,
and reuses persisted completed multipart parts. A changed device number is okay;
a mismatching disc, destination, image, or existing object is refused. A completed
source is a no-op on resume; this is not an independent re-verification command.

The private local image mount is `<work_dir>/<source_id>/.image-mount`.
Cleanup detaches it on success, file errors, and Ctrl-C; it never force-detaches
or recursively deletes a mount directory. An unclean process exit can leave an
image attached. In that case, ingestion refuses to proceed and prints the exact
`hdiutil detach` command to run before resuming. A detach failure also retains the
directory and fails ingestion. Missing `hdiutil`, an unsupported image filesystem,
or an unconfirmed read-only mount fails visibly; there is no fallback to hashing
the physical disc's files.

New manifests include the optional schema-v1 field `inventory_origin = "image"`.
Existing complete archives and published manifests are never rewritten. Older
unfinished ingestions rehash their file inventories from the image once, then
reuse image-based checkpoints on subsequent resumes.

Keep the original database and cache for resume. If the cache image is lost,
re-capture from the same source and verify its recorded hash before continuing.
If the local catalog is lost, stop and restore a backup before allocating more
IDs. Network failures after upload completion are handled by rechecking the
existing object. Expired multipart uploads are reported and their local upload
checkpoint reset; retry starts a new one. A crash between creating an upload and
saving its ID can leave an orphan multipart upload. Small catalog backups restart
on retry under a new run ID. The application never deletes objects or aborts
multipart uploads automatically; incomplete uploads may incur S3 storage charges.
Configure any bucket lifecycle cleanup separately and deliberately.

S3 requires object read/write and multipart permissions for the configured prefix
(including `s3:GetObject`, `s3:PutObject`, and any permissions required by the
bucket's encryption policy). No delete permission is needed. All archival writes
use conditional create semantics; a different existing object causes a visible
failure. Existing matching objects are fully read back before reuse.

Ingestion exit codes: `0` complete/already complete, `1` failure, `130` interrupted.
Persistent logs are source-specific and contain operation names, local file errors,
and exception types, not AWS credentials or SDK response bodies.
Errors retain the source ID, operation, resume advice,
and the fact that uploads may already exist. Nothing automatically clears the
local image or deletes remote data.

## Independently verify a source

```sh
family-archive verify SRC-0001 --config archive-config.local.toml
```

`verify` reads the versioned manifest and checks its declared preservation data.
Image manifests stream the **entire S3 image** through SHA-256, checking its exact
byte count. Files-only manifests read back and hash each archived regular file.
It never reads the physical disc, needs no local image, writes no S3 objects, and
does not update the catalog or source status. A failure does
not silently downgrade or repair the historical ingestion record.

When the configured local catalog exists, it is opened read-only and its saved
manifest/hash is used as an additional comparison. A missing catalog is allowed;
`--no-local-anchor` explicitly skips it. Without an independent local copy, this
checks consistency with the remote manifest, not cryptographic authenticity if
both the manifest and image were replaced together. ETags and uploaded hash
metadata are never accepted as verification. For image manifests, individual
file hashes are validated as metadata but those files are not extracted and
recalculated by this command.

Exit codes are `0` verified, `1` failed, and `130` interrupted. Verification is a
fresh full read on every run, so it takes download time/bandwidth and may incur
normal S3 request/transfer charges. It needs `s3:GetObject` access only.

## Recover a catalog without replacing the working database

Stop other ingestions before recovery, and choose a **new** output filename:

```sh
# Rebuild from source manifests and verify each complete archived image:
family-archive recover --config archive-config.local.toml --output recovered.db

# List the full keys of available SQLite checkpoint backups:
family-archive recover --config archive-config.local.toml --list-backups

# Restore one selected checkpoint and reconcile it with archived manifests:
family-archive recover --config archive-config.local.toml \
  --backup-key catalog/backups/SRC-0001/RUN_UUID/archive.db \
  --output restored.db
```

Use the full object key printed by `--list-backups`, including any configured S3
prefix. `RUN_UUID` above is a placeholder. Recovery requires `s3:ListBucket` for the
configured namespace as well as `s3:GetObject`; it performs no remote writes or
object deletions. Both modes fully download/hash completed images before marking
recovered sources complete. No original disc, cache image, or existing database
is required for a manifest rebuild.

Manifest recovery reconstructs source provenance, notes, image metadata, files,
directories, symlink targets, and duplicate hashes. It cannot reconstruct ingestion
run history or multipart upload IDs that were only in the working database or
checkpoint backups. Namespaces without completed manifests are retained as
`NEEDS_REVIEW` placeholders, reserving their numeric IDs. A completed manifest can
be recovered without its reservation object, but a namespace-only placeholder
cannot resume ingestion: restore a suitable checkpoint for that source.

Checkpoint recovery first checks SQLite integrity and the exact supported schema,
then compares source reservations and re-verifies archived images. An old
`VERIFYING` checkpoint may become `COMPLETE` only after successful fresh verification
against the published manifest. Unfinished sources without published manifests
become `INTERRUPTED`, retaining upload checkpoints and original provenance;
their cached file hashes are cleared for safe rehashing on resume. The configured
work directory is used for recovered records. Original disc access and, when
missing, image recapture are still needed to resume an unfinished source.
Unresolved run-history entries are retained as interrupted rather than guessed
complete. Newer remote source namespaces absent from the selected backup are also
included, so the next generated source ID stays above known allocated IDs.

Recovery uses private staging and publishes a complete SQLite file atomically
without overwriting an existing file, symlink, or database companion file. Errors,
interruption before publication, unsupported metadata/schema, missing or corrupt
images, and a changing source listing leave the requested output unpublished.
Output under a recorded original source mount is refused. The original working
catalog remains in place. Metadata reads are bounded at 64 MiB; checkpoint backups
have a 512 MiB limit. The database schema remains version 1; no old archive objects
or manifests need migration.

After checking a recovered database, use it explicitly with `--database recovered.db`
or update the `database` setting in your local configuration. Keep the old database
and use only one working catalog for a given archive. IDs that never reached S3
and were never backed up cannot be discovered from manifests alone.

## Development

```sh
python -m pytest
ruff check .
ruff format --check .
```

Tests simulate macOS property lists, command failures, and mounted roots; they
need no disc, AWS account, or network connection. Additional optical-drive validation
is still required: run inspection on a known ordinary data CD and confirm the
reported metadata against `diskutil info` for both the whole disk and volume,
and `drutil status`. Tests include metadata captured from a real DVD+R.
Do not treat a simulated test as evidence of real-disc readability.

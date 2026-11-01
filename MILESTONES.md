# Milestone plan

The supported first archival target includes ordinary mounted ISO 9660 data CDs
and single-track, single-session ISO 9660/UDF data DVDs, including the DVD+R
already used for hardware inspection. A separate experimental path handles one
Mode 2 data track per session on multisession CD-R/CD-RW. Inspection support
alone never authorizes archival handling of video, audio, mixed-mode, or unknown
media.

1. **Read-only optical inspection:** CD/DVD discovery, mounted metadata, likely
   video/audio markers, conservative review states. Implemented and checked on real
   DVD+R, single-session CD-RW, and multisession CD-RW hardware. Inspection
   correctly identifies ordinary data CDs and reports ambiguous layouts for review.
2. **First safe optical ingestion:** read-only whole-device logical-sector image,
   file inventory and SHA-256, portable schema-v1 manifest, migrated SQLite
   working catalog, standard AWS credentials, conditional S3 writes, full GET
   verification, interruption checkpoints and multipart resume, immutable catalog
   backups, logging, and a no-write preview. The verification/resume and minimum
   catalog foundations from the original milestones 3–4 belong here because the
   first ingestion must already preserve integrity. Implemented and validated on
   one real DVD+R: capture, Ctrl-C/resume, 2,141-file inventory, 56-part S3 upload,
   full object read-back, manifest/backups, and completed-source no-op. See
   [VALIDATION.md](VALIDATION.md). File inventory now uses a read-only local image
   mount; its hashes were checked against that real DVD's original inventory.
   A second single-session UDF DVD+R completed as SRC-0010: its 2,644,508,672-byte
   image and 319 file records passed independent S3 verification.
   Single-session Mode 1 CD ingestion is now validated on SRC-0005. Its ISO volume
   declared 1,296 sectors while the track declared 1,298; the two unreadable
   trailing sectors were excluded and recorded after the ISO boundary matched.
   The 2,654,208-byte image and manifest passed independent S3 verification with
   167 file records.
   A live SRC-0003 resume matched its retained prefix but hit repeatable I/O errors
   on the final two track sectors, so no image was uploaded. Single-session Mode 2
   ingestion is now validated on SRC-0008. Its ISO descriptor declared 13,515
   sectors of a 13,517-sector track; the two unreadable trailing sectors were
   excluded only after that exact boundary was confirmed. The 27,678,720-byte
   image and manifest passed independent S3 verification with 36 file records.
   See [VALIDATION.md](VALIDATION.md).
   SRC-0009 remains incomplete: its ISO declares 8,472 sectors, the track declares
   8,624, and a raw read failed at LBA 8,622. Partial-range archival is not yet
   implemented, so its readable volume has not been uploaded.
   For single-session Mode 2 CDs, inspection correlates the TOC with the mounted
   logical size; capture reads and validates raw 2352-byte sectors, extracting
   Form 1 payloads.
   A separate four-session CD-RW was successfully archived and independently
   verified as SRC-0004: 208,328,400 raw bytes and 150 file records. SRC-0002's
   three-session CD-RW is also validated. See [VALIDATION.md](VALIDATION.md).
   A real multipart process-interruption/resume trial is now recorded below.
   Files-only ingestion is also implemented for ordinary mounted data CDs/DVDs:
   it archives visible regular files individually with SHA-256 read-back, a
   schema-v3 manifest, catalog records, and resume checkpoints. This is useful
   when raw-sector capture fails but Finder can still read the mounted files.
   It deliberately does not claim to preserve the full disc. Search accepts
   `--media photo|video|audio` using filename-derived MIME types. Automated
   tests cover this path. Live files-only S3 ingestion and independent
   verification passed for SRC-0013 (8 JPEGs) and SRC-0014 (10 JPEGs and one
   data file). The TOC-unavailable fallback is restricted to a mounted,
   read-only, single-track Mode 1 filesystem and files-only handling.
3. **Recovery and independent verification:** catalog reconstruction/import from
   manifests and backup checkpoints, standalone verification of archived sources,
   recovery drills for lost local state, and further interruption/hardware testing.
   Implemented `verify` and `recover`. Live S3 trials verified SRC-0001, rebuilt a
   separate catalog from manifests, and restored its SQLite checkpoint into another
   separate catalog. Both recovered all 2,141 file records and original provenance;
   the working database was unchanged. Independent S3 verification also passed
   for SRC-0002, SRC-0004, SRC-0005, SRC-0008, and SRC-0010. A synthetic 12 MiB multipart upload was
   interrupted after its first part, resumed in a new process with only parts 2–3
   uploaded, and fully read back successfully. Other-drive, power-loss,
   physical-disconnection, and real network-loss trials remain pending.
4. **Catalog usability:** human-readable `status`, `list`, and case-insensitive
   `search` commands; commands use the configured/default local catalog in
   read-only mode and never create or migrate it. Verified ingestion continues
   to produce SQLite and CSV backups. Implemented and checked with automated
   tests; no derived library processing.
5. **Broader optical classification:** stronger identification of unusual layouts,
   recording formats, and ambiguous media without guessing archival strategies.
6. **Other optical archival formats:** deliberate preservation strategies for
   audio/video/mixed-mode media; never force them into ordinary data-disc images.
   Narrow multisession CD support is already described under Milestone 2 and
   documented in [MULTISESSION.md](MULTISESSION.md); it does not imply support
   for audio, video, or mixed-mode media. This milestone remains future work.
7. **Drive and directory file archival:** explicit file/image modes and scalable
   checkpoints for other source types.
8. **Derived family library:** metadata, thumbnails, optional deduplication and
   conversions, leaving archival masters intact. Recommended first slice:
   build a read-only media index from completed source manifests; show photos
   in a searchable thumbnail grid; keep generated thumbnails and extracted
   copies in a separate derived area; and retrieve originals through a private
   backend without exposing S3 credentials or public object URLs. Start with
   image browsing, then add video/audio playback and richer metadata. For
   image-mode sources, extract visible files from a verified read-only image
   into the derived area; files-only sources can use their archived file
   objects. Record the source ID and original path for every derived item.
   Work started: `family-archive library index` builds a deterministic local
   metadata index from validated complete manifests. It reads manifests only,
   keeps original paths, hashes, and archive references, and refuses to
   overwrite the requested output. `family-archive library materialize` now
   creates a private local content-addressed cache, verifies direct archived
   files, and extracts manifest-listed media from verified images mounted
   read-only. Optional `--publish` generates bounded JPEG thumbnails with macOS
   `sips`, then conditionally publishes verified media and thumbnails under the
   private `library/` prefix and the content-addressed index last. Source
   archive objects remain unchanged; retries reuse verified objects, checking
   S3 SHA-256 checksums and validated private thumbnail cache files. A live
   publish completed on 2026-10-25: 1,347 media records from six sources,
   1,480,159,492 media bytes, and 57,907,498 thumbnail bytes. Forty-four
   multisession records remain unsupported; one original JPEG was published
   without a generated thumbnail because `sips` could not decode it. Objects
   over 5 GB are rejected pending multipart publishing. The web presentation
   remains under validation. A
   read-only dry-run against the current archive found
   1,391 media records: 1,347 materializable, 1,480,159,492 bytes of unique
   cache content, and 3,750,597,013 bytes to stream and verify. Forty-four
   multisession records remain unsupported; no media payloads were downloaded.
   A live publish on 2026-10-30 updated the library to 4,825 media records from
   eight sources (5,200,292,792 media bytes and 111,974,970 thumbnail bytes).
   Incremental materialization is implemented with
   `library materialize --incremental`: it rehashes present cache files against
   the manifest, downloads only missing media from archive objects, skips
   already-cached files in disc images, reuses validated local thumbnails, and
   publishes the complete refreshed index last. The default path remains a full
   source read and verification for reconciliation; an empty cache in incremental
   mode also performs a full materialization. S3 content-addressed media and
   thumbnail objects are checked by checksum on each publish, so this version
   avoids repeat source downloads and thumbnail conversion but still makes S3
   object checks for the complete publication. Recovery directly from published
   library objects and parallel processing remain future improvements. Automated
   tests cover reuse and missing-cache behavior; live incremental publishing has
   not yet been validated.
9. **Family web interface:** nontechnical browsing of the derived library.
   The requested access scope is anywhere with sign-in. Initial AWS-oriented
   design: Cognito family sign-in, a hosted responsive app, and an authenticated
   backend that has read-only access to a separate private derived-library
   prefix. Keep archive objects private; return short-lived access to selected
   originals only. Do not provision cloud resources until the implementation,
   account allow-list, and access rules are reviewable. Start with photo search
   and browsing; add video, audio, and document views in later slices. See
   [PRESENTATION_PLAN.md](PRESENTATION_PLAN.md) for the proposed security
   boundary and first-slice acceptance criteria.
   The first static web client is implemented in `web/` with Vite and Lit: a
   responsive sign-in page, Cognito authorization-code flow with PKCE,
   tab-scoped session restoration and refresh, and searchable, paginated photo
   grid. The authenticated API
   reads the newest content-addressed library index and returns short-lived
   signed media links. Terraform applied 23 presentation resources on
   2026-10-25; the existing archive bucket was only read. The built site is
   deployed at https://d1nbk1tlbv97a1.cloudfront.net/. Its security headers
   were checked, and both API routes returned HTTP 401 without a token. The
   derived library index is now published. The user reports a family account
   has been created and signed in, but authenticated photo browsing is not yet
   validated. The CloudFront default
   certificate requires a `TLSv1` minimum; a TLS 1.2 minimum will require a
   custom domain and ACM certificate. The user reports that a family account
   has been created and signed in; authenticated library browsing still needs
   an end-to-end check.

Each stage should be validated before expanding archival support. A passing mock
test is not a claim that physical media is fully readable or successfully stored.

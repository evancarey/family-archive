# Family library presentation plan

The family library is a derived, read-only view of the archive. It must never
change archival masters, and it must not expose the private S3 bucket or AWS
credentials to a browser.

## Access and security

- The intended access scope is anywhere with sign-in.
- Use Amazon Cognito for authentication, with self-service registration disabled
  and family accounts added by an administrator.
- Host a responsive web client separately from the media objects.
- Put a Cognito JWT authorizer in front of the library API. The API runs with a
  read-only role scoped to the derived `library/` prefix.
- Keep both `sources/` and `library/` private. The API may issue a short-lived
  URL for a requested original after authorization; it must never return
  credentials or persistent public URLs.
- Provision cloud resources only after the local implementation, identity
  allow-list behavior, and access rules have been reviewed.

## First usable slice

1. Build a derived index only from `COMPLETE` sources and their versioned
   manifests. Each item keeps its source ID, original relative path, size,
   SHA-256, MIME type, and timestamp when available.
2. Materialize image files into a separate derived area. For files-only sources,
   copy from their verified S3 file objects. For image-mode sources, download
   the verified image, mount it read-only, and extract only files listed by the
   manifest. Verify each extracted file against its manifest hash before
   publishing it.
3. Generate thumbnails as replaceable derived artifacts. Never overwrite or
   transform an archival object.
4. Present a responsive photo grid with filename search, source and year
   filters, a detail view, and an original-download action.
5. The website now has a separate video view with paginated browsing, inline
   native playback, previous/next navigation, and original downloads. Playback
   uses an authorized, expiring URL with the indexed video MIME type. Browser
   codec support varies; unsupported originals remain downloadable. Audio and
   document views and richer video metadata remain later work. An opt-in
   `library materialize --video-playback` step now creates replaceable H.264/AAC
   MP4 copies for browser compatibility; source videos remain unchanged and
   downloadable. The same step creates JPEG video-frame thumbnails for the
   Videos grid, seeking to three seconds and falling back to the first frame.

The initial index and local materializer are implemented as
`family-archive library index` and `family-archive library materialize`. The
index reads only validated complete manifests. The materializer verifies
direct archived files and extracts listed files from read-only mounted images
into a private local content-addressed cache. With `--publish`, it creates
bounded JPEG thumbnails on macOS and conditionally publishes verified media,
thumbnails, then a content-addressed library index under the private `library/`
prefix. It never modifies source objects; failed runs may leave verified
content-addressed library objects and can be retried. Existing content-addressed
objects are checked against S3's stored SHA-256 checksum; old objects without a
stored checksum fall back to full read-back hashing. Existing local thumbnails
are reused after private-file and JPEG validation. A live publish completed on
2026-10-25 with 1,347 media records from six sources, 1,480,159,492 media bytes,
and 57,907,498 thumbnail bytes. Forty-four multisession raw-image entries
remain unsupported. One JPEG was unreadable by `sips`, so its original is
published without a thumbnail. Files over 5 GB remain rejected pending
multipart publishing support. The latest live publish on 2026-10-30 contains
4,825 media records from eight sources (5,200,292,792 media bytes and
111,974,970 thumbnail bytes). Incremental materialization and publishing is
available with `library materialize --incremental --publish`: validate and reuse
existing local content-addressed media, fetch only missing media from archived
sources, reuse valid local thumbnails, and publish the complete refreshed index
last. The default path remains a full source read for reconciliation; an empty
cache in incremental mode also rebuilds from sources. Incremental publishing
uses the hash-checked latest index as a checkpoint and skips S3 checks for
objects it already references; full publishing without `--incremental` remains
the complete S3 audit path. Direct cache recovery from published library
objects and parallel processing remain possible follow-up improvements. Live
incremental publishing has not yet been validated.
The user reports that a family account has been
created and signed in; authenticated library browsing still needs an end-to-end
check.

Keep index construction, filtering, and response formatting as pure functions
over explicit data where practical. Put catalog, filesystem, and S3 access in
thin adapters around that functional core.

## Suggested AWS shape

Use a static web client behind CloudFront, an API Gateway HTTP API with a
Cognito JWT authorizer, and small Lambda handlers for search and authorized
media URL issuance. Keep the derived index, thumbnails, and extracted copies in
the existing private archive bucket under `library/`; use a separate bucket
only if deployment review identifies a concrete isolation need. The browser
receives only authenticated API responses and short-lived media links. The
Lambda role gets read-only access to `library/` and no access to modify or
delete `sources/` objects.

Manage the presentation resources with Terraform from `infra/presentation/`.
The existing archive bucket is looked up as a data source; Terraform owns only
the new identity, web-hosting, API, and application resources. The first
Terraform configuration creates a private web bucket, CloudFront access
control, invite-only Cognito pool, JWT-protected API, and library endpoints.
These resources have been applied through Terraform in `us-east-1`; the existing
archive bucket in `eu-west-3` remains a read-only data source.

An initial static client now lives in `web/`. It uses Cognito hosted sign-in
with authorization code and PKCE, stores its token session in tab-scoped
`sessionStorage`, restores it after reload, and renews expired access tokens
through Cognito. The responsive page provides a
searchable, paginated photo grid backed by authenticated `GET /api/library`.
The client also offers a list layout, adjustable page sizes from 10 to 100,
and preview-only thumbnail rotation controls whose settings persist in that
browser.
The API reads the newest content-addressed index and returns short-lived links
to the selected page's private media. The static client is deployed at
https://d1nbk1tlbv97a1.cloudfront.net/. The derived library is published, and
the user reports a family account has been created and signed in. An
authenticated photo-browsing check remains to be completed.
The distribution uses the default CloudFront certificate, which requires its
TLSv1 minimum security policy. Enforcing a TLS 1.2 minimum requires a custom
domain and ACM certificate.
The current client uses Vite and Lit, with authentication and API access in
separate modules and rendering in a reusable web component. Keep view-model
transformations pure and browser/network effects at the edges.

## Validation and next setup steps

- Unit tests cover index generation, manifest/hash matching, source provenance,
  and exclusion of incomplete sources.
- API tests reject missing, expired, and wrong-audience tokens.
- Authorization tests verify that a signed-in family user can read library
  items but cannot list or write archive objects directly.
- A staging deployment is tested with one files-only source and one image-mode
  source before family use.
- The hosted page responds over HTTPS with the configured security headers.
  Anonymous requests to both API routes return HTTP 401. An authenticated
  end-to-end test remains pending until an administrator creates a family user.

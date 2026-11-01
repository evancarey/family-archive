# Presentation infrastructure

This Terraform root creates the first hosted presentation boundary:

- an invite-only Cognito user pool and browser OAuth client;
- a private S3 bucket for the static web client, readable only through
  CloudFront Origin Access Control;
- an HTTPS-only CloudFront distribution with security response headers;
- an API Gateway HTTP API with a Cognito JWT authorizer;
- an authenticated Lambda whose IAM role can read and list only
  the existing archive bucket's `library/` prefix.

The existing archive bucket is a Terraform data source. Terraform does not
create, import, reconfigure, or delete it. The role has no access to `sources/`
and no S3 write or delete permissions. The Lambda provides `GET /api/health`
and authenticated `GET /api/library`. The library route reads the newest
content-addressed index and issues short-lived pre-signed links for only the
selected page of originals, thumbnails, and downloads.
The CloudFront response policy includes a content security policy scoped to
the static origin, regional API/Cognito endpoints, and the existing archive
bucket's regional S3 hostname.
The distribution currently uses its default CloudFront certificate. AWS
requires that certificate to use the `TLSv1` minimum security policy; enforcing
a TLS 1.2 minimum requires a custom domain and ACM certificate.

The infrastructure and static site have been deployed. The Cognito pool allows
administrator-created accounts only. The API remains protected by Cognito, and
the archive bucket grants the Lambda read access only under `library/`. The
first private library index is published with 1,347 media records from six
sources. The user reports a family account has been created and signed in;
authenticated library browsing still needs an end-to-end check.

## Configure and inspect

Copy `terraform.tfvars.example` to `terraform.tfvars`, set the existing archive
bucket name and region, the presentation AWS region/profile, and a globally
unique Cognito domain prefix. Set `archive_prefix` to the same key prefix as
the CLI configuration (or leave it empty for a root-level archive).
The copied files are ignored by Git. Terraform state is stored in a separate
private S3 bucket with versioning, default encryption, all public access blocked,
and an HTTPS-only bucket policy. S3 versioning keeps recoverable prior state
versions, and Terraform's S3 lockfile support prevents concurrent writers. The
state contains account and resource identifiers; grant bucket access only to
trusted administrators.

Copy `backend.hcl.example` to `backend.hcl` and set `bucket` to the dedicated
state bucket for this account. The S3 backend configuration stays local because
bucket names vary by account. The initial setup migrates the existing local
state; after that, repeat the same init command when setting up Terraform on a
new machine:

```sh
cp backend.hcl.example backend.hcl
# Edit backend.hcl and set the bucket name.
AWS_PROFILE=personal-admin terraform init -backend-config=backend.hcl -migrate-state
```

The backend bucket must exist before initialization. Do not delete it or its
state objects. The presentation stack does not manage the backend bucket, so it
cannot accidentally destroy the state it uses.

```sh
terraform fmt -check -recursive
terraform validate
terraform plan
```

Review the complete plan before any apply. CloudFront and Cognito create AWS
resources and may incur charges. Do not run `terraform destroy` casually: it
removes the family sign-in pool and presentation resources. Archive objects are
outside this Terraform root.

The browser sends a Cognito access token as a bearer token. The static client
uses OAuth authorization code flow with PKCE, keeps the PKCE transaction only
for the active browser tab, and stores its token session in tab-scoped
`sessionStorage` for reload restoration and access-token renewal. The API
Gateway JWT authorizer checks the issuer and app-client audience before
invoking Lambda.

## Static client

The Vite/Lit static client is in `../../web/`. It implements the Cognito hosted
sign-in redirect, calls the authenticated health route, and displays a
searchable photo library with adjustable pagination, grid/list layouts, and
thumbnail-only rotation controls. Original media remains private; the API
returns short-lived pre-signed links only after the JWT authorizer accepts the
request.

From the repository root, install/build the site with `cd web && npm ci && npm run build`.
Copy `web/config.example.json` to `web/public/config.json` and fill in the
Terraform outputs for `api_url`, `cognito_hosted_ui_domain`, and
`cognito_app_client_id`, plus the exact CloudFront site URL as `redirectUri`.
The config file contains public client identifiers only and is excluded from
Git. The resources were applied on 2026-10-25 and the built site was uploaded to
the private web bucket. To publish a later client update, review the Terraform
state/plan, build the client, then sync `web/dist/` to the bucket without
`--delete`. The distribution URL is
https://d1nbk1tlbv97a1.cloudfront.net/.

## Deployment record

Terraform created 23 resources in `us-east-1`. The archive bucket is in
`eu-west-3` and remains an unmodified data source. The Cognito managed domain is
`family-library-74c3b9e1-62a9.auth.us-east-1.amazoncognito.com`. The hosted
page returned HTTP 200 with the configured security headers; anonymous requests
to the API health and library routes both returned HTTP 401. The derived
library index is published. The user reports a family account has been created
and signed in; authenticated library browsing remains to be checked.

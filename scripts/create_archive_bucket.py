#!/usr/bin/env python3
"""Create a new private archive bucket using normal AWS profile credentials.

Run from a terminal with AWS access. Never adopts/modifies an existing bucket,
never deletes objects, and never changes IAM, account policy, or credentials.
"""

import argparse
import json
import re
import sys
import uuid
from pathlib import Path

import boto3
from botocore.exceptions import BotoCoreError, ClientError


def create_archive_bucket(client, account: str, region: str, bucket: str) -> dict:
    if not re.fullmatch(r"\d{12}", account):
        raise ValueError("STS did not return a valid AWS account ID")
    # A globally unique new bucket prevents accidental modification of another archive.
    options = {"Bucket": bucket, "ObjectOwnership": "BucketOwnerEnforced"}
    if region != "us-east-1":
        options["CreateBucketConfiguration"] = {"LocationConstraint": region}
    client.create_bucket(**options)
    # No adoption on BucketAlreadyOwnedByYou: us-east-1 can return success for an
    # existing same-account bucket. The caller first checks existence below.
    scope = {"Bucket": bucket, "ExpectedBucketOwner": account}
    client.put_public_access_block(
        **scope,
        PublicAccessBlockConfiguration={
            "BlockPublicAcls": True,
            "IgnorePublicAcls": True,
            "BlockPublicPolicy": True,
            "RestrictPublicBuckets": True,
        },
    )
    client.put_bucket_versioning(**scope, VersioningConfiguration={"Status": "Enabled"})
    client.put_bucket_encryption(
        **scope,
        ServerSideEncryptionConfiguration={
            "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]
        },
    )
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "RequireTLS",
                "Effect": "Deny",
                "Principal": "*",
                "Action": "s3:*",
                "Resource": [f"arn:aws:s3:::{bucket}", f"arn:aws:s3:::{bucket}/*"],
                "Condition": {"Bool": {"aws:SecureTransport": "false"}},
            }
        ],
    }
    client.put_bucket_policy(**scope, Policy=json.dumps(policy))
    client.put_bucket_tagging(
        **scope,
        Tagging={
            "TagSet": [
                {"Key": "Project", "Value": "family-archive"},
                {"Key": "Purpose", "Value": "preservation"},
            ]
        },
    )
    # Verify actual service settings instead of reporting successful API submissions.
    public = client.get_public_access_block(**scope)["PublicAccessBlockConfiguration"]
    versioning = client.get_bucket_versioning(**scope)
    encryption = client.get_bucket_encryption(**scope)
    ownership = client.get_bucket_ownership_controls(**scope)
    actual_policy = json.loads(client.get_bucket_policy(**scope)["Policy"])
    if not all(
        public.get(key) is True
        for key in (
            "BlockPublicAcls",
            "IgnorePublicAcls",
            "BlockPublicPolicy",
            "RestrictPublicBuckets",
        )
    ):
        raise ValueError("Bucket public access protection did not verify")
    if versioning.get("Status") != "Enabled":
        raise ValueError("Bucket versioning did not verify")
    rules = encryption["ServerSideEncryptionConfiguration"]["Rules"]
    if not rules or rules[0]["ApplyServerSideEncryptionByDefault"]["SSEAlgorithm"] != "AES256":
        raise ValueError("Bucket encryption did not verify")
    if ownership["OwnershipControls"]["Rules"] != [{"ObjectOwnership": "BucketOwnerEnforced"}]:
        raise ValueError("Bucket ACL ownership controls did not verify")
    if actual_policy != policy:
        raise ValueError("Bucket HTTPS policy did not verify")
    return {
        "bucket": bucket,
        "region": region,
        "account": account,
        "public_access": "blocked",
        "versioning": "Enabled",
        "encryption": "SSE-S3",
        "acls": "disabled",
        "https_required": True,
        "lifecycle": "none",
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--profile", required=True, help="Personal AWS profile; never use a work account"
    )
    parser.add_argument("--region", help="Defaults to the profile region")
    parser.add_argument("--expected-account", help="Optional additional AWS account identity check")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("archive-bucket.local.json"),
        help="New local result file; never overwritten",
    )
    parser.add_argument(
        "--config-output",
        type=Path,
        default=Path("archive-config.local.toml"),
        help="New ingestion configuration; never overwritten",
    )
    args = parser.parse_args(argv)
    if args.output.exists():
        parser.error(f"{args.output} already exists; inspect it before creating another bucket")
    if args.config_output.exists():
        parser.error(f"{args.config_output} already exists; preserve its current configuration")
    bucket = None
    try:
        session = boto3.Session(profile_name=args.profile, region_name=args.region)
        region = session.region_name
        if not region:
            raise ValueError("Set --region or configure a default region in the AWS profile")
        identity = session.client("sts").get_caller_identity()
        account = identity["Account"]
        if args.expected_account and args.expected_account != account:
            raise ValueError("Authenticated AWS account does not match --expected-account")
        print(
            f"Authenticated account: {account}; region: {region}; profile: {args.profile}",
            flush=True,
        )
        # No bucket-name input: every invocation creates a unique bucket and cannot
        # intentionally target an existing bucket. Probe too, including us-east-1.
        client = session.client("s3")
        bucket = f"family-archive-{account}-{uuid.uuid4().hex}"
        try:
            client.head_bucket(Bucket=bucket)
        except ClientError as exc:
            if exc.response["Error"]["Code"] not in {"404", "NoSuchBucket", "NotFound"}:
                raise
        else:
            raise ValueError("Generated bucket already exists; refusing to modify it")
        print(f"Creating new bucket: {bucket}", flush=True)
        result = create_archive_bucket(client, account, region, bucket)
        result["profile"] = args.profile
        with args.output.open("x") as output:
            json.dump(result, output, indent=2)
            output.write("\n")
        with args.config_output.open("x") as output:
            output.write(f"bucket = {json.dumps(bucket)}\nprofile = {json.dumps(args.profile)}\n")
        print(f"Created and verified s3://{bucket}; settings saved to {args.output}")
        print(f"Ingestion configuration saved to {args.config_output}")
        print("No archive data uploaded. No lifecycle expiration or automatic deletion configured.")
        return 0
    except (BotoCoreError, ClientError, ValueError, OSError) as exc:
        print(f"Provisioning failed ({type(exc).__name__}): {exc}", file=sys.stderr)
        if bucket:
            print(
                f"Bucket may exist: {bucket}. Nothing was deleted; inspect before retrying.",
                file=sys.stderr,
            )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

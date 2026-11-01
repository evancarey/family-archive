import json
import runpy
from pathlib import Path

import boto3
import pytest
from botocore.validate import validate_parameters

provision = runpy.run_path(str(Path(__file__).parents[1] / "scripts" / "create_archive_bucket.py"))


class FakeS3:
    def __init__(self):
        self.calls = []
        self.settings = {}
        self.bad_public = False
        self.sdk = boto3.client(
            "s3",
            region_name="eu-west-3",
            aws_access_key_id="testing",
            aws_secret_access_key="testing",
        )

    def __getattr__(self, method):
        def call(**kwargs):
            model = self.sdk.meta.service_model.operation_model(
                self.sdk.meta.method_to_api_mapping[method]
            )
            validate_parameters(kwargs, model.input_shape)
            self.calls.append((method, kwargs))
            if method == "create_bucket":
                return {}
            if method.startswith("put_"):
                self.settings[method[4:]] = kwargs
                return {}
            setting = method[4:]
            source = self.settings.get(setting, {})
            if method == "get_bucket_ownership_controls":
                return {
                    "OwnershipControls": {"Rules": [{"ObjectOwnership": "BucketOwnerEnforced"}]}
                }
            if method == "get_bucket_policy":
                return {"Policy": source["Policy"]}
            if method == "get_bucket_versioning":
                return source["VersioningConfiguration"]
            if method == "get_public_access_block":
                return {
                    "PublicAccessBlockConfiguration": {}
                    if self.bad_public
                    else source["PublicAccessBlockConfiguration"]
                }
            return {
                "ServerSideEncryptionConfiguration": source["ServerSideEncryptionConfiguration"]
            }

        return call


@pytest.mark.parametrize("region", ["us-east-1", "eu-west-3"])
def test_secure_bucket_configuration(region):
    client = FakeS3()
    result = provision["create_archive_bucket"](client, "123456789012", region, "archive-test")
    request = client.calls[0][1]
    assert request["ObjectOwnership"] == "BucketOwnerEnforced"
    assert ("CreateBucketConfiguration" in request) == (region != "us-east-1")
    assert result["versioning"] == "Enabled"
    assert result["https_required"]
    assert result["lifecycle"] == "none"
    policy = json.loads(client.settings["bucket_policy"]["Policy"])
    assert policy["Statement"][0]["Effect"] == "Deny"
    assert policy["Statement"][0]["Condition"] == {"Bool": {"aws:SecureTransport": "false"}}
    assert not any("delete" in name or "lifecycle" in name for name, _ in client.calls)


def test_setup_rejects_failed_verification():
    client = FakeS3()
    client.bad_public = True
    with pytest.raises(ValueError, match="public access"):
        provision["create_archive_bucket"](client, "123456789012", "eu-west-3", "archive-test")


def test_invalid_account_never_creates():
    client = FakeS3()
    with pytest.raises(ValueError, match="account"):
        provision["create_archive_bucket"](client, "invalid", "eu-west-3", "archive-test")
    assert not client.calls

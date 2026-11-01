import hashlib
import importlib.util
import io
import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

HANDLER_PATH = Path(__file__).parents[1] / "infra" / "presentation" / "lambda" / "handler.py"
SPEC = importlib.util.spec_from_file_location("presentation_api_handler", HANDLER_PATH)
handler = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(handler)


class FakeS3:
    def __init__(self, objects):
        self.objects = objects
        self.presigned = []

    def list_objects_v2(self, **_parameters):
        return {
            "Contents": [
                {"Key": key, "LastModified": modified}
                for key, (modified, _body) in self.objects.items()
            ],
            "IsTruncated": False,
        }

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self.objects[Key][1])}

    def generate_presigned_url(self, operation, Params, ExpiresIn):
        self.presigned.append((operation, Params, ExpiresIn))
        return f"https://signed.example/{Params['Key']}?expires={ExpiresIn}"


def make_index():
    first = "a" * 64
    second = "b" * 64
    return {
        "schema_version": 1,
        "sources": [
            {
                "source_id": "SRC-0001",
                "source_type": "data_cd",
                "volume_label": "Family Photos",
                "completed_at": "2026-10-01T00:00:00Z",
                "item_count": 2,
            }
        ],
        "items": [
            {
                "source_id": "SRC-0001",
                "path": "Photos/one.jpg",
                "filename": "one.jpg",
                "media_type": "photo",
                "size": 10,
                "sha256": first,
                "media_key": f"archive/library/media/aa/{first}",
                "thumbnail": {
                    "key": f"archive/library/thumbnails/v1/512/{first}.jpg",
                    "sha256": "c" * 64,
                    "size": 8,
                    "max_dimension": 512,
                },
            },
            {
                "source_id": "SRC-0001",
                "path": "Scans/two.jpg",
                "filename": "two.jpg",
                "media_type": "photo",
                "size": 11,
                "sha256": second,
                "media_key": f"archive/library/media/bb/{second}",
                "thumbnail": None,
            },
        ],
    }


def memory_s3(document):
    body = json.dumps(document, sort_keys=True, indent=2).encode() + b"\n"
    digest = hashlib.sha256(body).hexdigest()
    key = f"archive/library/index/{digest}.json"
    modified = datetime(2026, 10, 1, tzinfo=UTC)
    return FakeS3({key: (modified, body)})


def response_json(response):
    return json.loads(response["body"])


def test_library_response_verifies_index_and_signs_only_page():
    s3 = memory_s3(make_index())

    response = handler.library_response(
        s3,
        "family-archive",
        "archive/library/",
        {"limit": "1", "offset": "0", "media": "photo"},
    )

    payload = response_json(response)
    assert response["statusCode"] == 200
    assert payload["total"] == 2
    assert len(payload["items"]) == 1
    assert payload["items"][0]["thumbnail_url"].startswith("https://signed.example/")
    assert payload["items"][0]["download_url"].startswith("https://signed.example/")
    assert len(s3.presigned) == 3
    download_request = next(
        item for item in s3.presigned if "ResponseContentDisposition" in item[1]
    )
    assert download_request[1]["ResponseContentDisposition"] == (
        "attachment; filename=\"one.jpg\"; filename*=UTF-8''one.jpg"
    )
    assert all(item[2] == 300 for item in s3.presigned)


def test_download_disposition_safely_encodes_unicode_and_unsafe_filename():
    document = make_index()
    document["items"][0]["filename"] = "../family phötø\r\n.txt"
    s3 = memory_s3(document)

    response = handler.library_response(
        s3, "family-archive", "archive/library/", {"limit": "1", "offset": "0"}
    )

    response_json(response)
    assert response["statusCode"] == 200
    download_request = next(
        item for item in s3.presigned if "ResponseContentDisposition" in item[1]
    )
    disposition = download_request[1]["ResponseContentDisposition"]
    assert 'filename="family_ph_t_.txt"' in disposition
    assert "filename*=UTF-8''family%20ph%C3%B6t%C3%B8.txt" in disposition


def test_library_response_searches_path_and_volume_label():
    s3 = memory_s3(make_index())

    response = handler.library_response(
        s3,
        "family-archive",
        "archive/library/",
        {"q": "family photos", "media": "photo"},
    )

    assert response["statusCode"] == 200
    assert response_json(response)["total"] == 2
    filtered = handler.library_response(
        s3,
        "family-archive",
        "archive/library/",
        {"q": "scans/two", "media": "photo"},
    )
    assert response_json(filtered)["total"] == 1
    assert response_json(filtered)["items"][0]["filename"] == "two.jpg"


def test_library_response_handles_missing_index_and_bad_pagination():
    missing = handler.library_response(FakeS3({}), "bucket", "library/", {})
    invalid = handler.library_response(
        memory_s3(make_index()), "bucket", "archive/library/", {"limit": "500"}
    )

    assert missing["statusCode"] == 404
    assert invalid["statusCode"] == 400


def test_library_response_rejects_unsafe_media_key():
    document = make_index()
    document["items"][0]["media_key"] = "archive/library/index/private.json"
    response = handler.library_response(
        memory_s3(document),
        "family-archive",
        "archive/library/",
        {"limit": "1"},
    )

    assert response["statusCode"] == 503
    assert "unsafe media key" in response_json(response)["error"]


def test_api_requires_jwt_claim_before_serving_health():
    response = handler.lambda_handler({"routeKey": "GET /api/health", "requestContext": {}}, None)

    assert response["statusCode"] == 401
    assert response_json(response)["error"] == "Sign-in required"


def test_lambda_s3_client_uses_the_archive_bucket_region_and_virtual_host(monkeypatch):
    import boto3
    from botocore.config import Config

    calls = []
    original_client = boto3.client

    def fake_client(service_name, **parameters):
        calls.append((service_name, parameters))
        return original_client(
            service_name,
            **parameters,
            aws_access_key_id="test",
            aws_secret_access_key="test",
        )

    monkeypatch.setattr(boto3, "client", fake_client)
    monkeypatch.setenv("ARCHIVE_REGION", "eu-west-3")

    client = handler._s3_client()

    service_name, parameters = calls[0]
    assert service_name == "s3"
    assert parameters["region_name"] == "eu-west-3"
    assert parameters["endpoint_url"] == "https://s3.eu-west-3.amazonaws.com"
    assert isinstance(parameters["config"], Config)
    assert parameters["config"].s3["addressing_style"] == "virtual"
    signed = client.generate_presigned_url(
        "get_object",
        Params={"Bucket": "family-archive", "Key": "library/media/example"},
        ExpiresIn=300,
    )
    assert urlparse(signed).netloc == "family-archive.s3.eu-west-3.amazonaws.com"

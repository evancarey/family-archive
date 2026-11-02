import hashlib
import importlib.util
import io
import json
from datetime import UTC, datetime
from decimal import Decimal
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


class FakeTagTable:
    def __init__(self, records=None):
        self.records = records or {}

    def read_all(self):
        return [dict(sha256=digest, **record) for digest, record in self.records.items()]

    def read_many(self, digests):
        return {digest: self.records[digest] for digest in digests if digest in self.records}

    def put_item(self, Item, ConditionExpression, ExpressionAttributeNames,
                 ExpressionAttributeValues=None):
        digest = Item["sha256"]
        expected = ExpressionAttributeValues.get(":expected") if ExpressionAttributeValues else 0
        current = self.records.get(digest)
        if (expected == 0 and current is not None) or (
            expected > 0 and (current is None or current["version"] != expected)
        ):
            error = type("ConditionalFailure", (Exception,), {})()
            error.response = {"Error": {"Code": "ConditionalCheckFailedException"}}
            raise error
        self.records[digest] = {
            "tags": Item["tags"],
            "version": Item["version"],
        }


class ResourceStyleTagTable:
    def __init__(self, records=None):
        self.records = records or {}
        self.read_keys = []

    def get_item(self, **parameters):
        digest = parameters["Key"]["sha256"]
        self.read_keys.append(digest)
        record = self.records.get(digest)
        if record is None:
            return {}
        return {"Item": {"tags": record["tags"], "version": Decimal(record["version"])}}


def make_index():
    first = "a" * 64
    second = "b" * 64
    playback = "e" * 64
    return {
        "schema_version": 1,
        "sources": [
            {
                "source_id": "SRC-0001",
                "source_type": "data_cd",
                "volume_label": "Family Photos",
                "completed_at": "2026-10-01T00:00:00Z",
                "item_count": 3,
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
            {
                "source_id": "SRC-0001",
                "path": "Movies/home-movie.mov",
                "filename": "home-movie.mov",
                "media_type": "video",
                "mime_type": "video/quicktime",
                "size": 12,
                "sha256": "d" * 64,
                "media_key": f"archive/library/media/dd/{'d' * 64}",
                "playback": {
                    "key": f"archive/library/playback/v1/ee/{playback}.mp4",
                    "sha256": playback,
                    "size": 20,
                    "mime_type": "video/mp4",
                    "version": 1,
                },
                "thumbnail": {
                    "key": f"archive/library/thumbnails/v1/512/{'d' * 64}.jpg",
                    "sha256": "f" * 64,
                    "size": 8,
                    "max_dimension": 512,
                },
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


def test_library_response_includes_shared_tags_and_searches_them():
    digest = "a" * 64
    tags = FakeTagTable({digest: {"tags": ["birthday", "family"], "version": 3}})

    response = handler.library_response(
        memory_s3(make_index()),
        "family-archive",
        "archive/library/",
        {"q": "birth", "media": "photo"},
        tags,
    )

    payload = response_json(response)
    assert payload["total"] == 1
    assert payload["items"][0]["sha256"] == digest
    assert payload["items"][0]["tags"] == ["birthday", "family"]
    assert payload["items"][0]["tag_version"] == 3


def test_exact_tag_search_prefers_tag_matches_over_metadata_substrings():
    tagged = {"filename": "one.jpg", "sha256": "a" * 64, "media_type": "photo"}
    unrelated = {
        "filename": "wordly wise 3000 C Test.jpg",
        "sha256": "b" * 64,
        "media_type": "photo",
    }

    matches = handler._filter_items(
        [tagged, unrelated], [], "test", "", "photo", {"a" * 64: ["test"]}
    )

    assert matches == [tagged]


def test_library_response_reads_tags_through_dynamodb_resource_key_format():
    digest = "a" * 64
    tags = ResourceStyleTagTable({digest: {"tags": ["birthday"], "version": 3}})

    response = handler.library_response(
        memory_s3(make_index()),
        "family-archive",
        "archive/library/",
        {"limit": "1"},
        tags,
    )

    item = json.loads(response["body"])["items"][0]
    assert item["tags"] == ["birthday"]
    assert item["tag_version"] == 3
    assert tags.read_keys == [digest]


def test_tag_updates_are_versioned_normalized_and_shared_by_hash():
    digest = "a" * 64
    s3 = memory_s3(make_index())
    table = FakeTagTable()

    saved = handler.update_tags_response(
        s3,
        table,
        "family-archive",
        "archive/library/",
        {"sha256": digest, "tags": [" Birthday ", "birthday", "Family"], "expected_version": 0},
    )

    assert saved["statusCode"] == 200
    assert response_json(saved) == {
        "sha256": digest,
        "tags": ["Birthday", "Family"],
        "version": 1,
    }
    stale = handler.update_tags_response(
        s3,
        table,
        "family-archive",
        "archive/library/",
        {"sha256": digest, "tags": ["new"], "expected_version": 0},
    )
    assert stale["statusCode"] == 409


def test_tag_updates_reject_unknown_files_and_invalid_tags():
    s3 = memory_s3(make_index())
    table = FakeTagTable()
    unknown = handler.update_tags_response(
        s3,
        table,
        "family-archive",
        "archive/library/",
        {"sha256": "f" * 64, "tags": ["family"], "expected_version": 0},
    )
    invalid = handler.update_tags_response(
        s3,
        table,
        "family-archive",
        "archive/library/",
        {"sha256": "a" * 64, "tags": ["\ninvalid"], "expected_version": 0},
    )

    assert unknown["statusCode"] == 404
    assert invalid["statusCode"] == 400
    assert table.records == {}


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


def test_video_results_include_an_inline_playback_url_with_video_content_type():
    s3 = memory_s3(make_index())

    response = handler.library_response(
        s3,
        "family-archive",
        "archive/library/",
        {"limit": "10", "offset": "0", "media": "video"},
    )

    payload = response_json(response)
    assert response["statusCode"] == 200
    assert payload["total"] == 1
    assert payload["items"][0]["playback_url"].startswith("https://signed.example/")
    assert payload["items"][0]["thumbnail_url"].startswith("https://signed.example/")
    playback_request = next(
        item for item in s3.presigned if "/playback/" in item[1]["Key"]
    )
    assert playback_request[1]["ResponseContentType"] == "video/mp4"
    assert playback_request[1]["ResponseContentDisposition"] == "inline"
    assert playback_request[2] == handler.PRESIGNED_VIDEO_URL_SECONDS


def test_video_without_playback_derivative_keeps_original_play_url():
    document = make_index()
    document["items"][2].pop("playback")
    s3 = memory_s3(document)

    response = handler.library_response(
        s3, "family-archive", "archive/library/", {"media": "video"}
    )

    item = response_json(response)["items"][0]
    assert item["play_url"].startswith("https://signed.example/")
    assert "playback_url" not in item


def test_video_playback_rejects_a_key_outside_the_derived_playback_prefix():
    document = make_index()
    document["items"][2]["playback"]["key"] = "archive/library/media/private.mp4"

    response = handler.library_response(
        memory_s3(document), "family-archive", "archive/library/", {"media": "video"}
    )

    assert response["statusCode"] == 503
    assert "unsafe video playback key" in response_json(response)["error"]


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


def test_library_filter_accepts_multiple_source_ids():
    items = [
        {"source_id": "SRC-0001", "filename": "one.jpg", "media_type": "photo"},
        {"source_id": "SRC-0002", "filename": "two.jpg", "media_type": "photo"},
        {"source_id": "SRC-0003", "filename": "three.jpg", "media_type": "photo"},
    ]

    filtered = handler._filter_items(items, [], "", "SRC-0001,SRC-0003", "photo")

    assert [item["source_id"] for item in filtered] == ["SRC-0001", "SRC-0003"]


def test_library_response_sorts_by_file_date_before_pagination_and_keeps_missing_dates_last():
    document = make_index()
    document["items"][0]["mtime_ns"] = 10
    document["items"][1]["mtime_ns"] = 20
    newest = handler.library_response(
        memory_s3(document),
        "family-archive",
        "archive/library/",
        {"media": "photo", "sort": "newest", "limit": "1", "offset": "0"},
    )
    assert response_json(newest)["items"][0]["filename"] == "two.jpg"

    document["items"][1].pop("mtime_ns")
    missing_date = handler.library_response(
        memory_s3(document),
        "family-archive",
        "archive/library/",
        {"media": "photo", "sort": "newest", "limit": "2", "offset": "0"},
    )
    assert [item["filename"] for item in response_json(missing_date)["items"]] == [
        "one.jpg",
        "two.jpg",
    ]


def test_library_response_rejects_unsupported_sort_order():
    response = handler.library_response(
        memory_s3(make_index()), "family-archive", "archive/library/", {"sort": "filename"}
    )

    assert response["statusCode"] == 400
    assert "Sort order" in response_json(response)["error"]


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

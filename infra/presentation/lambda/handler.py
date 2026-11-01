"""Authenticated API for the private derived family library."""

import hashlib
import json
import logging
import os
import re
from urllib.parse import quote

LOGGER = logging.getLogger()
LOGGER.setLevel(logging.INFO)
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
DEFAULT_PAGE_SIZE = 60
MAX_PAGE_SIZE = 100
PRESIGNED_URL_SECONDS = 300


def lambda_handler(event, _context):
    claims = event.get("requestContext", {}).get("authorizer", {}).get("jwt", {}).get("claims", {})
    if not claims.get("sub"):
        return _response(401, {"error": "Sign-in required"})

    route = event.get("routeKey")
    if route == "GET /api/health":
        return _response(200, {"status": "ready"})
    if route == "GET /api/library":
        try:
            client = _s3_client()
            return library_response(
                client,
                os.environ["LIBRARY_BUCKET"],
                os.environ["LIBRARY_PREFIX"],
                event.get("queryStringParameters") or {},
            )
        except Exception:
            LOGGER.exception("Could not load the derived family library")
            return _response(500, {"error": "The library could not be loaded"})
    return _response(404, {"error": "Not found"})


def _s3_client():
    import boto3
    from botocore.config import Config

    archive_region = os.environ["ARCHIVE_REGION"]
    return boto3.client(
        "s3",
        region_name=archive_region,
        endpoint_url=f"https://s3.{archive_region}.amazonaws.com",
        config=Config(s3={"addressing_style": "virtual"}),
    )


def library_response(s3, bucket, prefix, query):
    """Read the newest immutable index and sign only the requested page."""
    try:
        offset = _bounded_integer(query.get("offset", "0"), 0, 10_000_000)
        limit = _bounded_integer(query.get("limit", str(DEFAULT_PAGE_SIZE)), 1, MAX_PAGE_SIZE)
        media_type = query.get("media", "")
        if media_type not in {"", "photo", "video", "audio"}:
            raise ValueError("Media filter must be photo, video, or audio")
    except ValueError as exc:
        return _response(400, {"error": str(exc)})

    normalized_prefix = prefix.rstrip("/") + "/"
    index_prefix = normalized_prefix + "index/"
    latest = _latest_index(s3, bucket, index_prefix)
    if latest is None:
        return _response(404, {"error": "The family library has not been published yet"})

    body = s3.get_object(Bucket=bucket, Key=latest["Key"])["Body"].read()
    expected_hash = latest["Key"].rsplit("/", 1)[-1].removesuffix(".json")
    if (
        not SHA256_PATTERN.fullmatch(expected_hash)
        or hashlib.sha256(body).hexdigest() != expected_hash
    ):
        return _response(503, {"error": "The published library index failed its integrity check"})
    document = json.loads(body)
    if (
        document.get("schema_version") != 1
        or not isinstance(document.get("items"), list)
        or not isinstance(document.get("sources"), list)
    ):
        return _response(503, {"error": "The published library index has an unsupported format"})

    items = _filter_items(
        document["items"],
        document["sources"],
        query.get("q", ""),
        query.get("source", ""),
        media_type,
    )
    page = items[offset : offset + limit]
    signed_items = []
    for item in page:
        if not isinstance(item, dict) or not isinstance(item.get("sha256"), str):
            return _response(503, {"error": "The published library index contains an invalid item"})
        digest = item["sha256"]
        media_key = item.get("media_key")
        expected_media_key = f"{normalized_prefix}media/{digest[:2]}/{digest}"
        if not SHA256_PATTERN.fullmatch(digest) or media_key != expected_media_key:
            return _response(
                503, {"error": "The published library index contains an unsafe media key"}
            )
        signed = dict(item)
        signed["original_url"] = s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": media_key},
            ExpiresIn=PRESIGNED_URL_SECONDS,
        )
        filename = item.get("filename")
        if not isinstance(filename, str) or not filename:
            filename = digest
        filename = filename.replace("\\", "/").rsplit("/", 1)[-1]
        filename = filename.replace("\r", "").replace("\n", "")
        if filename in {"", ".", ".."}:
            filename = digest
        fallback_name = re.sub(r"[^A-Za-z0-9._-]", "_", filename).strip("._") or digest
        disposition = (
            f"attachment; filename=\"{fallback_name}\"; filename*=UTF-8''{quote(filename, safe='')}"
        )
        signed["download_url"] = s3.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": bucket,
                "Key": media_key,
                "ResponseContentDisposition": disposition,
            },
            ExpiresIn=PRESIGNED_URL_SECONDS,
        )
        thumbnail = item.get("thumbnail")
        if thumbnail is not None:
            thumbnail_key = thumbnail.get("key") if isinstance(thumbnail, dict) else None
            dimension = thumbnail.get("max_dimension") if isinstance(thumbnail, dict) else None
            expected_thumbnail_key = (
                f"{normalized_prefix}thumbnails/v1/{dimension}/{digest}.jpg"
                if type(dimension) is int and 64 <= dimension <= 2048
                else None
            )
            if (
                thumbnail_key != expected_thumbnail_key
                or not isinstance(thumbnail.get("sha256"), str)
                or not SHA256_PATTERN.fullmatch(thumbnail["sha256"])
            ):
                return _response(
                    503,
                    {"error": "The published library index contains an unsafe thumbnail key"},
                )
            signed["thumbnail_url"] = s3.generate_presigned_url(
                "get_object",
                Params={"Bucket": bucket, "Key": thumbnail_key},
                ExpiresIn=PRESIGNED_URL_SECONDS,
            )
        signed_items.append(signed)

    return _response(
        200,
        {
            "schema_version": 1,
            "total": len(items),
            "offset": offset,
            "limit": limit,
            "sources": document["sources"],
            "items": signed_items,
        },
    )


def _latest_index(s3, bucket, prefix):
    continuation = None
    candidates = []
    while True:
        parameters = {"Bucket": bucket, "Prefix": prefix}
        if continuation:
            parameters["ContinuationToken"] = continuation
        page = s3.list_objects_v2(**parameters)
        candidates.extend(
            item
            for item in page.get("Contents", [])
            if item.get("Key", "").endswith(".json")
            and SHA256_PATTERN.fullmatch(
                item.get("Key", "").rsplit("/", 1)[-1].removesuffix(".json")
            )
        )
        if not page.get("IsTruncated"):
            break
        continuation = page.get("NextContinuationToken")
    if not candidates:
        return None
    return max(candidates, key=lambda item: (item["LastModified"], item["Key"]))


def _filter_items(items, sources, query, source_id, media_type):
    term = query.casefold().strip()
    source_id = source_id.strip()
    labels = {item.get("source_id"): item.get("volume_label", "") for item in sources}
    return [
        item
        for item in items
        if isinstance(item, dict)
        and (not source_id or item.get("source_id") == source_id)
        and (not media_type or item.get("media_type") == media_type)
        and (
            not term
            or term in _text(item.get("filename")).casefold()
            or term in _text(item.get("path")).casefold()
            or term in _text(item.get("source_id")).casefold()
            or term in _text(labels.get(item.get("source_id"))).casefold()
        )
    ]


def _text(value):
    return value if isinstance(value, str) else ""


def _bounded_integer(value, minimum, maximum):
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Pagination values must be integers") from exc
    if result < minimum or result > maximum:
        raise ValueError("Pagination value is outside the allowed range")
    return result


def _response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {"content-type": "application/json", "cache-control": "private, no-store"},
        "body": json.dumps(body),
    }

"""Authenticated API for the private derived family library."""

import hashlib
import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from urllib.parse import quote

LOGGER = logging.getLogger()
LOGGER.setLevel(logging.INFO)
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
DEFAULT_PAGE_SIZE = 60
MAX_PAGE_SIZE = 100
PRESIGNED_URL_SECONDS = 300
PRESIGNED_VIDEO_URL_SECONDS = 3600
MAX_TAGS_PER_FILE = 30
MAX_TAG_LENGTH = 50


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
                _tag_table(),
            )
        except Exception:
            LOGGER.exception("Could not load the derived family library")
            return _response(500, {"error": "The library could not be loaded"})
    if route == "PUT /api/tags":
        try:
            payload = json.loads(event.get("body") or "{}")
            return update_tags_response(
                _s3_client(),
                _tag_table(),
                os.environ["LIBRARY_BUCKET"],
                os.environ["LIBRARY_PREFIX"],
                payload,
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            return _response(400, {"error": f"Invalid tag request: {exc}"})
        except Exception:
            LOGGER.exception("Could not update library tags")
            return _response(500, {"error": "Tags could not be saved"})
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


def _tag_table():
    import boto3

    return boto3.resource("dynamodb").Table(os.environ["ANNOTATIONS_TABLE"])


def library_response(s3, bucket, prefix, query, tag_table=None):
    """Read the newest immutable index and sign only the requested page."""
    try:
        offset = _bounded_integer(query.get("offset", "0"), 0, 10_000_000)
        limit = _bounded_integer(query.get("limit", str(DEFAULT_PAGE_SIZE)), 1, MAX_PAGE_SIZE)
        media_type = query.get("media", "")
        if media_type not in {"", "photo", "video", "audio"}:
            raise ValueError("Media filter must be photo, video, or audio")
        sort_order = query.get("sort", "archive")
        if sort_order not in {"archive", "newest", "oldest"}:
            raise ValueError("Sort order must be archive, newest, or oldest")
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

    search_term = query.get("q", "")
    search_tags = (
        _scan_tag_map(tag_table)
        if isinstance(search_term, str) and search_term.strip()
        else {}
    )
    items = _filter_items(
        document["items"],
        document["sources"],
        search_term,
        query.get("source", ""),
        media_type,
        search_tags,
    )
    items = _sort_items(items, sort_order)
    page = items[offset : offset + limit]
    page_tags = _batch_read_tags(tag_table, [item.get("sha256", "") for item in page])
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
        annotation = page_tags.get(digest, {})
        signed["tags"] = annotation.get("tags", [])
        signed["tag_version"] = annotation.get("version", 0)
        signed["original_url"] = s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": media_key},
            ExpiresIn=PRESIGNED_URL_SECONDS,
        )
        if item.get("media_type") == "video":
            playback = item.get("playback")
            if playback is not None:
                playback_hash = playback.get("sha256") if isinstance(playback, dict) else None
                playback_version = playback.get("version") if isinstance(playback, dict) else None
                playback_key = playback.get("key") if isinstance(playback, dict) else None
                expected_playback_key = (
                    f"{normalized_prefix}playback/v1/{playback_hash[:2]}/{playback_hash}.mp4"
                    if type(playback_version) is int
                    and playback_version == 1
                    and isinstance(playback_hash, str)
                    and SHA256_PATTERN.fullmatch(playback_hash)
                    and playback.get("mime_type") == "video/mp4"
                    else None
                )
                if playback_key != expected_playback_key:
                    return _response(
                        503,
                        {
                            "error": "The published library index contains an unsafe "
                            "video playback key"
                        },
                    )
                signed["playback_url"] = s3.generate_presigned_url(
                    "get_object",
                    Params={
                        "Bucket": bucket,
                        "Key": playback_key,
                        "ResponseContentType": "video/mp4",
                        "ResponseContentDisposition": "inline",
                    },
                    ExpiresIn=PRESIGNED_VIDEO_URL_SECONDS,
                )
            else:
                mime_type = item.get("mime_type")
                if not isinstance(mime_type, str) or not re.fullmatch(
                    r"video/[A-Za-z0-9.+-]+", mime_type
                ):
                    return _response(
                        503,
                        {"error": "The published library index contains an invalid video type"},
                    )
                signed["play_url"] = s3.generate_presigned_url(
                    "get_object",
                    Params={
                        "Bucket": bucket,
                        "Key": media_key,
                        "ResponseContentType": mime_type,
                        "ResponseContentDisposition": "inline",
                    },
                    ExpiresIn=PRESIGNED_VIDEO_URL_SECONDS,
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


def _filter_items(items, sources, query, source_id, media_type, tags_by_hash=None):
    term = query.casefold().strip()
    source_ids = {value.strip() for value in source_id.split(",") if value.strip()}
    labels = {item.get("source_id"): item.get("volume_label", "") for item in sources}
    tags_by_hash = tags_by_hash or {}
    exact_tag_hashes = {
        digest
        for digest, tags in tags_by_hash.items()
        if any(term == tag.casefold() for tag in tags)
    }
    return [
        item
        for item in items
        if isinstance(item, dict)
        and (not source_ids or item.get("source_id") in source_ids)
        and (not media_type or item.get("media_type") == media_type)
        and (
            not term
            or (
                item.get("sha256") in exact_tag_hashes
                if exact_tag_hashes
                else term in _text(item.get("filename")).casefold()
                or term in _text(item.get("path")).casefold()
                or term in _text(item.get("source_id")).casefold()
                or term in _text(labels.get(item.get("source_id"))).casefold()
                or any(term in tag.casefold() for tag in tags_by_hash.get(item.get("sha256"), []))
            )
        )
    ]


def _scan_tag_map(table):
    if table is None:
        return {}
    if hasattr(table, "read_all"):
        records = table.read_all()
    else:
        records = []
        request = {
            "ProjectionExpression": "#sha, #tags",
            "ExpressionAttributeNames": {"#sha": "sha256", "#tags": "tags"},
        }
        while True:
            response = table.scan(**request)
            records.extend(response.get("Items", []))
            last_key = response.get("LastEvaluatedKey")
            if not last_key:
                break
            request["ExclusiveStartKey"] = last_key
    return {
        item["sha256"]: [tag for tag in item.get("tags", []) if isinstance(tag, str)]
        for item in records
        if isinstance(item.get("sha256"), str)
    }


def _batch_read_tags(table, digests):
    unique = sorted(
        {
            digest
            for digest in digests
            if isinstance(digest, str) and SHA256_PATTERN.fullmatch(digest)
        }
    )
    if table is None or not unique:
        return {}
    if hasattr(table, "read_many"):
        return table.read_many(unique)

    def read_one(digest):
        response = table.get_item(
            Key={"sha256": digest},
            ConsistentRead=True,
            ProjectionExpression="#tags, #version",
            ExpressionAttributeNames={"#tags": "tags", "#version": "version"},
        )
        item = response.get("Item", {})
        return digest, {
            "tags": [tag for tag in item.get("tags", []) if isinstance(tag, str)],
            "version": int(item.get("version", 0)),
        }

    with ThreadPoolExecutor(max_workers=min(16, len(unique))) as executor:
        return dict(executor.map(read_one, unique))


def update_tags_response(s3, table, bucket, prefix, payload):
    """Validate, version-check, and save shared tags for a published file hash."""
    if not isinstance(payload, dict):
        return _response(400, {"error": "Request body must be a JSON object"})
    digest = payload.get("sha256")
    expected_version = payload.get("expected_version")
    tags = payload.get("tags")
    if not isinstance(digest, str) or not SHA256_PATTERN.fullmatch(digest):
        return _response(400, {"error": "A valid file SHA-256 is required"})
    if type(expected_version) is not int or expected_version < 0:
        return _response(400, {"error": "A non-negative expected_version is required"})
    if not isinstance(tags, list) or len(tags) > MAX_TAGS_PER_FILE:
        return _response(400, {"error": f"Provide at most {MAX_TAGS_PER_FILE} tags"})
    normalized = []
    seen = set()
    for tag in tags:
        if not isinstance(tag, str):
            return _response(400, {"error": "Tags must be text"})
        if any(ord(char) < 32 for char in tag):
            return _response(
                400,
                {"error": f"Tags must be 1–{MAX_TAG_LENGTH} characters without control characters"},
            )
        value = tag.strip()
        if not value or len(value) > MAX_TAG_LENGTH:
            return _response(
                400,
                {"error": f"Tags must be 1–{MAX_TAG_LENGTH} characters without control characters"},
            )
        folded = value.casefold()
        if folded not in seen:
            normalized.append(value)
            seen.add(folded)
    normalized.sort(key=str.casefold)

    index_prefix = prefix.rstrip("/") + "/index/"
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
    try:
        document = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _response(503, {"error": "The published library index is invalid"})
    if not isinstance(document, dict) or not isinstance(document.get("items"), list):
        return _response(503, {"error": "The published library index is invalid"})
    if not any(
        isinstance(item, dict) and item.get("sha256") == digest for item in document["items"]
    ):
        return _response(404, {"error": "This file is not in the published library"})

    item = {
        "sha256": digest,
        "tags": normalized,
        "version": expected_version + 1,
        "updated_at": datetime.now(UTC).isoformat(),
    }
    parameters = {
        "Item": item,
        "ConditionExpression": (
            "attribute_not_exists(#sha)" if expected_version == 0 else "#version = :expected"
        ),
        "ExpressionAttributeNames": (
            {"#sha": "sha256"} if expected_version == 0 else {"#version": "version"}
        ),
    }
    if expected_version > 0:
        parameters["ExpressionAttributeValues"] = {":expected": expected_version}
    try:
        table.put_item(**parameters)
    except Exception as exc:
        error = getattr(exc, "response", {}).get("Error", {}).get("Code")
        if error == "ConditionalCheckFailedException":
            return _response(
                409, {"error": "These tags changed in another session. Reload and retry."}
            )
        raise
    return _response(200, {"sha256": digest, "tags": normalized, "version": expected_version + 1})


def _sort_items(items, sort_order):
    if sort_order == "archive":
        return items

    dated = []
    undated = []
    for item in items:
        mtime_ns = item.get("mtime_ns")
        if isinstance(mtime_ns, int) and not isinstance(mtime_ns, bool):
            dated.append((item, mtime_ns))
        else:
            undated.append(item)
    direction = -1 if sort_order == "newest" else 1
    dated.sort(
        key=lambda pair: (
            direction * pair[1],
            _text(pair[0].get("source_id")),
            _text(pair[0].get("path")).casefold(),
        )
    )
    return [item for item, _mtime in dated] + undated


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

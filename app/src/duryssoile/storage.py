"""RustFS object storage.

RustFS speaks the S3 API, so we drive it with boto3. boto3 is synchronous, so
every call is pushed to the threadpool to avoid blocking the event loop.
"""

from functools import lru_cache
from typing import BinaryIO

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError
from fastapi.concurrency import run_in_threadpool

from .config import get_settings


class AudioNotFound(Exception):
    pass


def audio_key(word_type: str, word_id: int, suffix: str) -> str:
    """The object key for a word's clip, e.g. 'parasite/42.mp3'.

    Derived from the id rather than the original filename: S3 request signing
    and Cyrillic keys are a bad combination, and several words share a
    filename.
    """
    return f'{word_type}/{word_id}{suffix.lower()}'


def opus_key(word_id: int) -> str:
    """The object key of a word's clip re-encoded as OGG/Opus (see voice.py).

    Created on first request and deleted whenever the word's clip changes,
    so it is never stale.
    """
    return f'opus/{word_id}.ogg'


async def forget_opus(word_id: int) -> None:
    try:
        await delete_object(opus_key(word_id))
    except Exception:  # noqa: BLE001 - it is recreated on the next request
        pass


@lru_cache
def get_client():
    settings = get_settings()
    return boto3.client(
        's3',
        endpoint_url=settings.rustfs_endpoint,
        aws_access_key_id=settings.rustfs_access_key,
        aws_secret_access_key=settings.rustfs_secret_key,
        region_name=settings.rustfs_region,
        # RustFS expects path-style addressing (no bucket-as-subdomain).
        config=Config(
            signature_version='s3v4',
            s3={'addressing_style': 'path'},
            max_pool_connections=32,
        ),
    )


def ensure_bucket() -> None:
    """Create the bucket if it does not exist. Safe to call repeatedly."""
    settings = get_settings()
    client = get_client()
    try:
        client.head_bucket(Bucket=settings.rustfs_bucket)
    except ClientError:
        try:
            client.create_bucket(Bucket=settings.rustfs_bucket)
        except ClientError as exc:
            # Another worker won the race -- that is fine.
            if exc.response.get('Error', {}).get('Code') not in (
                'BucketAlreadyOwnedByYou',
                'BucketAlreadyExists',
            ):
                raise


def _raise_not_found(exc: ClientError, key: str) -> None:
    code = exc.response.get('Error', {}).get('Code')
    if code in ('NoSuchKey', 'NoSuchBucket', '404', 'NotFound'):
        raise AudioNotFound(key) from exc
    raise exc


def _get_object_sync(key: str) -> tuple[bytes, str, int, str]:
    settings = get_settings()
    try:
        response = get_client().get_object(
            Bucket=settings.rustfs_bucket, Key=key
        )
    except ClientError as exc:
        _raise_not_found(exc, key)

    body = response['Body'].read()
    content_type = response.get('ContentType') or 'audio/mpeg'
    return body, content_type, len(body), response['ETag']


def _object_etag_sync(key: str) -> str:
    settings = get_settings()
    try:
        response = get_client().head_object(Bucket=settings.rustfs_bucket, Key=key)
    except ClientError as exc:
        _raise_not_found(exc, key)
    return response['ETag']


async def object_etag(key: str) -> str:
    """The stored object's ETag (a content hash), without downloading it."""
    return await run_in_threadpool(_object_etag_sync, key)


async def get_object(key: str) -> tuple[bytes, str, int, str]:
    """Return (body, content_type, size, etag), or raise AudioNotFound."""
    return await run_in_threadpool(_get_object_sync, key)


def _put_object_sync(key: str, fileobj: BinaryIO, content_type: str) -> None:
    settings = get_settings()
    get_client().upload_fileobj(
        fileobj,
        settings.rustfs_bucket,
        key,
        ExtraArgs={'ContentType': content_type},
    )


async def put_object(key: str, fileobj: BinaryIO, content_type: str) -> None:
    await run_in_threadpool(_put_object_sync, key, fileobj, content_type)


def _delete_object_sync(key: str) -> None:
    settings = get_settings()
    get_client().delete_object(Bucket=settings.rustfs_bucket, Key=key)


async def delete_object(key: str) -> None:
    await run_in_threadpool(_delete_object_sync, key)


# Bulk operations for the seeder. Synchronous: it runs them on its own
# thread pool, outside any request.

def upload_file(path: str, key: str, content_type: str) -> None:
    settings = get_settings()
    get_client().upload_file(
        path, settings.rustfs_bucket, key, ExtraArgs={'ContentType': content_type}
    )


def empty_bucket() -> int:
    """Delete every object in the bucket. Returns how many there were."""
    settings = get_settings()
    client = get_client()
    # The bucket is created by the backend on startup, which may not have run
    # yet on a fresh deployment.
    ensure_bucket()
    keys = [
        item['Key']
        for page in client.get_paginator('list_objects_v2').paginate(
            Bucket=settings.rustfs_bucket
        )
        for item in page.get('Contents', [])
    ]
    for start in range(0, len(keys), 1000):
        client.delete_objects(
            Bucket=settings.rustfs_bucket,
            Delete={
                'Objects': [{'Key': key} for key in keys[start:start + 1000]],
                'Quiet': True,
            },
        )
    return len(keys)

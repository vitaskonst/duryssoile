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


def _get_object_sync(key: str) -> tuple[bytes, str, int]:
    settings = get_settings()
    try:
        response = get_client().get_object(
            Bucket=settings.rustfs_bucket, Key=key
        )
    except ClientError as exc:
        code = exc.response.get('Error', {}).get('Code')
        if code in ('NoSuchKey', 'NoSuchBucket', '404'):
            raise AudioNotFound(key) from exc
        raise

    body = response['Body'].read()
    content_type = response.get('ContentType') or 'audio/mpeg'
    return body, content_type, len(body)


async def get_object(key: str) -> tuple[bytes, str, int]:
    """Return (body, content_type, size) for an object, or raise AudioNotFound."""
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

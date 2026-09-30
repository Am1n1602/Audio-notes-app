"""Object storage adapter. The only module that knows the storage vendor is S3."""

from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Protocol

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from app.core.config import Settings, get_settings

_NOT_FOUND_CODES = {"404", "NoSuchKey", "NotFound"}


class StorageError(Exception):
    """Storage is unreachable or refused us (outage, permissions). NOT the same as "object missing"."""


@dataclass(frozen=True)
class StoredObject:
    size_bytes: int
    content_type: str | None


class ObjectStorage(Protocol):
    def create_upload_url(self, key: str, content_type: str, size_bytes: int, expires_seconds: int) -> str: ...

    def create_download_url(self, key: str, expires_seconds: int) -> str: ...

    def head(self, key: str) -> StoredObject | None:
        """Metadata of the stored object, or None if it does not exist. Raises StorageError on any other failure."""
        ...


class S3Storage:
    def __init__(self, client: Any, bucket: str) -> None:  # boto3 ships no type stubs, so the client is Any
        self._client = client
        self._bucket = bucket

    @classmethod
    def from_settings(cls, settings: Settings) -> "S3Storage":
        endpoint = settings.storage_endpoint or None
        client = boto3.client(
            "s3",
            region_name=settings.storage_region,
            # Pin the regional host. Without it boto3 presigns against the global bucket.s3.amazonaws.com,
            # which S3 can 307-redirect for non-us-east-1 buckets; a redirect breaks the signature (host is signed).
            endpoint_url=endpoint or f"https://s3.{settings.storage_region}.amazonaws.com",
            aws_access_key_id=settings.storage_access_key_id,
            aws_secret_access_key=settings.storage_secret_access_key.get_secret_value(),
            config=Config(
                signature_version="s3v4",  # required outside us-east-1
                s3={"addressing_style": "auto" if endpoint else "virtual"},  # path style is deprecated on AWS
                connect_timeout=5,  # bounded: a hung storage call must not hang a request
                read_timeout=10,
                retries={"max_attempts": 3, "mode": "standard"},
            ),
        )
        return cls(client, settings.storage_bucket)

    def create_upload_url(self, key: str, content_type: str, size_bytes: int, expires_seconds: int) -> str:
        """Presigned PUT. Content-Type and Content-Length are part of the signature, so S3 itself rejects an
        upload whose type or size differs from what /initiate validated. No bytes ever reach our API."""
        return str(
            self._client.generate_presigned_url(
                "put_object",
                Params={
                    "Bucket": self._bucket,
                    "Key": key,
                    "ContentType": content_type,
                    "ContentLength": size_bytes,
                },
                ExpiresIn=expires_seconds,
                HttpMethod="PUT",
            )
        )

    def create_download_url(self, key: str, expires_seconds: int) -> str:
        """Presigned GET, the link the worker will hand to Gnani (Phase 3). A bearer credential while valid."""
        return str(
            self._client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self._bucket, "Key": key},
                ExpiresIn=expires_seconds,
            )
        )

    def head(self, key: str) -> StoredObject | None:
        try:
            response = self._client.head_object(Bucket=self._bucket, Key=key)
        except ClientError as exc:
            if str(exc.response.get("Error", {}).get("Code")) in _NOT_FOUND_CODES:
                return None
            code = exc.response.get("Error", {}).get("Code")
            hint = (
                " (S3 answers 403 for a MISSING object when the IAM user lacks s3:ListBucket on the bucket, "
                "so check that permission along with s3:GetObject)"
                if str(code) == "403"
                else ""
            )
            raise StorageError(f"head_object failed: {code}{hint}") from exc
        except BotoCoreError as exc:  # timeouts, connection errors
            raise StorageError(f"head_object failed: {type(exc).__name__}") from exc
        return StoredObject(size_bytes=int(response["ContentLength"]), content_type=response.get("ContentType"))


@lru_cache
def get_storage() -> ObjectStorage:
    """FastAPI dependency; tests override it with an in-memory fake."""
    return S3Storage.from_settings(get_settings())

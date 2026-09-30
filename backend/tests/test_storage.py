from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

from app.core.config import get_settings
from app.providers.storage import S3Storage, StorageError


def s3_storage() -> S3Storage:
    return S3Storage.from_settings(get_settings())  # dummy credentials from the root conftest; nothing is sent


def query(url: str) -> dict[str, list[str]]:
    return parse_qs(urlparse(url).query)


def test_upload_url_signs_content_type_and_length_on_the_regional_virtual_host() -> None:
    url = s3_storage().create_upload_url("uploads/abc/audio.mp3", "audio/mpeg", 12345, 900)
    parsed = urlparse(url)
    assert parsed.netloc == "test-bucket.s3.ap-south-1.amazonaws.com"  # regional + virtual-hosted, no redirect
    assert parsed.path == "/uploads/abc/audio.mp3"
    q = query(url)
    assert q["X-Amz-Algorithm"] == ["AWS4-HMAC-SHA256"]
    assert q["X-Amz-Expires"] == ["900"]
    assert q["X-Amz-SignedHeaders"] == ["content-length;content-type;host"]  # S3 enforces size and type itself
    assert q["X-Amz-Credential"][0].split("/")[2] == "ap-south-1"


def test_download_url_is_a_short_lived_signed_get() -> None:
    url = s3_storage().create_download_url("uploads/abc/audio.mp3", 600)
    q = query(url)
    assert urlparse(url).netloc == "test-bucket.s3.ap-south-1.amazonaws.com"
    assert q["X-Amz-Expires"] == ["600"]
    assert q["X-Amz-SignedHeaders"] == ["host"]


def test_secret_key_is_not_in_urls_or_in_the_settings_repr() -> None:
    secret = "test-secret-value"
    assert secret not in s3_storage().create_upload_url("k.mp3", "audio/mpeg", 1, 60)
    assert secret not in repr(get_settings())


class FakeS3Client:
    """Just enough of boto3's client to drive head()."""

    def __init__(self, *, response: dict[str, Any] | None = None, error: Exception | None = None) -> None:
        self.response, self.error = response, error

    def head_object(self, **_: Any) -> dict[str, Any]:
        if self.error:
            raise self.error
        assert self.response is not None
        return self.response


def client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "x"}}, "HeadObject")


def test_head_returns_size_and_type() -> None:
    fake = FakeS3Client(response={"ContentLength": 42, "ContentType": "audio/mpeg"})
    stored = S3Storage(fake, "b").head("k")
    assert stored is not None and (stored.size_bytes, stored.content_type) == (42, "audio/mpeg")


@pytest.mark.parametrize("code", ["404", "NoSuchKey", "NotFound"])
def test_head_returns_none_when_the_object_does_not_exist(code: str) -> None:
    assert S3Storage(FakeS3Client(error=client_error(code)), "b").head("k") is None


@pytest.mark.parametrize(
    "error",
    [client_error("403"), client_error("500"), EndpointConnectionError(endpoint_url="https://x")],
)  # permission problems and outages must NOT be mistaken for "not uploaded yet"
def test_head_raises_storage_error_for_everything_else(error: Exception) -> None:
    with pytest.raises(StorageError):
        S3Storage(FakeS3Client(error=error), "b").head("k")


def test_a_403_explains_the_list_bucket_permission_trap() -> None:
    # Seen live: without s3:ListBucket, S3 answers 403 (not 404) for a missing object.
    with pytest.raises(StorageError, match="s3:ListBucket"):
        S3Storage(FakeS3Client(error=client_error("403")), "b").head("k")

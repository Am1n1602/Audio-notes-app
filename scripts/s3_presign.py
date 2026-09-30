"""Upload a file to the private S3 bucket and print a presigned GET URL, to prove Gnani can fetch it.

    pip install boto3 httpx
    python scripts/s3_presign.py clip.wav          # URL -> stdout, status -> stderr
    python scripts/gnani_smoke_test.py (python scripts/s3_presign.py clip.wav)     # PowerShell
    python scripts/gnani_smoke_test.py "$(python scripts/s3_presign.py clip.wav)"  # bash

Reads STORAGE_REGION, STORAGE_BUCKET, STORAGE_ACCESS_KEY_ID, STORAGE_SECRET_ACCESS_KEY from the
environment or .env (STORAGE_ENDPOINT only for non-AWS S3-compatible stores). The object key is
generated; the filename is never used as a storage path. The URL is a secret while it is valid.
"""

from __future__ import annotations

import mimetypes
import os
import sys
import uuid
from pathlib import Path

import boto3
import httpx
from botocore.config import Config

REQUIRED = ("STORAGE_REGION", "STORAGE_BUCKET", "STORAGE_ACCESS_KEY_ID", "STORAGE_SECRET_ACCESS_KEY")
# Gnani may queue the job before it downloads; the app will use a shorter, per-job value.
EXPIRES_SECONDS = 3600


def load_env(path: str = ".env") -> None:
    if not Path(path).is_file():
        return
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        name, sep, value = line.partition("=")
        if sep and not name.lstrip().startswith("#"):
            os.environ.setdefault(name.strip(), value.strip().strip("\"'"))


def s3_client():
    region, custom = os.environ["STORAGE_REGION"], os.environ.get("STORAGE_ENDPOINT")
    return boto3.client(
        "s3",
        region_name=region,
        # Pin the regional host: without it boto3 presigns against the global bucket.s3.amazonaws.com, which
        # 307-redirects for non-us-east-1 buckets and the redirect breaks the signature (host is signed).
        endpoint_url=custom or f"https://s3.{region}.amazonaws.com",
        aws_access_key_id=os.environ["STORAGE_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["STORAGE_SECRET_ACCESS_KEY"],
        # s3v4 is required outside us-east-1; virtual-hosted style (bucket.s3.<region>...) since path style is deprecated.
        config=Config(signature_version="s3v4", s3={"addressing_style": "auto" if custom else "virtual"}),
    )


def presign_get(s3, bucket: str, key: str, expires: int = EXPIRES_SECONDS) -> str:
    return s3.generate_presigned_url("get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=expires)


def main() -> None:
    load_env()
    if len(sys.argv) != 2 or not Path(sys.argv[1]).is_file():
        sys.exit("usage: python scripts/s3_presign.py <audio file>")
    if missing := [k for k in REQUIRED if not os.environ.get(k)]:
        sys.exit(f"missing in environment/.env: {', '.join(missing)}")

    src, bucket = Path(sys.argv[1]), os.environ["STORAGE_BUCKET"]
    key = f"smoke-tests/{uuid.uuid4()}{src.suffix.lower()}"
    s3 = s3_client()
    s3.upload_file(
        str(src),
        bucket,
        key,
        ExtraArgs={"ContentType": mimetypes.guess_type(src.name)[0] or "application/octet-stream"},
    )
    url = presign_get(s3, bucket, key)

    # Prove the URL works for an anonymous HTTPS client before blaming Gnani: fetch a single byte.
    status = httpx.get(url, headers={"Range": "bytes=0-0"}, timeout=30).status_code
    print(f"uploaded s3://{bucket}/{key}; anonymous presigned GET -> HTTP {status}", file=sys.stderr)
    if status not in (200, 206):
        sys.exit("presigned URL is not fetchable (check region, bucket name and key permissions)")
    print(url)


if __name__ == "__main__":
    main()

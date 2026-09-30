from app.providers.storage import StorageError, StoredObject


class FakeStorage:
    """In-memory stand-in for S3Storage. Records what was asked of it and can simulate an outage."""

    def __init__(self) -> None:
        self.objects: dict[str, StoredObject] = {}
        self.issued_uploads: list[tuple[str, str, int, int]] = []  # (key, content_type, size, expires)
        self.head_calls = 0
        self.outage = False

    def create_upload_url(self, key: str, content_type: str, size_bytes: int, expires_seconds: int) -> str:
        self.issued_uploads.append((key, content_type, size_bytes, expires_seconds))
        return f"https://storage.test/{key}?signature=fake"

    def create_download_url(self, key: str, expires_seconds: int) -> str:
        return f"https://storage.test/{key}?download=fake&expires={expires_seconds}"

    def head(self, key: str) -> StoredObject | None:
        self.head_calls += 1
        if self.outage:
            raise StorageError("simulated outage")
        return self.objects.get(key)

    def put(self, key: str, size_bytes: int, content_type: str | None = None) -> None:
        """What the browser's direct upload to S3 would have done."""
        self.objects[key] = StoredObject(size_bytes=size_bytes, content_type=content_type)

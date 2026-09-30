import threading
import uuid
from typing import Any

from app.providers.gnani import BatchFile, BatchJob, Transcript
from app.providers.llm import ChatResult
from app.providers.queue import QueueError
from app.providers.storage import StorageError, StoredObject


class FakeStorage:
    """In-memory stand-in for S3Storage. Records what was asked of it and can simulate an outage."""

    def __init__(self) -> None:
        self.objects: dict[str, StoredObject] = {}
        self.issued_uploads: list[tuple[str, str, int, int]] = []  # (key, content_type, size, expires)
        self.head_calls = 0
        self.outage = False
        # Race tests: make every caller wait here, so all of them have read the job (still UPLOADING) before any
        # of them changes it. Without this, threads finish one after another and the race never actually happens.
        self.head_barrier: threading.Barrier | None = None

    def create_upload_url(self, key: str, content_type: str, size_bytes: int, expires_seconds: int) -> str:
        self.issued_uploads.append((key, content_type, size_bytes, expires_seconds))
        return f"https://storage.test/{key}?signature=fake"

    def create_download_url(self, key: str, expires_seconds: int) -> str:
        return f"https://storage.test/{key}?download=fake&expires={expires_seconds}"

    def head(self, key: str) -> StoredObject | None:
        self.head_calls += 1
        if self.head_barrier is not None:
            self.head_barrier.wait(timeout=10)
        if self.outage:
            raise StorageError("simulated outage")
        return self.objects.get(key)

    def put(self, key: str, size_bytes: int, content_type: str | None = None) -> None:
        """What the browser's direct upload to S3 would have done."""
        self.objects[key] = StoredObject(size_bytes=size_bytes, content_type=content_type)


class FakeQueue:
    """Records what the API asked the worker to do; can simulate Redis being down."""

    def __init__(self) -> None:
        self.enqueued: list[tuple[uuid.UUID, int]] = []
        self.outage = False

    def enqueue_process_job(self, job_id: uuid.UUID, countdown: int = 0) -> None:
        if self.outage:
            raise QueueError("simulated broker outage")
        self.enqueued.append((job_id, countdown))


class FakeLlm:
    """A scriptable summary LLM. `script` is consumed in order and its LAST entry repeats; an Exception is raised.

    Entries are the raw message text the model would return (str) or an exception such as LlmTransientError.
    """

    GOOD = (
        '{"overview": "A planning meeting.", "key_points": ["Launch on 14th October"], '
        '"action_items": ["Priya publishes the testing plan"], "decisions": [], "uncertainties": []}'
    )

    def __init__(self) -> None:
        self.script: list[Any] = [self.GOOD]
        self.calls: list[tuple[str, str]] = []  # (system, user) of every call

    def complete_json(self, system: str, user: str) -> ChatResult:
        self.calls.append((system, user))
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(item, Exception):
            raise item
        return ChatResult(content=str(item), prompt_tokens=100, completion_tokens=40)


class FakeGnani:
    """A scriptable Gnani. Each *_script is consumed in order and its LAST entry repeats; an Exception is raised.

    Script "429, 429, then success" as create_script = [transient, transient, "job-1"].
    """

    def __init__(self) -> None:
        self.create_script: list[Any] = ["gnani-job-1"]
        self.start_script: list[Any] = [None]
        self.job_script: list[Any] = [BatchJob("COMPLETED", None, 1, 1, 0)]
        self.files_script: list[Any] = [[BatchFile("f1", "COMPLETED", "https://s3.test/t.json?sig=secret", None)]]
        self.transcript_script: list[Any] = [Transcript("hello world", 6.0, "en-IN")]
        self.calls: list[str] = []
        self.created_with: list[tuple[str, str]] = []

    @staticmethod
    def _next(script: list[Any]) -> Any:
        item = script.pop(0) if len(script) > 1 else script[0]
        if isinstance(item, Exception):
            raise item
        return item

    def create_batch_job(self, source_url: str, language_code: str) -> str:
        self.calls.append("create")
        self.created_with.append((source_url, language_code))
        return str(self._next(self.create_script))

    def start_batch_job(self, job_id: str) -> None:
        self.calls.append("start")
        self._next(self.start_script)

    def get_batch_job(self, job_id: str) -> BatchJob:
        self.calls.append("get_job")
        job: BatchJob = self._next(self.job_script)
        return job

    def get_batch_files(self, job_id: str) -> list[BatchFile]:
        self.calls.append("get_files")
        files: list[BatchFile] = self._next(self.files_script)
        return files

    def download_transcript(self, url: str) -> Transcript:
        self.calls.append("download")
        transcript: Transcript = self._next(self.transcript_script)
        return transcript

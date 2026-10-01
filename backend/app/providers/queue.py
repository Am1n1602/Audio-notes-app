"""Hands work to the worker. The API and the worker share only a broker URL and a task NAME (no code imports)."""

import uuid
from functools import lru_cache
from typing import Protocol

from celery import Celery
from kombu.exceptions import KombuError

from app.core.config import Settings, get_settings

PROCESS_JOB_TASK = "worker.process_job"


class QueueError(Exception):
    """The job could not be handed to the queue (Redis or Cloud Tasks down or unreachable)."""


class JobQueue(Protocol):
    def enqueue_process_job(self, job_id: uuid.UUID, countdown: int = 0) -> None:
        """Ask the worker to run one processing step for this job, `countdown` seconds from now."""
        ...


def make_celery_app(settings: Settings, *, include: list[str] | None = None) -> Celery:
    """One Celery configuration for both sides. No result backend: results live in Postgres, not in Celery."""
    app = Celery("audio_notes", broker=settings.redis_url, include=include or [])
    app.conf.update(
        task_acks_late=True,  # ack after the task finishes, so a task whose worker died is redelivered
        worker_prefetch_multiplier=1,  # do not hoard queued tasks in one worker
        broker_connection_retry_on_startup=True,
        broker_connection_timeout=2,  # bounded: /complete must answer even when Redis is down
        # A step held by a worker that dies is redelivered after this long (default 1 h). Steps take seconds, so 5
        # minutes is generous. (worker/tasks.py also re-enqueues in-flight jobs at startup for an instant resume.)
        broker_transport_options={"visibility_timeout": 300},
        # Explicit rather than inherited: every step is short and guarded by an atomic claim, so a task delivered
        # twice after a broker outage is harmless and cancelling a running one is not needed.
        worker_cancel_long_running_tasks_on_connection_loss=False,
        timezone="UTC",
    )
    return app


class CeleryJobQueue:
    def __init__(self, app: Celery) -> None:
        self._app = app

    def enqueue_process_job(self, job_id: uuid.UUID, countdown: int = 0) -> None:
        try:
            self._app.send_task(PROCESS_JOB_TASK, args=[str(job_id)], countdown=countdown)
        except KombuError as exc:  # kombu's OperationalError when the broker cannot be reached
            raise QueueError(f"could not enqueue: {type(exc).__name__}") from exc


@lru_cache
def get_queue() -> JobQueue:
    """FastAPI dependency; tests override it with a fake that records calls."""
    settings = get_settings()
    if settings.queue_backend == "cloudtasks":
        from app.providers.cloud_tasks import CloudTasksJobQueue  # not imported locally: it pulls in Google's SDK

        return CloudTasksJobQueue(settings)
    return CeleryJobQueue(make_celery_app(settings))

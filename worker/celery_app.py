from celery import Celery

from app.core.config import get_settings

# No result backend on purpose: results live in Postgres (the source of truth), not in Celery.
celery_app = Celery("audio_notes", broker=get_settings().redis_url, include=["worker.tasks"])
celery_app.conf.update(
    task_acks_late=True,  # ack after the task finishes, so a task whose worker died is redelivered
    worker_prefetch_multiplier=1,  # do not hoard queued tasks in one worker
    broker_connection_retry_on_startup=True,
    timezone="UTC",
)

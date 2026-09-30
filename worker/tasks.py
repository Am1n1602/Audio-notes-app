from worker.celery_app import celery_app


@celery_app.task(name="worker.ping")
def ping() -> str:
    """Boot check only: proves the worker is connected to Redis and can run a task."""
    return "pong"

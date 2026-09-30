from worker.celery_app import celery_app
from worker.tasks import ping


def test_ping_task_runs() -> None:
    assert ping.apply().get() == "pong"


def test_worker_is_registered_and_configured_for_redelivery() -> None:
    assert "worker.ping" in celery_app.tasks
    assert celery_app.conf.broker_url == "redis://localhost:6379/0"
    assert celery_app.conf.task_acks_late is True
    assert celery_app.conf.worker_prefetch_multiplier == 1

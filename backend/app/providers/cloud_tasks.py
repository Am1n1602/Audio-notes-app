"""Hands the next step to Google Cloud Tasks, which calls the private steps service when the step is due.

Production replaces Redis + an always-on worker with this: Cloud Tasks keeps the task durably, waits until
`schedule_time` (the countdown), then POSTs to <steps_url>/internal/steps as a service account whose token Cloud Run
checks. A reply of 2xx removes the task; any other reply, or none within the dispatch deadline, makes Cloud Tasks retry
it under the queue's retry settings (this is what Celery's autoretry did).
"""

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import requests
from google.api_core.exceptions import GoogleAPIError
from google.auth.exceptions import GoogleAuthError
from google.cloud import tasks_v2

from app.core.config import Settings
from app.providers.queue import QueueError

CREATE_TIMEOUT_SECONDS = 10  # bounded: /complete must answer even when Cloud Tasks does not


class CloudTasksJobQueue:
    def __init__(self, settings: Settings, client: Any = None) -> None:
        self._client = client  # created on first use, so that missing credentials fail as a QueueError, not at startup
        self._queue = settings.cloud_tasks_queue
        self._steps_url = settings.steps_url.rstrip("/")
        self._invoker = settings.steps_invoker_email
        self._deadline = timedelta(seconds=settings.steps_dispatch_deadline_seconds)

    def enqueue_process_job(self, job_id: uuid.UUID, countdown: int = 0) -> None:
        task: dict[str, Any] = {
            "http_request": {
                "http_method": tasks_v2.HttpMethod.POST,
                "url": f"{self._steps_url}/internal/steps",
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps({"job_id": str(job_id)}).encode(),
                # Cloud Run accepts the token only if its audience is the service URL (no path).
                "oidc_token": {"service_account_email": self._invoker, "audience": self._steps_url},
            },
            "dispatch_deadline": self._deadline,
        }
        if countdown > 0:
            task["schedule_time"] = datetime.now(UTC) + timedelta(seconds=countdown)
        try:
            if self._client is None:
                # REST rather than gRPC: Cloud Run takes the CPU away between requests, which breaks a gRPC channel's
                # keepalives.
                self._client = tasks_v2.CloudTasksClient(transport="rest")
            self._client.create_task(request={"parent": self._queue, "task": task}, timeout=CREATE_TIMEOUT_SECONDS)
        except (GoogleAPIError, GoogleAuthError, requests.RequestException) as exc:
            raise QueueError(f"could not enqueue: {type(exc).__name__}") from exc

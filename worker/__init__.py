"""Celery entrypoint package.

`celery -A worker.celery_app worker` runs from the repo root, where the shared application code in
backend/app is not importable by default (uvicorn gets --app-dir, Celery has no equivalent). Putting
backend/ on sys.path here means the worker starts the same way locally and on a deploy host.
"""

import sys
from pathlib import Path

_BACKEND = str(Path(__file__).resolve().parents[1] / "backend")
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

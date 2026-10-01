# One image for every Cloud Run piece: the public API, the private step service, and the migration job.
# What differs is configuration only (SERVICE_ROLE, QUEUE_BACKEND, ...), see README "Deploy".
# Build from the repository root: docker build -t audio-notes .
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# The app finds prompts/ next to backend/ (REPO_ROOT in app/core/config.py), so keep that layout.
WORKDIR /app
COPY backend/requirements.txt backend/requirements.txt
RUN pip install -r backend/requirements.txt
COPY backend backend
COPY prompts prompts

# Not root. Alembic also runs from here: `alembic upgrade head` (alembic.ini is in backend/).
RUN useradd --create-home app && chown -R app /app
USER app
WORKDIR /app/backend

# Cloud Run sends the port in $PORT, so a shell expands it; exec hands the process to uvicorn so it gets SIGTERM.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}"]

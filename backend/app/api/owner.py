"""Per-browser history: who is asking, and which jobs are theirs.

There are no accounts. Each browser generates a random client id (a UUID v4) once, keeps it in localStorage and sends it
in the X-Client-Id header on every call. The server stores only its SHA-256 on the jobs it creates and shows a job only
to the browser whose hash matches. So a public demo does not show one visitor's uploads to another.

This is privacy between visitors, not authentication: whoever holds the id holds the history (clearing site data loses
it, another browser starts empty, copying the id to another browser shares it), and anything that can read the page's
localStorage can read it. A job id alone is no longer enough to read a job.
"""

import hashlib
import secrets
import uuid

from fastapi import Depends, Header
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.db.models import AudioJob
from app.db.session import get_db
from app.services import jobs


def owner_id_for(client_id: str) -> str:
    """What is stored on a job: the SHA-256 of the canonical client id (never the id itself)."""
    return hashlib.sha256(client_id.encode("utf-8")).hexdigest()


def current_owner(x_client_id: str | None = Header(default=None)) -> str:
    """The caller's owner id, from the X-Client-Id header, or a 401 that says what is wrong."""
    if not x_client_id:
        raise AppError(401, "CLIENT_ID_REQUIRED", "Send your browser's client id in the X-Client-Id header.")
    try:
        parsed = uuid.UUID(x_client_id)
    except ValueError:
        parsed = None
    if parsed is None or parsed.version != 4:
        raise AppError(401, "CLIENT_ID_INVALID", "The X-Client-Id header must be a random UUID (version 4).")
    return owner_id_for(str(parsed))  # canonical form: upper case, braces or urn: prefixes all mean the same browser


def owned_job(job_id: uuid.UUID, owner: str = Depends(current_owner), db: Session = Depends(get_db)) -> AudioJob:
    """The job, but only if it belongs to the caller. Someone else's job answers exactly like one that does not exist,
    so the API never confirms that an id is real."""
    job = jobs.get_job(db, job_id)
    if not secrets.compare_digest((job.owner_id or "").encode(), owner.encode()):
        raise AppError(404, "JOB_NOT_FOUND", "We could not find that upload.")
    return job

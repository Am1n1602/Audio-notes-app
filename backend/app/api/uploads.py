from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.orm import Session

from app.api.owner import current_owner, owned_job
from app.core.config import Settings, get_settings
from app.db.models import AudioJob
from app.db.session import get_db
from app.providers.queue import JobQueue, get_queue
from app.providers.storage import ObjectStorage, get_storage
from app.schemas.uploads import (
    AudioLink,
    InitiateRequest,
    InitiateResponse,
    UploadDetail,
    UploadListItem,
    UploadTarget,
)
from app.services import jobs, recovery, uploads

router = APIRouter(prefix="/uploads", tags=["uploads"])


@router.post("/initiate", response_model=InitiateResponse, status_code=201)
def initiate(
    body: InitiateRequest,
    db: Session = Depends(get_db),
    storage: ObjectStorage = Depends(get_storage),
    queue: JobQueue = Depends(get_queue),
    settings: Settings = Depends(get_settings),
    owner: str = Depends(current_owner),
) -> InitiateResponse:
    """Validate, schedule the recording's deletion, create the job, and return a signed URL. The browser then uploads
    straight to storage."""
    job, url, content_type = uploads.initiate_upload(
        db,
        storage,
        queue,
        settings,
        owner_id=owner,
        filename=body.filename,
        size_bytes=body.size_bytes,
        language_code=body.language_code,
    )
    return InitiateResponse(
        id=job.id,
        status=job.status,
        upload=UploadTarget(
            url=url,
            headers={"Content-Type": content_type},
            expires_in_seconds=settings.upload_url_expires_seconds,
        ),
    )


@router.post("/{job_id}/complete", response_model=UploadDetail)
def complete(
    job: AudioJob = Depends(owned_job),
    db: Session = Depends(get_db),
    storage: ObjectStorage = Depends(get_storage),
    queue: JobQueue = Depends(get_queue),
) -> UploadDetail:
    """The browser says the upload finished. We verify the object exists before believing it, then hand the job
    to the worker. Idempotent: a repeat call reports the current state and starts nothing."""
    job, _moved = uploads.complete_upload(db, storage, queue, job.id)
    return UploadDetail.model_validate(job)


@router.post("/{job_id}/retry", response_model=UploadDetail)
def retry(
    job: AudioJob = Depends(owned_job),
    db: Session = Depends(get_db),
    queue: JobQueue = Depends(get_queue),
) -> UploadDetail:
    """Start a new attempt for a failed job (only for failures where retrying can help). Idempotent."""
    return UploadDetail.model_validate(uploads.retry_upload(db, queue, job.id))


@router.get("", response_model=list[UploadListItem])
def list_uploads(
    limit: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db),
    owner: str = Depends(current_owner),
    queue: JobQueue = Depends(get_queue),
    settings: Settings = Depends(get_settings),
) -> list[UploadListItem]:
    """This browser's uploads, newest first. Any that stopped moving are given a new step (recovery.revive_stalled)."""
    listed = jobs.list_jobs(db, limit, owner)
    revived = recovery.revive_stalled(db, queue, settings, listed)
    for job in listed:
        if job.id in revived:  # the claim wrote these behind the ORM's back (the big deferred columns stay unloaded)
            db.refresh(job, attribute_names=["updated_at", "next_step_at"])
    return [UploadListItem.model_validate(job) for job in listed]


@router.get("/{job_id}", response_model=UploadDetail)
def get_upload(
    job: AudioJob = Depends(owned_job),
    db: Session = Depends(get_db),
    queue: JobQueue = Depends(get_queue),
    settings: Settings = Depends(get_settings),
) -> UploadDetail:
    if recovery.revive_stalled(db, queue, settings, [job]):
        db.refresh(job)  # the claim changed the row behind the ORM's back
    return UploadDetail.model_validate(job)


@router.get("/{job_id}/audio", response_model=AudioLink)
def audio(
    response: Response,
    job: AudioJob = Depends(owned_job),
    storage: ObjectStorage = Depends(get_storage),
    settings: Settings = Depends(get_settings),
) -> AudioLink:
    """A short-lived signed link to play the recording. It is a credential while valid, so it is never cached."""
    response.headers["Cache-Control"] = "no-store"
    return AudioLink(
        url=uploads.audio_url(storage, settings, job), expires_in_seconds=settings.audio_url_expires_seconds
    )

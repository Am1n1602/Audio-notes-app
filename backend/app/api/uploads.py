import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.db.session import get_db
from app.providers.storage import ObjectStorage, get_storage
from app.schemas.uploads import InitiateRequest, InitiateResponse, UploadDetail, UploadListItem, UploadTarget
from app.services import jobs, uploads

router = APIRouter(prefix="/uploads", tags=["uploads"])


@router.post("/initiate", response_model=InitiateResponse, status_code=201)
def initiate(
    body: InitiateRequest,
    db: Session = Depends(get_db),
    storage: ObjectStorage = Depends(get_storage),
    settings: Settings = Depends(get_settings),
) -> InitiateResponse:
    """Validate, create the job, and return a signed URL. The browser then uploads straight to storage."""
    job, url, content_type = uploads.initiate_upload(
        db,
        storage,
        settings,
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
    job_id: uuid.UUID,
    db: Session = Depends(get_db),
    storage: ObjectStorage = Depends(get_storage),
) -> UploadDetail:
    """The browser says the upload finished. We verify the object exists before believing it. Idempotent."""
    job, _moved = uploads.complete_upload(db, storage, job_id)
    return UploadDetail.model_validate(job)


@router.get("", response_model=list[UploadListItem])
def list_uploads(limit: int = Query(50, ge=1, le=100), db: Session = Depends(get_db)) -> list[UploadListItem]:
    return [UploadListItem.model_validate(job) for job in jobs.list_jobs(db, limit)]


@router.get("/{job_id}", response_model=UploadDetail)
def get_upload(job_id: uuid.UUID, db: Session = Depends(get_db)) -> UploadDetail:
    return UploadDetail.model_validate(jobs.get_job(db, job_id))

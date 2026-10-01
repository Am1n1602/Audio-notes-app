from fastapi import APIRouter, Depends, Response
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.db.session import get_db
from app.schemas.health import HealthResponse
from app.services import health as health_service

router = APIRouter()


@router.get("/health", response_model=HealthResponse)
def health(
    response: Response,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> HealthResponse:
    """200 when Postgres (and Redis, if the queue is Redis) answer, 503 (with per-dependency detail) otherwise."""
    db_ok = health_service.check_db(db)
    redis_ok = health_service.check_redis(settings.redis_url) if settings.queue_backend == "celery" else None
    healthy = db_ok and redis_ok is not False
    if not healthy:
        response.status_code = 503
    return HealthResponse(
        status="ok" if healthy else "degraded",
        db="ok" if db_ok else "error",
        redis=None if redis_ok is None else ("ok" if redis_ok else "error"),
    )

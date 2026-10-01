from fastapi import APIRouter, Depends

from app.core.config import Settings, get_settings
from app.schemas.app_config import AppConfig, Language
from app.services.upload_rules import AUDIO_TYPES, DEFAULT_LANGUAGE, LANGUAGE_NAMES, MAX_LANGUAGES

router = APIRouter()


@router.get("/config", response_model=AppConfig)
def app_config(settings: Settings = Depends(get_settings)) -> AppConfig:
    """Public and read-only: the file types, size limit and languages that /api/uploads/initiate accepts."""
    return AppConfig(
        max_upload_bytes=settings.max_upload_bytes,
        upload_url_expires_seconds=settings.upload_url_expires_seconds,
        audio_retention_seconds=settings.audio_retention_seconds,
        audio_extensions=list(AUDIO_TYPES),
        languages=[Language(code=code, name=name) for code, name in LANGUAGE_NAMES.items()],
        max_languages=MAX_LANGUAGES,
        default_language_code=DEFAULT_LANGUAGE,
    )

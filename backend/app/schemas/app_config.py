from pydantic import BaseModel


class Language(BaseModel):
    code: str
    name: str


class AppConfig(BaseModel):
    """The limits the backend actually enforces, so the page can show them without copying them."""

    max_upload_bytes: int
    upload_url_expires_seconds: int
    audio_extensions: list[str]
    languages: list[Language]
    max_languages: int
    default_language_code: str

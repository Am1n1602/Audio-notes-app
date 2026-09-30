"""Pure validation and naming rules for uploads. No I/O, so every rule is a one-line unit test."""

import re
from dataclasses import dataclass
from pathlib import PurePosixPath

from app.core.errors import AppError

# Gnani Batch's supported formats, mapped to the Content-Type WE choose.
# The browser's own MIME guess is never trusted or even asked for: it is empty or wrong for .m4a/.flac
# on some browsers, and the value we pick is signed into the upload URL.
AUDIO_TYPES: dict[str, str] = {
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".mp4": "video/mp4",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
    ".opus": "audio/opus",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".webm": "audio/webm",
    ".amr": "audio/amr",
}

# Languages Gnani Batch accepts. Up to three comma-separated codes turn on language identification.
SUPPORTED_LANGUAGES = frozenset({"bn-IN", "en-IN", "hi-IN", "kn-IN", "ml-IN", "mr-IN", "ta-IN", "te-IN"})
MAX_LANGUAGES = 3
MAX_FILENAME_LENGTH = 255

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


@dataclass(frozen=True)
class ValidatedUpload:
    original_filename: str
    extension: str
    mime_type: str
    size_bytes: int
    language_code: str


def clean_filename(raw: str) -> str:
    """Display name only: drop any directory part and control characters. Never used as a storage path."""
    name = raw.replace("\\", "/").rsplit("/", 1)[-1]
    name = _CONTROL_CHARS.sub("", name).strip()
    return name[-MAX_FILENAME_LENGTH:]  # keep the tail so the extension survives


def normalize_language(raw: str) -> str:
    codes = [code.strip() for code in raw.split(",")]
    unsupported = [c for c in codes if c not in SUPPORTED_LANGUAGES]
    if unsupported or not (1 <= len(codes) <= MAX_LANGUAGES) or len(set(codes)) != len(codes):
        raise AppError(
            422,
            "UNSUPPORTED_LANGUAGE",
            f"Language must be 1 to {MAX_LANGUAGES} distinct codes from: {', '.join(sorted(SUPPORTED_LANGUAGES))}.",
        )
    return ",".join(codes)


def format_bytes(size: int) -> str:
    return f"{size / 1024**3:.1f} GB" if size >= 1024**3 else f"{size / 1024**2:.0f} MB"


def validate_upload(filename: str, size_bytes: int, language_code: str, max_bytes: int) -> ValidatedUpload:
    name = clean_filename(filename)
    if not name:
        raise AppError(422, "INVALID_FILENAME", "The file name is empty or invalid.")

    extension = PurePosixPath(name).suffix.lower()
    if extension not in AUDIO_TYPES:
        shown = extension or "This kind of"
        raise AppError(
            422,
            "UNSUPPORTED_FILE_TYPE",
            f"{shown} files are not supported. Supported types: {', '.join(sorted(AUDIO_TYPES))}.",
        )

    if size_bytes <= 0:
        raise AppError(422, "EMPTY_FILE", "The file is empty.")
    if size_bytes > max_bytes:
        raise AppError(413, "FILE_TOO_LARGE", f"The file is too large. The maximum size is {format_bytes(max_bytes)}.")

    return ValidatedUpload(
        original_filename=name,
        extension=extension,
        mime_type=AUDIO_TYPES[extension],
        size_bytes=size_bytes,
        language_code=normalize_language(language_code),
    )


def object_key(job_id: object, extension: str) -> str:
    """Deterministic and built only from server-controlled values: the job UUID and a whitelisted extension."""
    return f"uploads/{job_id}/audio{extension}"

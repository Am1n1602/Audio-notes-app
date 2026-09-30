import uuid

import pytest

from app.core.errors import AppError
from app.services.upload_rules import (
    AUDIO_TYPES,
    ValidatedUpload,
    clean_filename,
    normalize_language,
    object_key,
    validate_upload,
)

MAX = 1000


def check(filename: str = "call.mp3", size: int = 10, language: str = "en-IN") -> ValidatedUpload:
    return validate_upload(filename, size, language, MAX)


def error_code(**kwargs: object) -> str:
    with pytest.raises(AppError) as info:
        check(**kwargs)  # type: ignore[arg-type]
    return info.value.code


def test_object_key_is_deterministic_and_built_from_server_values_only() -> None:
    job_id = uuid.UUID("12345678-1234-5678-1234-567812345678")
    assert object_key(job_id, ".mp3") == "uploads/12345678-1234-5678-1234-567812345678/audio.mp3"
    assert object_key(job_id, ".mp3") == object_key(job_id, ".mp3")


@pytest.mark.parametrize(
    ("raw", "cleaned"),
    [
        ("../../etc/passwd.mp3", "passwd.mp3"),  # path traversal
        ("C:\\Users\\me\\Meeting.wav", "Meeting.wav"),  # Windows path
        ("call\x00\x1f.mp3", "call.mp3"),  # control characters
        ("  spaced.mp3  ", "spaced.mp3"),
        ("a/b/", ""),  # nothing left
    ],
)
def test_filenames_lose_directories_and_control_characters(raw: str, cleaned: str) -> None:
    assert clean_filename(raw) == cleaned


def test_very_long_filename_keeps_its_extension() -> None:
    assert clean_filename("x" * 400 + ".mp3").endswith(".mp3")
    assert len(clean_filename("x" * 400 + ".mp3")) == 255


def test_valid_upload_gets_a_server_chosen_content_type() -> None:
    ok = check("Team Call.MP3")  # extension matching is case-insensitive
    assert (ok.original_filename, ok.extension, ok.mime_type) == ("Team Call.MP3", ".mp3", "audio/mpeg")
    assert {check(f"a{ext}").mime_type for ext in AUDIO_TYPES} == set(AUDIO_TYPES.values())


@pytest.mark.parametrize("name", ["notes.txt", "photo.jpg", "recording", "evil.mp3.exe", ".mp3x", "archive.zip"])
def test_unsupported_file_types_are_refused(name: str) -> None:
    assert error_code(filename=name) == "UNSUPPORTED_FILE_TYPE"


def test_empty_filename_is_refused() -> None:
    assert error_code(filename="../") == "INVALID_FILENAME"


def test_size_limits() -> None:
    assert error_code(size=0) == "EMPTY_FILE"
    assert error_code(size=-5) == "EMPTY_FILE"
    assert check(size=MAX).size_bytes == MAX  # exactly the limit is fine
    with pytest.raises(AppError) as info:
        check(size=MAX + 1)
    assert (info.value.status_code, info.value.code) == (413, "FILE_TOO_LARGE")


@pytest.mark.parametrize(
    ("raw", "normalized"),
    [("en-IN", "en-IN"), ("hi-IN,en-IN", "hi-IN,en-IN"), (" hi-IN , en-IN ", "hi-IN,en-IN")],
)
def test_supported_languages(raw: str, normalized: str) -> None:
    assert normalize_language(raw) == normalized


@pytest.mark.parametrize(
    "raw",
    ["xx-XX", "gu-IN", "pa-IN", "", "en-IN,en-IN", "hi-IN,en-IN,kn-IN,ta-IN", "en-IN,"],
)  # gu-IN and pa-IN are REST-only at Gnani: Batch rejects them
def test_unsupported_languages_are_refused(raw: str) -> None:
    with pytest.raises(AppError) as info:
        normalize_language(raw)
    assert info.value.code == "UNSUPPORTED_LANGUAGE"

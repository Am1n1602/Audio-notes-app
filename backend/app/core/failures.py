"""Stable failure codes stored on a job (audio_jobs.error_code) and which of them a user may retry.

A failed job always carries a code (for the UI to switch on) and a message (safe to show to a person).
Neither may contain secrets, signed URLs or exception text.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Failure:
    code: str
    message: str


UPLOAD_SIZE_MISMATCH = "UPLOAD_SIZE_MISMATCH"
QUEUE_UNAVAILABLE = "QUEUE_UNAVAILABLE"  # could not hand the job to the worker
SUBMIT_UNCONFIRMED = "SUBMIT_UNCONFIRMED"  # Create Job outcome unknown (crash or lost reply); never auto-retried
TRANSCRIPTION_UNAVAILABLE = "TRANSCRIPTION_UNAVAILABLE"  # Gnani busy or down after bounded retries
TRANSCRIPTION_AUTH_FAILED = "TRANSCRIPTION_AUTH_FAILED"  # our Gnani credentials are wrong: an operator problem
TRANSCRIPTION_REQUEST_REJECTED = "TRANSCRIPTION_REQUEST_REJECTED"  # Gnani refused the request as invalid
SOURCE_UNREACHABLE = "SOURCE_UNREACHABLE"  # Gnani could not download the audio from our signed link
EMPTY_TRANSCRIPT = "EMPTY_TRANSCRIPT"  # silence or no recognisable speech
INVALID_AUDIO = "INVALID_AUDIO"  # Gnani could not decode the file: corrupt, or not audio at all
RECORDING_TOO_LONG = "RECORDING_TOO_LONG"  # over Gnani's 4 hour limit per file: fails again
RECORDING_MISSING = "RECORDING_MISSING"  # the uploaded file is gone from storage: only a new upload helps
TRANSCRIPTION_FAILED = "TRANSCRIPTION_FAILED"  # Gnani processed the file and failed for another reason
TRANSCRIPTION_TIMEOUT = "TRANSCRIPTION_TIMEOUT"  # still not finished after gnani_max_transcribe_seconds
TRANSCRIPTION_CANCELLED = "TRANSCRIPTION_CANCELLED"
SUMMARY_UNAVAILABLE = "SUMMARY_UNAVAILABLE"  # the LLM was busy or down after bounded retries
SUMMARY_RATE_LIMITED = "SUMMARY_RATE_LIMITED"  # the LLM asked us to wait longer than we are willing to inside a step
SUMMARY_INVALID_RESPONSE = "SUMMARY_INVALID_RESPONSE"  # the model answered, but not with the required structure
SUMMARY_AUTH_FAILED = "SUMMARY_AUTH_FAILED"  # our LLM credentials are wrong: an operator problem
SUMMARY_REQUEST_REJECTED = "SUMMARY_REQUEST_REJECTED"  # the LLM refused the request as invalid
INTERNAL_ERROR = "INTERNAL_ERROR"

# Codes where pressing Retry can plausibly help. Everything else needs a different file or an operator.
RETRYABLE_CODES = frozenset(
    {
        QUEUE_UNAVAILABLE,
        SUBMIT_UNCONFIRMED,
        TRANSCRIPTION_UNAVAILABLE,
        SOURCE_UNREACHABLE,
        TRANSCRIPTION_FAILED,
        TRANSCRIPTION_TIMEOUT,
        TRANSCRIPTION_CANCELLED,
        SUMMARY_UNAVAILABLE,
        SUMMARY_RATE_LIMITED,
        SUMMARY_INVALID_RESPONSE,
        INTERNAL_ERROR,
    }
)

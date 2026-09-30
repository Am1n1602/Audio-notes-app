import logging


def configure_logging(level: str) -> None:
    logging.basicConfig(level=level.upper(), format="%(asctime)s %(levelname)s %(name)s %(message)s")


def log_event(logger: logging.Logger, event: str, **fields: object) -> None:
    """One structured line per event: `event=upload_created job_id=... size_bytes=...`.

    Callers choose the fields. Never pass signed URLs, API keys or transcript text.
    """
    logger.info(" ".join([f"event={event}", *(f"{key}={value}" for key, value in fields.items())]))

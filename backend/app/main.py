from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import app_config, health, uploads
from app.core.config import get_settings
from app.core.errors import register_error_handlers
from app.core.logging import configure_logging


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(title="Audio Notes API")
    # The browser calls this API directly from another origin (Vercel -> API host), so CORS is required.
    # No cookies or auth in this demo, hence no allow_credentials.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    register_error_handlers(app)
    app.include_router(health.router, prefix="/api")
    app.include_router(app_config.router, prefix="/api")
    app.include_router(uploads.router, prefix="/api")
    return app


app = create_app()

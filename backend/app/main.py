from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from app.api import app_config, health, internal, uploads
from app.core.config import get_settings
from app.core.errors import register_error_handlers
from app.core.logging import configure_logging
from app.services import steps


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
        # The page compares the API's timestamps with the server's clock, not the browser's (which can be minutes
        # off). A cross-origin page can only read the response Date header if it is exposed here.
        expose_headers=["Date"],
    )
    # A long recording's transcript makes the job answer 40 KB (a 4 hour one about 250 KB), and an open page asks for
    # it every few seconds while the summary is written. Text compresses to a fraction; small answers are left alone.
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    register_error_handlers(app)
    if settings.service_role == "steps":
        # The private service Cloud Tasks calls. It registers NO /api routes, and the public service registers no
        # /internal route: Cloud Run grants access per service, so the two cannot share one.
        steps.assert_ready(settings)
        app.include_router(internal.router, prefix="/internal")
    else:
        app.include_router(health.router, prefix="/api")
        app.include_router(app_config.router, prefix="/api")
        app.include_router(uploads.router, prefix="/api")
    return app


app = create_app()

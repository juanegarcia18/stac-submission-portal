"""Create the Flask application for the STAC Submission Portal.

The app factory wires HTTP routes to persisted job state and a single
background worker. Importing this module does not run ETL work; preparation and
commit operations start only after explicit browser actions enqueue jobs.
"""

from __future__ import annotations

import os
from pathlib import Path

from flask import Flask
from werkzeug.middleware.proxy_fix import ProxyFix

from backend.routes.job_status_routes import job_status_routes
from backend.routes.health_check_routes import health_check_routes
from backend.routes.workflow_routes import workflow_routes
from backend.services.execute_background_jobs import BackgroundJobExecutor
from backend.services.persist_job_state import create_job_store
from backend.services.validate_web_access import prepare_csrf_token

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_ROOT = PROJECT_ROOT / "frontend"


def create_flask_application(config: dict | None = None) -> Flask:
    """Create and configure the Flask application."""

    app = Flask(
        __name__,
        template_folder=str(FRONTEND_ROOT / "templates"),
        static_folder=str(FRONTEND_ROOT / "static"),
        static_url_path="/static",
    )
    _configure_application(app, config or {})
    _register_application_routes(app)
    _initialize_application_services(app)
    return app


def _configure_application(app: Flask, overrides: dict) -> None:
    """Apply security-sensitive defaults and caller overrides."""

    app.config.update(
        SECRET_KEY=os.getenv("NACC_FLASK_SECRET", os.urandom(32)),
        MAX_CONTENT_LENGTH=int(os.getenv("NACC_MAX_UPLOAD_BYTES", str(5 * 1024 * 1024))),
        NACC_JOB_ROOT=os.getenv("NACC_JOB_ROOT", str(PROJECT_ROOT / ".runtime" / "flask_jobs")),
        ENABLE_DEV_AUTH=os.getenv("NACC_ENABLE_DEV_AUTH", "0") in {"1", "true", "yes"},
        DEV_AUTH_USER=os.getenv("NACC_DEV_AUTH_USER", "development-user"),
        TRUSTED_AUTH_ENABLED=os.getenv("NACC_TRUSTED_AUTH_ENABLED", "0") in {"1", "true", "yes"},
        TRUSTED_USER_HEADER=os.getenv("NACC_TRUSTED_USER_HEADER", "X-Remote-User"),
        TRUST_PROXY_HEADERS=os.getenv("NACC_TRUST_PROXY_HEADERS", "0") in {"1", "true", "yes"},
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE=os.getenv("NACC_SESSION_COOKIE_SAMESITE", "Lax"),
        SESSION_COOKIE_SECURE=os.getenv("NACC_SESSION_COOKIE_SECURE", "0") in {"1", "true", "yes"},
        PREFERRED_URL_SCHEME=os.getenv("NACC_PREFERRED_URL_SCHEME", "https"),
        START_BACKGROUND_WORKER=True,
    )
    app.config.update(overrides)
    if app.config.get("TRUST_PROXY_HEADERS", False):
        _apply_proxy_header_trust(app)


def _register_application_routes(app: Flask) -> None:
    """Register browser and status routes."""

    app.register_blueprint(health_check_routes)
    app.register_blueprint(workflow_routes)
    app.register_blueprint(job_status_routes)
    app.context_processor(lambda: {"csrf_token": prepare_csrf_token()})


def _initialize_application_services(app: Flask) -> None:
    """Initialize durable job storage and the single background worker."""

    job_root = create_job_store(app.config["NACC_JOB_ROOT"])
    executor = BackgroundJobExecutor(job_root, max_queue_size=app.config.get("NACC_QUEUE_SIZE", 20))
    app.extensions["nacc_job_root"] = job_root
    app.extensions["nacc_job_executor"] = executor
    if app.config.get("START_BACKGROUND_WORKER", True):
        executor.start()


def _apply_proxy_header_trust(app: Flask) -> None:
    """Trust reverse-proxy headers only when deployment explicitly enables it."""

    app.wsgi_app = ProxyFix(
        app.wsgi_app,
        x_for=int(app.config.get("NACC_PROXY_FIX_FOR", os.getenv("NACC_PROXY_FIX_FOR", "1"))),
        x_proto=int(app.config.get("NACC_PROXY_FIX_PROTO", os.getenv("NACC_PROXY_FIX_PROTO", "1"))),
        x_host=int(app.config.get("NACC_PROXY_FIX_HOST", os.getenv("NACC_PROXY_FIX_HOST", "0"))),
        x_port=int(app.config.get("NACC_PROXY_FIX_PORT", os.getenv("NACC_PROXY_FIX_PORT", "0"))),
        x_prefix=int(app.config.get("NACC_PROXY_FIX_PREFIX", os.getenv("NACC_PROXY_FIX_PREFIX", "0"))),
    )

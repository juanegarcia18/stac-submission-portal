"""Run the Flask application with Waitress for single-container deployment.

This module is the production container entry point. It configures logging,
creates exactly one Flask application instance, starts the app-owned background
worker, and serves requests with Waitress. The process is intended to run as one
Waitress process behind an Infrastructure-managed reverse proxy.
"""

from __future__ import annotations

import atexit
import logging
import os
import signal
import sys
from dataclasses import dataclass
from pathlib import Path

from backend.flask_application import create_flask_application

DEFAULT_STATE_ROOT = Path("/var/lib/nacc-pipeline")
log = logging.getLogger(__name__)


@dataclass(frozen=True)
class WaitressSettings:
    """Runtime settings used by the Waitress server."""

    host: str
    port: int
    threads: int


def create_production_application(config: dict | None = None):
    """Create the deployment Flask app and register worker shutdown hooks."""

    configure_runtime_logging()
    runtime_config = load_flask_runtime_config(config or {})
    validate_deployment_security(runtime_config)
    app = create_flask_application(runtime_config)
    register_worker_shutdown(app)
    return app


def execute_waitress_server() -> None:
    """Create the app and serve it with Waitress."""

    app = create_production_application()
    settings = load_waitress_settings()
    log.info("Starting Waitress on %s:%s with %s threads", settings.host, settings.port, settings.threads)
    from waitress import serve

    serve(app, host=settings.host, port=settings.port, threads=settings.threads)


def load_flask_runtime_config(overrides: dict) -> dict:
    """Load container-oriented Flask configuration from environment variables."""

    config = {
        "NACC_JOB_ROOT": os.getenv("NACC_JOB_ROOT", str(DEFAULT_STATE_ROOT / "jobs")),
        "MAX_CONTENT_LENGTH": int(os.getenv("NACC_MAX_UPLOAD_BYTES", str(5 * 1024 * 1024))),
        "SESSION_COOKIE_SECURE": os.getenv("NACC_SESSION_COOKIE_SECURE", "1") in {"1", "true", "yes"},
        "START_BACKGROUND_WORKER": True,
    }
    config.update(overrides)
    return config


def load_waitress_settings() -> WaitressSettings:
    """Return validated Waitress bind settings."""

    return WaitressSettings(
        host=os.getenv("NACC_HOST", "0.0.0.0"),
        port=_positive_int("NACC_PORT", 8000),
        threads=_positive_int("NACC_WAITRESS_THREADS", 4),
    )


def validate_deployment_security(config: dict) -> None:
    """Fail closed for production deployments missing required auth settings."""

    deployment = os.getenv("NACC_DEPLOYMENT_ENV", "production").strip().lower()
    if deployment == "production" and not os.getenv("NACC_FLASK_SECRET"):
        raise RuntimeError("NACC_FLASK_SECRET is required for production deployment.")
    if deployment == "production" and os.getenv("NACC_ENABLE_DEV_AUTH", "0") in {"1", "true", "yes"}:
        raise RuntimeError("NACC_ENABLE_DEV_AUTH must not be enabled in production.")
    if config.get("SESSION_COOKIE_SECURE") is False and deployment == "production":
        raise RuntimeError("Secure session cookies are required in production.")


def register_worker_shutdown(app) -> None:
    """Stop the in-process worker on normal process shutdown.

    If the container is terminated during an external write, restart recovery
    marks that job as requiring reconciliation before any retry can occur.
    """

    def stop_worker() -> None:
        executor = app.extensions.get("nacc_job_executor")
        if executor:
            log.info("Stopping background worker")
            executor.stop()

    atexit.register(stop_worker)
    _register_signal_handler(stop_worker)


def configure_runtime_logging() -> None:
    """Configure stdout logging for container operation."""

    level = os.getenv("NACC_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
        force=True,
    )


def _register_signal_handler(stop_worker) -> None:
    """Install simple SIGTERM/SIGINT hooks for container shutdown."""

    def handle_signal(signum, _frame) -> None:
        log.info("Received signal %s; shutting down", signum)
        stop_worker()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)


def _positive_int(name: str, default: int) -> int:
    """Read a positive integer environment setting."""

    value = int(os.getenv(name, str(default)))
    if value < 1:
        raise RuntimeError(f"{name} must be a positive integer.")
    return value


if __name__ == "__main__":
    execute_waitress_server()

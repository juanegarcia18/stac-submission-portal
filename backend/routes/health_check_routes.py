"""Health check route for container and reverse-proxy probes.

The endpoint confirms that Flask can respond and that the private job store is
configured. It intentionally avoids REDCap, Flywheel, job metadata contents, and
participant-level artifacts so health probes cannot trigger external work or
leak protected information.
"""

from __future__ import annotations

from flask import Blueprint, current_app, jsonify

health_check_routes = Blueprint("health_check_routes", __name__)


@health_check_routes.get("/healthz")
def fetch_health_status():
    """Return a lightweight application health response."""

    job_root = current_app.extensions.get("nacc_job_root")
    return jsonify({
        "status": "ok",
        "job_store": "ready" if job_root and job_root.exists() else "unavailable",
    })

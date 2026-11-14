"""Routes for job status, protected review files, approval, and cancellation."""

from __future__ import annotations

import queue

from flask import Blueprint, abort, current_app, jsonify, redirect, render_template, send_file, url_for

from backend.services.persist_job_state import (
    JobStateError,
    load_job_record,
    persist_job_record,
    resolve_artifact_path,
    transition_job_record,
)
from backend.services.transition_job_state import CANCELLED
from backend.services.transition_job_state import TERMINAL_STATES, can_approve_job, can_cancel_job
from backend.services.validate_web_access import (
    create_job_approval,
    prepare_csrf_token,
    validate_cancel_request,
    validate_job_access,
)

job_status_routes = Blueprint("job_status_routes", __name__)


@job_status_routes.get("/jobs/<job_id>")
def show_job_status(job_id: str):
    """Render a review/status page for one job."""

    job = _load_or_404(job_id)
    validate_job_access(job)
    return render_template(
        "job_status.html",
        job=job,
        public_job=_public_job(job),
        csrf_token=prepare_csrf_token(),
    )


@job_status_routes.get("/api/jobs/<job_id>")
def fetch_job_status(job_id: str):
    """Return sanitized job status JSON for polling."""

    job = _load_or_404(job_id)
    validate_job_access(job)
    return jsonify(_public_job(job))


@job_status_routes.get("/jobs/<job_id>/files/<artifact_id>")
def fetch_review_artifact(job_id: str, artifact_id: str):
    """Download a prepared review artifact by opaque artifact ID."""

    job = _load_or_404(job_id)
    validate_job_access(job)
    path = resolve_artifact_path(job, artifact_id, review=True)
    return send_file(path, as_attachment=True, download_name=path.name)


@job_status_routes.post("/jobs/<job_id>/approve")
def approve_job_commit(job_id: str):
    """Record server-side approval and enqueue the commit job."""

    job = _load_or_404(job_id)
    job["approval"] = create_job_approval(job)
    persist_job_record(job)
    try:
        _executor().enqueue_commit(job["job_id"])
    except queue.Full:
        job["approval"] = {}
        job["error"] = "Job queue is full. Try approval again after another job finishes."
        persist_job_record(job)
        abort(503)
    return redirect(url_for("job_status_routes.show_job_status", job_id=job_id))


@job_status_routes.post("/jobs/<job_id>/cancel")
def cancel_job(job_id: str):
    """Cancel a queued or awaiting-review job without touching external systems."""

    job = _load_or_404(job_id)
    validate_cancel_request(job)
    transition_job_record(job, CANCELLED)
    return redirect(url_for("job_status_routes.show_job_status", job_id=job_id))


def _load_or_404(job_id: str) -> dict:
    """Load a job or return a 404 for invalid/unavailable IDs."""

    try:
        return load_job_record(current_app.extensions["nacc_job_root"], job_id)
    except JobStateError:
        abort(404)


def _public_job(job: dict) -> dict:
    """Return browser-safe job metadata without filesystem paths."""

    return {
        "job_id": job["job_id"],
        "workflow": job["workflow"],
        "status": job["status"],
        "created_at": job["created_at"],
        "updated_at": job["updated_at"],
        "parameters": job.get("parameters", {}),
        "preparation": job.get("preparation", {}),
        "commit": job.get("commit", {}),
        "upload_destination": job.get("upload_destination", ""),
        "error": job.get("error", ""),
        "artifacts": [{"id": item["id"], "name": item["name"]} for item in job.get("review_artifacts", [])],
        "can_approve": can_approve_job(job["status"]) and bool(job.get("artifacts")) and not job.get("approval"),
        "can_cancel": can_cancel_job(job["status"]) and not job.get("approval"),
        "terminal": job["status"] in TERMINAL_STATES,
    }


def _executor():
    """Return the configured background executor."""

    return current_app.extensions["nacc_job_executor"]

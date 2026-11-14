"""Routes for workflow selection and job creation."""

from __future__ import annotations

import logging
import queue

from flask import Blueprint, current_app, redirect, render_template, request, url_for

from backend.services.config import legacy_form_config
from backend.services.persist_job_state import (
    JobStateError,
    create_job_record,
    save_uploaded_file,
    transition_job_record,
)
from backend.services.transition_job_state import FAILED
from backend.services.validate_web_access import (
    fetch_authenticated_user,
    prepare_csrf_token,
    validate_csrf_token,
)

workflow_routes = Blueprint("workflow_routes", __name__)
log = logging.getLogger(__name__)


@workflow_routes.get("/")
def show_workflow_selection():
    """Render the browser landing page with enabled form choices."""

    fetch_authenticated_user()
    forms = sorted(legacy_form_config().keys())
    return render_template("workflow_selection.html", forms=forms, csrf_token=prepare_csrf_token())


@workflow_routes.post("/jobs/udsv4")
def create_udsv4_job():
    """Create a queued UDSv4 submission preparation job."""

    user = fetch_authenticated_user()
    validate_csrf_token()
    selected_forms = request.form.getlist("forms")
    upload = request.files.get("filter_csv")
    if not selected_forms or not upload or not upload.filename:
        return _render_selection_error("Select forms and upload a CSV."), 400
    if not _valid_selected_forms(selected_forms):
        return _render_selection_error("Invalid form selection."), 400

    job = create_job_record(
        _job_root(),
        "udsv4_submission",
        {"selected_forms": selected_forms},
        owner=user,
    )
    log.info("Created UDSv4 job %s for %s form(s)", job["job_id"], len(selected_forms))
    try:
        save_uploaded_file(job, "filter_csv", upload)
        _executor().enqueue_prepare(job["job_id"])
    except Exception as exc:
        transition_job_record(job, FAILED, error="Could not queue UDSv4 job.")
        return _render_selection_error(_friendly_queue_error(exc)), 400
    return redirect(url_for("job_status_routes.show_job_status", job_id=job["job_id"]))


@workflow_routes.post("/jobs/enrollment")
def create_enrollment_job():
    """Create a queued enrollment submission preparation job."""

    user = fetch_authenticated_user()
    validate_csrf_token()
    job = create_job_record(_job_root(), "enrollment_submission", owner=user)
    log.info("Created enrollment job %s", job["job_id"])
    _enqueue_or_fail(job)
    return redirect(url_for("job_status_routes.show_job_status", job_id=job["job_id"]))


@workflow_routes.post("/jobs/naccid")
def create_naccid_job():
    """Create a queued NACCID import preparation job."""

    user = fetch_authenticated_user()
    validate_csrf_token()
    job = create_job_record(_job_root(), "naccid_import", owner=user)
    log.info("Created NACCID import job %s", job["job_id"])
    _enqueue_or_fail(job)
    return redirect(url_for("job_status_routes.show_job_status", job_id=job["job_id"]))


def _enqueue_or_fail(job: dict) -> None:
    """Queue preparation or mark the job failed if the queue is full."""

    try:
        _executor().enqueue_prepare(job["job_id"])
    except queue.Full:
        transition_job_record(job, FAILED, error="Job queue is full.")


def _render_selection_error(message: str):
    """Render a sanitized workflow-selection error."""

    return render_template(
        "workflow_selection.html",
        forms=sorted(legacy_form_config().keys()),
        csrf_token=prepare_csrf_token(),
        error=message,
    )


def _friendly_queue_error(exc: Exception) -> str:
    """Return a browser-safe message for upload and queue failures."""

    if isinstance(exc, queue.Full):
        return "Job queue is full."
    if isinstance(exc, JobStateError):
        return "Uploaded file must be a CSV."
    return "Could not queue UDSv4 job."


def _valid_selected_forms(selected_forms: list[str]) -> bool:
    """Return True when every browser-selected form is enabled in YAML."""

    enabled = set(legacy_form_config())
    return bool(selected_forms) and all(form in enabled for form in selected_forms)


def _job_root():
    """Return the configured private job root."""

    return current_app.extensions["nacc_job_root"]


def _executor():
    """Return the configured background executor."""

    return current_app.extensions["nacc_job_executor"]

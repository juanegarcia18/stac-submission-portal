"""Validate Flask access, CSRF, and server-side job approval.

This module intentionally does not implement production authentication. It can
accept a trusted upstream user header only when that deployment contract is
explicitly enabled, or a development-only user when local preview auth is
enabled. Commit routes must never accept browser-created approval objects.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone

from flask import abort, current_app, request, session

from backend.services.transition_job_state import can_approve_job, can_cancel_job


def fetch_authenticated_user() -> str:
    """Return a trusted user identifier or reject anonymous access."""

    if current_app.config.get("TRUSTED_AUTH_ENABLED", False):
        header = current_app.config.get("TRUSTED_USER_HEADER", "X-Remote-User")
        user = request.headers.get(header, "").strip()
        if user:
            return user
    if current_app.config.get("ENABLE_DEV_AUTH", False):
        return current_app.config.get("DEV_AUTH_USER", "development-user")
    abort(401)


def validate_job_access(job: dict) -> str:
    """Require the current user to own a persisted job before exposing it.

    Production deployments must create jobs with an owner. Development mode can
    still inspect ownerless preview jobs created before ownership was added.
    """

    user = fetch_authenticated_user()
    owner = str(job.get("owner", "")).strip()
    if owner == user:
        return user
    if not owner and current_app.config.get("ENABLE_DEV_AUTH", False):
        return user
    abort(403)


def prepare_csrf_token() -> str:
    """Return the session CSRF token used by browser forms."""

    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(24)
    return session["csrf_token"]


def validate_csrf_token() -> None:
    """Reject state-changing requests without the session CSRF token."""

    expected = session.get("csrf_token")
    submitted = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
    if not expected or not submitted or not secrets.compare_digest(expected, submitted):
        abort(400)


def validate_cancel_request(job: dict) -> None:
    """Require authentication, CSRF, and a cancellable job status."""

    validate_job_access(job)
    validate_csrf_token()
    if job.get("approval") or not can_cancel_job(job["status"]):
        abort(409)


def create_job_approval(job: dict) -> dict:
    """Create persisted approval metadata after validating the reviewed job."""

    user = validate_job_access(job)
    validate_csrf_token()
    if not can_approve_job(job["status"]) or job.get("approval"):
        abort(409)
    return {
        "operation": _approval_operation(job["workflow"]),
        "approved_by": user,
        "approved_at": datetime.now(timezone.utc).isoformat(),
        "artifact_ids": [item["id"] for item in job.get("artifacts", [])],
    }


def _approval_operation(workflow: str) -> str:
    """Map workflow type to the backend approval contract operation."""

    if workflow in {"udsv4_submission", "enrollment_submission"}:
        return "flywheel_upload"
    if workflow == "naccid_import":
        return "naccid_import"
    abort(400)

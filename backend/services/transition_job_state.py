"""Validate job lifecycle transitions for the Flask workflow runner.

Routes and background workers must not assign arbitrary status strings. Keeping
state changes behind this module prevents duplicate commits, cancelled writes,
and accidental retries of interrupted external operations.
"""

from __future__ import annotations

QUEUED = "queued"
PREPARING = "preparing"
AWAITING_REVIEW = "awaiting_review"
COMMITTING = "committing"
COMPLETED = "completed"
CANCELLED = "cancelled"
FAILED = "failed"
RECONCILIATION_REQUIRED = "reconciliation_required"

TERMINAL_STATES = {COMPLETED, CANCELLED, FAILED, RECONCILIATION_REQUIRED}

ALLOWED_TRANSITIONS = {
    QUEUED: {PREPARING, CANCELLED, FAILED},
    PREPARING: {AWAITING_REVIEW, FAILED, CANCELLED},
    AWAITING_REVIEW: {COMMITTING, CANCELLED},
    COMMITTING: {COMPLETED, FAILED, RECONCILIATION_REQUIRED},
    COMPLETED: set(),
    CANCELLED: set(),
    FAILED: set(),
    RECONCILIATION_REQUIRED: set(),
}


class InvalidJobTransition(RuntimeError):
    """Raised when a route or worker attempts an invalid job transition."""


def validate_job_transition(current_status: str, next_status: str) -> None:
    """Raise when a job cannot move from ``current_status`` to ``next_status``."""

    allowed = ALLOWED_TRANSITIONS.get(current_status)
    if allowed is None:
        raise InvalidJobTransition(f"Unknown job status: {current_status}")
    if next_status not in allowed:
        raise InvalidJobTransition(f"Cannot transition job from {current_status} to {next_status}.")


def can_cancel_job(status: str) -> bool:
    """Return True when a job can be cancelled before an external write."""

    return status in {QUEUED, AWAITING_REVIEW}


def can_approve_job(status: str) -> bool:
    """Return True when a prepared job can be approved exactly once."""

    return status == AWAITING_REVIEW

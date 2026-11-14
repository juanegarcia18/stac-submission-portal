"""Validate approval records before backend external write operations.

The current CLI still gathers approval interactively. Backend commit functions
accept this small authorization object so future Flask jobs have a concrete
handoff point. It is not authentication, authorization, or durable approval
storage; those belong to the future job service.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


class ApprovalError(PermissionError):
    """Raised when an external write is attempted without valid approval."""


@dataclass(frozen=True)
class ApprovalAuthorization:
    """Minimal proof that a reviewed operation was approved for commit."""

    operation: str
    approved_by: str
    approved_at: str
    artifact_ids: tuple[str, ...] = ()


def create_cli_approval(operation: str, artifacts: Iterable[Path] = ()) -> ApprovalAuthorization:
    """Build an approval record after an existing CLI prompt succeeds."""

    return ApprovalAuthorization(
        operation=operation,
        approved_by="cli",
        approved_at=datetime.now(timezone.utc).isoformat(),
        artifact_ids=tuple(Path(path).name for path in artifacts),
    )


def validate_approval_authorization(
    approval: ApprovalAuthorization,
    operation: str,
) -> ApprovalAuthorization:
    """Require a matching, non-empty approval record before writing externally."""

    # A bare boolean is too weak for the web path because it carries no reviewer
    # or operation context for audit or job-state validation.
    if not isinstance(approval, ApprovalAuthorization):
        raise ApprovalError(f"{operation} requires an ApprovalAuthorization.")
    if approval.operation != operation:
        raise ApprovalError(f"Approval is for {approval.operation}, not {operation}.")
    if not approval.approved_by.strip() or not approval.approved_at.strip():
        raise ApprovalError(f"{operation} approval is missing reviewer metadata.")
    return approval

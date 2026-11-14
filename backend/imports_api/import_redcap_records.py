"""Approved REDCap record import operations for backend callers.

The backend transaction module owns the NACCID business safeguards: fresh
eligibility checks, overwrite protection, locking, verification, and recovery
journaling. This wrapper enforces approval and returns sanitized, structured
outcomes.
"""

from __future__ import annotations

import re
from typing import Callable

import pandas as pd
import requests

from backend.imports_api.external_write_results import FAILURE, NO_DATA, PARTIAL, SUCCESS, UNCERTAIN, RedcapImportResult
from backend.services.validate_job_approval import ApprovalAuthorization, validate_approval_authorization

NACCID_IMPORT_OPERATION = "naccid_import"


def import_approved_redcap_records(
    records: pd.DataFrame,
    approval: ApprovalAuthorization,
    *,
    importer: Callable[[pd.DataFrame], dict] | None = None,
) -> RedcapImportResult:
    """Import approved NACCID records through the existing protected workflow."""

    validate_approval_authorization(approval, NACCID_IMPORT_OPERATION)
    if records.empty:
        return RedcapImportResult(NO_DATA, attempted=0, message="No records to import.")

    try:
        summary = _run_importer(records, importer)
    except Exception as exc:
        return _result_from_exception(exc, len(records))

    return _result_from_summary(summary, len(records))


def _run_importer(records: pd.DataFrame, importer: Callable[[pd.DataFrame], dict] | None) -> dict:
    """Call the transaction importer so safeguards stay in one place."""

    if importer is not None:
        return importer(records)

    from backend.imports_api.import_naccid_transactions import import_naccids

    return import_naccids(records)


def _result_from_summary(summary: dict, attempted: int) -> RedcapImportResult:
    """Convert the transaction import summary to a backend outcome."""

    submitted = int(summary.get("submitted", 0))
    verified = int(summary.get("verified", 0))
    failed = int(summary.get("failed", 0))
    skipped = int(summary.get("skipped", 0))
    if submitted == 0:
        status = NO_DATA
    elif failed == 0 and verified == submitted:
        status = SUCCESS
    else:
        status = PARTIAL
    return RedcapImportResult(status, attempted, submitted, verified, skipped, failed)


def _result_from_exception(exc: Exception, attempted: int) -> RedcapImportResult:
    """Return sanitized failure details without participant IDs or API bodies."""

    if _is_uncertain_redcap_write(exc):
        return RedcapImportResult(
            UNCERTAIN,
            attempted,
            requires_reconciliation=True,
            message="REDCap import outcome is unknown and requires reconciliation.",
        )
    return RedcapImportResult(FAILURE, attempted, message=_sanitize_error(exc))


def _is_uncertain_redcap_write(exc: Exception) -> bool:
    """Identify network failures where REDCap may have accepted the write."""

    if isinstance(exc, (requests.Timeout, requests.ConnectionError)):
        return True
    return isinstance(exc, requests.RequestException) and exc.response is None


def _sanitize_error(exc: Exception) -> str:
    """Describe the failure class without exposing raw REDCap responses."""

    message = str(exc).splitlines()[0].strip()
    if not message:
        return exc.__class__.__name__
    if any(word in message.lower() for word in ("token", "authorization", "password", "secret")):
        return f"{exc.__class__.__name__}: sensitive details omitted."
    message = re.sub(r"\b\d{3,}\b", "[id]", message)
    message = re.sub(r"NACC[-_A-Za-z0-9]+", "[id]", message)
    return message[:180]

"""Prepare and commit reviewed NACCID imports.

Preparation reads pending REDCap enrollments and recent Flywheel enrollment
subjects, preserving the existing 30-day window and Flywheel creation date as
the accepted date. Commit uses the exact reviewed payload and delegates to the
protected REDCap import API.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Callable

import pandas as pd

from backend.imports_api.fetch_flywheel_naccids import connect_flywheel_client, fetch_recent_flywheel_naccids
from backend.imports_api.fetch_redcap_reports import fetch_form
from backend.imports_api.import_redcap_records import import_approved_redcap_records
from backend.services.config import resolve_naccid_import
from backend.services.job_workspace_files import prepare_private_workspace, prepare_review_files
from backend.services.validate_job_approval import ApprovalAuthorization
from backend.transforms.prepare_naccid_import_records import prepare_naccid_import_records
from backend.workflows.prepare_workflow_results import (
    ModulePreparationSummary,
    NO_DATA,
    PROCESSING_FAILED,
    SUCCESS,
    VALIDATION_FAILED,
    WorkflowPreparationResult,
)
WORKFLOW = "naccid_import"


def prepare_naccid_import(
    *,
    days: int = 30,
    workspace: Path | None = None,
    report_fetcher: Callable[[str], pd.DataFrame] | None = None,
    flywheel_client=None,
) -> WorkflowPreparationResult:
    """Prepare the exact REDCap import payload for manual review."""

    workspace = Path(workspace) if workspace else prepare_private_workspace()
    try:
        payload = _prepare_import_payload(days, _resolve_report_fetcher(report_fetcher), flywheel_client)
    except ValueError as exc:
        return _preparation_error(VALIDATION_FAILED, workspace, exc)
    except Exception as exc:
        return _preparation_error(PROCESSING_FAILED, workspace, exc)

    if payload.empty:
        return WorkflowPreparationResult(
            WORKFLOW,
            NO_DATA,
            workspace=workspace,
            payload=payload,
            module_summaries=(
                ModulePreparationSummary(
                    "NACCID Import",
                    requested_records=0,
                    prepared_records=0,
                    status=NO_DATA,
                    findings=("NACCID Import: No pending records were prepared.",),
                ),
            ),
        )
    artifact = _write_import_artifact(payload, workspace)
    review_file = prepare_review_files([artifact], workspace)[0]
    return WorkflowPreparationResult(
        WORKFLOW,
        SUCCESS,
        prepared_records=len(payload),
        workspace=workspace,
        artifacts=(artifact,),
        review_artifacts=(review_file,),
        payload=payload,
        module_summaries=(
            ModulePreparationSummary(
                "NACCID Import",
                requested_records=len(payload),
                prepared_records=len(payload),
                status=SUCCESS,
                artifact_name=artifact.name,
            ),
        ),
    )


def commit_naccid_import(
    payload: pd.DataFrame,
    approval: ApprovalAuthorization,
    *,
    importer: Callable[[pd.DataFrame], dict] | None = None,
):
    """Import an approved NACCID payload through the protected REDCap path."""

    return import_approved_redcap_records(payload.copy(), approval, importer=importer)


def _prepare_import_payload(days: int, report_fetcher, flywheel_client) -> pd.DataFrame:
    """Retrieve external sources and build the reviewable import payload."""

    naccid_config = resolve_naccid_import()
    columns = naccid_config["columns"]
    redcap = report_fetcher("enroll_checker")
    if redcap.empty:
        return pd.DataFrame(columns=columns)
    fw = flywheel_client if flywheel_client is not None else connect_flywheel_client()
    naccids = fetch_recent_flywheel_naccids(fw, days=days)
    if naccids.empty:
        return pd.DataFrame(columns=columns)
    return prepare_naccid_import_records(naccids, redcap, columns, naccid_config["event"])


def _resolve_report_fetcher(report_fetcher: Callable[[str], pd.DataFrame] | None):
    """Load the backend REDCap report fetcher only when preparation runs."""

    if report_fetcher is not None:
        return report_fetcher

    return fetch_form


def _write_import_artifact(payload: pd.DataFrame, workspace: Path) -> Path:
    """Write the reviewed import payload without changing column order."""

    workspace.mkdir(parents=True, exist_ok=True)
    output_file = workspace / f"{datetime.now().strftime('%m_%d_%Y_%H%M%S')}_naccid_import.csv"
    payload.to_csv(output_file, index=False)
    return output_file


def _preparation_error(status: str, workspace: Path, exc: Exception) -> WorkflowPreparationResult:
    """Return sanitized NACCID preparation failure details."""

    return WorkflowPreparationResult(WORKFLOW, status, workspace=workspace, error=_sanitize_message(exc))


def _sanitize_message(exc: Exception) -> str:
    """Remove credentials and participant-like identifiers from status messages."""

    message = str(exc).splitlines()[0].strip() or exc.__class__.__name__
    if any(word in message.lower() for word in ("token", "authorization", "password", "secret")):
        return f"{exc.__class__.__name__}: sensitive details omitted."
    message = re.sub(r"\b\d{3,}\b", "[id]", message)
    message = re.sub(r"NACC[-_A-Za-z0-9]+", "[id]", message)
    return message[:180]

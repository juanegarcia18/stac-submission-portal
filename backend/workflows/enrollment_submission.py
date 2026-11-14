"""Prepare and commit NACC enrollment submission artifacts.

Enrollment preparation keeps the existing confirmation-date, blank NACCID, and
duplicate-PTID rules from the legacy processor. Commit uploads the reviewed
original CSV to the explicit enrollment Flywheel destination.
"""

from __future__ import annotations

import csv
import re
from datetime import datetime
from pathlib import Path
from typing import Callable

import pandas as pd

from backend.imports_api.upload_flywheel_files import ENROLLMENT_SUBMISSION_DESTINATION, upload_flywheel_files
from backend.services.job_workspace_files import prepare_private_workspace, prepare_review_files, validate_original_files
from backend.services.validate_job_approval import ApprovalAuthorization
from backend.transforms.prepare_enrollment_records import (
    drop_invalid_enrollment_keys,
    filter_eligible_enrollment_records,
    match_eligible_enrollment_records,
    prepare_enrollment_participant_keys,
    prepare_enrollment_submission_output,
)
from backend.workflows.prepare_workflow_results import (
    ModulePreparationSummary,
    NO_DATA,
    PROCESSING_FAILED,
    SUCCESS,
    VALIDATION_FAILED,
    WorkflowPreparationResult,
)

WORKFLOW = "enrollment_submission"


def prepare_enrollment_submission(
    *,
    workspace: Path | None = None,
    report_fetcher: Callable[[str], pd.DataFrame] | None = None,
) -> WorkflowPreparationResult:
    """Prepare an enrollment CSV artifact for manual review."""

    workspace = Path(workspace) if workspace else prepare_private_workspace()
    try:
        fetcher = _resolve_report_fetcher(report_fetcher)
        checker = fetcher("enroll_checker")
        output = _prepare_enrollment_output(checker, fetcher)
    except ValueError as exc:
        return _preparation_error(VALIDATION_FAILED, workspace, exc)
    except Exception as exc:
        return _preparation_error(PROCESSING_FAILED, workspace, exc)

    if output.empty:
        return WorkflowPreparationResult(
            WORKFLOW,
            NO_DATA,
            workspace=workspace,
            module_summaries=(
                ModulePreparationSummary(
                    "Enrollment",
                    requested_records=0,
                    prepared_records=0,
                    status=NO_DATA,
                    findings=("Enrollment: No eligible records were prepared.",),
                ),
            ),
        )
    artifact = _write_enrollment_artifact(output, workspace)
    review_file = prepare_review_files([artifact], workspace)[0]
    return WorkflowPreparationResult(
        WORKFLOW,
        SUCCESS,
        prepared_records=len(output),
        workspace=workspace,
        artifacts=(artifact,),
        review_artifacts=(review_file,),
        module_summaries=(
            ModulePreparationSummary(
                "Enrollment",
                requested_records=len(output),
                prepared_records=len(output),
                status=SUCCESS,
                artifact_name=artifact.name,
            ),
        ),
    )


def commit_enrollment_submission(
    artifacts: list[Path] | tuple[Path, ...],
    approval: ApprovalAuthorization,
    *,
    workspace: Path | None = None,
    uploader_factory: Callable | None = None,
    expected_destination: str | None = None,
):
    """Upload approved original enrollment artifacts to the enrollment project."""

    originals = validate_original_files(artifacts, workspace)
    return upload_flywheel_files(
        originals,
        ENROLLMENT_SUBMISSION_DESTINATION,
        approval,
        uploader_factory=uploader_factory,
        expected_destination=expected_destination,
    )


def _prepare_enrollment_output(checker: pd.DataFrame, report_fetcher) -> pd.DataFrame:
    """Apply enrollment eligibility and matching operations."""

    if checker.empty:
        return pd.DataFrame()
    eligible = filter_eligible_enrollment_records(checker)
    if eligible.empty:
        return pd.DataFrame()
    eligible = prepare_enrollment_participant_keys(eligible, ["enrl_ptid"], "Enrollment checker")
    eligible = drop_invalid_enrollment_keys(eligible, "Enrollment checker")
    report = report_fetcher("enroll")
    if report.empty:
        raise ValueError("NACC enrollment report returned no records.")
    matched = match_eligible_enrollment_records(report, set(eligible[".key_ptid"]))
    return prepare_enrollment_submission_output(matched)


def _resolve_report_fetcher(report_fetcher: Callable[[str], pd.DataFrame] | None):
    """Load the backend REDCap report fetcher only when preparation runs."""

    if report_fetcher is not None:
        return report_fetcher
    from backend.imports_api.fetch_redcap_reports import fetch_form

    return fetch_form


def _write_enrollment_artifact(output: pd.DataFrame, workspace: Path) -> Path:
    """Write the legacy quoted enrollment CSV artifact."""

    workspace.mkdir(parents=True, exist_ok=True)
    output_file = workspace / f"{datetime.now().strftime('%m_%d_%Y')}_sub-enroll.csv"
    output.to_csv(output_file, index=False, quoting=csv.QUOTE_ALL)
    return output_file


def _preparation_error(status: str, workspace: Path, exc: Exception) -> WorkflowPreparationResult:
    """Return sanitized enrollment preparation failure details."""

    return WorkflowPreparationResult(WORKFLOW, status, workspace=workspace, error=_sanitize_message(exc))


def _sanitize_message(exc: Exception) -> str:
    """Remove credentials and participant-like identifiers from status messages."""

    message = str(exc).splitlines()[0].strip() or exc.__class__.__name__
    if any(word in message.lower() for word in ("token", "authorization", "password", "secret")):
        return f"{exc.__class__.__name__}: sensitive details omitted."
    message = re.sub(r"\b\d{3,}\b", "[id]", message)
    message = re.sub(r"NACC[-_A-Za-z0-9]+", "[id]", message)
    return message[:180]

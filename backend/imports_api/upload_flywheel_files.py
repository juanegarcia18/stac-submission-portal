"""Approved Flywheel upload operations for backend callers.

This module separates the commit boundary from CLI review prompts. Callers must
provide an approval authorization and a logical destination; enrollment uploads
therefore require ``FW_ENROLL`` through the shared configuration path.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Iterable

from backend.imports_api.external_write_results import (
    FAILURE,
    NO_DATA,
    PARTIAL,
    SUCCESS,
    UNCERTAIN,
    FileUploadResult,
    FlywheelUploadResult,
)
from backend.services.validate_job_approval import ApprovalAuthorization, validate_approval_authorization
from backend.services.config import resolve_flywheel_destination

FORM_SUBMISSION_DESTINATION = "form_submissions"
ENROLLMENT_SUBMISSION_DESTINATION = "enrollment_submissions"
UPLOAD_OPERATION = "flywheel_upload"


def upload_flywheel_files(
    files: Iterable[Path],
    destination_name: str,
    approval: ApprovalAuthorization,
    *,
    uploader_factory: Callable | None = None,
    expected_destination: str | None = None,
) -> FlywheelUploadResult:
    """Upload approved CSV files to one explicit Flywheel destination."""

    validate_approval_authorization(approval, UPLOAD_OPERATION)
    paths = [Path(path) for path in files]
    if not paths:
        return FlywheelUploadResult(NO_DATA, attempted=0, message="No files to upload.")

    project = resolve_flywheel_destination(destination_name)
    uploader = _build_uploader(uploader_factory)
    destination = f"{uploader.group}/{project}"
    if expected_destination is not None and destination != expected_destination:
        return FlywheelUploadResult(FAILURE, attempted=0, message="Flywheel destination changed. Prepare a new job.")
    if not uploader.login():
        return FlywheelUploadResult(FAILURE, attempted=0, message="Flywheel login failed.")

    try:
        results = tuple(_upload_one_file(uploader, path, destination, project) for path in paths)
    finally:
        uploader.logout()

    return _summarize_upload_results(results)


def _build_uploader(uploader_factory: Callable | None):
    """Create the SDK uploader with strict backend destination checks."""

    if uploader_factory is not None:
        return uploader_factory()

    from backend.imports_api.flywheel_sdk_uploader import FlywheelSdkUploader

    return FlywheelSdkUploader(require_enrollment_destination=True)


def _upload_one_file(uploader, path: Path, destination_name: str, project: str) -> FileUploadResult:
    """Upload one approved file and sanitize the file-level outcome."""

    status, message = uploader.upload_file_to_project(path, project)
    return FileUploadResult(
        file_name=path.name,
        destination=destination_name,
        status=status,
        message=message,
    )


def _summarize_upload_results(results: tuple[FileUploadResult, ...]) -> FlywheelUploadResult:
    """Collapse per-file Flywheel results into a predictable batch status."""

    succeeded = sum(result.status == SUCCESS for result in results)
    failed = sum(result.status == FAILURE for result in results)
    uncertain = sum(result.status == UNCERTAIN for result in results)
    status = _batch_status(succeeded, failed, uncertain, len(results))
    return FlywheelUploadResult(status, len(results), succeeded, failed, uncertain, results)


def _batch_status(succeeded: int, failed: int, uncertain: int, attempted: int) -> str:
    """Return the least misleading batch status for mixed upload outcomes."""

    if uncertain and succeeded + failed == 0:
        return UNCERTAIN
    if uncertain or (succeeded and failed):
        return PARTIAL
    if succeeded == attempted:
        return SUCCESS
    return FAILURE

"""Execute Flask workflow jobs outside request threads.

The worker is intentionally small: one daemon thread consumes a bounded queue,
prepares jobs, releases jobs awaiting review, and later commits only after a
server-created approval has been recorded. It never retries uncertain writes.
"""

from __future__ import annotations

from backend.imports_api.external_write_results import FlywheelUploadResult
from backend.services.config import resolve_flywheel_auth, resolve_flywheel_destination

import queue
import threading
import logging
from pathlib import Path

import pandas as pd

from backend.imports_api.external_write_results import UNCERTAIN
from backend.services.persist_job_state import (
    JobStateError,
    load_all_job_records,
    load_job_record,
    mark_interrupted_jobs_for_recovery,
    persist_job_record,
    resolve_artifact_path,
    transition_job_record,
    update_job_commit,
    update_job_preparation,
)
from backend.services.transition_job_state import (
    AWAITING_REVIEW,
    CANCELLED,
    COMPLETED,
    FAILED,
    PREPARING,
    QUEUED,
    RECONCILIATION_REQUIRED,
    COMMITTING,
)
from backend.services.validate_job_approval import ApprovalAuthorization
from backend.workflows.enrollment_submission import commit_enrollment_submission, prepare_enrollment_submission
from backend.workflows.naccid_import import commit_naccid_import, prepare_naccid_import
from backend.workflows.prepare_workflow_results import PROCESSING_FAILED, VALIDATION_FAILED
from backend.workflows.udsv4_submission import commit_udsv4_submission, prepare_udsv4_submission

PREPARE_ACTION = "prepare"
COMMIT_ACTION = "commit"
log = logging.getLogger(__name__)


class BackgroundJobExecutor:
    """Single-worker background queue for workflow preparation and commits."""

    def __init__(self, job_root: Path, *, max_queue_size: int = 20):
        self.job_root = Path(job_root)
        self.queue: queue.Queue[tuple[str, str]] = queue.Queue(maxsize=max_queue_size)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start the worker once and recover persisted jobs."""

        if self._thread and self._thread.is_alive():
            return
        recovered = mark_interrupted_jobs_for_recovery(self.job_root)
        if recovered:
            log.warning("Marked %s interrupted commit job(s) for reconciliation", len(recovered))
        self._thread = threading.Thread(target=self._work_forever, name="nacc-job-worker", daemon=True)
        self._thread.start()
        log.info("Started background job worker")
        self.enqueue_persisted_jobs()

    def stop(self) -> None:
        """Ask the worker to stop after its current item."""

        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def enqueue_prepare(self, job_id: str) -> None:
        """Queue a preparation job."""

        self.queue.put((PREPARE_ACTION, job_id), block=False)

    def enqueue_commit(self, job_id: str) -> None:
        """Queue an approved commit job."""

        self.queue.put((COMMIT_ACTION, job_id), block=False)

    def enqueue_persisted_jobs(self) -> None:
        """Requeue jobs that were waiting before restart.

        An awaiting-review job with recorded approval has not entered the
        external write boundary until it transitions to ``committing``. It can
        therefore be safely queued after restart without retrying an uncertain
        write.
        """

        for job in load_all_job_records(self.job_root):
            if job.get("status") == QUEUED:
                self.enqueue_prepare(job["job_id"])
            elif job.get("status") == AWAITING_REVIEW and job.get("approval"):
                self.enqueue_commit(job["job_id"])

    def _work_forever(self) -> None:
        """Process queued jobs until shutdown."""

        while not self._stop.is_set():
            try:
                action, job_id = self.queue.get(timeout=0.25)
            except queue.Empty:
                continue
            try:
                self._execute_job(action, job_id)
            finally:
                self.queue.task_done()

    def _execute_job(self, action: str, job_id: str) -> None:
        """Dispatch one queued item to preparation or commit."""

        try:
            log.info("Executing %s job %s", action, job_id)
            job = load_job_record(self.job_root, job_id)
            if job.get("status") == CANCELLED:
                return
            if action == PREPARE_ACTION:
                execute_preparation_job(job)
            elif action == COMMIT_ACTION:
                execute_commit_job(job)
        except Exception:
            log.exception("Background job failed: %s", job_id)
            try:
                job = load_job_record(self.job_root, job_id)
                job["status"] = FAILED
                job["error"] = "Background job failed."
                persist_job_record(job)
            except Exception:
                return


def execute_preparation_job(job: dict) -> None:
    """Run one read-only workflow preparation and persist its result."""

    transition_job_record(job, PREPARING)
    result = _prepare_workflow(job)
    update_job_preparation(job, result)
    if result.status in {VALIDATION_FAILED, PROCESSING_FAILED}:
        transition_job_record(job, FAILED, error=result.error)
    elif result.approval_ready:
        destination = {"udsv4_submission": "form_submissions", "enrollment_submission": "enrollment_submissions"}.get(job["workflow"])
        if destination:
            group = resolve_flywheel_auth()["group"]
            project = resolve_flywheel_destination(destination, required=True)
            job["upload_destination"] = f"{group}/{project}"
        transition_job_record(job, AWAITING_REVIEW)
    else:
        transition_job_record(job, COMPLETED)


def execute_commit_job(job: dict) -> None:
    """Commit an approved job through workflow commit functions."""

    transition_job_record(job, COMMITTING)
    result = _commit_workflow(job)
    update_job_commit(job, result)
    if getattr(result, "requires_reconciliation", False) or result.status == UNCERTAIN:
        transition_job_record(job, RECONCILIATION_REQUIRED)
    elif result.status == "failure":
        transition_job_record(job, FAILED, error=getattr(result, "message", "Commit failed."))
    else:
        transition_job_record(job, COMPLETED)


def _prepare_workflow(job: dict):
    """Call the correct backend preparation workflow for one job."""

    workflow = job["workflow"]
    workspace = Path(job["workspace"])
    if workflow == "udsv4_submission":
        upload = Path(job["workspace"]) / job["input_artifacts"]["filter_csv"]["path"]
        return prepare_udsv4_submission(selected_forms=job["parameters"].get("selected_forms"), filter_csv=upload, workspace=workspace)
    if workflow == "enrollment_submission":
        return prepare_enrollment_submission(workspace=workspace)
    if workflow == "naccid_import":
        return prepare_naccid_import(workspace=workspace)
    raise JobStateError("Unsupported workflow.")


def _commit_workflow(job: dict):
    """Call the correct backend commit workflow for one approved job."""

    approval = _approval_from_job(job)
    originals = [resolve_artifact_path(job, item["id"], review=False) for item in job["artifacts"]]
    if job["workflow"] in {"udsv4_submission", "enrollment_submission"}:
        destination = job.get("upload_destination")
        if not destination:
            return FlywheelUploadResult("failure", attempted=0, message="Upload destination was not recorded. Prepare a new job.")
        commit = commit_udsv4_submission if job["workflow"] == "udsv4_submission" else commit_enrollment_submission
        return commit(originals, approval, workspace=Path(job["workspace"]), expected_destination=destination)
    if job["workflow"] == "naccid_import":
        return commit_naccid_import(_load_import_payload(originals), approval)
    raise JobStateError("Unsupported workflow.")


def _approval_from_job(job: dict) -> ApprovalAuthorization:
    """Recreate the backend approval object from persisted server metadata."""

    approval = job.get("approval") or {}
    return ApprovalAuthorization(
        operation=approval.get("operation", ""),
        approved_by=approval.get("approved_by", ""),
        approved_at=approval.get("approved_at", ""),
        artifact_ids=tuple(approval.get("artifact_ids", [])),
    )


def _load_import_payload(artifacts: list[Path]) -> pd.DataFrame:
    """Load the exact prepared NACCID import artifact for commit."""

    if len(artifacts) != 1:
        raise JobStateError("NACCID import requires exactly one prepared payload.")
    return pd.read_csv(artifacts[0], dtype=str)

"""Persist Flask workflow job metadata and private artifact references.

The store uses one private directory per job and atomic JSON metadata writes.
CSV artifacts and REDCap import payloads stay on disk; metadata only contains
sanitized counts, filenames, statuses, and relative artifact identifiers.
"""

from __future__ import annotations

import json
import os
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from werkzeug.datastructures import FileStorage
from werkzeug.utils import secure_filename

from backend.services.transition_job_state import (
    COMMITTING,
    FAILED,
    PREPARING,
    QUEUED,
    RECONCILIATION_REQUIRED,
    validate_job_transition,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_JOB_ROOT = PROJECT_ROOT / ".runtime" / "flask_jobs"
METADATA_FILE = "job.json"
UPLOAD_DIR = "uploads"


class JobStateError(RuntimeError):
    """Raised when persisted job metadata or artifacts are invalid."""


def create_job_store(root: Path | str | None = None) -> Path:
    """Create the private job root used by the Flask application."""

    job_root = Path(root) if root else DEFAULT_JOB_ROOT
    job_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    job_root.chmod(0o700)
    return job_root


def create_job_record(
    job_root: Path,
    workflow: str,
    parameters: dict[str, Any] | None = None,
    *,
    owner: str = "",
) -> dict:
    """Create and persist a new queued job metadata record."""

    job_id = uuid.uuid4().hex
    workspace = job_root / job_id
    workspace.mkdir(mode=0o700)
    now = _timestamp()
    job = {
        "job_id": job_id,
        "workflow": workflow,
        "owner": owner,
        "status": QUEUED,
        "created_at": now,
        "updated_at": now,
        "workspace": str(workspace),
        "parameters": parameters or {},
        "artifacts": [],
        "review_artifacts": [],
        "input_artifacts": {},
        "preparation": {},
        "approval": {},
        "commit": {},
        "error": "",
    }
    persist_job_record(job)
    return job


def persist_job_record(job: dict) -> None:
    """Atomically write one job metadata file."""

    job["updated_at"] = _timestamp()
    path = _metadata_path(job)
    temp_path = path.with_suffix(".tmp")
    temp_path.write_text(json.dumps(_metadata_copy(job), indent=2, sort_keys=True), encoding="utf-8")
    os.replace(temp_path, path)


def load_job_record(job_root: Path, job_id: str) -> dict:
    """Load one job by ID after validating the identifier."""

    if not _valid_job_id(job_id):
        raise JobStateError("Invalid job identifier.")
    path = job_root / job_id / METADATA_FILE
    if not path.exists():
        raise JobStateError("Job not found.")
    return json.loads(path.read_text(encoding="utf-8"))


def load_all_job_records(job_root: Path) -> list[dict]:
    """Return all readable persisted jobs sorted by creation time."""

    jobs = []
    for path in sorted(job_root.glob(f"*/{METADATA_FILE}")):
        try:
            jobs.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return sorted(jobs, key=lambda item: item.get("created_at", ""))


def transition_job_record(job: dict, next_status: str, **updates) -> dict:
    """Apply a validated state transition and persist the updated job."""

    validate_job_transition(job["status"], next_status)
    job["status"] = next_status
    job.update(updates)
    persist_job_record(job)
    return job


def save_uploaded_file(job: dict, field_name: str, upload: FileStorage) -> Path:
    """Store an uploaded file inside the job workspace using a safe filename."""

    filename = secure_filename(upload.filename or "")
    if not filename.lower().endswith(".csv"):
        raise JobStateError("Uploaded file must be a CSV.")
    upload_dir = Path(job["workspace"]) / UPLOAD_DIR
    upload_dir.mkdir(mode=0o700, exist_ok=True)
    path = upload_dir / filename
    upload.save(path)
    path.chmod(0o600)
    job["input_artifacts"][field_name] = _artifact_ref(job, path)
    persist_job_record(job)
    return path


def update_job_preparation(job: dict, result) -> dict:
    """Store sanitized preparation metadata and artifact references."""

    job["preparation"] = _preparation_metadata(result)
    job["artifacts"] = [_artifact_ref(job, path) for path in result.artifacts]
    job["review_artifacts"] = [_artifact_ref(job, path) for path in result.review_artifacts]
    job["error"] = result.error
    persist_job_record(job)
    return job


def update_job_commit(job: dict, result) -> dict:
    """Store sanitized commit metadata without raw API responses."""

    job["commit"] = _commit_metadata(result)
    job["error"] = getattr(result, "message", "") or ""
    persist_job_record(job)
    return job


def mark_interrupted_jobs_for_recovery(job_root: Path) -> list[dict]:
    """Mark interrupted external writes for manual reconciliation on restart."""

    recovered = []
    for job in load_all_job_records(job_root):
        if job.get("status") == COMMITTING:
            job["status"] = RECONCILIATION_REQUIRED
            job["error"] = "Application restarted during commit; reconcile external outcome manually."
            persist_job_record(job)
            recovered.append(job)
        elif job.get("status") == PREPARING:
            job["status"] = FAILED
            job["error"] = "Application restarted during preparation; submit a new job."
            persist_job_record(job)
    return recovered


def resolve_artifact_path(job: dict, artifact_id: str, *, review: bool = True) -> Path:
    """Resolve a known artifact ID without allowing path traversal."""

    artifacts = job["review_artifacts"] if review else job["artifacts"]
    matches = [item for item in artifacts if item["id"] == artifact_id]
    if not matches:
        raise JobStateError("Artifact not found.")
    path = (Path(job["workspace"]) / matches[0]["path"]).resolve()
    workspace = Path(job["workspace"]).resolve()
    if workspace not in path.parents or not path.is_file():
        raise JobStateError("Artifact is unavailable.")
    return path


def remove_job_workspace(job: dict) -> None:
    """Delete a job workspace after cancellation when no write has started."""

    shutil.rmtree(Path(job["workspace"]), ignore_errors=True)


def _preparation_metadata(result) -> dict:
    """Return browser-safe preparation fields."""

    return {
        "status": result.status,
        "prepared_records": result.prepared_records,
        "forms_prepared": list(result.forms_prepared),
        "forms_without_output": list(result.forms_without_output),
        "warnings": list(result.warnings),
        "module_summaries": _module_summary_metadata(result),
        "approval_ready": result.approval_ready,
    }


def _module_summary_metadata(result) -> list[dict]:
    """Return selected-module summaries with safe review artifact references."""

    review_by_name = {
        path.name: {"id": path.name, "name": path.name}
        for path in result.review_artifacts
    }
    summaries = []
    for summary in getattr(result, "module_summaries", ()):
        item = summary.as_metadata()
        item["artifact"] = review_by_name.get(item.get("artifact_name", ""))
        summaries.append(item)
    return summaries


def _commit_metadata(result) -> dict:
    """Return browser-safe commit fields."""

    payload = {
        "status": result.status,
        "message": getattr(result, "message", ""),
        "requires_reconciliation": getattr(result, "requires_reconciliation", False),
    }
    for field in ("attempted", "succeeded", "failed", "uncertain", "submitted", "verified", "skipped"):
        if hasattr(result, field):
            payload[field] = getattr(result, field)
    if hasattr(result, "results"):
        payload["results"] = [item.__dict__ for item in result.results]
    return payload


def _artifact_ref(job: dict, path: Path) -> dict:
    """Create a stable artifact reference relative to the job workspace."""

    workspace = Path(job["workspace"]).resolve()
    resolved = Path(path).resolve()
    if workspace not in resolved.parents:
        raise JobStateError("Artifact is outside the job workspace.")
    return {"id": resolved.name, "name": resolved.name, "path": str(resolved.relative_to(workspace))}


def _metadata_copy(job: dict) -> dict:
    """Remove non-JSON values before writing metadata."""

    return json.loads(json.dumps(job, default=str))


def _metadata_path(job: dict) -> Path:
    """Return the metadata path for a job record."""

    return Path(job["workspace"]) / METADATA_FILE


def _valid_job_id(job_id: str) -> bool:
    """Validate untrusted job identifiers from URLs."""

    return isinstance(job_id, str) and len(job_id) == 32 and all(ch in "0123456789abcdef" for ch in job_id)


def _timestamp() -> str:
    """Return an ISO timestamp for persisted metadata."""

    return datetime.now(timezone.utc).isoformat()

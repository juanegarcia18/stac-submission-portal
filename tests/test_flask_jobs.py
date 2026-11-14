"""Offline Flask and background-job tests for Milestone 7 hardening."""

import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.flask_application import create_flask_application
from backend.imports_api.external_write_results import RedcapImportResult, SUCCESS, UNCERTAIN
from backend.services.execute_background_jobs import execute_commit_job
from backend.services.persist_job_state import (
    create_job_record,
    load_job_record,
    mark_interrupted_jobs_for_recovery,
    persist_job_record,
)
from backend.services.transition_job_state import (
    AWAITING_REVIEW,
    CANCELLED,
    COMMITTING,
    COMPLETED,
    FAILED,
    QUEUED,
    RECONCILIATION_REQUIRED,
)
from backend.workflows.prepare_workflow_results import (
    ModulePreparationSummary,
    SUCCESS as PREP_SUCCESS,
    WorkflowPreparationResult,
)


class FlaskJobTests(unittest.TestCase):
    """Exercise the browser app and persisted worker state without external APIs."""

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.app = create_flask_application({
            "TESTING": True,
            "NACC_JOB_ROOT": str(self.root),
            "START_BACKGROUND_WORKER": True,
            "ENABLE_DEV_AUTH": True,
            "SECRET_KEY": "test-secret",
        })
        self.client = self.app.test_client()

    def tearDown(self):
        """Stop the background worker created for each test."""

        self.app.extensions["nacc_job_executor"].stop()

    def csrf(self):
        """Create and return a test session CSRF token."""

        with self.client.session_transaction() as sess:
            sess["csrf_token"] = "csrf"
        return "csrf"

    def test_application_initialization_and_template_rendering(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"UDSv4 Submission", response.data)
        self.assertIn("nacc_job_executor", self.app.extensions)
        self.assertEqual(self.app.config["MAX_CONTENT_LENGTH"], 5 * 1024 * 1024)

    def test_unauthorized_access_and_csrf_rejection(self):
        app = create_flask_application({
            "TESTING": True,
            "NACC_JOB_ROOT": str(self.root / "locked"),
            "START_BACKGROUND_WORKER": False,
            "ENABLE_DEV_AUTH": False,
            "SECRET_KEY": "test-secret",
        })
        self.assertEqual(app.test_client().get("/").status_code, 401)
        blocked = app.test_client().get("/", headers={"X-Remote-User": "intruder"})
        self.assertEqual(blocked.status_code, 401)
        self.assertEqual(self.client.post("/jobs/enrollment").status_code, 400)

    def test_trusted_header_requires_explicit_deployment_config(self):
        app = create_flask_application({
            "TESTING": True,
            "NACC_JOB_ROOT": str(self.root / "trusted"),
            "START_BACKGROUND_WORKER": False,
            "ENABLE_DEV_AUTH": False,
            "TRUSTED_AUTH_ENABLED": True,
            "SECRET_KEY": "test-secret",
        })
        allowed = app.test_client().get("/", headers={"X-Remote-User": "alice"})
        self.assertEqual(allowed.status_code, 200)

    def test_invalid_form_selection_and_missing_upload_are_rejected(self):
        token = self.csrf()
        missing = self.client.post("/jobs/udsv4", data={"csrf_token": token, "forms": ["UDS"]})
        self.assertEqual(missing.status_code, 400)
        invalid = self.client.post(
            "/jobs/udsv4",
            data={
                "csrf_token": token,
                "forms": ["BAD"],
                "filter_csv": (io.BytesIO(b"ptid\n001\n"), "filter.csv"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(invalid.status_code, 400)
        wrong_type = self.client.post(
            "/jobs/udsv4",
            data={
                "csrf_token": token,
                "forms": ["UDS"],
                "filter_csv": (io.BytesIO(b"ptid\n001\n"), "filter.txt"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(wrong_type.status_code, 400)
        self.assertIn(b"Uploaded file must be a CSV.", wrong_type.data)

    def test_job_creation_persistence_and_queue_processing(self):
        token = self.csrf()
        with patch("backend.services.execute_background_jobs.prepare_enrollment_submission") as prepare:
            prepare.side_effect = lambda workspace: _prepared_file_result(Path(workspace))
            response = self.client.post("/jobs/enrollment", data={"csrf_token": token})
            job_id = response.location.rsplit("/", 1)[-1]
            self.app.extensions["nacc_job_executor"].queue.join()

        job = load_job_record(self.app.extensions["nacc_job_root"], job_id)
        self.assertEqual(job["status"], AWAITING_REVIEW)
        self.assertEqual(job["owner"], "development-user")
        self.assertTrue(job["review_artifacts"])
        status = self.client.get(f"/api/jobs/{job_id}").json
        self.assertEqual(status["status"], AWAITING_REVIEW)
        self.assertTrue(status["can_approve"])
        self.assertEqual(status["preparation"]["module_summaries"][0]["prepared_records"], 1)
        self.assertEqual(status["preparation"]["module_summaries"][0]["artifact"]["id"], "prepared.csv")

    def test_job_owner_is_required_for_status_and_files(self):
        app = create_flask_application({
            "TESTING": True,
            "NACC_JOB_ROOT": str(self.root / "owned"),
            "START_BACKGROUND_WORKER": False,
            "ENABLE_DEV_AUTH": False,
            "TRUSTED_AUTH_ENABLED": True,
            "SECRET_KEY": "test-secret",
        })
        client = app.test_client()
        job = create_job_record(app.extensions["nacc_job_root"], "enrollment_submission", owner="alice")
        artifact = Path(job["workspace"]) / "review.csv"
        artifact.write_text("ptid\n001\n")
        job["review_artifacts"] = [{"id": artifact.name, "name": artifact.name, "path": artifact.name}]
        persist_job_record(job)

        bob_status = client.get(f"/jobs/{job['job_id']}", headers={"X-Remote-User": "bob"})
        alice_status = client.get(f"/jobs/{job['job_id']}", headers={"X-Remote-User": "alice"})
        self.assertEqual(bob_status.status_code, 403)
        self.assertEqual(alice_status.status_code, 200)
        blocked_file = client.get(f"/jobs/{job['job_id']}/files/{artifact.name}", headers={"X-Remote-User": "bob"})
        self.assertEqual(blocked_file.status_code, 403)

    def test_status_page_polling_renders_review_sections_without_refresh(self):
        job = create_job_record(self.root, "enrollment_submission")
        artifact = Path(job["workspace"]) / "prepared.csv"
        artifact.write_text("ptid\n001\n")
        job.update({
            "status": AWAITING_REVIEW,
            "artifacts": [{"id": artifact.name, "name": artifact.name, "path": artifact.name}],
            "review_artifacts": [{"id": artifact.name, "name": artifact.name, "path": artifact.name}],
            "preparation": {
                "status": "success",
                "prepared_records": 1,
                "module_summaries": [{
                    "module": "Enrollment",
                    "requested_records": 1,
                    "prepared_records": 1,
                    "status": "success",
                    "artifact": {"id": artifact.name, "name": artifact.name},
                    "findings": [],
                }],
                "warnings": [],
                "approval_ready": True,
            },
        })
        persist_job_record(job)

        page = self.client.get(f"/jobs/{job['job_id']}")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"setInterval", page.data)
        self.assertIn(b"3000", page.data)
        self.assertIn(b"renderReviewFiles", page.data)
        self.assertIn(b"Approve And Commit", page.data)

    def test_multiple_queued_jobs_continue_without_browser_connection(self):
        token = self.csrf()
        with patch(
            "backend.services.execute_background_jobs.prepare_enrollment_submission",
            side_effect=lambda workspace: _prepared_file_result(Path(workspace)),
        ):
            first = self.client.post("/jobs/enrollment", data={"csrf_token": token}).location.rsplit("/", 1)[-1]
            second = self.client.post("/jobs/enrollment", data={"csrf_token": token}).location.rsplit("/", 1)[-1]
            self.app.extensions["nacc_job_executor"].queue.join()
        self.assertEqual(load_job_record(self.root, first)["status"], AWAITING_REVIEW)
        self.assertEqual(load_job_record(self.root, second)["status"], AWAITING_REVIEW)

    def test_approval_duplicate_prevention_and_commit_result(self):
        job = create_job_record(self.root, "naccid_import")
        artifact = Path(job["workspace"]) / "payload.csv"
        artifact.write_text("ptid\n001\n")
        job.update({
            "status": AWAITING_REVIEW,
            "artifacts": [{"id": artifact.name, "name": artifact.name, "path": artifact.name}],
            "review_artifacts": [{"id": artifact.name, "name": artifact.name, "path": artifact.name}],
        })
        persist_job_record(job)
        token = self.csrf()

        with patch(
            "backend.services.execute_background_jobs.commit_naccid_import",
            return_value=RedcapImportResult(SUCCESS, attempted=1, submitted=1, verified=1),
        ):
            approved = self.client.post(f"/jobs/{job['job_id']}/approve", data={"csrf_token": token})
            duplicate = self.client.post(f"/jobs/{job['job_id']}/approve", data={"csrf_token": token})
            self.app.extensions["nacc_job_executor"].queue.join()

        self.assertEqual(approved.status_code, 302)
        self.assertEqual(duplicate.status_code, 409)
        self.assertEqual(load_job_record(self.root, job["job_id"])["status"], COMPLETED)
        status = self.client.get(f"/api/jobs/{job['job_id']}").get_json()
        self.assertTrue(status["terminal"])
        self.assertEqual(
            {key: status["commit"][key] for key in ("status", "submitted", "verified", "skipped", "failed")},
            {"status": SUCCESS, "submitted": 1, "verified": 1, "skipped": 0, "failed": 0},
        )

    def test_approval_queue_full_does_not_leave_stuck_approval(self):
        app = create_flask_application({
            "TESTING": True,
            "NACC_JOB_ROOT": str(self.root / "full"),
            "START_BACKGROUND_WORKER": False,
            "ENABLE_DEV_AUTH": True,
            "NACC_QUEUE_SIZE": 1,
            "SECRET_KEY": "test-secret",
        })
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["csrf_token"] = "csrf"
        executor = app.extensions["nacc_job_executor"]
        executor.queue.put(("prepare", "0" * 32), block=False)

        job = create_job_record(app.extensions["nacc_job_root"], "naccid_import", owner="development-user")
        artifact = Path(job["workspace"]) / "payload.csv"
        artifact.write_text("ptid\n001\n")
        job.update({
            "status": AWAITING_REVIEW,
            "artifacts": [{"id": artifact.name, "name": artifact.name, "path": artifact.name}],
            "review_artifacts": [{"id": artifact.name, "name": artifact.name, "path": artifact.name}],
        })
        persist_job_record(job)

        response = client.post(f"/jobs/{job['job_id']}/approve", data={"csrf_token": "csrf"})
        saved = load_job_record(app.extensions["nacc_job_root"], job["job_id"])
        self.assertEqual(response.status_code, 503)
        self.assertEqual(saved["status"], AWAITING_REVIEW)
        self.assertEqual(saved["approval"], {})
        self.assertIn("queue is full", saved["error"])

    def test_cancel_and_private_file_access(self):
        job = create_job_record(self.root, "enrollment_submission")
        token = self.csrf()
        cancelled = self.client.post(f"/jobs/{job['job_id']}/cancel", data={"csrf_token": token})
        self.assertEqual(cancelled.status_code, 302)
        self.assertEqual(load_job_record(self.root, job["job_id"])["status"], CANCELLED)
        self.assertEqual(self.client.get(f"/jobs/{job['job_id']}/files/../../secret").status_code, 404)

    def test_restart_recovery_and_uncertain_commit(self):
        job = create_job_record(self.root, "naccid_import")
        job["status"] = COMMITTING
        persist_job_record(job)
        recovered = mark_interrupted_jobs_for_recovery(self.root)
        self.assertEqual(recovered[0]["status"], RECONCILIATION_REQUIRED)

        uncertain = create_job_record(self.root, "naccid_import")
        payload = Path(uncertain["workspace"]) / "payload.csv"
        payload.write_text("ptid\n001\n")
        uncertain.update({
            "status": AWAITING_REVIEW,
            "approval": {"operation": "naccid_import", "approved_by": "tester", "approved_at": "now", "artifact_ids": [payload.name]},
            "artifacts": [{"id": payload.name, "name": payload.name, "path": payload.name}],
        })
        persist_job_record(uncertain)
        with patch(
            "backend.services.execute_background_jobs.commit_naccid_import",
            return_value=RedcapImportResult(UNCERTAIN, attempted=1, requires_reconciliation=True),
        ):
            execute_commit_job(uncertain)
        self.assertEqual(load_job_record(self.root, uncertain["job_id"])["status"], RECONCILIATION_REQUIRED)

    def test_restart_requeues_approved_jobs_before_commit_boundary(self):
        job = create_job_record(self.root, "naccid_import")
        artifact = Path(job["workspace"]) / "payload.csv"
        artifact.write_text("ptid\n001\n")
        job.update({
            "status": AWAITING_REVIEW,
            "approval": {"operation": "naccid_import", "approved_by": "tester", "approved_at": "now", "artifact_ids": [artifact.name]},
            "artifacts": [{"id": artifact.name, "name": artifact.name, "path": artifact.name}],
        })
        persist_job_record(job)
        executor = self.app.extensions["nacc_job_executor"]
        executor.enqueue_persisted_jobs()
        action, job_id = executor.queue.get_nowait()
        self.assertEqual((action, job_id), ("commit", job["job_id"]))
        executor.queue.task_done()


def _prepared_file_result(workspace: Path) -> WorkflowPreparationResult:
    """Create a fictional prepared artifact result inside a workspace."""

    workspace.mkdir(parents=True, exist_ok=True)
    original = workspace / "prepared.csv"
    review_dir = workspace / "review"
    review_dir.mkdir(exist_ok=True)
    review = review_dir / "prepared.csv"
    original.write_text("ptid\n001\n")
    review.write_text("ptid\n001\n")
    return WorkflowPreparationResult(
        "enrollment_submission",
        PREP_SUCCESS,
        prepared_records=1,
        workspace=workspace,
        artifacts=(original,),
        review_artifacts=(review,),
        module_summaries=(
            ModulePreparationSummary(
                "Enrollment",
                requested_records=1,
                prepared_records=1,
                status=PREP_SUCCESS,
                artifact_name=original.name,
            ),
        ),
    )


if __name__ == "__main__":
    unittest.main()

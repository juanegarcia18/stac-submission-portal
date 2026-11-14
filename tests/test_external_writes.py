"""Offline tests for Milestone 4 external write backend contracts."""

import os
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import Mock, patch

import pandas as pd
import requests

from backend.imports_api.import_redcap_records import import_approved_redcap_records
from backend.imports_api.external_write_results import FAILURE, NO_DATA, PARTIAL, SUCCESS, UNCERTAIN
from backend.imports_api.upload_flywheel_files import (
    ENROLLMENT_SUBMISSION_DESTINATION,
    FORM_SUBMISSION_DESTINATION,
    upload_flywheel_files,
)
from backend.services.validate_job_approval import ApprovalError, ApprovalAuthorization
from backend.services.redcap_recovery import recover_redcap_locks
from backend.services.job_workspace_files import (
    cleanup_job_workspace,
    prepare_private_workspace,
    prepare_review_files,
    validate_workspace_path,
)
from backend.services import redcap_import_safety


ENV = {
    "BASE_URL": "https://redcap.invalid/api/",
    "UDSV4_TOKEN": "offline-token",
    "FW_GROUP": "fictional-group",
    "FW_PROJECT": "forms",
    "FW_ENROLL": "enrollment",
    "FLYWHEEL_TOKEN": "offline-token",
}


def approval(operation: str) -> ApprovalAuthorization:
    """Return a fictional approval authorization for offline tests."""

    return ApprovalAuthorization(operation, "reviewer", "2026-11-13T12:00:00Z")


class FakeUploader:
    """Small Flywheel uploader double with per-file statuses."""

    group = "test-group"

    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.uploads = []
        self.logged_out = False

    def login(self):
        return True

    def logout(self):
        self.logged_out = True

    def upload_file_to_project(self, path, project):
        self.uploads.append((Path(path).name, project))
        return self.statuses.pop(0)


class FlywheelBackendTests(unittest.TestCase):
    """Verify approval, explicit destinations, and structured upload outcomes."""

    def setUp(self):
        self.enterContext(patch.dict(os.environ, ENV, clear=True))

    def test_upload_requires_approval_and_explicit_enrollment_destination(self):
        fake = FakeUploader([("success", "")])
        with self.assertRaises(ApprovalError):
            upload_flywheel_files([Path("enroll.csv")], ENROLLMENT_SUBMISSION_DESTINATION, True)

        with patch.dict(os.environ, {"FW_ENROLL": ""}):
            with self.assertRaisesRegex(Exception, "FW_ENROLL"):
                upload_flywheel_files(
                    [Path("enroll.csv")],
                    ENROLLMENT_SUBMISSION_DESTINATION,
                    approval("flywheel_upload"),
                    uploader_factory=lambda: fake,
                )

    def test_upload_reports_success_failure_and_uncertain_per_file(self):
        fake = FakeUploader([
            ("success", ""),
            ("failure", "Flywheel upload command failed."),
            ("uncertain", "Flywheel upload timed out; remote outcome is unknown."),
        ])
        result = upload_flywheel_files(
            [Path("a.csv"), Path("b.csv"), Path("c.csv")],
            FORM_SUBMISSION_DESTINATION,
            approval("flywheel_upload"),
            uploader_factory=lambda: fake,
        )

        self.assertEqual(result.status, PARTIAL)
        self.assertEqual((result.succeeded, result.failed, result.uncertain), (1, 1, 1))
        self.assertTrue(result.requires_reconciliation)
        self.assertEqual(
            [(item.file_name, item.destination, item.status) for item in result.results],
            [("a.csv", "test-group/forms", "success"),
             ("b.csv", "test-group/forms", "failure"),
             ("c.csv", "test-group/forms", "uncertain")],
        )
        self.assertEqual(fake.uploads, [("a.csv", "forms"), ("b.csv", "forms"), ("c.csv", "forms")])
        self.assertTrue(fake.logged_out)

    def test_upload_no_data_and_login_failure(self):
        self.assertEqual(
            upload_flywheel_files([], FORM_SUBMISSION_DESTINATION, approval("flywheel_upload")).status,
            NO_DATA,
        )
        fake = Mock(login=Mock(return_value=False), logout=Mock())
        result = upload_flywheel_files(
            [Path("a.csv")],
            FORM_SUBMISSION_DESTINATION,
            approval("flywheel_upload"),
            uploader_factory=lambda: fake,
        )
        self.assertEqual(result.status, FAILURE)
        fake.logout.assert_not_called()

    def test_reviewed_destination_mismatch_blocks_write(self):
        fake = FakeUploader([("success", "")])
        result = upload_flywheel_files(
            [Path("a.csv")], FORM_SUBMISSION_DESTINATION, approval("flywheel_upload"),
            uploader_factory=lambda: fake, expected_destination="test-group/sandbox-form",
        )
        self.assertEqual(result.status, FAILURE)
        self.assertEqual(fake.uploads, [])


class RedcapBackendTests(unittest.TestCase):
    """Verify REDCap approval, importer delegation, and uncertain outcomes."""

    def test_import_requires_approval_and_handles_empty_records(self):
        df = pd.DataFrame({"ptid": []})
        with self.assertRaises(ApprovalError):
            import_approved_redcap_records(df, True)
        result = import_approved_redcap_records(df, approval("naccid_import"), importer=Mock())
        self.assertEqual(result.status, NO_DATA)

    def test_import_delegates_to_existing_safeguarded_importer(self):
        df = pd.DataFrame({"ptid": ["001", "002"]})
        importer = Mock(return_value={"submitted": 1, "verified": 1, "failed": 0, "skipped": 1})
        result = import_approved_redcap_records(df, approval("naccid_import"), importer=importer)

        importer.assert_called_once_with(df)
        self.assertEqual(result.status, SUCCESS)
        self.assertEqual((result.attempted, result.submitted, result.verified, result.skipped), (2, 1, 1, 1))

    def test_import_partial_failed_and_uncertain_outcomes_are_distinct(self):
        df = pd.DataFrame({"ptid": ["001"]})
        partial = import_approved_redcap_records(
            df,
            approval("naccid_import"),
            importer=Mock(return_value={"submitted": 1, "verified": 0, "failed": 1, "skipped": 0}),
        )
        uncertain = import_approved_redcap_records(
            df,
            approval("naccid_import"),
            importer=Mock(side_effect=requests.Timeout("token offline-token PTID 001")),
        )
        failure = import_approved_redcap_records(
            df,
            approval("naccid_import"),
            importer=Mock(side_effect=ValueError("Invalid token offline-token PTID 001")),
        )

        self.assertEqual(partial.status, PARTIAL)
        self.assertEqual(uncertain.status, UNCERTAIN)
        self.assertTrue(uncertain.requires_reconciliation)
        self.assertEqual(failure.status, FAILURE)
        self.assertNotIn("offline-token", failure.message)
        self.assertNotIn("001", failure.message)


class WorkspaceAndRecoveryTests(unittest.TestCase):
    """Verify private file handling and REDCap recovery service behavior."""

    def test_review_copy_is_separate_and_original_integrity_is_preserved(self):
        workspace = prepare_private_workspace()
        try:
            self.assertEqual(workspace.stat().st_mode & 0o077, 0)
            original = workspace / "submission.csv"
            original.write_text("ptid\n001\n", encoding="utf-8")

            review, = prepare_review_files([original], workspace)
            review.write_text("edited\n", encoding="utf-8")

            self.assertEqual(original.read_text(encoding="utf-8"), "ptid\n001\n")
            self.assertEqual(review.parent.name, "review")
        finally:
            cleanup_job_workspace(workspace)

    def test_workspace_rejects_outside_paths_and_preserves_requested_artifacts(self):
        with tempfile.TemporaryDirectory() as folder:
            workspace = Path(folder)
            keep = workspace / "keep.csv"
            drop = workspace / "drop.csv"
            outside = Path(tempfile.NamedTemporaryFile(delete=False).name)
            keep.write_text("keep", encoding="utf-8")
            drop.write_text("drop", encoding="utf-8")
            try:
                with self.assertRaisesRegex(ValueError, "outside"):
                    validate_workspace_path(outside, workspace)
                cleanup_job_workspace(workspace, preserve_paths=[keep])
                self.assertTrue(keep.exists())
                self.assertFalse(drop.exists())
            finally:
                outside.unlink(missing_ok=True)

    def test_recovery_service_preserves_existing_recovery_semantics(self):
        with (
            patch.object(redcap_import_safety, "import_lock", return_value=nullcontext()),
            patch.object(redcap_import_safety, "recover_locks", return_value=2),
        ):
            result = recover_redcap_locks()
        self.assertEqual(result.status, SUCCESS)
        self.assertEqual(result.verified, 2)

        with (
            patch.object(redcap_import_safety, "import_lock", return_value=nullcontext()),
            patch.object(redcap_import_safety, "recover_locks", side_effect=RuntimeError("PTID 001 failed")),
        ):
            failed = recover_redcap_locks()
        self.assertEqual(failed.status, UNCERTAIN)
        self.assertTrue(failed.requires_reconciliation)
        self.assertNotIn("001", failed.message)


if __name__ == "__main__":
    unittest.main()

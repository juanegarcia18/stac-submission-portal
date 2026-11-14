"""Offline tests for backend workflow preparation and commit interfaces."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import pandas as pd
import requests

from backend.imports_api.external_write_results import FAILURE, NO_DATA, PARTIAL, SUCCESS, UNCERTAIN
from backend.services.config import DisabledFormError
from backend.services.validate_job_approval import ApprovalAuthorization, ApprovalError
from backend.workflows.enrollment_submission import (
    commit_enrollment_submission,
    prepare_enrollment_submission,
)
from backend.workflows.naccid_import import commit_naccid_import, prepare_naccid_import
from backend.workflows.prepare_workflow_results import (
    NO_DATA as PREP_NO_DATA,
    PARTIAL as PREP_PARTIAL,
    PROCESSING_FAILED,
    SUCCESS as PREP_SUCCESS,
    VALIDATION_FAILED,
)
from backend.workflows.udsv4_submission import commit_udsv4_submission, prepare_udsv4_submission


ENV = {
    "BASE_URL": "https://redcap.invalid/api/",
    "UDSV4_TOKEN": "offline-token",
    "FW_GROUP": "fictional-group",
    "FW_PROJECT": "forms",
    "FW_ENROLL": "enrollment",
    "FLYWHEEL_TOKEN": "offline-token",
}


def approval(operation: str) -> ApprovalAuthorization:
    """Return a fictional approval authorization for workflow tests."""

    return ApprovalAuthorization(operation, "reviewer", "2026-11-13T12:00:00Z")


class FakeUploader:
    """Flywheel uploader test double that records exact paths and projects."""

    group = "test-group"

    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.uploads = []

    def login(self):
        return True

    def logout(self):
        return None

    def upload_file_to_project(self, path, project):
        self.uploads.append((Path(path), project))
        return self.statuses.pop(0)


class Udsv4WorkflowTests(unittest.TestCase):
    """Cover UDSv4 preparation, form-specific rules, and approved commit."""

    def setUp(self):
        self.enterContext(patch.dict(os.environ, ENV, clear=True))
        self.workspace = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_prepare_one_form_and_commit_uses_original_artifact(self):
        report = pd.DataFrame({
            "ptid": ["001"], "visitnum": ["2"], "packet": ["I"], "note": ["a,b"], "redcap_event_name": ["hidden"],
        })
        filter_df = pd.DataFrame({"ptid": ["001.0"], "visitnum": ["2.0"]})
        result = prepare_udsv4_submission(
            selected_forms=["UDS"],
            filter_df=filter_df,
            workspace=self.workspace,
            report_fetcher=Mock(return_value=report),
        )

        self.assertEqual(result.status, PREP_SUCCESS)
        self.assertEqual(result.prepared_records, 1)
        self.assertEqual(result.forms_prepared, ("UDS",))
        self.assertEqual(result.module_summaries[0].module, "UDS")
        self.assertEqual(result.module_summaries[0].requested_records, 1)
        self.assertEqual(result.module_summaries[0].prepared_records, 1)
        self.assertEqual(result.artifacts[0].read_text(), 'ptid,visitnum,packet,note\n001,2,I,"a,b"\n')
        result.review_artifacts[0].write_text("edited\n")

        fake = FakeUploader([("success", "")])
        commit = commit_udsv4_submission(
            result.artifacts,
            approval("flywheel_upload"),
            workspace=self.workspace,
            uploader_factory=lambda: fake,
        )
        self.assertEqual(commit.status, SUCCESS)
        self.assertEqual(commit.results[0].destination, "test-group/forms")
        self.assertEqual(fake.uploads, [(result.artifacts[0], "forms")])

    def test_prepare_multiple_forms_preserves_np_and_mlst_rules(self):
        reports = {
            "m1": pd.DataFrame({"mlst_ptid": ["001"], "mlst_visitnum": ["2"], "mlst_packet": ["I"], "note": ["mlst"]}),
            "np": pd.DataFrame({"np_ptid": ["002"], "np_packet": ["I"], "note": ["np"], "np_loc_today": ["x"], "np_initials": ["y"]}),
        }
        filter_df = pd.DataFrame({"ptid": ["001", "002"], "mlst_visitnum": ["2", ""], "npv11_submit": ["0", "1"]})
        result = prepare_udsv4_submission(
            selected_forms=["MLST", "NP"],
            filter_df=filter_df,
            workspace=self.workspace,
            report_fetcher=lambda name: reports[name],
        )

        self.assertEqual(result.status, PREP_SUCCESS)
        contents = {path.name: path.read_text() for path in result.artifacts}
        self.assertTrue(any("ptid,packet,note" in text for text in contents.values()))
        self.assertFalse(any("visitnum" in text.splitlines()[0] for text in contents.values()))
        self.assertEqual([item.module for item in result.module_summaries], ["MLST", "NP"])

    def test_prepare_counts_requested_and_prepared_records_not_files(self):
        filter_df = pd.DataFrame({
            "ptid": [f"{number:03d}" for number in range(1, 26)],
            "visitnum": ["1"] * 25,
        })
        report = pd.DataFrame({
            "ptid": [f"{number:03d}" for number in range(1, 24)],
            "visitnum": ["1"] * 23,
            "packet": ["I"] * 23,
        })
        result = prepare_udsv4_submission(
            selected_forms=["UDS"],
            filter_df=filter_df,
            workspace=self.workspace,
            report_fetcher=Mock(return_value=report),
        )

        self.assertEqual(result.status, PREP_PARTIAL)
        self.assertEqual(result.prepared_records, 23)
        summary = result.module_summaries[0]
        self.assertEqual(summary.requested_records, 25)
        self.assertEqual(summary.prepared_records, 23)
        self.assertIn("UDS: 2 requested record(s) were not prepared.", summary.findings)
        self.assertIn("UDS: 2 requested participant(s) were not found in the REDCap report.", summary.findings)
        self.assertEqual(len(pd.read_csv(result.artifacts[0], dtype=str)), 23)

    def test_prepare_matching_diagnostics_respect_form_rules(self):
        visit_result = prepare_udsv4_submission(
            selected_forms=["UDS"],
            filter_df=pd.DataFrame({"ptid": ["001", "002"], "visitnum": ["1", "2"]}),
            workspace=self.workspace,
            report_fetcher=Mock(return_value=pd.DataFrame({"ptid": ["001", "002"], "visitnum": ["9", "2"]})),
        )
        visit_findings = " ".join(visit_result.module_summaries[0].findings)
        self.assertIn("1 requested record(s) were not prepared", visit_findings)
        self.assertIn("1 requested record(s) had no matching visit", visit_findings)
        self.assertNotIn("participant(s) were not found", visit_findings)

        np_result = prepare_udsv4_submission(
            selected_forms=["NP"],
            filter_df=pd.DataFrame({"ptid": ["001", "002"], "npv11_submit": ["1", "1"]}),
            workspace=self.workspace,
            report_fetcher=Mock(return_value=pd.DataFrame({"np_ptid": ["002"], "np_packet": ["I"]})),
        )
        np_findings = " ".join(np_result.module_summaries[0].findings)
        self.assertIn("1 requested participant(s) were not found", np_findings)
        self.assertNotIn("visit", np_findings.lower())

    def test_prepare_unknown_disabled_empty_partial_and_extraction_failure(self):
        filter_df = pd.DataFrame({"ptid": ["001"], "visitnum": ["2"]})
        unknown = prepare_udsv4_submission(selected_forms=["UNKNOWN"], filter_df=filter_df, workspace=self.workspace)
        self.assertEqual(unknown.status, VALIDATION_FAILED)

        with patch("backend.workflows.udsv4_submission.get_selected_form_configs", side_effect=DisabledFormError("Form is disabled")):
            disabled = prepare_udsv4_submission(selected_forms=["UDS"], filter_df=filter_df, workspace=self.workspace)
        self.assertEqual(disabled.status, VALIDATION_FAILED)

        empty = prepare_udsv4_submission(
            selected_forms=["UDS"],
            filter_df=filter_df,
            workspace=self.workspace,
            report_fetcher=Mock(return_value=pd.DataFrame({"ptid": ["999"], "visitnum": ["2"]})),
        )
        self.assertEqual(empty.status, PREP_NO_DATA)
        self.assertEqual(empty.forms_without_output, ("UDS",))
        self.assertEqual(empty.module_summaries[0].module, "UDS")
        self.assertEqual(empty.module_summaries[0].prepared_records, 0)

        partial = prepare_udsv4_submission(
            selected_forms=["UDS", "NP"],
            filter_df=pd.DataFrame({"ptid": ["001"], "visitnum": ["2"], "npv11_submit": ["1"]}),
            workspace=self.workspace,
            report_fetcher=lambda name: pd.DataFrame({"ptid": ["001"], "visitnum": ["2"]}) if name == "uds" else pd.DataFrame(),
        )
        self.assertEqual(partial.status, PREP_PARTIAL)
        self.assertEqual(partial.forms_prepared, ("UDS",))
        self.assertEqual(partial.forms_without_output, ("NP",))
        self.assertEqual([item.module for item in partial.module_summaries], ["UDS", "NP"])

        failed = prepare_udsv4_submission(
            selected_forms=["UDS"],
            filter_df=filter_df,
            workspace=self.workspace,
            report_fetcher=Mock(side_effect=RuntimeError("token offline-token PTID 001")),
        )
        self.assertEqual(failed.status, PROCESSING_FAILED)
        self.assertNotIn("offline-token", failed.error)
        self.assertNotIn("001", failed.error)

    def test_commit_rejects_missing_approval_and_review_copy(self):
        original = self.workspace / "prepared.csv"
        original.write_text("ptid\n001\n")
        review_dir = self.workspace / "review"
        review_dir.mkdir()
        review = review_dir / "prepared.csv"
        review.write_text("edited\n")
        with self.assertRaises(ApprovalError):
            commit_udsv4_submission([original], True, workspace=self.workspace)
        with self.assertRaisesRegex(ValueError, "Review copies"):
            commit_udsv4_submission([review], approval("flywheel_upload"), workspace=self.workspace)


class EnrollmentWorkflowTests(unittest.TestCase):
    """Cover enrollment preparation and explicit enrollment upload behavior."""

    def setUp(self):
        self.enterContext(patch.dict(os.environ, ENV, clear=True))
        self.workspace = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_prepare_eligible_records_preserves_quoted_csv_format(self):
        today = pd.Timestamp.today().date().isoformat()
        checker = pd.DataFrame([{"enrl_ptid": "001", "enrl_submit_confirm": "1", "enrl_submit_date": today, "enrl_accepted_date": "", "enrl_naccid": ""}])
        report = pd.DataFrame([["001", "07", "4", "a,b", "hidden"]], columns=["enrl_ptid", "enrl_adcid", "enrl_formver", "note", "redcap_event_name"])
        result = prepare_enrollment_submission(
            workspace=self.workspace,
            report_fetcher=Mock(side_effect=[checker, report]),
        )

        self.assertEqual(result.status, PREP_SUCCESS)
        self.assertEqual(result.prepared_records, 1)
        self.assertEqual(result.artifacts[0].read_text(), '"ptid","adcid","formver","note"\n"001","07","4","a,b"\n')

    def test_prepare_no_data_duplicates_and_missing_matches(self):
        today = pd.Timestamp.today().date().isoformat()
        no_eligible = pd.DataFrame([{"enrl_ptid": "001", "enrl_submit_confirm": "0", "enrl_submit_date": today, "enrl_accepted_date": "", "enrl_naccid": ""}])
        no_data = prepare_enrollment_submission(workspace=self.workspace, report_fetcher=Mock(return_value=no_eligible))
        self.assertEqual(no_data.status, PREP_NO_DATA)
        self.assertEqual(no_data.module_summaries[0].prepared_records, 0)

        duplicate = pd.DataFrame([
            {"enrl_ptid": "001", "enrl_submit_confirm": "1", "enrl_submit_date": today, "enrl_accepted_date": "", "enrl_naccid": ""},
            {"enrl_ptid": "001", "enrl_submit_confirm": "1", "enrl_submit_date": today, "enrl_accepted_date": "", "enrl_naccid": ""},
        ])
        self.assertEqual(prepare_enrollment_submission(workspace=self.workspace, report_fetcher=Mock(return_value=duplicate)).status, VALIDATION_FAILED)

        checker = duplicate.iloc[:1]
        report = pd.DataFrame({"enrl_ptid": ["002"], "enrl_adcid": ["07"], "enrl_formver": ["4"]})
        missing = prepare_enrollment_submission(workspace=self.workspace, report_fetcher=Mock(side_effect=[checker, report]))
        self.assertEqual(missing.status, VALIDATION_FAILED)
        self.assertNotIn("001", missing.error)

        failed = prepare_enrollment_submission(
            workspace=self.workspace,
            report_fetcher=Mock(side_effect=RuntimeError("token offline-token PTID 001")),
        )
        self.assertEqual(failed.status, PROCESSING_FAILED)
        self.assertNotIn("offline-token", failed.error)

    def test_commit_requires_approval_and_fw_enroll(self):
        artifact = self.workspace / "enroll.csv"
        artifact.write_text("ptid\n001\n")
        with self.assertRaises(ApprovalError):
            commit_enrollment_submission([artifact], True, workspace=self.workspace)
        with patch.dict(os.environ, {"FW_ENROLL": ""}):
            with self.assertRaisesRegex(Exception, "FW_ENROLL"):
                commit_enrollment_submission([artifact], approval("flywheel_upload"), workspace=self.workspace, uploader_factory=lambda: FakeUploader([("success", "")]))


class NaccidWorkflowTests(unittest.TestCase):
    """Cover NACCID preparation payloads and approved REDCap commit outcomes."""

    def setUp(self):
        self.enterContext(patch.dict(os.environ, ENV, clear=True))
        self.workspace = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_prepare_success_preserves_exact_import_payload(self):
        redcap = pd.DataFrame({"enrl_ptid": [" 001 "], "enrl_naccid": [""], "enrl_accepted_date": [""]})
        fw = Mock()
        naccids = pd.DataFrame({"ptid": ["001"], "naccid": ["FAKE-1"], "accepted_date": ["2026-11-13"]})
        with patch("backend.workflows.naccid_import.fetch_recent_flywheel_naccids", return_value=naccids):
            result = prepare_naccid_import(workspace=self.workspace, report_fetcher=Mock(return_value=redcap), flywheel_client=fw)

        self.assertEqual(result.status, PREP_SUCCESS)
        self.assertEqual(result.payload.to_csv(index=False), "ptid,redcap_event_name,enrl_ptid,enrl_accepted_date,enrl_naccid\n001,enrollment_id_arm_1,001,2026-11-13,FAKE-1\n")
        self.assertEqual(result.artifacts[0].read_text(), result.payload.to_csv(index=False))

    def test_prepare_no_pending_duplicates_and_sanitized_failures(self):
        empty = prepare_naccid_import(workspace=self.workspace, report_fetcher=Mock(return_value=pd.DataFrame()))
        self.assertEqual(empty.status, PREP_NO_DATA)
        self.assertEqual(empty.module_summaries[0].prepared_records, 0)

        redcap = pd.DataFrame({"enrl_ptid": ["001"], "enrl_naccid": [""], "enrl_accepted_date": [""]})
        duplicate_fw = pd.DataFrame({"ptid": ["001", "001"], "naccid": ["FAKE-1", "FAKE-2"], "accepted_date": ["2026-11-13", "2026-11-13"]})
        with patch("backend.workflows.naccid_import.fetch_recent_flywheel_naccids", return_value=duplicate_fw):
            duplicate = prepare_naccid_import(workspace=self.workspace, report_fetcher=Mock(return_value=redcap), flywheel_client=Mock())
        self.assertEqual(duplicate.status, VALIDATION_FAILED)
        self.assertNotIn("001", duplicate.error)

        failed = prepare_naccid_import(workspace=self.workspace, report_fetcher=Mock(side_effect=RuntimeError("token offline-token PTID 001")))
        self.assertEqual(failed.status, PROCESSING_FAILED)
        self.assertNotIn("offline-token", failed.error)

    def test_commit_requires_approval_rechecks_and_reports_uncertain(self):
        payload = pd.DataFrame({"ptid": ["001"]})
        with self.assertRaises(ApprovalError):
            commit_naccid_import(payload, True)

        importer = Mock(return_value={"submitted": 1, "verified": 1, "failed": 0, "skipped": 0})
        success = commit_naccid_import(payload, approval("naccid_import"), importer=importer)
        self.assertEqual(success.status, SUCCESS)
        importer.assert_called_once()

        uncertain = commit_naccid_import(
            payload,
            approval("naccid_import"),
            importer=Mock(side_effect=requests.ConnectionError("lost connection")),
        )
        self.assertEqual(uncertain.status, UNCERTAIN)
        self.assertTrue(uncertain.requires_reconciliation)


if __name__ == "__main__":
    unittest.main()

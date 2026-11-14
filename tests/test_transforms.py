"""Direct offline tests for shared ETL transformations."""

import unittest

import pandas as pd

from backend.services.config import load_config
from backend.transforms.prepare_naccid_import_records import prepare_naccid_import_records
from backend.transforms.prepare_output_columns import (
    drop_configured_final_output_columns,
    prepare_submission_output_columns,
    prepare_enrollment_output_columns,
)
from backend.transforms.prepare_participant_ids import (
    prepare_participant_key,
    filter_valid_participant_keys,
    normalize_submission_identifier_series,
)
from backend.transforms.match_submission_records import (
    filter_submission_records,
    match_submission_report,
)


class IdentifierTransformTests(unittest.TestCase):
    """Pin workflow-specific PTID normalization differences."""

    def test_submission_identifier_normalization_removes_trailing_decimal(self):
        values = pd.Series([" 001.0 ", "002", "", "nan", "None", "<NA>"])
        result = normalize_submission_identifier_series(values)
        self.assertEqual(result.astype(object).where(result.notna(), None).tolist(),
                         ["001", "002", None, None, None, None])

    def test_enrollment_participant_key_preserves_decimal_text(self):
        frame = pd.DataFrame({"PTID": [" 001.0 ", "002", "", "nan", "None"]})
        keyed = prepare_participant_key(frame, ["ptid"], name="Enrollment checker")
        self.assertEqual(keyed[".key_ptid"].tolist(), ["001.0", "002", "", "nan", "None"])
        filtered = filter_valid_participant_keys(keyed, name="Enrollment checker")
        self.assertEqual(filtered[".key_ptid"].tolist(), ["001.0", "002"])

    def test_enrollment_duplicate_keys_are_rejected_after_missing_removed(self):
        frame = pd.DataFrame({".key_ptid": ["001", "", "001", "None"]})
        with self.assertRaisesRegex(ValueError, "Duplicate PTIDs"):
            filter_valid_participant_keys(frame, name="Enrollment checker")


class SubmissionTransformTests(unittest.TestCase):
    """Exercise form-specific UDSv4 filtering, matching, and output shaping."""

    @classmethod
    def setUpClass(cls):
        cls.forms = load_config(use_cache=False)["forms"]

    def test_visit_form_matching_preserves_leading_zeros_and_normalizes_decimal_visit(self):
        config = self.forms["UDS"]
        selected = pd.DataFrame({"ptid": [" 001.0 ", "1"], "visitnum": ["2.0", "2"]})
        report = pd.DataFrame({
            "ptid": ["001", "1"],
            "visitnum": ["2", "2"],
            "packet": ["I", "wrong"],
            "redcap_event_name": ["hidden", "hidden"],
        })
        filtered = filter_submission_records(selected, "UDS", config)
        matched = match_submission_report(filtered, report, "UDS", config)
        output = prepare_submission_output_columns(matched, "UDS", config)
        self.assertEqual(output.to_csv(index=False), "ptid,visitnum,packet\n001,2,I\n1,2,wrong\n")

    def test_np_flag_matching_uses_ptid_only(self):
        config = self.forms["NP"]
        selected = pd.DataFrame({"ptid": ["001", "002", "003"], "npv11_submit": ["1", "0", ""]})
        report = pd.DataFrame({"np_ptid": ["001", "002"], "np_packet": ["I", "I"], "np_initials": ["AA", "BB"]})
        filtered = filter_submission_records(selected, "NP", config)
        matched = match_submission_report(filtered, report, "NP", config)
        output = prepare_submission_output_columns(matched, "NP", config)
        self.assertEqual(output.to_csv(index=False), "ptid,packet\n001,I\n")

    def test_mlst_final_output_removes_visitnum_after_matching(self):
        config = self.forms["MLST"]
        report = pd.DataFrame({"mlst_ptid": ["001"], "mlst_visitnum": ["2"], "mlst_packet": ["I"]})
        selected = pd.DataFrame({"ptid": ["001"], "mlst_visitnum": ["2"]})
        matched = match_submission_report(filter_submission_records(selected, "MLST", config), report, "MLST", config)
        output = drop_configured_final_output_columns(prepare_submission_output_columns(matched, "MLST", config), config)
        self.assertEqual(output.columns.tolist(), ["ptid", "packet"])


class EnrollmentAndNaccidTransformTests(unittest.TestCase):
    """Cover enrollment output cleanup and NACCID import preparation."""

    def test_enrollment_metadata_cleanup_preserves_submission_order(self):
        frame = pd.DataFrame({
            "ptid": ["001"], "adcid": ["07"], ".key_ptid": ["001"],
            "redcap_event_name": ["hidden"], "NACCID": ["hidden"], "note": ["a,b"],
        })
        output = prepare_enrollment_output_columns(frame)
        self.assertEqual(output.to_csv(index=False), "ptid,adcid,note\n001,07,\"a,b\"\n")

    def test_naccid_import_preparation_filters_pending_and_preserves_columns(self):
        columns = ["ptid", "redcap_event_name", "enrl_ptid", "enrl_accepted_date", "enrl_naccid"]
        fw = pd.DataFrame([("001", "NACC-1", "2026-11-12"), ("002", "NACC-2", "2026-11-13")],
                          columns=["ptid", "naccid", "accepted_date"])
        redcap = pd.DataFrame([(" 001 ", "", ""), ("002", "EXISTING", ""), ("003", "", "2026-11-10")],
                              columns=["enrl_ptid", "enrl_naccid", "enrl_accepted_date"])
        result = prepare_naccid_import_records(fw, redcap, columns, "enrollment_id_arm_1")
        self.assertEqual(result.to_csv(index=False),
                         "ptid,redcap_event_name,enrl_ptid,enrl_accepted_date,enrl_naccid\n"
                         "001,enrollment_id_arm_1,001,2026-11-12,NACC-1\n")

    def test_naccid_import_duplicate_ptids_are_rejected(self):
        columns = ["ptid", "redcap_event_name", "enrl_ptid", "enrl_accepted_date", "enrl_naccid"]
        fw = pd.DataFrame([("001", "NACC-1", "2026-11-12"), ("001", "NACC-2", "2026-11-13")],
                          columns=["ptid", "naccid", "accepted_date"])
        redcap = pd.DataFrame([("001", "", "")], columns=["enrl_ptid", "enrl_naccid", "enrl_accepted_date"])
        with self.assertRaisesRegex(ValueError, "Duplicate enrollment PTIDs found in Flywheel"):
            prepare_naccid_import_records(fw, redcap, columns, "enrollment_id_arm_1")


if __name__ == "__main__":
    unittest.main()

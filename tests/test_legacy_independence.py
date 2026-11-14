"""Validate Flask/backend operation when legacy directories are unavailable."""

from __future__ import annotations

import builtins
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock, patch

import pandas as pd

from backend.flask_application import create_flask_application
from backend.workflows.enrollment_submission import prepare_enrollment_submission
from backend.workflows.naccid_import import prepare_naccid_import
from backend.workflows.udsv4_submission import prepare_udsv4_submission


@contextmanager
def block_legacy_imports():
    """Raise ModuleNotFoundError whenever legacy packages are imported."""

    original_import = builtins.__import__

    def blocked_import(name, globals=None, locals=None, fromlist=(), level=0):
        root = name.split(".", 1)[0]
        if root in {"Form_Submissions", "Subject_Enroll", "utils", "main"}:
            raise ModuleNotFoundError(f"Blocked legacy module import: {name}")
        return original_import(name, globals, locals, fromlist, level)

    with patch("builtins.__import__", side_effect=blocked_import):
        yield


class LegacyIndependenceTests(unittest.TestCase):
    """Ensure backend workflows and Flask startup no longer import legacy code."""

    def test_flask_and_workflows_run_when_legacy_imports_are_blocked(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with block_legacy_imports():
                app = create_flask_application({
                    "TESTING": True,
                    "START_BACKGROUND_WORKER": False,
                    "ENABLE_DEV_AUTH": True,
                    "SECRET_KEY": "test-secret",
                    "NACC_JOB_ROOT": str(root / "jobs"),
                })
                self.assertEqual(app.test_client().get("/").status_code, 200)

                uds_workspace = root / "uds"
                uds_filter = pd.DataFrame({"ptid": ["001"], "visitnum": ["1"]})
                uds_report = pd.DataFrame({"ptid": ["001"], "visitnum": ["1"], "packet": ["I"]})
                uds_result = prepare_udsv4_submission(
                    selected_forms=["UDS"],
                    filter_df=uds_filter,
                    workspace=uds_workspace,
                    report_fetcher=Mock(return_value=uds_report),
                )
                self.assertEqual(uds_result.status, "success")

                enroll_workspace = root / "enroll"
                today = pd.Timestamp.today().date().isoformat()
                checker = pd.DataFrame([{
                    "enrl_ptid": "001",
                    "enrl_submit_confirm": "1",
                    "enrl_submit_date": today,
                    "enrl_accepted_date": "",
                    "enrl_naccid": "",
                }])
                report = pd.DataFrame([{"enrl_ptid": "001", "enrl_adcid": "07", "enrl_formver": "4"}])
                enroll_result = prepare_enrollment_submission(
                    workspace=enroll_workspace,
                    report_fetcher=Mock(side_effect=[checker, report]),
                )
                self.assertEqual(enroll_result.status, "success")

                naccid_workspace = root / "naccid"
                redcap = pd.DataFrame({"enrl_ptid": ["001"], "enrl_naccid": [""], "enrl_accepted_date": [""]})
                flywheel_rows = pd.DataFrame({"ptid": ["001"], "naccid": ["FAKE-1"], "accepted_date": ["2026-11-13"]})
                with patch("backend.workflows.naccid_import.fetch_recent_flywheel_naccids", return_value=flywheel_rows):
                    naccid_result = prepare_naccid_import(
                        workspace=naccid_workspace,
                        report_fetcher=Mock(return_value=redcap),
                        flywheel_client=Mock(),
                    )
                self.assertEqual(naccid_result.status, "success")


if __name__ == "__main__":
    unittest.main()

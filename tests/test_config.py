"""Offline tests for YAML-backed backend configuration."""

import os
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import yaml

from backend.services import config as backend_config


ORIGINAL_FORM_CONFIG = {
    "UDS": {
        "api_form": "uds",
        "filter_visitnum_col": "visitnum",
        "report_ptid_cols": ["ptid", "enrl_ptid"],
        "report_visitnum_cols": ["visitnum"],
        "prefix": "uds",
        "slug": "uds",
        "output_rename": {},
        "drop_columns": ["redcap_event_name"],
    },
    "MLST": {
        "api_form": "m1",
        "filter_visitnum_col": "mlst_visitnum",
        "report_ptid_cols": ["mlst_ptid", "ptid"],
        "report_visitnum_cols": ["mlst_visitnum"],
        "prefix": "mlst",
        "slug": "mlst",
        "output_rename": {
            "mlst_ptid": "ptid",
            "mlst_visitnum": "visitnum",
            "mlst_packet": "packet",
            "mlst_formver": "formver",
            "mlst_adcid": "adcid",
            "mlst_visitdate": "visitdate",
            "mlst_initials": "initials",
        },
        "drop_columns": [],
        "final_output_drop_columns": ["visitnum"],
    },
    "CLS": {
        "api_form": "cls",
        "filter_visitnum_col": "cls_visitnum",
        "report_ptid_cols": ["cls_ptid", "ptid"],
        "report_visitnum_cols": ["cls_visitnum"],
        "prefix": "cls",
        "slug": "cls",
        "output_rename": {
            "cls_ptid": "ptid",
            "cls_visitnum": "visitnum",
            "cls_packet": "packet",
            "cls_formver": "formver",
            "cls_adcid": "adcid",
            "cls_visitdate": "visitdate",
            "cls_initials": "initials",
        },
        "drop_columns": [],
    },
    "FTLD": {
        "api_form": "ftd",
        "filter_visitnum_col": "ftd_visitnum",
        "report_ptid_cols": ["ftd_ptid", "ptid"],
        "report_visitnum_cols": ["ftd_visitnum"],
        "prefix": "ftd",
        "slug": "ftld",
        "output_rename": {
            "ftd_ptid": "ptid",
            "ftd_visitnum": "visitnum",
            "ftd_packet": "packet",
            "ftd_formver": "formver",
            "ftd_adcid": "adcid",
            "ftd_visitdate": "visitdate",
            "ftd_initials": "initials",
        },
        "drop_columns": [],
    },
    "LBD": {
        "api_form": "lbd",
        "filter_visitnum_col": "lbd_visitnum",
        "report_ptid_cols": ["lbd_ptid", "ptid"],
        "report_visitnum_cols": ["lbd_visitnum"],
        "prefix": "lbd",
        "slug": "lbd",
        "output_rename": {
            "lbd_ptid": "ptid",
            "lbd_visitnum": "visitnum",
            "lbd_packet": "packet",
            "lbd_formver": "formver",
            "lbd_adcid": "adcid",
            "lbd_visitdate": "visitdate",
            "lbd_initials": "initials",
        },
        "drop_columns": [],
    },
    "NP": {
        "api_form": "np",
        "filter_visitnum_col": None,
        "submit_flag_col": "npv11_submit",
        "report_ptid_cols": ["np_ptid", "ptid"],
        "report_visitnum_cols": [],
        "prefix": "np",
        "slug": "np",
        "output_rename": {
            "np_ptid": "ptid",
            "np_formver": "formver",
            "np_adcid": "adcid",
            "np_packet": "packet",
        },
        "drop_columns": ["np_loc_today", "np_initials"],
    },
}


class ConfigurationTests(unittest.TestCase):
    """Validate the YAML configuration contract without external services."""

    def setUp(self):
        backend_config.clear_config_cache()

    def load_default(self):
        """Load the repository YAML without using the module cache."""

        return backend_config.load_config(use_cache=False)

    def load_raw_yaml(self):
        """Load YAML documents in their on-disk shape for mutation tests."""

        config_dir = backend_config.DEFAULT_CONFIG_DIR
        forms_doc = yaml.safe_load((config_dir / "forms.yaml").read_text(encoding="utf-8"))
        integrations_doc = yaml.safe_load((config_dir / "integrations.yaml").read_text(encoding="utf-8"))
        return forms_doc, integrations_doc

    def write_temp_config(self, forms_data=None, integrations_data=None) -> Path:
        """Write modified config files and return the temporary directory."""

        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        forms_doc, integrations_doc = self.load_raw_yaml()
        if forms_data is not None:
            forms_doc = {"forms": deepcopy(forms_data)}
        if integrations_data is not None:
            integrations_doc = deepcopy(integrations_data)
        (folder / "forms.yaml").write_text(yaml.safe_dump(forms_doc), encoding="utf-8")
        (folder / "integrations.yaml").write_text(yaml.safe_dump(integrations_doc), encoding="utf-8")
        return folder

    def original_shape(self, forms):
        """Return the pre-refactor form config shape for snapshot comparison."""

        result = {}
        for form_name, expected in ORIGINAL_FORM_CONFIG.items():
            result[form_name] = {key: deepcopy(forms[form_name].get(key)) for key in expected}
            if form_name == "MLST":
                result[form_name]["final_output_drop_columns"] = forms[form_name].get("final_output_drop_columns")
        return result

    def test_yaml_files_load_and_define_all_forms(self):
        loaded = self.load_default()
        self.assertEqual(set(loaded["forms"]), {"UDS", "MLST", "CLS", "FTLD", "LBD", "NP"})
        self.assertIn("redcap", loaded["integrations"])
        self.assertIn("flywheel", loaded["integrations"])

    def test_required_form_mappings_are_present(self):
        forms = self.load_default()["forms"]
        for form_name, expected in ORIGINAL_FORM_CONFIG.items():
            with self.subTest(form=form_name):
                for key, value in expected.items():
                    self.assertEqual(forms[form_name].get(key), value)
                self.assertTrue(forms[form_name]["enabled"])
        self.assertEqual(forms["NP"]["submit_flag_value"], "1")

    def test_unknown_and_disabled_forms_are_rejected(self):
        loaded = self.load_default()
        with self.assertRaisesRegex(backend_config.UnknownFormError, "Unknown form"):
            backend_config.get_form_config("UNKNOWN", loaded)

        forms, _ = self.load_raw_yaml()
        forms = forms["forms"]
        forms["UDS"]["enabled"] = False
        folder = self.write_temp_config(forms_data=forms)
        disabled = backend_config.load_config(folder, use_cache=False)
        with self.assertRaisesRegex(backend_config.DisabledFormError, "disabled"):
            backend_config.get_form_config("UDS", disabled)

    def test_missing_mappings_and_unsupported_modes_are_rejected(self):
        forms, _ = self.load_raw_yaml()
        forms = forms["forms"]
        del forms["NP"]["matching"]["report_ptid_cols"]
        folder = self.write_temp_config(forms_data=forms)
        with self.assertRaisesRegex(backend_config.InvalidConfiguration, "report_ptid_cols"):
            backend_config.load_config(folder, use_cache=False)

        forms, _ = self.load_raw_yaml()
        forms = forms["forms"]
        forms["UDS"]["matching"]["mode"] = "made_up"
        folder = self.write_temp_config(forms_data=forms)
        with self.assertRaisesRegex(backend_config.InvalidConfiguration, "unsupported matching mode"):
            backend_config.load_config(folder, use_cache=False)

    def test_malformed_form_field_types_are_rejected(self):
        cases = [
            ("UDS", ("enabled",), "yes", "UDS.enabled"),
            ("UDS", ("api_form",), "", "UDS.api_form"),
            ("MLST", ("output_rename", "mlst_ptid"), 42, "MLST.output_rename"),
            ("NP", ("drop_columns",), ["np_initials", 42], "NP.drop_columns"),
            ("NP", ("submit_flag_col",), 42, "NP.submit_flag_col"),
        ]
        for form_name, path, value, message in cases:
            with self.subTest(field=".".join(path)):
                forms, _ = self.load_raw_yaml()
                target = forms["forms"][form_name]
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = value
                folder = self.write_temp_config(forms_data=forms["forms"])
                with self.assertRaisesRegex(backend_config.InvalidConfiguration, message):
                    backend_config.load_config(folder, use_cache=False)

    def test_missing_environment_variables_do_not_expose_secret_values(self):
        loaded = self.load_default()
        env = {
            "BASE_URL": "https://redcap.invalid/api/",
            "UDSV4_TOKEN": "super-secret-token",
        }
        with self.assertRaisesRegex(backend_config.MissingEnvironmentVariable, "REPORT_ID_UDS") as caught:
            backend_config.resolve_redcap_report("uds", loaded, env)
        self.assertNotIn("super-secret-token", str(caught.exception))

    def test_local_dotenv_does_not_overwrite_external_environment(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"BASE_URL": "external"}, clear=True):
            env_path = Path(folder) / "stac_submission_portal.env"
            env_path.write_text("BASE_URL=local\nUDSV4_TOKEN=local-token\n", encoding="utf-8")
            backend_config.load_environment_file(env_path)
            self.assertEqual(os.environ["BASE_URL"], "external")
            self.assertEqual(os.environ["UDSV4_TOKEN"], "local-token")

    def test_explicit_environment_resolution_does_not_load_dotenv(self):
        loaded = self.load_default()
        env = {
            "BASE_URL": "https://redcap.invalid/api/",
            "UDSV4_TOKEN": "token",
            "REPORT_ID_UDS": "123",
            "FLYWHEEL_TOKEN": "fw-token",
            "FW_GROUP": "group",
            "FW_PROJECT": "forms",
        }
        with patch.object(backend_config, "load_environment_file") as load_env:
            backend_config.resolve_redcap_report("uds", loaded, env)
            backend_config.resolve_flywheel_auth(loaded, env)
            backend_config.resolve_flywheel_destination("form_submissions", loaded, env)
        load_env.assert_not_called()

    def test_enrollment_destination_requires_fw_enroll(self):
        loaded = self.load_default()
        env = {
            "FLYWHEEL_TOKEN": "fictional-token",
            "FW_GROUP": "fictional-group",
            "FW_PROJECT": "forms-project",
            "FW_ENROLL": "enrollment-project",
        }
        self.assertEqual(
            backend_config.resolve_flywheel_destination("enrollment_submissions", loaded, env),
            "enrollment-project",
        )
        self.assertEqual(
            backend_config.resolve_flywheel_destination("form_submissions", loaded, env),
            "forms-project",
        )
        with self.assertRaisesRegex(backend_config.MissingEnvironmentVariable, "FW_ENROLL"):
            backend_config.resolve_flywheel_destination("enrollment_submissions", loaded, {"FW_PROJECT": "forms"})

    def test_form_yaml_remains_equivalent_to_legacy_processor_config(self):
        forms = self.load_default()["forms"]
        self.assertEqual(self.original_shape(forms), ORIGINAL_FORM_CONFIG)


if __name__ == "__main__":
    unittest.main()

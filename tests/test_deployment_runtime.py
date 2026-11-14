"""Offline tests for container-oriented Flask deployment behavior."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.flask_application import create_flask_application
from backend.serve_flask_app import (
    load_flask_runtime_config,
    load_waitress_settings,
    validate_deployment_security,
)


class DeploymentRuntimeTests(unittest.TestCase):
    """Validate deployment defaults without starting Waitress or external APIs."""

    def test_health_endpoint_is_available_without_authentication(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        app = create_flask_application({
            "TESTING": True,
            "NACC_JOB_ROOT": str(root),
            "START_BACKGROUND_WORKER": False,
            "ENABLE_DEV_AUTH": False,
            "SECRET_KEY": "test-secret",
        })

        response = app.test_client().get("/healthz")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["status"], "ok")
        self.assertEqual(response.json["job_store"], "ready")

    def test_production_security_rejects_missing_secret_and_dev_auth(self):
        with patch.dict(os.environ, {"NACC_DEPLOYMENT_ENV": "production"}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "NACC_FLASK_SECRET"):
                validate_deployment_security({"SESSION_COOKIE_SECURE": True})

        env = {
            "NACC_DEPLOYMENT_ENV": "production",
            "NACC_FLASK_SECRET": "secret",
            "NACC_ENABLE_DEV_AUTH": "1",
        }
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(RuntimeError, "DEV_AUTH"):
                validate_deployment_security({"SESSION_COOKIE_SECURE": True})

    def test_runtime_config_and_waitress_settings_use_container_defaults(self):
        env = {
            "NACC_DEPLOYMENT_ENV": "production",
            "NACC_FLASK_SECRET": "secret",
            "NACC_PORT": "9000",
            "NACC_WAITRESS_THREADS": "3",
        }
        with patch.dict(os.environ, env, clear=True):
            config = load_flask_runtime_config({"START_BACKGROUND_WORKER": False})
            settings = load_waitress_settings()

        self.assertEqual(config["NACC_JOB_ROOT"], "/var/lib/nacc-pipeline/jobs")
        self.assertFalse(config["START_BACKGROUND_WORKER"])
        self.assertTrue(config["SESSION_COOKIE_SECURE"])
        self.assertEqual(settings.port, 9000)
        self.assertEqual(settings.threads, 3)


if __name__ == "__main__":
    unittest.main()

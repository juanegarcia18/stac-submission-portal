import unittest
from pathlib import Path
from types import SimpleNamespace
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from backend.imports_api import flywheel_sdk_uploader as sdk


class FlywheelSdkUploaderTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(sdk, "load_environment_file"))
        self.enterContext(patch.object(sdk, "resolve_flywheel_auth", return_value={
            "token": "test-key", "group": "test-group",
        }))
        self.factory = self.enterContext(patch.object(sdk.flywheel, "Client"))
        self.uploader = sdk.FlywheelSdkUploader(sandbox=True)
        self.client = self.factory.return_value
        self.client.lookup.return_value = Mock(container_type="project", id="sandbox-id")
        self.path = Path(self.enterContext(TemporaryDirectory())) / "sandbox-uds.csv"
        self.path.write_bytes(b"ptid\nTEST\n")
        self.client.get_project_file_info.side_effect = [
            sdk.ApiException(status=404),
            type("File", (), {"name": self.path.name, "size": self.path.stat().st_size})(),
        ]
        self.assertTrue(self.uploader.login())
        self.addCleanup(self.uploader.logout)

    def upload(self):
        return self.uploader.upload_file_to_project(self.path, "sandbox-form")[0]

    def test_success(self):
        self.factory.assert_called_once_with("test-key")
        self.assertEqual(self.upload(), "success")
        self.client.lookup.assert_called_once_with("south-texas/sandbox-form")
        self.client.upload_file_to_project.assert_called_once_with("sandbox-id", str(self.path), signed=False)

    def test_authentication_failure(self):
        self.uploader.logout()
        self.factory.side_effect = RuntimeError()
        self.assertFalse(self.uploader.login())

    def test_smoke_mode_blocks_existing_filename(self):
        self.client.get_project_file_info.side_effect = None
        status, _ = self.uploader.upload_file_to_project(self.path, "sandbox-form", allow_existing=False)
        self.assertEqual(status, "failure")
        self.client.upload_file_to_project.assert_not_called()

    def test_repeat_filename_requires_matching_new_version(self):
        previous = SimpleNamespace(file_id="file-id", version=1, name=self.path.name, size=self.path.stat().st_size)
        uploaded = SimpleNamespace(**{**vars(previous), "version": 2})
        for remote, expected in ((uploaded, "success"), (previous, "uncertain")):
            with self.subTest(expected=expected):
                self.client.get_project_file_info.side_effect = [previous, remote]
                self.client.upload_file_to_project.reset_mock()
                self.client.upload_file_to_project.return_value = [uploaded]
                self.assertEqual(self.upload(), expected)
                self.client.upload_file_to_project.assert_called_once()

    def test_inaccessible_project(self):
        self.client.lookup.side_effect = sdk.ApiException(status=403)
        self.assertEqual(self.upload(), "failure")
        self.client.upload_file_to_project.assert_not_called()

    def test_missing_file(self):
        self.path.unlink()
        self.assertEqual(self.upload(), "failure")
        self.client.lookup.assert_not_called()

    def test_rejected_upload(self):
        self.client.upload_file_to_project.side_effect = sdk.ApiException(status=403)
        self.assertEqual(self.upload(), "failure")
        self.client.upload_file_to_project.assert_called_once()

    def test_timeout(self):
        self.client.upload_file_to_project.side_effect = TimeoutError()
        self.assertEqual(self.upload(), "uncertain")
        self.client.upload_file_to_project.assert_called_once()

    def test_unverified_upload(self):
        self.client.get_project_file_info.side_effect = sdk.ApiException(status=404)
        self.assertEqual(self.upload(), "uncertain")
        self.client.upload_file_to_project.assert_called_once()

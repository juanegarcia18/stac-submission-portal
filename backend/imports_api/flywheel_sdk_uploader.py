"""Upload approved artifacts through the Flywheel Python SDK."""

from pathlib import Path

import flywheel
from flywheel.rest import ApiException

from backend.services.config import (
    load_environment_file,
    resolve_flywheel_auth,
    resolve_flywheel_destination,
)


class FlywheelSdkUploader:
    def __init__(self, *, require_enrollment_destination=False, sandbox=False):
        load_environment_file()
        auth = resolve_flywheel_auth()
        self.token = auth["token"]
        self.group = "south-texas" if sandbox else auth["group"]
        self.project = "sandbox-form" if sandbox else resolve_flywheel_destination("form_submissions")
        self.enroll_project = None if sandbox else resolve_flywheel_destination(
            "enrollment_submissions", required=require_enrollment_destination,
        )
        self.client = None

    def login(self) -> bool:
        if self.client is not None:
            return True
        try:
            self.client = flywheel.Client(self.token)
        except Exception:
            return False
        return True

    def logout(self) -> None:
        client, self.client = self.client, None
        if client is not None:
            try:
                client.shutdown()
            except Exception:
                pass

    def preflight(self, project, filename, *, allow_existing=False):
        if self.client is None or project not in (self.project, self.enroll_project) or not project:
            raise ValueError("Flywheel destination is unavailable.")
        target = self.client.lookup(f"{self.group}/{project}")
        if target is None or target.container_type != "project":
            raise ValueError("Flywheel destination is not a project.")
        try:
            existing = self.client.get_project_file_info(target.id, filename)
        except ApiException as error:
            if error.status == 404:
                return target.id, None
            raise
        if not allow_existing:
            raise FileExistsError("A file with that name already exists in Flywheel.")
        return target.id, existing

    def upload_file_to_project(self, file_path: Path, project: str, *, allow_existing=True) -> tuple[str, str]:
        if not file_path.is_file():
            return "failure", "Local upload file is missing."
        try:
            target_id, existing = self.preflight(project, file_path.name, allow_existing=allow_existing)
        except FileExistsError:
            return "failure", "A file with that name already exists in Flywheel."
        except Exception:
            return "failure", "Could not verify the Flywheel destination."
        try:
            uploaded = self.client.upload_file_to_project(target_id, str(file_path), signed=False)
        except ApiException as error:
            if error.status in (400, 401, 403, 404, 409, 413, 415, 422):
                return "failure", "Flywheel rejected the upload."
            return "uncertain", "Flywheel upload outcome is unknown."
        except Exception:
            return "uncertain", "Flywheel upload outcome is unknown."
        try:
            remote = self.client.get_project_file_info(target_id, file_path.name)
            if remote.name == file_path.name and remote.size == file_path.stat().st_size:
                if existing is None or (
                    remote.file_id and isinstance(remote.version, int)
                    and existing.file_id and isinstance(existing.version, int)
                    and (remote.file_id != existing.file_id or remote.version > existing.version)
                    and any(
                        item.name == remote.name and item.file_id == remote.file_id and item.version == remote.version
                        for item in uploaded
                    )
                ):
                    return "success", ""
        except Exception:
            pass
        return "uncertain", "Could not verify the uploaded file in Flywheel."

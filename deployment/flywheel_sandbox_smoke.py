"""Run with python -m deployment.flywheel_sandbox_smoke."""

import argparse
from pathlib import Path
from tempfile import TemporaryDirectory

from backend.imports_api.flywheel_sdk_uploader import FlywheelSdkUploader

DESTINATION = "fw://south-texas/sandbox-form"
FILENAME = "sandbox-uds.csv"
PAYLOAD = b"ptid\nSYNTHETIC-SDK-SMOKE\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--check", action="store_true", help="Read-only destination and filename check")
    modes.add_argument("--upload", action="store_true", help="Attempt one explicitly approved upload")
    args = parser.parse_args()
    print(DESTINATION, FILENAME, sep=" / ")
    print(PAYLOAD.decode(), end="")
    if not (args.check or args.upload):
        return 0
    if args.upload and input(f"Type {DESTINATION}/{FILENAME} to approve the CSV above: ") != f"{DESTINATION}/{FILENAME}":
        return 1
    try:
        uploader = FlywheelSdkUploader(sandbox=True)
    except Exception:
        print("failure: Flywheel configuration unavailable")
        return 1
    try:
        if not uploader.login():
            print("failure: Flywheel authentication failed")
            return 1
        if args.check:
            try:
                uploader.preflight(uploader.project, FILENAME)
            except FileExistsError:
                print("Project accessible. The filename already exists.")
                return 1
            except Exception:
                print("failure: Could not verify the sandbox destination")
                return 1
            print("Project accessible. No existing file with that name.")
            return 0
        with TemporaryDirectory() as directory:
            path = Path(directory) / FILENAME
            path.write_bytes(PAYLOAD)
            status, detail = uploader.upload_file_to_project(path, uploader.project, allow_existing=False)
        print(status, detail)
        return {"success": 0, "failure": 1, "uncertain": 2}[status]
    finally:
        uploader.logout()


if __name__ == "__main__":
    raise SystemExit(main())

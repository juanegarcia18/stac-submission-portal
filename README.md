# STAC Submission Portal

The Flask application prepares UDSv4 submissions, Enrollment/Transfer Status
submissions, and NACCID imports. Users review protected CSV copies and explicitly
approve the job before it writes to Flywheel or REDCap.

## Local Development

Use Python 3.11 or later. Create the environment with
`python3.11 -m venv stac_submission_portal.venv`, install dependencies with
`stac_submission_portal.venv/bin/python -m pip install -r requirements.txt`, and configure your local environment
using `stac_submission_portal.env.example` as a reference. Keep credentials outside version control.

```bash
NACC_ENABLE_DEV_AUTH=1 stac_submission_portal.venv/bin/python -m flask --app backend.flask_application:create_flask_application run
```

### Environment Configuration

The CLI can use a local environment file:

```text
stac_submission_portal.env
```

This file contains API credentials, REDCap report identifiers, and Flywheel destination information. Deployment environments may provide the same values directly through process environment variables; local dotenv loading does not override existing environment values in the shared backend configuration path.

Example configuration:

```dotenv
# REDCap
BASE_URL=https://your-redcap-instance.edu/api/
UDSV4_TOKEN=your_redcap_api_token

# REDCap Reports
REPORT_ID_UDS=12345
REPORT_ID_M1=12346
REPORT_ID_CLS=12347
REPORT_ID_FTD=12348
REPORT_ID_LBD=12349
REPORT_ID_NPV11=12350
ENROLL_ID=12351
ENROLL_SUBS=12352

# Flywheel
FLYWHEEL_TOKEN=your_flywheel_token
FW_GROUP=your_group
FW_PROJECT=your_form_submission_project
FW_ENROLL=your_enrollment_project
```

Replace all example values with authorized project settings.

**Security:** Never commit `stac_submission_portal.env`, API tokens, credentials, or participant data to GitHub.

### YAML Configuration

Shared declarative configuration lives in:

```text
backend/config/forms.yaml
backend/config/integrations.yaml
```

`backend/config/forms.yaml` defines the six supported submission forms: UDS, MLST, CLS, FTLD, LBD, and NP. It stores REDCap report references, output slugs, matching fields, output renames, and excluded columns. NP is configured as PTID-only matching because it is selected by the one-time `npv11_submit=1` flag rather than a visit number. MLST still removes the final `visitnum` output column after using it for matching. UDS, MLST, CLS, FTLD, and LBD use separate REDCap visit fields because the project tracks each form's visit number independently.

`backend/config/integrations.yaml` stores environment variable references for REDCap and Flywheel integrations, REDCap report identifiers, Flywheel destinations, and NACCID import event, instrument, and ordered columns. It stores only variable names, never tokens or credentials. The backend configuration loader resolves those values from the environment at runtime.

The future backend path requires enrollment uploads and NACCID extraction to resolve through `FW_ENROLL`. This differs from the legacy CLI uploader, which temporarily preserves its filename-based enrollment routing and fallback to `FW_PROJECT` when `FW_ENROLL` is absent so existing workflows continue to run until those callers are migrated.

## Deployment

Install Docker Engine and the Docker Compose plugin. Copy
`stac_submission_portal.env.example` to `stac_submission_portal.env` and
fill in the REDCap and Flywheel settings and a stable `NACC_FLASK_SECRET`.
For local sandbox use, explicitly set `NACC_DEPLOYMENT_ENV=development`,
`NACC_ENABLE_DEV_AUTH=1`, `FW_GROUP=south-texas`, and `FW_PROJECT=sandbox-form`.

Run `./stac_submission_portal_deploy.sh` or pass an alternate environment
file as its only argument. The script builds and starts Waitress and checks
health before reporting the URL. Before redeploying, pause portal use and
upstream traffic until the update completes. The job-state check is a snapshot.

- Open `http://127.0.0.1:8000`.
- Check health at `http://127.0.0.1:8000/healthz`.
- View logs with `docker logs stac-submission-portal`.
- Stop with `docker stop stac-submission-portal`. This retains
  `stac_submission_portal_state`.

Infrastructure manages production DNS, HTTPS, credentials, persistent storage,
and trusted proxy identity. Production requires trusted authentication with
development authentication disabled.

## Tests

Run `python -m unittest discover -s tests -v` from the repository root.

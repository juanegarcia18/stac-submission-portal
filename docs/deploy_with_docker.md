# Docker and Waitress Deployment

This deployment runs one Flask application container. Waitress serves HTTP
inside the container, and an Infrastructure-managed reverse proxy terminates
HTTPS, authenticates users, and forwards requests to Waitress.

## Architecture

```text
Browser -> reverse proxy -> Waitress -> Flask routes
                              |
                              +-- one in-process background worker
                              +-- mounted private state volume
                              +-- REDCap and Flywheel external services
```

Do not run multiple application containers against the same job-state volume.
The current worker model is single-process and limits ETL execution to one
active background job while allowing multiple browser requests.

## Build

Build the STAC Submission Portal image:

```bash
docker build -t stac-submission-portal .
```

## Run

Example local run for smoke testing:

```bash
docker run -d --name stac-submission-portal \
  --env-file stac_submission_portal.env \
  -p 127.0.0.1:8000:8000 \
  -v stac_submission_portal_state:/var/lib/nacc-pipeline \
  -e NACC_DEPLOYMENT_ENV=development \
  -e NACC_ENABLE_DEV_AUTH=1 \
  -e FW_GROUP=south-texas \
  -e FW_PROJECT=sandbox-form \
  stac-submission-portal
```

Health check:

```bash
curl http://127.0.0.1:8000/healthz
```

Production must also provide REDCap and Flywheel settings before workflows can
prepare or commit data.

## Required Environment

Application:

- `NACC_FLASK_SECRET`: stable Flask session secret; required in production.
- `NACC_JOB_ROOT`: job metadata and protected artifacts, default `/var/lib/nacc-pipeline/jobs`.
- `NACC_RUNTIME_ROOT`: REDCap import lock and recovery journal, default `/var/lib/nacc-pipeline/runtime`.
- `NACC_HOST`: Waitress bind host, default `0.0.0.0`.
- `NACC_PORT`: Waitress bind port, default `8000`.
- `NACC_WAITRESS_THREADS`: Waitress request threads, default `4`.
- `NACC_MAX_UPLOAD_BYTES`: browser upload limit, default `5242880`.
- `NACC_LOG_LEVEL`: Python logging level, default `INFO`.

Authentication and proxy:

- `NACC_TRUSTED_AUTH_ENABLED=1`: enable trusted identity-header authentication.
- `NACC_TRUSTED_USER_HEADER`: upstream identity header, default `X-Remote-User`.
- `NACC_TRUST_PROXY_HEADERS=1`: trust proxy forwarding headers only when the proxy is controlled.
- `NACC_SESSION_COOKIE_SECURE=1`: required in production HTTPS deployments.
- `NACC_ENABLE_DEV_AUTH`: development-only; never enable in production.

REDCap and reports:

- `BASE_URL`
- `UDSV4_TOKEN`
- `ENROLL_ID`
- `ENROLL_SUBS`
- `REPORT_ID_UDS`
- `REPORT_ID_M1`
- `REPORT_ID_CLS`
- `REPORT_ID_FTD`
- `REPORT_ID_LBD`
- `REPORT_ID_NPV11`

Flywheel:

- `FLYWHEEL_TOKEN`
- `FW_GROUP`
- `FW_PROJECT`
- `FW_ENROLL`

The backend validates integration variables when a workflow needs them. The
landing page and health endpoint do not require live REDCap or Flywheel access.

## State and Recovery

Mount `/var/lib/nacc-pipeline` as persistent private storage. The Flask job
store keeps metadata, original artifacts, and review artifacts under
`NACC_JOB_ROOT`. REDCap import locking and lock-recovery journals use
`NACC_RUNTIME_ROOT`; this path must not be deleted during ordinary job cleanup.

On restart, interrupted commit jobs are marked `reconciliation_required`.
Automatic retry is intentionally not performed because a lost connection can
leave the external write outcome unknown.

## Reverse Proxy Contract

Infrastructure owns HTTPS termination and institutional authentication. The
proxy must strip any client-supplied identity header, authenticate the request,
then set the configured trusted user header before forwarding to Waitress. Do
not expose Waitress directly to untrusted clients in production.

Enable `NACC_TRUST_PROXY_HEADERS=1` only when the forwarding proxy is trusted.
The Flask app defaults to rejecting anonymous access and does not implement
institutional authentication by itself.

## Flywheel SDK

Uploads authenticate through the Python SDK using `FLYWHEEL_TOKEN`.
Run `python -m deployment.flywheel_sandbox_smoke` to preview the sandbox CSV
and destination, or add `--check` for read-only preflight. Use `--upload`
only after explicit approval of the displayed destination and exact payload.

## Shutdown

Container termination asks the in-process worker to stop. If termination occurs
during an external write and the process exits before completion, restart
recovery marks the job as requiring manual reconciliation instead of retrying.

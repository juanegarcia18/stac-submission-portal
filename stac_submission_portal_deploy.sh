#!/bin/sh
set -eu

fail() {
    printf '%s\n' "$*" >&2
    exit 1
}

root=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
[ "$#" -le 1 ] || fail "Usage: $0 [environment-file]"
case ${1-} in
    '') STAC_PORTAL_ENV_FILE="$root/stac_submission_portal.env" ;;
    /*) STAC_PORTAL_ENV_FILE=$1 ;;
    *) STAC_PORTAL_ENV_FILE="$PWD/$1" ;;
esac
export STAC_PORTAL_ENV_FILE
cd "$root"
[ -f "$STAC_PORTAL_ENV_FILE" ] && [ -r "$STAC_PORTAL_ENV_FILE" ] ||
    fail "Selected environment file is missing or unreadable."
for file in Dockerfile compose.yaml requirements.txt; do
    [ -f "$file" ] || fail "Missing $file."
done
command -v docker >/dev/null || fail "Docker CLI is required."
docker info >/dev/null 2>&1 || fail "Docker Engine is not reachable."
docker compose version >/dev/null 2>&1 || fail "Docker Compose plugin is required."

compose() {
    docker compose --project-name stac-submission-portal --file "$root/compose.yaml" "$@"
}

compose config --quiet >/dev/null 2>&1 || fail "Invalid Compose configuration or environment file."
container=stac-submission-portal
volume=stac_submission_portal_state
existing=$(docker ps -aq --filter "name=^/$container$")
owner=
if [ -n "$existing" ]; then
    owner=$(docker inspect --format '{{index .Config.Labels "com.docker.compose.project"}}' "$existing")
    mounted=$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/var/lib/nacc-pipeline"}}{{.Name}}{{end}}{{end}}' "$existing")
    image=$(docker inspect --format '{{.Config.Image}}' "$existing")
    [ "$mounted" = "$volume" ] && [ "$image" = "$container" ] ||
        fail "Existing container does not match the portal image and state volume."
    case "$owner" in
        ''|'<no value>'|stac-submission-portal) ;;
        *) fail "Existing container belongs to another Compose project." ;;
    esac
fi
if command -v curl >/dev/null && curl --noproxy '*' -sS --max-time 2 http://127.0.0.1:8000/healthz >/dev/null 2>&1; then
    [ -n "$existing" ] && docker port "$existing" 8000/tcp | grep -qx '127.0.0.1:8000' ||
        fail "Port 8000 is already in use."
fi

compose build || fail "Image build failed. Existing container was left untouched."
docker volume create "$volume" >/dev/null || fail "Could not access persistent storage."
compose run --rm --no-deps -T --entrypoint python portal -c '
import os
from backend.services.config import validate_environment
try:
    validate_environment()
except Exception:
    raise SystemExit("Missing or invalid REDCap or Flywheel configuration.")
required = ("BASE_URL", "UDSV4_TOKEN", "FLYWHEEL_TOKEN", "FW_GROUP", "FW_PROJECT", "FW_ENROLL", "NACC_FLASK_SECRET")
for key in required:
    value = os.getenv(key, "").strip().lower()
    if not value or value.startswith(("your_", "replace_", "change_me")) or "example." in value:
        raise SystemExit("Missing or placeholder setting: " + key)
mode = os.getenv("NACC_DEPLOYMENT_ENV", "production").lower()
dev = os.getenv("NACC_ENABLE_DEV_AUTH", "0").lower() in {"1", "true", "yes"}
if mode == "development":
    if not dev or (os.getenv("FW_GROUP"), os.getenv("FW_PROJECT")) != ("south-texas", "sandbox-form"):
        raise SystemExit("Local deployment requires explicit development authentication and south-texas/sandbox-form.")
elif mode != "production" or dev or os.getenv("NACC_TRUSTED_AUTH_ENABLED", "0").lower() not in {"1", "true", "yes"}:
    raise SystemExit("Production requires trusted authentication and development authentication disabled.")
if os.getenv("NACC_PORT") != "8000" or os.getenv("NACC_HOST") != "0.0.0.0":
    raise SystemExit("Container must listen on 0.0.0.0:8000.")
if os.getenv("NACC_JOB_ROOT") != "/var/lib/nacc-pipeline/jobs" or os.getenv("NACC_RUNTIME_ROOT") != "/var/lib/nacc-pipeline/runtime":
    raise SystemExit("Job and recovery paths must use the portal state volume.")
' </dev/null || fail "Deployment settings failed validation."

if [ -n "$existing" ] && [ "$(docker inspect --format '{{.State.Running}}' "$existing")" = true ]; then
    printf '%s\n' "Pause portal use and upstream traffic before updating. Type idle to confirm:"
    read -r answer && [ "$answer" = idle ] || fail "Redeployment cancelled."
    docker exec "$existing" python -c '
import json, os
from pathlib import Path
try:
    root = Path(os.environ["NACC_JOB_ROOT"])
    assert root.resolve() == Path("/var/lib/nacc-pipeline/jobs") and root.is_dir()
    jobs = [json.loads((folder / "job.json").read_text()) for folder in root.iterdir() if folder.is_dir()]
    safe = {"completed", "failed", "cancelled", "reconciliation_required", "awaiting_review"}
    if any(job["status"] not in safe or (job["status"] == "awaiting_review" and job.get("approval")) for job in jobs):
        raise SystemExit("Active or approved queued jobs block deployment.")
    print("Reconciliation-required jobs preserved:", sum(job["status"] == "reconciliation_required" for job in jobs))
except Exception:
    raise SystemExit("Unable to verify job state safely.")
' || fail "Existing portal was left running."
fi

if [ -n "$existing" ] && [ "$owner" != stac-submission-portal ]; then
    docker stop "$existing" >/dev/null
    docker rm "$existing" >/dev/null
fi
compose up --detach --no-build --wait --wait-timeout 90 ||
    fail "Portal did not become healthy. Check container status and review logs privately. The state volume was preserved."

url=http://127.0.0.1:8000
if command -v curl >/dev/null; then
    curl --noproxy '*' -fsS --max-time 5 "$url/healthz" | grep -Eq '"status"[[:space:]]*:[[:space:]]*"ok"' ||
        fail "Container is healthy, but the host loopback endpoint did not respond as expected."
    alias=http://stac-submission-portal.localhost:8000
    if curl --noproxy '*' -fsS --max-time 5 "$alias/healthz" | grep -Eq '"status"[[:space:]]*:[[:space:]]*"ok"'; then
        url=$alias
    fi
fi
printf '\nSTAC Submission Portal deployed successfully.\n\nService: portal\nContainer: %s\nImage: %s\nVolume: %s\nHealth: OK\n\nApplication: %s\n' "$container" "$container" "$volume" "$url"

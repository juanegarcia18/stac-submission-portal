# STAC Submission Portal

STAC Submission Portal is a web application developed by the South Texas Alzheimer's Disease Research Center (STAC) to support research data submissions and management.

The application provides a central place to prepare, review, and approve data transfers between REDCap and Flywheel. It helps reduce manual steps while giving users control over what is submitted.

## What the Application Does

STAC Submission Portal supports three workflows:

- **UDSv4 Submissions:** Prepare and submit supported NACC forms from REDCap to Flywheel.
- **Enrollment and Transfer:** Process participant enrollment and transfer records from REDCap to Flywheel.
- **NACCID Imports:** Retrieve accepted NACCIDs from Flywheel and prepare updates to REDCap.

Each workflow follows a review and approval process before making changes to Flywheel or REDCap. The application maintains job records to support tracking and troubleshooting.

## Project Structure & Configuration

The application separates its backend logic, web interface, configuration, and automated tests.

| Directory | Purpose |
|---|---|
| `backend/` | Application logic, integrations, and data processing |
| `backend/config/` | YAML configuration for forms, integrations, and workflow settings |
| `frontend/` | Web pages, styling, and application assets |
| `tests/` | Automated tests for application behavior and safeguards |

**Configuration Standards**

- Configuration settings belong in YAML files rather than being hardcoded throughout the application.
- `backend/config/forms.yaml` defines form-specific processing rules.
- `backend/config/integrations.yaml` defines integration settings and references to environment variables.
- Credentials, tokens, and environment-specific values belong in `stac_submission_portal.env` or the deployment environment, never in YAML or source code.
- Changes to configuration should not require modifying Python code unless application behavior itself is changing.

## Deployment

The application runs in Docker using Flask, Waitress, and Docker Compose.

### 1. Configure the Environment

Copy the included environment template:

```bash
cp stac_submission_portal.env.example stac_submission_portal.env
```

Update `stac_submission_portal.env` with the authorized REDCap and Flywheel settings, report identifiers, and a stable `NACC_FLASK_SECRET`.

For local sandbox deployment, configure:

```dotenv
NACC_DEPLOYMENT_ENV=development
NACC_ENABLE_DEV_AUTH=1
FW_GROUP=your-groupid
FW_PROJECT=sandbox-form
```

Never commit environment files containing credentials, API tokens, or participant data.

### 2. Start the Application

Ensure Docker Engine and Docker Compose are installed and running.

From the project directory:

```bash
./stac_submission_portal_deploy.sh
```

The deployment script builds the application, validates its configuration, starts the container, and checks application health.

### 3. Access the Application

- **Application:** http://127.0.0.1:8000
- **Health Check:** http://127.0.0.1:8000/healthz

To view logs:

```bash
docker logs stac-submission-portal
```

To stop the application:

```bash
docker stop stac-submission-portal
```

Job records are stored in the persistent `stac_submission_portal_state` Docker volume.

Production deployment requires infrastructure-managed authentication, HTTPS, credentials, and persistent storage. Development authentication must be disabled in production.

## From NACC Weaver to STAC Submission Portal

This project began as **NACC Weaver**, an effort to improve how research data moves between systems.

We shared that early work at the NACC conference in October 2025. Since then, the project has grown from its original approach into a dedicated web application that brings together data preparation, review, approval, and submission.

STAC Submission Portal represents the next chapter of that work, building on what we started with NACC Weaver while making the process easier to manage and use.

---

Developed for the South Texas Alzheimer's Disease Research Center (STAC).
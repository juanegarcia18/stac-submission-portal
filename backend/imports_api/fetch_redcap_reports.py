"""REDCap report retrieval for backend workflows.

This module centralizes authenticated report extraction with retry handling so
Flask workflows do not depend on legacy package paths.
"""

from __future__ import annotations

import json
import logging
import os
import random
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests

from backend.services.config import load_config, load_environment_file, resolve_redcap_connection, resolve_redcap_report

log = logging.getLogger(__name__)

CONFIG = load_config()
load_environment_file()
BASE_URL = resolve_redcap_connection("udsv4", CONFIG)["base_url"]

CACHE_ENABLED = os.getenv("CACHE_API_RESPONSES", "false").lower() in ("1", "true", "yes")
CACHE_DIR = Path(os.getenv("API_CACHE_DIR", Path(__file__).resolve().parents[2] / "logs" / "api_cache"))
if CACHE_ENABLED:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)


def fetch_redcap_report(
    token: str,
    report_id: str,
    *,
    base_url: str = BASE_URL,
    retries: int = 3,
    timeout: int = 90,
    cache_name: str | None = None,
) -> pd.DataFrame:
    """Return one REDCap report as a DataFrame using raw field names."""

    payload = {
        "token": token,
        "content": "report",
        "format": "json",
        "report_id": str(report_id),
        "rawOrLabel": "raw",
        "rawOrLabelHeaders": "raw",
        "returnFormat": "json",
    }
    for attempt in range(1, retries + 1):
        try:
            response = requests.post(base_url, data=payload, timeout=timeout)
            if not response.ok:
                log.error(
                    "REDCap report %s failed: HTTP %s | Response: %s",
                    report_id,
                    response.status_code,
                    response.text[:500],
                )
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, list):
                log.warning("Unexpected REDCap response type for %s: %s", report_id, type(data))
                data = []
            if CACHE_ENABLED and cache_name:
                _cache_response(cache_name, data)
            return pd.DataFrame(data).astype(str) if data else pd.DataFrame()
        except requests.exceptions.RequestException:
            if attempt == retries:
                raise
            wait = (2**attempt) + random.uniform(0, 2)
            log.warning("REDCap request attempt %s/%s failed; retrying in %.1fs", attempt, retries, wait)
            time.sleep(wait)
        except json.JSONDecodeError:
            log.error("Failed to parse REDCap JSON response for report %s", report_id)
            raise


def fetch_form(form_name: str) -> pd.DataFrame:
    """Resolve configured report credentials and fetch one logical report."""

    report = resolve_redcap_report(str(form_name).lower().strip(), CONFIG)
    return fetch_redcap_report(
        report["token"],
        report["report_id"],
        base_url=report["base_url"],
        cache_name=str(form_name).lower().strip(),
    )


def _cache_response(cache_name: str, payload: list[dict]) -> None:
    """Persist one raw REDCap response when API caching is enabled."""

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    cache_path = CACHE_DIR / f"{cache_name}_{timestamp}.json"
    with cache_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
    log.info("Saved REDCap cache file: %s", cache_path)

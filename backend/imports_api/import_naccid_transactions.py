"""Protected REDCap NACCID import transaction.

This module keeps eligibility rechecks, overwrite protection, lock handling,
verification, and recovery journaling in backend-owned code.
"""

from __future__ import annotations

import logging

import pandas as pd
import requests

from backend.imports_api.fetch_redcap_reports import BASE_URL, fetch_form
from backend.services.config import resolve_naccid_import, resolve_redcap_connection
from backend.services.redcap_import_safety import RECOVERY_FILE, import_lock, recover_locks, save_recovery
from backend.services.redcap_locking_operations import get_lock_states, temporary_unlock

log = logging.getLogger(__name__)

NACCID_IMPORT = resolve_naccid_import()
EVENT = NACCID_IMPORT["event"]
INSTRUMENT = NACCID_IMPORT["instrument"]
COLUMNS = NACCID_IMPORT["columns"]
FIELDS = NACCID_IMPORT["fields"]


def import_naccids(records: pd.DataFrame) -> dict:
    """Validate, import, and verify approved NACCID records."""

    summary = {"submitted": 0, "verified": 0, "failed": 0, "skipped": 0}
    if records.empty:
        return summary
    missing = set(COLUMNS) - set(records.columns)
    if missing:
        raise ValueError(f"Missing import columns: {sorted(missing)}")
    rows = _normalize_import_rows(records[COLUMNS].copy())
    if rows.empty:
        return summary
    with import_lock():
        safe_rows = check_pending_eligibility(rows)
        summary["skipped"] += len(rows) - len(safe_rows)
        safe_rows = check_existing_values(safe_rows)
        summary["skipped"] += len(rows) - summary["skipped"] - len(safe_rows)
        if safe_rows.empty:
            return summary
        def execute_import():
            submitted = submit_redcap_import(safe_rows)
            summary["submitted"] = submitted
            summary["verified"] = verify_import_values(safe_rows)
            summary["failed"] = summary["submitted"] - summary["verified"]
            return submitted

        _protected_import(list(safe_rows["ptid"]), execute_import)
    return summary


def fetch_records(ptids: list[str]) -> pd.DataFrame:
    """Read enrollment records directly from REDCap for eligibility checks."""

    if not ptids:
        return pd.DataFrame(columns=COLUMNS)
    token = resolve_redcap_connection("udsv4")["token"]
    payload = {
        "token": token,
        "content": "record",
        "format": "json",
        "type": "flat",
        "events[0]": EVENT,
        "rawOrLabel": "raw",
        "returnFormat": "json",
    }
    payload.update({f"fields[{index}]": field for index, field in enumerate(FIELDS)})
    payload.update({f"records[{index}]": ptid for index, ptid in enumerate(ptids)})
    response = requests.post(BASE_URL, data=payload, timeout=60)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, list):
        raise ValueError("Unexpected REDCap record response.")
    return pd.DataFrame(data)


def check_pending_eligibility(rows: pd.DataFrame) -> pd.DataFrame:
    """Keep only records that remain pending in enrollment checker."""

    pending = fetch_form("enroll_checker")
    required = {"enrl_ptid", "enrl_naccid", "enrl_accepted_date"}
    if pending.empty:
        return rows.iloc[0:0].copy()
    if not required.issubset(pending.columns):
        raise ValueError("Missing REDCap checker columns.")
    normalized = pending.copy()
    normalized[list(required)] = normalized[list(required)].fillna("").astype(str)
    eligible = normalized.loc[
        normalized["enrl_naccid"].str.strip().eq("") & normalized["enrl_accepted_date"].str.strip().eq(""),
        "enrl_ptid",
    ].str.strip()
    return rows[rows["ptid"].isin(eligible)].copy()


def check_existing_values(rows: pd.DataFrame) -> pd.DataFrame:
    """Exclude records that already have accepted date or NACCID."""

    if rows.empty:
        return rows.copy()
    existing = fetch_records(rows["ptid"].tolist())
    required = set(FIELDS) | {"redcap_event_name"}
    if existing.empty or not required.issubset(existing.columns):
        raise ValueError("REDCap record response is missing required fields.")
    existing = existing.fillna("").astype(str)
    existing = existing[existing["redcap_event_name"].eq(EVENT)]
    if existing["ptid"].duplicated().any():
        raise ValueError("Duplicate REDCap records returned.")
    eligible = existing.loc[
        existing["ptid"].eq(existing["enrl_ptid"])
        & existing["enrl_naccid"].str.strip().eq("")
        & existing["enrl_accepted_date"].str.strip().eq(""),
        "ptid",
    ]
    return rows[rows["ptid"].isin(eligible)].copy()


def submit_redcap_import(rows: pd.DataFrame) -> int:
    """Submit import payload rows to REDCap and validate submission count."""

    token = resolve_redcap_connection("udsv4")["token"]
    payload = {
        "token": token,
        "content": "record",
        "action": "import",
        "format": "csv",
        "type": "flat",
        "overwriteBehavior": "normal",
        "forceAutoNumber": "false",
        "data": rows.to_csv(index=False),
        "returnContent": "count",
        "returnFormat": "json",
    }
    response = requests.post(BASE_URL, data=payload, timeout=90)
    response.raise_for_status()
    result = response.json()
    count = int(result.get("count", 0))
    if count != len(rows):
        raise ValueError(f"REDCap import count mismatch: expected {len(rows)}, got {count}")
    return count


def verify_import_values(rows: pd.DataFrame) -> int:
    """Verify accepted-date and NACCID values after import."""

    actual = fetch_records(rows["ptid"].tolist())
    required = {"ptid", "enrl_naccid", "enrl_accepted_date"}
    if actual.empty or not required.issubset(actual.columns):
        raise ValueError("REDCap verification response is incomplete.")
    if actual["ptid"].duplicated().any():
        raise ValueError("Duplicate REDCap verification records.")
    actual = actual.fillna("").astype(str).set_index("ptid")
    verified = 0
    for _, row in rows.iterrows():
        ptid = row["ptid"]
        if ptid not in actual.index:
            continue
        record = actual.loc[ptid]
        if all(record[field] == row[field] for field in ("enrl_naccid", "enrl_accepted_date")):
            verified += 1
    return verified


def _protected_import(ptids: list[str], action) -> int:
    """Run import while preserving lock recovery journal semantics."""

    if RECOVERY_FILE.exists():
        raise RuntimeError("An unresolved recovery journal exists. Resolve it before starting another enrollment import.")
    states = get_lock_states(ptids, EVENT, INSTRUMENT)
    save_recovery(states, EVENT, INSTRUMENT)
    try:
        with temporary_unlock(ptids, EVENT, INSTRUMENT, states=states):
            submitted = action()
    except BaseException:
        try:
            recover_locks()
        except Exception:
            log.exception("Automatic recovery failed. Recovery journal preserved.")
        raise
    RECOVERY_FILE.unlink()
    return submitted


def _normalize_import_rows(rows: pd.DataFrame) -> pd.DataFrame:
    """Normalize string fields and drop invalid rows before commit."""

    rows = rows.copy()
    for column in COLUMNS:
        rows[column] = rows[column].fillna("").astype(str).str.strip()
    if rows.eq("").any().any():
        raise ValueError("Import contains blank required values.")
    if rows["ptid"].duplicated().any():
        raise ValueError("Duplicate PTIDs in approved import payload.")
    if not rows["redcap_event_name"].eq(EVENT).all():
        raise ValueError("Import payload has unexpected REDCap event values.")
    if not rows["ptid"].eq(rows["enrl_ptid"]).all():
        raise ValueError("Enrollment PTID mismatch in approved import payload.")
    return rows

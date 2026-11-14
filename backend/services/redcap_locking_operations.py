"""REDCap locking API operations for protected NACCID imports.

These helpers preserve the existing lock/unlock/verify behavior used by import
protection and recovery services.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Iterator

import requests

from backend.services.config import load_config, load_environment_file, resolve_redcap_connection

log = logging.getLogger(__name__)

CONFIG = load_config()
load_environment_file()
REDCAP = resolve_redcap_connection("udsv4", CONFIG)
LOCK_API = f"{REDCAP['base_url'].rstrip('/')}/?NOAUTH=&type=module&prefix=locking_api&page="
TIMEOUT = 30
VALID_ACTIONS = {"status", "lock", "unlock"}


def lock_api_request(ptid: str, event: str, instrument: str, action: str) -> list[dict]:
    """Send one authenticated request to the REDCap locking API module."""

    if action not in VALID_ACTIONS:
        raise ValueError(f"Unsupported locking action: {action}")
    payload = {
        "token": REDCAP["token"],
        "returnFormat": "json",
        "record": str(ptid),
        "event": event,
        "instrument": instrument,
    }
    response = requests.post(LOCK_API + action, data=payload, timeout=TIMEOUT)
    response.raise_for_status()
    return response.json()


def get_lock_status(ptid: str, event: str, instrument: str) -> bool:
    """Return whether the instrument is currently locked."""

    result = lock_api_request(ptid, event, instrument, "status")
    if not isinstance(result, list) or len(result) != 1:
        raise ValueError(f"Unexpected lock status response: PTID {ptid}")
    status = str(result[0].get("locked", ""))
    if status not in {"0", "1"}:
        raise ValueError(f"Invalid lock status for PTID {ptid}: {status}")
    return status == "1"


def get_lock_states(ptids: list[str], event: str, instrument: str) -> dict[str, bool]:
    """Capture original lock states for a set of PTIDs."""

    states = {}
    for ptid in dict.fromkeys(map(str, ptids)):
        states[ptid] = get_lock_status(ptid, event, instrument)
    return states


def restore_locks(states: dict[str, bool], event: str, instrument: str, ptids: list[str] | None = None) -> None:
    """Restore and verify originally locked instruments."""

    targets = list(states) if ptids is None else list(ptids)
    failures = []
    for ptid in targets:
        if not states.get(ptid, False):
            continue
        try:
            if not get_lock_status(ptid, event, instrument):
                lock_api_request(ptid, event, instrument, "lock")
            if not get_lock_status(ptid, event, instrument):
                raise RuntimeError("Lock verification failed.")
        except Exception:
            failures.append(ptid)
    if failures:
        raise RuntimeError(f"Lock restoration failed for PTIDs: {failures}")


def unlock_records(states: dict[str, bool], event: str, instrument: str) -> list[str]:
    """Unlock originally locked records and verify each unlock."""

    unlocked = []
    try:
        for ptid, was_locked in states.items():
            if not was_locked:
                continue
            if not get_lock_status(ptid, event, instrument):
                raise RuntimeError(f"Original lock state changed: PTID {ptid}")
            unlocked.append(ptid)
            lock_api_request(ptid, event, instrument, "unlock")
            if get_lock_status(ptid, event, instrument):
                raise RuntimeError(f"Unlock verification failed: PTID {ptid}")
    except Exception:
        restore_locks(states, event, instrument, ptids=unlocked)
        raise
    return unlocked


@contextmanager
def temporary_unlock(
    ptids: list[str],
    event: str,
    instrument: str,
    *,
    states: dict[str, bool] | None = None,
) -> Iterator[dict[str, bool]]:
    """Unlock instruments for import and always restore original lock state."""

    known_states = states or get_lock_states(ptids, event, instrument)
    if set(map(str, ptids)) != set(known_states):
        raise ValueError("Provided lock states do not match requested PTIDs.")
    try:
        unlock_records(known_states, event, instrument)
        yield known_states
    except BaseException:
        try:
            restore_locks(known_states, event, instrument)
        except Exception:
            log.exception("Lock restoration failed while handling an earlier error.")
        raise
    else:
        restore_locks(known_states, event, instrument)

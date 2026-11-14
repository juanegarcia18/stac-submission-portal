"""Process lock and recovery journal safeguards for REDCap imports.

Import transactions use one machine-local lock file and one recovery journal so
interrupted lock restoration can be reconciled safely on restart.
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from backend.services.redcap_locking_operations import get_lock_status, restore_locks

BASE_DIR = Path(__file__).resolve().parents[2]
RUNTIME_DIR = Path(os.getenv("NACC_RUNTIME_ROOT", str(BASE_DIR / ".runtime")))
LOCK_FILE = RUNTIME_DIR / "import.lock"
RECOVERY_FILE = RUNTIME_DIR / "recovery.json"


@contextmanager
def import_lock() -> Iterator[None]:
    """Prevent concurrent NACCID imports within one runtime instance."""

    RUNTIME_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(LOCK_FILE, os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "r+") as lock_handle:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Another enrollment import is already running.") from exc
        try:
            yield
        finally:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def save_recovery(states: dict[str, bool], event: str, instrument: str) -> None:
    """Atomically persist original lock states before unlocking records."""

    if not states:
        raise ValueError("Cannot create a recovery journal without lock states.")
    RUNTIME_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    if RECOVERY_FILE.exists():
        raise RuntimeError("An unresolved recovery journal already exists.")
    journal = {
        "event": event,
        "instrument": instrument,
        "original_states": states,
        "status": "pending_recovery",
    }
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=RUNTIME_DIR,
            prefix=".recovery_",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_path = Path(temp_file.name)
            temp_path.chmod(0o600)
            json.dump(journal, temp_file, indent=2)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.link(temp_path, RECOVERY_FILE)
        dir_fd = os.open(RUNTIME_DIR, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def load_recovery() -> dict | None:
    """Return the current recovery journal payload, if it exists."""

    if not RECOVERY_FILE.exists():
        return None
    with RECOVERY_FILE.open("r", encoding="utf-8") as handle:
        journal = json.load(handle)
    _validate_recovery_journal(journal)
    return journal


def recover_locks() -> int:
    """Restore lock states from the saved recovery journal."""

    journal = load_recovery()
    if journal is None:
        return 0
    event = journal["event"]
    instrument = journal["instrument"]
    states = journal["original_states"]
    locked_states = {ptid: locked for ptid, locked in states.items() if locked}
    restore_locks(locked_states, event, instrument)
    for ptid in locked_states:
        if not get_lock_status(ptid, event, instrument):
            raise RuntimeError(f"Recovery verification failed for PTID {ptid}.")
    RECOVERY_FILE.unlink()
    return len(locked_states)


def _validate_recovery_journal(journal: dict) -> None:
    """Ensure journal structure is complete before recovery operations."""

    required = {"event", "instrument", "original_states", "status"}
    if not isinstance(journal, dict) or not required.issubset(journal):
        raise ValueError("Recovery journal is missing required information.")
    states = journal["original_states"]
    if (
        journal["status"] != "pending_recovery"
        or not isinstance(journal["event"], str)
        or not journal["event"]
        or not isinstance(journal["instrument"], str)
        or not journal["instrument"]
        or not isinstance(states, dict)
        or not states
        or any(not isinstance(ptid, str) or not ptid or type(locked) is not bool for ptid, locked in states.items())
    ):
        raise ValueError("Recovery journal contains invalid values.")

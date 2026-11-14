"""Backend entry points for REDCap lock recovery.

The recovery journal is managed by backend runtime services and must remain
independent of ordinary job workspace cleanup. These wrappers keep recovery
callable from startup code with consistent reconciliation semantics.
"""

from __future__ import annotations

import re

from backend.imports_api.external_write_results import NO_DATA, SUCCESS, UNCERTAIN, RedcapImportResult


def load_redcap_recovery_journal() -> dict | None:
    """Return the unresolved REDCap recovery journal, if one exists."""

    from backend.services.redcap_import_safety import load_recovery

    return load_recovery()


def recover_redcap_locks() -> RedcapImportResult:
    """Restore REDCap locks from the saved journal under the import lock."""

    from backend.services.redcap_import_safety import import_lock, recover_locks

    try:
        with import_lock():
            restored = recover_locks()
    except Exception as exc:
        return RedcapImportResult(
            UNCERTAIN,
            attempted=0,
            requires_reconciliation=True,
            message=_sanitize_recovery_error(exc),
        )

    if restored == 0:
        return RedcapImportResult(NO_DATA, attempted=0, message="No recovery journal found.")
    return RedcapImportResult(SUCCESS, attempted=restored, submitted=0, verified=restored)


def _sanitize_recovery_error(exc: Exception) -> str:
    """Report recovery failure without leaking participant-level details."""

    message = str(exc).splitlines()[0].strip()
    if not message:
        return exc.__class__.__name__
    message = re.sub(r"\b\d{3,}\b", "[id]", message)
    return message[:180]

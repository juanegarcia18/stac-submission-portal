"""Structured outcomes for approved external write operations.

Statuses are intentionally small and display-oriented:
``success`` completed and verified as far as the workflow allows,
``failure`` is a known failed write, ``no_data`` means nothing was attempted,
``partial`` means some writes succeeded and others did not, and ``uncertain``
means the remote outcome cannot be safely inferred from the local exception.
"""

from __future__ import annotations

from dataclasses import dataclass, field

SUCCESS = "success"
FAILURE = "failure"
NO_DATA = "no_data"
PARTIAL = "partial"
UNCERTAIN = "uncertain"


@dataclass(frozen=True)
class FileUploadResult:
    """Outcome for one approved Flywheel file upload attempt."""

    file_name: str
    destination: str
    status: str
    message: str = ""


@dataclass(frozen=True)
class FlywheelUploadResult:
    """Batch outcome for approved Flywheel uploads."""

    status: str
    attempted: int
    succeeded: int = 0
    failed: int = 0
    uncertain: int = 0
    results: tuple[FileUploadResult, ...] = field(default_factory=tuple)
    message: str = ""

    @property
    def requires_reconciliation(self) -> bool:
        """Return True when a remote upload may have completed unknown locally."""

        return self.uncertain > 0


@dataclass(frozen=True)
class RedcapImportResult:
    """Outcome for an approved REDCap record import."""

    status: str
    attempted: int
    submitted: int = 0
    verified: int = 0
    skipped: int = 0
    failed: int = 0
    requires_reconciliation: bool = False
    message: str = ""

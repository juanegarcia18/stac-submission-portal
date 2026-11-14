"""Structured preparation outcomes for backend workflow review steps.

Preparation may read external systems and create local artifacts, but it never
commits external writes. These results give future Flask code enough detail to
show reviewable artifacts or payloads without inspecting legacy return values.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

SUCCESS = "success"
NO_DATA = "no_data"
PARTIAL = "partial"
VALIDATION_FAILED = "validation_failed"
PROCESSING_FAILED = "processing_failed"


@dataclass(frozen=True)
class ModulePreparationSummary:
    """Review metadata for one selected submission module.

    Counts are record counts, not file counts. ``artifact_name`` is the basename
    of the prepared CSV so Flask can safely associate it with the protected
    review artifact reference stored in job metadata.
    """

    module: str
    requested_records: int | None = None
    prepared_records: int = 0
    status: str = NO_DATA
    artifact_name: str = ""
    findings: tuple[str, ...] = field(default_factory=tuple)

    def as_metadata(self) -> dict:
        """Return JSON-safe fields for persisted job metadata."""

        return {
            "module": self.module,
            "requested_records": self.requested_records,
            "prepared_records": self.prepared_records,
            "status": self.status,
            "artifact_name": self.artifact_name,
            "findings": list(self.findings),
        }


@dataclass(frozen=True)
class WorkflowPreparationResult:
    """Outcome from a read-only workflow preparation step."""

    workflow: str
    status: str
    prepared_records: int = 0
    workspace: Path | None = None
    artifacts: tuple[Path, ...] = ()
    review_artifacts: tuple[Path, ...] = ()
    payload: pd.DataFrame | None = None
    forms_prepared: tuple[str, ...] = ()
    forms_without_output: tuple[str, ...] = ()
    warnings: tuple[str, ...] = field(default_factory=tuple)
    module_summaries: tuple[ModulePreparationSummary, ...] = field(default_factory=tuple)
    error: str = ""

    @property
    def approval_ready(self) -> bool:
        """Return True when the result contains data that can be reviewed."""

        return self.status in {SUCCESS, PARTIAL} and (
            bool(self.artifacts) or (self.payload is not None and not self.payload.empty)
        )

"""Prepare, validate, and clean private workspace files for pipeline jobs.

Generated originals stay in the private workspace and are the only files passed
to external commits. Review copies are disposable duplicates for manual QC, so
editing them cannot alter the approved originals.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def prepare_private_workspace(prefix: str = "nacc_submission_") -> Path:
    """Create a restrictive temporary directory for one workflow."""

    workspace = Path(tempfile.mkdtemp(prefix=prefix))
    workspace.chmod(0o700)
    return workspace


def prepare_review_files(original_files: Iterable[Path], workspace: Path) -> list[Path]:
    """Copy approved originals to a separate review directory."""

    root = _validated_workspace(workspace)
    review_dir = root / "review"
    review_dir.mkdir(mode=0o700, exist_ok=True)
    review_files = []
    for original in original_files:
        source = validate_workspace_path(original, root)
        review_path = review_dir / source.name
        # Manual reviewers can edit this copy without changing the original
        # artifact that a later approved commit will upload or import.
        shutil.copy2(source, review_path)
        review_path.chmod(0o600)
        review_files.append(review_path)
    return review_files


def validate_workspace_path(path: Path, workspace: Path) -> Path:
    """Reject paths outside the private workspace."""

    root = _validated_workspace(workspace)
    root_resolved = root.resolve()
    target = Path(path)
    target_resolved = target.resolve()
    if not target_resolved.is_file() or root_resolved not in (target_resolved, *target_resolved.parents):
        raise ValueError("File is outside the approved workspace.")
    return target


def validate_original_files(paths: Iterable[Path], workspace: Path | None = None) -> list[Path]:
    """Return original artifact paths and reject disposable review copies."""

    originals = [Path(path) for path in paths]
    if any("review" in path.parts for path in originals):
        raise ValueError("Review copies cannot be committed.")
    if workspace is None:
        return originals
    return [validate_workspace_path(path, workspace) for path in originals]


def cleanup_job_workspace(workspace: Path, preserve_paths: Iterable[Path] = ()) -> None:
    """Remove disposable job files while preserving requested artifacts."""

    root = _validated_workspace(workspace)
    preserved = {validate_workspace_path(path, root).resolve() for path in preserve_paths}
    if not preserved:
        shutil.rmtree(root)
        return

    for child in list(root.iterdir()):
        if _contains_preserved_path(child.resolve(), preserved):
            continue
        _remove_path(child)


def _validated_workspace(workspace: Path) -> Path:
    """Ensure cleanup and review operations stay within a real temp workspace."""

    root = Path(workspace)
    if not root.exists() or not root.is_dir():
        raise ValueError("Workspace does not exist.")
    resolved = root.resolve()
    if resolved in {Path("/").resolve(), Path.home().resolve(), PROJECT_ROOT.resolve()}:
        raise ValueError("Refusing to operate on an unsafe workspace.")
    return root


def _contains_preserved_path(path: Path, preserved: set[Path]) -> bool:
    """Return True when a path is, or contains, a preserved artifact."""

    return any(path == item or path in item.parents for item in preserved)


def _remove_path(path: Path) -> None:
    """Remove a file or directory under an already validated workspace."""

    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)

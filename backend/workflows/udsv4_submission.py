"""Prepare and commit UDSv4 form submission artifacts.

The workflow coordinates the existing YAML form configuration, REDCap report
fetching, shared matching transforms, and approved Flywheel uploads. It has no
terminal prompts and does not upload during preparation.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

import pandas as pd

from backend.imports_api.upload_flywheel_files import FORM_SUBMISSION_DESTINATION, upload_flywheel_files
from backend.services.config import ConfigurationError, get_selected_form_configs
from backend.services.job_workspace_files import prepare_private_workspace, prepare_review_files, validate_original_files
from backend.services.validate_job_approval import ApprovalAuthorization
from backend.transforms.match_submission_records import filter_submission_records, match_submission_report
from backend.transforms.prepare_output_columns import (
    drop_configured_final_output_columns,
    find_first_existing_column,
    prepare_submission_output_columns,
)
from backend.transforms.prepare_participant_ids import normalize_submission_identifier_series
from backend.workflows.prepare_workflow_results import (
    ModulePreparationSummary,
    NO_DATA,
    PARTIAL,
    PROCESSING_FAILED,
    SUCCESS,
    VALIDATION_FAILED,
    WorkflowPreparationResult,
)

WORKFLOW = "udsv4_submission"


def prepare_udsv4_submission(
    *,
    selected_forms: Iterable[str] | None = None,
    filter_csv: Path | None = None,
    filter_df: pd.DataFrame | None = None,
    workspace: Path | None = None,
    report_fetcher: Callable[[str], pd.DataFrame] | None = None,
) -> WorkflowPreparationResult:
    """Prepare selected UDSv4 form CSV artifacts for manual review."""

    workspace = Path(workspace) if workspace else prepare_private_workspace()
    try:
        form_configs = get_selected_form_configs(selected_forms)
        filter_data = _load_submission_filter(filter_csv, filter_df)
        result = _prepare_selected_forms(form_configs, filter_data, workspace, _resolve_report_fetcher(report_fetcher))
    except ConfigurationError as exc:
        return _preparation_error(VALIDATION_FAILED, workspace, exc)
    except ValueError as exc:
        return _preparation_error(VALIDATION_FAILED, workspace, exc)
    except Exception as exc:
        return _preparation_error(PROCESSING_FAILED, workspace, exc)

    return _with_review_files(result, workspace)


def commit_udsv4_submission(
    artifacts: Iterable[Path],
    approval: ApprovalAuthorization,
    *,
    workspace: Path | None = None,
    uploader_factory: Callable | None = None,
    expected_destination: str | None = None,
):
    """Upload approved original UDSv4 artifacts to the form-submission project."""

    originals = validate_original_files(artifacts, workspace)
    return upload_flywheel_files(
        originals,
        FORM_SUBMISSION_DESTINATION,
        approval,
        uploader_factory=uploader_factory,
        expected_destination=expected_destination,
    )


def _load_submission_filter(filter_csv: Path | None, filter_df: pd.DataFrame | None) -> pd.DataFrame:
    """Load a submission filter from an uploaded DataFrame or CSV artifact."""

    if filter_df is not None:
        data = filter_df.copy()
    elif filter_csv is not None and Path(filter_csv).exists():
        data = pd.read_csv(filter_csv, dtype=str)
    else:
        raise ValueError("Submission filter data is required.")
    if data.empty:
        raise ValueError("Submission filter contains no records.")
    if "ptid" not in data.columns:
        raise ValueError("Submission filter is missing required column: ptid")
    return data


def _resolve_report_fetcher(report_fetcher: Callable[[str], pd.DataFrame] | None):
    """Load the backend REDCap report fetcher only when preparation runs."""

    if report_fetcher is not None:
        return report_fetcher
    from backend.imports_api.fetch_redcap_reports import fetch_form

    return fetch_form


def _prepare_selected_forms(form_configs, filter_df, workspace, report_fetcher) -> WorkflowPreparationResult:
    """Process selected forms independently so partial output is preserved."""

    artifacts, prepared, missing, warnings, summaries = [], [], [], [], []
    for form_name, config in form_configs.items():
        try:
            path, summary = _prepare_one_form(form_name, config, filter_df, workspace, report_fetcher)
        except ValueError as exc:
            missing.append(form_name)
            warnings.append(f"{form_name}: {_sanitize_message(exc)}")
            summaries.append(_summary_from_error(form_name, exc))
            continue
        summaries.append(summary)
        if path is None or summary.prepared_records == 0:
            missing.append(form_name)
            continue
        artifacts.append(path)
        prepared.append(form_name)
    status = _preparation_status(prepared, missing, summaries)
    return WorkflowPreparationResult(
        WORKFLOW,
        status,
        sum(item.prepared_records for item in summaries),
        workspace,
        tuple(artifacts),
        forms_prepared=tuple(prepared),
        forms_without_output=tuple(missing),
        warnings=tuple(warnings),
        module_summaries=tuple(summaries),
    )


def _prepare_one_form(form_name: str, config: dict, filter_df: pd.DataFrame, workspace: Path, report_fetcher):
    """Prepare one configured form using matching and output transforms."""

    requested = filter_submission_records(filter_df, form_name, config)
    requested_count = _requested_record_count(requested, form_name, config)
    if requested.empty:
        return None, ModulePreparationSummary(
            form_name,
            requested_records=0,
            prepared_records=0,
            status=NO_DATA,
            findings=(f"{form_name}: No eligible rows in submission filter.",),
        )
    report = report_fetcher(config["api_form"])
    if report.empty:
        return None, ModulePreparationSummary(
            form_name,
            requested_records=requested_count,
            prepared_records=0,
            status=NO_DATA,
            findings=(f"{form_name}: REDCap report returned no records.",),
        )
    matched = match_submission_report(requested, report, form_name, config)
    findings = _diagnose_unmatched_requests(requested, report, matched, form_name, config)
    if matched.empty:
        return None, ModulePreparationSummary(
            form_name,
            requested_records=requested_count,
            prepared_records=0,
            status=NO_DATA,
            findings=tuple(findings or ["No matching records were prepared."]),
        )
    output = prepare_submission_output_columns(matched, form_name, config)
    output = drop_configured_final_output_columns(output, config)
    path = _write_submission_artifact(output, config, workspace)
    status = SUCCESS if len(output) == requested_count else PARTIAL
    return path, ModulePreparationSummary(
        form_name,
        requested_records=requested_count,
        prepared_records=len(output),
        status=status,
        artifact_name=path.name,
        findings=tuple(findings),
    )


def _write_submission_artifact(output: pd.DataFrame, config: dict, workspace: Path) -> Path:
    """Write one original UDSv4 CSV artifact in the legacy filename format."""

    workspace.mkdir(parents=True, exist_ok=True)
    month_year = datetime.now().strftime("%b_%Y")
    output_file = workspace / f"{month_year}-nacc-{config['slug']}.csv"
    output.to_csv(output_file, index=False)
    return output_file


def _with_review_files(result: WorkflowPreparationResult, workspace: Path) -> WorkflowPreparationResult:
    """Attach disposable review copies without replacing original artifacts."""

    if not result.artifacts:
        return result
    review_files = prepare_review_files(result.artifacts, workspace)
    return WorkflowPreparationResult(**{**result.__dict__, "review_artifacts": tuple(review_files)})


def _preparation_status(prepared: list[str], missing: list[str], summaries: list[ModulePreparationSummary]) -> str:
    """Summarize form-level preparation outcomes."""

    if any(summary.status == PARTIAL for summary in summaries):
        return PARTIAL
    if prepared and missing:
        return PARTIAL
    if prepared:
        return SUCCESS
    return NO_DATA


def _requested_record_count(requested: pd.DataFrame, form_name: str, config: dict) -> int:
    """Count distinct requested records using the form's configured match key."""

    if requested.empty:
        return 0
    columns = ["ptid"] if form_name == "NP" else ["ptid", config["filter_visitnum_col"]]
    return len(requested.loc[:, columns].dropna().drop_duplicates())


def _diagnose_unmatched_requests(requested, report, matched, form_name: str, config: dict) -> list[str]:
    """Explain unmatched selected records using already-fetched REDCap data.

    Diagnostics intentionally report aggregate counts rather than PTIDs so job
    status pages and logs can support QC without exposing identifiers.
    """

    report_ptid_col = find_first_existing_column(report, config["report_ptid_cols"])
    report_visit_col = find_first_existing_column(report, config.get("report_visitnum_cols", []))
    if not report_ptid_col or (form_name != "NP" and not report_visit_col):
        return []

    requested_keys = _request_key_frame(requested, form_name, config)
    matched_keys = _matched_key_frame(matched, report_ptid_col, report_visit_col, form_name)
    unmatched = _left_unmatched(requested_keys, matched_keys, form_name)
    if unmatched.empty:
        return []

    report_keys = _report_key_frame(report, report_ptid_col, report_visit_col, form_name)
    missing_ptids = unmatched.loc[~unmatched["ptid"].isin(set(report_keys["ptid"]))]
    findings = _unmatched_findings(form_name, len(unmatched), len(missing_ptids))
    if form_name != "NP":
        report_ptids = set(report_keys["ptid"])
        report_visits = set(zip(report_keys["ptid"], report_keys["visitnum"]))
        missing_visits = unmatched.loc[
            unmatched["ptid"].isin(report_ptids)
            & ~unmatched.apply(lambda row: (row["ptid"], row["visitnum"]) in report_visits, axis=1)
        ]
        if len(missing_visits):
            findings.append(f"{form_name}: {len(missing_visits)} requested record(s) had no matching visit in the REDCap report.")
    return findings


def _request_key_frame(requested: pd.DataFrame, form_name: str, config: dict) -> pd.DataFrame:
    """Return unique normalized keys requested by the uploaded filter."""

    keys = pd.DataFrame({"ptid": normalize_submission_identifier_series(requested["ptid"])})
    if form_name != "NP":
        keys["visitnum"] = normalize_submission_identifier_series(requested[config["filter_visitnum_col"]])
    return keys.dropna().drop_duplicates()


def _matched_key_frame(matched: pd.DataFrame, ptid_col: str, visit_col: str | None, form_name: str) -> pd.DataFrame:
    """Return unique normalized keys that were actually prepared."""

    keys = pd.DataFrame({"ptid": normalize_submission_identifier_series(matched[ptid_col])})
    if form_name != "NP":
        keys["visitnum"] = normalize_submission_identifier_series(matched[visit_col])
    return keys.dropna().drop_duplicates()


def _report_key_frame(report: pd.DataFrame, ptid_col: str, visit_col: str | None, form_name: str) -> pd.DataFrame:
    """Return unique normalized REDCap report keys available for matching."""

    keys = pd.DataFrame({"ptid": normalize_submission_identifier_series(report[ptid_col])})
    if form_name != "NP":
        keys["visitnum"] = normalize_submission_identifier_series(report[visit_col])
    return keys.dropna().drop_duplicates()


def _left_unmatched(requested_keys: pd.DataFrame, matched_keys: pd.DataFrame, form_name: str) -> pd.DataFrame:
    """Return requested keys that were not present in the prepared output."""

    columns = ["ptid"] if form_name == "NP" else ["ptid", "visitnum"]
    merged = requested_keys.merge(matched_keys, on=columns, how="left", indicator=True)
    return merged.loc[merged["_merge"].eq("left_only"), columns]


def _unmatched_findings(form_name: str, unmatched_count: int, missing_ptid_count: int) -> list[str]:
    """Build aggregate QC findings for unmatched request keys."""

    findings = [f"{form_name}: {unmatched_count} requested record(s) were not prepared."]
    if missing_ptid_count:
        findings.append(f"{form_name}: {missing_ptid_count} requested participant(s) were not found in the REDCap report.")
    return findings


def _summary_from_error(form_name: str, exc: Exception) -> ModulePreparationSummary:
    """Preserve selected-module visibility when a module prepares no output."""

    return ModulePreparationSummary(
        form_name,
        requested_records=0,
        prepared_records=0,
        status=NO_DATA,
        findings=(f"{form_name}: {_sanitize_message(exc)}",),
    )


def _preparation_error(status: str, workspace: Path, exc: Exception) -> WorkflowPreparationResult:
    """Return a sanitized preparation failure result."""

    return WorkflowPreparationResult(WORKFLOW, status, workspace=workspace, error=_sanitize_message(exc))


def _sanitize_message(exc: Exception) -> str:
    """Remove credentials and participant-like identifiers from status messages."""

    message = str(exc).splitlines()[0].strip() or exc.__class__.__name__
    if any(word in message.lower() for word in ("token", "authorization", "password", "secret")):
        return f"{exc.__class__.__name__}: sensitive details omitted."
    message = re.sub(r"\b\d{3,}\b", "[id]", message)
    message = re.sub(r"NACC[-_A-Za-z0-9]+", "[id]", message)
    return message[:180]

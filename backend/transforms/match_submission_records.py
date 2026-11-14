"""Filter and match UDSv4 submission records.

These transformations apply the form mappings from ``backend/config/forms.yaml``. NP is
kept on its PTID-only path, while UDS, MLST, CLS, FTLD, and LBD use their
configured REDCap visit fields for participant/visit matching.
"""

from __future__ import annotations

import pandas as pd

from backend.transforms.prepare_output_columns import find_first_existing_column
from backend.transforms.prepare_participant_ids import (
    normalize_submission_identifier_series,
)


def filter_submission_records(filter_df: pd.DataFrame, form_name: str, config: dict) -> pd.DataFrame:
    """Return requested UDSv4 submissions from the user-supplied filter CSV.

    NP is a one-time form selected with ``npv11_submit=1`` and therefore does
    not use visit-number matching. Other forms require their configured visit
    field because REDCap stores form-specific visit columns.
    """

    _validate_submission_filter_columns(filter_df, form_name, config)
    filtered = filter_df.copy()
    filtered["ptid"] = normalize_submission_identifier_series(filtered["ptid"])

    if form_name == "NP":
        flags = normalize_submission_identifier_series(filtered[config["submit_flag_col"]])
        return filtered.loc[flags.eq(config.get("submit_flag_value", "1")).fillna(False) & filtered["ptid"].notna()].copy()

    visit_col = config["filter_visitnum_col"]
    filtered[visit_col] = normalize_submission_identifier_series(filtered[visit_col])
    return filtered.loc[filtered[visit_col].notna()].copy()


def match_submission_report(
    form_filter: pd.DataFrame,
    report_df: pd.DataFrame,
    form_name: str,
    config: dict,
) -> pd.DataFrame:
    """Match requested UDSv4 submissions against a REDCap report.

    Inputs are the already-filtered request rows, the raw REDCap report, a form
    name, and its YAML-derived config. The returned frame has temporary match
    columns removed and preserves REDCap report column order.
    """

    report_ptid_col = _resolve_report_ptid_column(report_df, form_name, config)
    report_visit_col = _resolve_report_visit_column(report_df, form_name, config)
    report = report_df.copy()
    report[report_ptid_col] = normalize_submission_identifier_series(report[report_ptid_col])
    if form_name != "NP":
        report[report_visit_col] = normalize_submission_identifier_series(report[report_visit_col])

    keyed_filter, keyed_report, match_columns = _prepare_submission_match_keys(form_filter, report, report_ptid_col, report_visit_col, form_name, config)
    requested_keys = keyed_filter[match_columns].dropna().drop_duplicates()
    if requested_keys.empty:
        return report.iloc[0:0].copy()

    matched = keyed_report.merge(requested_keys, on=match_columns, how="inner")
    _validate_submission_duplicates(matched, form_name)
    return matched.drop(columns=match_columns, errors="ignore")


def _validate_submission_filter_columns(filter_df: pd.DataFrame, form_name: str, config: dict) -> None:
    """Validate form-specific filter CSV requirements."""

    if "ptid" not in filter_df.columns:
        raise ValueError("Submission filter is missing required column: ptid")
    if form_name == "NP" and config.get("submit_flag_col") not in filter_df.columns:
        raise ValueError(f"{form_name} submission filter is missing required column: {config.get('submit_flag_col')}")
    if form_name != "NP" and config["filter_visitnum_col"] not in filter_df.columns:
        raise ValueError(f"{form_name} filter column not found: {config['filter_visitnum_col']}")


def _resolve_report_ptid_column(report_df: pd.DataFrame, form_name: str, config: dict) -> str:
    """Return the configured PTID report column or raise a clear error."""

    column = find_first_existing_column(report_df, config["report_ptid_cols"])
    if not column:
        raise ValueError(f"{form_name} report missing PTID column: {', '.join(config['report_ptid_cols'])}")
    return column


def _resolve_report_visit_column(report_df: pd.DataFrame, form_name: str, config: dict) -> str | None:
    """Return the configured visit column, except for PTID-only NP matching."""

    column = find_first_existing_column(report_df, config["report_visitnum_cols"])
    if form_name != "NP" and not column:
        raise ValueError(f"{form_name} report missing VISITNUM column: {', '.join(config['report_visitnum_cols'])}")
    return column


def _prepare_submission_match_keys(form_filter, report_df, report_ptid_col, report_visit_col, form_name, config):
    """Attach temporary match keys to filter and report frames."""

    keyed_filter = form_filter.copy()
    keyed_report = report_df.copy()
    keyed_filter["_match_ptid"] = keyed_filter["ptid"]
    keyed_report["_match_ptid"] = keyed_report[report_ptid_col]
    match_columns = ["_match_ptid"]
    if form_name != "NP":
        keyed_filter["_match_visitnum"] = keyed_filter[config["filter_visitnum_col"]]
        keyed_report["_match_visitnum"] = keyed_report[report_visit_col]
        match_columns.append("_match_visitnum")
    return keyed_filter, keyed_report, match_columns


def _validate_submission_duplicates(matched: pd.DataFrame, form_name: str) -> None:
    """Reject duplicate REDCap records for the configured matching mode."""

    subset = ["_match_ptid"] if form_name == "NP" else ["_match_ptid", "_match_visitnum"]
    duplicate_mask = matched.duplicated(subset=subset, keep=False)
    if duplicate_mask.any():
        duplicates = matched.loc[duplicate_mask, subset].dropna().drop_duplicates().to_dict("records")
        raise ValueError(f"{form_name} contains duplicate matching records: {duplicates}")

"""Prepare REDCap NACCID import review records from Flywheel matches.

This module contains only the Flywheel-to-REDCap matching transformation. It
does not retrieve Flywheel data, write to REDCap, manage locks, or perform
post-write verification.
"""

from __future__ import annotations

import pandas as pd

from backend.transforms.prepare_participant_ids import normalize_stripped_text


def prepare_naccid_import_records(
    fw_df: pd.DataFrame,
    redcap_df: pd.DataFrame,
    columns: list[str],
    event: str,
) -> pd.DataFrame:
    """Match Flywheel NACCIDs to pending REDCap enrollments for review.

    REDCap rows are eligible only when both accepted date and NACCID are blank.
    Both data sources must be one row per PTID before matching so the review
    file cannot silently choose between duplicate participant records.
    """

    validate_naccid_import_sources(fw_df, redcap_df)
    if fw_df.empty or redcap_df.empty:
        return pd.DataFrame(columns=columns)

    pending = filter_pending_redcap_enrollments(redcap_df)
    naccids = fw_df.copy()
    naccids["ptid"] = normalize_stripped_text(naccids["ptid"])
    validate_unique_naccid_ptids(pending, naccids)
    return _format_naccid_import_rows(pending, naccids, columns, event)


def validate_naccid_import_sources(fw_df: pd.DataFrame, redcap_df: pd.DataFrame) -> None:
    """Validate required Flywheel and REDCap columns before matching."""

    if not {"enrl_ptid", "enrl_naccid", "enrl_accepted_date"}.issubset(redcap_df.columns):
        raise ValueError("Missing required REDCap enrollment columns.")
    if not {"ptid", "naccid", "accepted_date"}.issubset(fw_df.columns):
        raise ValueError("Missing required Flywheel enrollment columns.")


def filter_pending_redcap_enrollments(redcap_df: pd.DataFrame) -> pd.DataFrame:
    """Keep REDCap enrollments still missing NACCID and accepted date."""

    pending = redcap_df.copy()
    pending["enrl_ptid"] = normalize_stripped_text(pending["enrl_ptid"])
    naccid = normalize_stripped_text(pending["enrl_naccid"])
    accepted = normalize_stripped_text(pending["enrl_accepted_date"])
    return pending.loc[pending["enrl_ptid"].ne("") & naccid.eq("") & accepted.eq("")].copy()


def validate_unique_naccid_ptids(pending: pd.DataFrame, naccids: pd.DataFrame) -> None:
    """Reject duplicate PTIDs before one-to-one REDCap/Flywheel matching."""

    _reject_duplicate_values(pending["enrl_ptid"], "Duplicate enrollment PTIDs found in REDCap.")
    _reject_duplicate_values(naccids["ptid"], "Duplicate enrollment PTIDs found in Flywheel.")


def _format_naccid_import_rows(pending: pd.DataFrame, naccids: pd.DataFrame, columns: list[str], event: str) -> pd.DataFrame:
    """Merge matched rows and return the configured REDCap import column order."""

    matched = pending.merge(naccids, left_on="enrl_ptid", right_on="ptid", how="inner", validate="one_to_one")
    matched["ptid"] = matched["enrl_ptid"]
    matched["redcap_event_name"] = event
    matched["enrl_accepted_date"] = matched["accepted_date"]
    matched["enrl_naccid"] = matched["naccid"]
    return matched[columns].copy()


def _reject_duplicate_values(series: pd.Series, message: str) -> None:
    """Raise when a matching key contains duplicate values."""

    if series.duplicated().any():
        raise ValueError(message)

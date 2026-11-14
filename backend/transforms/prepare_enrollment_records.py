"""Enrollment preparation transforms used by backend workflows.

These helpers preserve the existing enrollment eligibility and matching rules
without relying on legacy package directories.
"""

from __future__ import annotations

from typing import Iterable

import pandas as pd

from backend.transforms.prepare_output_columns import prepare_enrollment_output_columns
from backend.transforms.prepare_participant_ids import filter_valid_participant_keys, prepare_participant_key


def filter_eligible_enrollment_records(df: pd.DataFrame) -> pd.DataFrame:
    """Return enrollment-checker rows that are eligible for submission."""

    required = ["enrl_ptid", "enrl_submit_confirm", "enrl_submit_date", "enrl_accepted_date", "enrl_naccid"]
    missing = [column for column in required if column not in df.columns]
    if missing:
        raise ValueError(f"Enrollment checker missing required columns: {missing}")
    today = pd.Timestamp.today().normalize()
    cutoff = today - pd.Timedelta(days=30)
    submit_date = pd.to_datetime(df["enrl_submit_date"], format="%Y-%m-%d", errors="coerce").dt.normalize()
    ptid = df["enrl_ptid"].fillna("").astype(str).str.strip()
    confirm = df["enrl_submit_confirm"].fillna("").astype(str).str.strip()
    accepted = df["enrl_accepted_date"].fillna("").astype(str).str.strip()
    naccid = df["enrl_naccid"].fillna("").astype(str).str.strip()
    eligible = ptid.ne("") & confirm.eq("1") & submit_date.between(cutoff, today) & accepted.eq("") & naccid.eq("")
    return df.loc[eligible].copy()


def prepare_enrollment_participant_keys(df: pd.DataFrame, expected_columns: Iterable[str], name: str) -> pd.DataFrame:
    """Normalize participant IDs and create the shared enrollment key column."""

    return prepare_participant_key(df, list(expected_columns), name=name)


def drop_invalid_enrollment_keys(df: pd.DataFrame, name: str) -> pd.DataFrame:
    """Remove empty IDs and reject duplicate participant keys."""

    return filter_valid_participant_keys(df, name=name)


def match_eligible_enrollment_records(enrollment_df: pd.DataFrame, eligible_ptids: set) -> pd.DataFrame:
    """Match eligible participants against the enrollment REDCap report."""

    renamed = enrollment_df.rename(columns={"enrl_ptid": "ptid", "enrl_adcid": "adcid", "enrl_formver": "formver"})
    keyed = prepare_enrollment_participant_keys(renamed, ["ptid", "PTID"], "REDCap enrollment report")
    matched = keyed.loc[keyed[".key_ptid"].isin(eligible_ptids)].copy()
    if matched.empty:
        raise ValueError("No eligible PTIDs matched the enrollment report")
    matched = drop_invalid_enrollment_keys(matched, "REDCap enrollment report")
    missing_ptids = eligible_ptids - set(matched[".key_ptid"])
    if missing_ptids:
        raise ValueError(f"Eligible PTIDs missing from enrollment report: {sorted(missing_ptids)[:10]}")
    return matched


def prepare_enrollment_submission_output(df: pd.DataFrame) -> pd.DataFrame:
    """Return enrollment submission columns with REDCap metadata removed."""

    return prepare_enrollment_output_columns(df)

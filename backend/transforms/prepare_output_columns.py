"""Prepare output columns for NACC submission and review files."""

from __future__ import annotations

from collections.abc import Sequence

import pandas as pd


def find_first_existing_column(df: pd.DataFrame, candidates: Sequence[str]) -> str | None:
    """Return the first configured column that exists in ``df``."""

    return next((column for column in candidates if column in df.columns), None)


def prepare_submission_output_columns(df: pd.DataFrame, form_name: str, config: dict) -> pd.DataFrame:
    """Rename and remove configured UDSv4 non-submission columns.

    REDCap arm metadata is removed for all forms because it is an operational
    field, not part of NACC submission CSVs. Duplicate output headers are
    rejected after renaming because they make CSV submission files ambiguous.
    """

    output = df.copy()
    rename_map = {old: new for old, new in config.get("output_rename", {}).items() if old in output.columns}
    output = output.rename(columns=rename_map)
    duplicate_columns = output.columns[output.columns.duplicated()].unique().tolist()
    if duplicate_columns:
        raise ValueError(f"{form_name} contains duplicate output columns: {', '.join(map(str, duplicate_columns))}")

    redcap_arm = [column for column in output.columns if "redcap" in str(column).lower() and "arm" in str(column).lower()]
    configured = [column for column in config.get("drop_columns", []) if column in output.columns]
    return output.drop(columns=list(dict.fromkeys(configured + redcap_arm)), errors="ignore")


def drop_configured_final_output_columns(df: pd.DataFrame, config: dict) -> pd.DataFrame:
    """Remove columns used for matching but excluded from the final CSV.

    MLST uses a visit number for record selection but omits ``visitnum`` from
    the final NACC MLST submission. The configured list keeps that rule outside
    the transformation logic.
    """

    drop_columns = [column for column in config.get("final_output_drop_columns", []) if column in df.columns]
    return df.drop(columns=drop_columns, errors="ignore") if drop_columns else df


def prepare_enrollment_output_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Remove REDCap/internal fields that are not part of enrollment CSV output."""

    metadata_cols = {"Consent Date", "consent_date", "Submission Date", "submission_date", "NACCID", "naccid"}
    keep_cols = [
        column for column in df.columns
        if not column.lower().startswith("redcap_")
        and not column.startswith(".key_")
        and column not in metadata_cols
    ]
    return df.loc[:, keep_cols].copy()

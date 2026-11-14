"""Prepare and normalize participant identifiers for matching.

The pipeline intentionally has more than one PTID normalization rule. UDSv4
submission matching strips trailing spreadsheet ``.0`` values, while enrollment
matching preserves the stripped text exactly. Keeping these helpers separate
makes those differences explicit for future backend callers.
"""

from __future__ import annotations

from typing import Sequence

import pandas as pd


def normalize_submission_identifier_series(series: pd.Series) -> pd.Series:
    """Normalize UDSv4 submission identifiers and visit keys.

    Values are converted to pandas strings, whitespace is stripped, a trailing
    ``.0`` is removed, and common text representations of missing values become
    ``pd.NA``. This mirrors the legacy UDSv4 matching behavior.
    """

    cleaned = series.astype("string").str.strip().str.replace(r"\.0$", "", regex=True)
    return cleaned.replace({"": pd.NA, "nan": pd.NA, "NaN": pd.NA, "None": pd.NA, "<NA>": pd.NA})


def normalize_stripped_text(series: pd.Series) -> pd.Series:
    """Return stripped strings with missing values represented as empty text."""

    return series.fillna("").astype(str).str.strip()


def prepare_participant_key(
    df: pd.DataFrame,
    candidates: Sequence[str],
    *,
    key_col: str = ".key_ptid",
    name: str = "data",
) -> pd.DataFrame:
    """Copy ``df`` and prepare an enrollment-style participant matching key.

    Enrollment reports accept several PTID header spellings case-insensitively
    and do not strip spreadsheet ``.0`` suffixes. That is distinct from UDSv4
    submission matching and is preserved here.
    """

    columns = {column.lower().strip(): column for column in df.columns}
    ptid_col = next((columns[item.lower()] for item in candidates if item.lower() in columns), None)
    if ptid_col is None:
        raise ValueError(f"{name}: Missing PTID column. Expected one of {list(candidates)}")

    keyed = df.copy()
    keyed[key_col] = normalize_stripped_text(keyed[ptid_col])
    return keyed


def filter_valid_participant_keys(
    df: pd.DataFrame,
    *,
    key_col: str = ".key_ptid",
    name: str = "data",
) -> pd.DataFrame:
    """Remove blank enrollment keys and reject duplicate participant IDs.

    Enrollment submission and NACCID review files must be one row per PTID; a
    duplicate key would make the downstream CSV ambiguous.
    """

    valid = df[key_col].ne("") & ~df[key_col].isin(["nan", "None"])
    filtered = df.loc[valid].copy()
    if filtered.empty:
        raise ValueError(f"{name}: No valid participant IDs remain")

    duplicates = filtered.loc[filtered[key_col].duplicated(), key_col]
    if not duplicates.empty:
        examples = duplicates.unique()[:10].tolist()
        raise ValueError(f"{name}: Duplicate PTIDs found: {examples}")
    return filtered

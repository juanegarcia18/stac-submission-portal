"""Flywheel enrollment extraction for NACCID import preparation."""

from __future__ import annotations

import pandas as pd
import flywheel

from backend.services.config import resolve_flywheel_auth, resolve_flywheel_destination


def connect_flywheel_client() -> flywheel.Client:
    """Create a Flywheel SDK client using configured token authentication."""

    return flywheel.Client(resolve_flywheel_auth()["token"])


def fetch_recent_flywheel_naccids(client: flywheel.Client, *, days: int = 30) -> pd.DataFrame:
    """Return recent enrollment records with PTID, NACCID, and accepted date."""

    auth = resolve_flywheel_auth()
    project_name = resolve_flywheel_destination("naccid_extraction")
    project = client.lookup(f"{auth['group']}/{project_name}")
    if not project:
        raise ValueError("Flywheel enrollment project not found.")
    tree = client.fetch_tree(
        {
            "projects": {
                "fields": ["label"],
                "subjects": {"fields": ["label", "info", "created"], "limit": 0},
            }
        },
        filter=f"_id={project.id}",
        include_all_info=True,
    )
    if not tree or len(tree) != 1:
        raise ValueError("Expected exactly one Flywheel project.")
    cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=days)
    records = []
    for subject in tree[0].get("subjects", []):
        enrollment = (subject.get("info") or {}).get("enrollment")
        if not isinstance(enrollment, dict):
            continue
        created = pd.to_datetime(subject.get("created"), utc=True, errors="coerce")
        if pd.isna(created) or created < cutoff:
            continue
        ptid = str(enrollment.get("ptid") or "").strip()
        naccid = str(enrollment.get("naccid") or "").strip()
        if not ptid or not naccid:
            continue
        records.append({"ptid": ptid, "naccid": naccid, "accepted_date": created.date().isoformat()})
    return pd.DataFrame(records, columns=["ptid", "naccid", "accepted_date"])

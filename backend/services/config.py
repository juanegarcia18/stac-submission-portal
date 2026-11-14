"""Validated YAML configuration for REDCap, Flywheel, and form processing.

The loader reads ``backend/config/forms.yaml`` and ``backend/config/integrations.yaml`` once,
validates their NACC-specific structure, and resolves credentials from the
environment only when a caller asks for a concrete integration target. Keeping
tokens outside YAML lets Docker, deployment infrastructure, or a local
``stac_submission_portal.env`` provide secrets without committing them.
"""

from __future__ import annotations

import os
from copy import deepcopy
from pathlib import Path
from typing import Mapping, MutableMapping

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_DIR = PROJECT_ROOT / "backend" / "config"
ENV_FILE = PROJECT_ROOT / "stac_submission_portal.env"
SUPPORTED_MATCH_MODES = {"ptid_visit", "ptid_only"}

_CONFIG_CACHE: dict | None = None


class ConfigurationError(RuntimeError):
    """Base error for invalid pipeline configuration."""


class MissingConfigurationFile(ConfigurationError):
    """Raised when a required YAML configuration file is absent."""


class InvalidConfiguration(ConfigurationError):
    """Raised when YAML exists but lacks required NACC mappings."""


class UnknownFormError(ConfigurationError):
    """Raised when a caller requests a form not defined in YAML."""


class DisabledFormError(ConfigurationError):
    """Raised when a caller requests a configured but disabled form."""


class MissingEnvironmentVariable(ConfigurationError):
    """Raised when an env reference cannot be resolved without exposing secrets."""


def clear_config_cache() -> None:
    """Clear the cached YAML structure, primarily for focused tests."""

    global _CONFIG_CACHE
    _CONFIG_CACHE = None


def load_config(
    config_dir: Path | str | None = None,
    *,
    use_cache: bool = True,
) -> dict:
    """Return validated forms and integration configuration.

    ``config_dir`` defaults to the repository ``config`` directory. YAML is
    cached for normal application startup, while tests can pass ``use_cache`` as
    ``False`` to validate temporary files independently.
    """

    global _CONFIG_CACHE
    if config_dir is None and use_cache and _CONFIG_CACHE is not None:
        return deepcopy(_CONFIG_CACHE)

    root = Path(config_dir) if config_dir else DEFAULT_CONFIG_DIR
    config = {
        "forms": validate_forms_config(_read_yaml(root / "forms.yaml", "forms")),
        "integrations": validate_integrations_config(
            _read_yaml(root / "integrations.yaml", "integrations")
        ),
    }

    if config_dir is None and use_cache:
        _CONFIG_CACHE = deepcopy(config)

    return deepcopy(config)


def load_environment_file(env_path: Path | str | None = None) -> None:
    """Load a local dotenv file without replacing deployment-provided values."""

    path = Path(env_path) if env_path else ENV_FILE
    if path.exists():
        load_dotenv(path, override=False)


def _read_yaml(path: Path, label: str) -> dict:
    """Read one YAML file and return its mapping body."""

    if not path.exists():
        raise MissingConfigurationFile(f"Missing {label} configuration file: {path}")

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise InvalidConfiguration(f"Invalid YAML in {path.name}: {exc}") from exc

    if not isinstance(data, dict):
        raise InvalidConfiguration(f"{path.name} must contain a YAML mapping.")
    return data


def validate_forms_config(data: Mapping) -> dict:
    """Validate form mappings and return legacy-compatible form config."""

    forms = data.get("forms")
    if not isinstance(forms, dict) or not forms:
        raise InvalidConfiguration("forms.yaml must define a non-empty forms mapping.")

    return {name: _validate_form(name, body) for name, body in forms.items()}


def _validate_form(name: str, body: Mapping) -> dict:
    """Validate one form and flatten matching fields for current processors."""

    if not isinstance(body, dict):
        raise InvalidConfiguration(f"{name} form configuration must be a mapping.")

    matching = body.get("matching")
    if not isinstance(matching, dict):
        raise InvalidConfiguration(f"{name} is missing matching configuration.")

    mode = matching.get("mode")
    if mode not in SUPPORTED_MATCH_MODES:
        raise InvalidConfiguration(f"{name} uses unsupported matching mode: {mode}")

    required = ["enabled", "api_form", "prefix", "slug", "output_rename", "drop_columns"]
    _require_keys(body, required, f"{name} form")
    _validate_form_fields(name, body)
    _validate_matching(name, matching, mode)

    config = {key: deepcopy(value) for key, value in body.items() if key != "matching"}
    config.update({
        "matching_mode": mode,
        "filter_ptid_col": matching["filter_ptid_col"],
        "filter_visitnum_col": matching.get("filter_visitnum_col"),
        "report_ptid_cols": matching["report_ptid_cols"],
        "report_visitnum_cols": matching.get("report_visitnum_cols", []),
    })
    return config


def _validate_matching(name: str, matching: Mapping, mode: str) -> None:
    """Validate PTID and visit matching fields for one form."""

    _require_keys(matching, ["filter_ptid_col", "report_ptid_cols"], f"{name} matching")
    _require_nonempty_string(matching["filter_ptid_col"], name, "matching.filter_ptid_col")
    if not _string_list(matching["report_ptid_cols"]):
        raise InvalidConfiguration(f"{name} matching requires report_ptid_cols.")

    if mode == "ptid_visit":
        _require_keys(matching, ["filter_visitnum_col", "report_visitnum_cols"], f"{name} matching")
        _require_nonempty_string(matching["filter_visitnum_col"], name, "matching.filter_visitnum_col")
        if not _string_list(matching["report_visitnum_cols"]):
            raise InvalidConfiguration(f"{name} visit matching requires visit columns.")

    if mode == "ptid_only" and matching.get("filter_visitnum_col") is not None:
        raise InvalidConfiguration(f"{name} PTID-only matching cannot define a visit column.")


def validate_integrations_config(data: Mapping) -> dict:
    """Validate REDCap and Flywheel integration mappings."""

    redcap = data.get("redcap")
    flywheel = data.get("flywheel")
    if not isinstance(redcap, dict) or not isinstance(flywheel, dict):
        raise InvalidConfiguration("integrations.yaml must define redcap and flywheel.")

    _validate_redcap(redcap)
    _validate_flywheel(flywheel)
    return deepcopy({"redcap": redcap, "flywheel": flywheel})


def _validate_redcap(redcap: Mapping) -> None:
    """Validate REDCap connections, reports, and NACCID import fields."""

    connections = redcap.get("connections")
    reports = redcap.get("reports")
    naccid = redcap.get("naccid_import")
    if not isinstance(connections, dict) or not isinstance(reports, dict):
        raise InvalidConfiguration("REDCap connections and reports are required.")

    for name, connection in connections.items():
        _require_keys(connection, ["api_url_env", "token_env"], f"REDCap connection {name}")
        _require_nonempty_string(connection["api_url_env"], f"REDCap connection {name}", "api_url_env")
        _require_nonempty_string(connection["token_env"], f"REDCap connection {name}", "token_env")

    for report, body in reports.items():
        _require_keys(body, ["connection", "report_id_env"], f"REDCap report {report}")
        _require_nonempty_string(body["connection"], f"REDCap report {report}", "connection")
        _require_nonempty_string(body["report_id_env"], f"REDCap report {report}", "report_id_env")
        if body["connection"] not in connections:
            raise InvalidConfiguration(f"REDCap report {report} references an unknown connection.")

    _require_keys(naccid, ["connection", "event", "instrument", "columns", "fields"], "NACCID import")
    _require_nonempty_string(naccid["event"], "NACCID import", "event")
    _require_nonempty_string(naccid["instrument"], "NACCID import", "instrument")
    if naccid["connection"] not in connections or not _string_list(naccid["columns"]) or not _string_list(naccid["fields"]):
        raise InvalidConfiguration("NACCID import configuration is incomplete.")


def _validate_flywheel(flywheel: Mapping) -> None:
    """Validate Flywheel authentication and destination mappings."""

    auth = flywheel.get("auth")
    destinations = flywheel.get("destinations")
    _require_keys(auth, ["token_env", "group_env"], "Flywheel auth")
    _require_nonempty_string(auth["token_env"], "Flywheel auth", "token_env")
    _require_nonempty_string(auth["group_env"], "Flywheel auth", "group_env")
    if not isinstance(destinations, dict) or not destinations:
        raise InvalidConfiguration("Flywheel destinations are required.")

    for name, destination in destinations.items():
        _require_keys(destination, ["project_env"], f"Flywheel destination {name}")
        _require_nonempty_string(destination["project_env"], f"Flywheel destination {name}", "project_env")
        if "required" in destination and type(destination["required"]) is not bool:
            raise InvalidConfiguration(f"Flywheel destination {name}.required must be a boolean.")


def get_form_config(form_name: str, config: Mapping | None = None) -> dict:
    """Return one enabled form configuration or raise a clear selection error."""

    forms = (config or load_config())["forms"]
    normalized = str(form_name).strip().upper()
    if normalized not in forms:
        raise UnknownFormError(f"Unknown form selection: {form_name}")
    if not forms[normalized].get("enabled", False):
        raise DisabledFormError(f"Form is disabled in configuration: {normalized}")
    return deepcopy(forms[normalized])


def get_selected_form_configs(selected_forms, config: Mapping | None = None) -> dict:
    """Resolve enabled form configurations for a selected form list."""

    source = config or load_config()
    if selected_forms is None:
        return {name: body for name, body in source["forms"].items() if body.get("enabled")}

    resolved = {}
    for form_name in selected_forms:
        normalized = str(form_name).strip().upper()
        resolved[normalized] = get_form_config(normalized, source)
    return resolved


def resolve_redcap_connection(
    connection_name: str = "udsv4",
    config: Mapping | None = None,
    env: Mapping[str, str] | None = None,
) -> dict:
    """Resolve REDCap API URL and token for a configured connection."""

    if env is None:
        load_environment_file()
    source = (config or load_config())["integrations"]["redcap"]
    connection = source["connections"].get(connection_name)
    if not connection:
        raise InvalidConfiguration(f"Unknown REDCap connection: {connection_name}")

    values = env or os.environ
    return {
        "base_url": _env_value(values, connection["api_url_env"], "REDCap API URL"),
        "token": _env_value(values, connection["token_env"], "REDCap API token"),
    }


def resolve_redcap_report(
    report_name: str,
    config: Mapping | None = None,
    env: Mapping[str, str] | None = None,
) -> dict:
    """Resolve the REDCap connection and report ID for a logical report."""

    source = (config or load_config())["integrations"]["redcap"]
    report_key = str(report_name).strip().lower()
    report = source["reports"].get(report_key)
    if not report:
        raise InvalidConfiguration(f"Unsupported REDCap report: {report_name}")

    connection = resolve_redcap_connection(report["connection"], config, env)
    connection["report_id"] = _env_value(env or os.environ, report["report_id_env"], f"REDCap report {report_key}")
    return connection


def resolve_naccid_import(config: Mapping | None = None) -> dict:
    """Return NACCID import event, instrument, and ordered REDCap columns."""

    source = (config or load_config())["integrations"]["redcap"]["naccid_import"]
    return deepcopy(source)


def resolve_flywheel_auth(
    config: Mapping | None = None,
    env: Mapping[str, str] | None = None,
) -> dict:
    """Resolve Flywheel token and group without authenticating."""

    if env is None:
        load_environment_file()
    auth = (config or load_config())["integrations"]["flywheel"]["auth"]
    values = env or os.environ
    return {
        "token": _env_value(values, auth["token_env"], "Flywheel token"),
        "group": _env_value(values, auth["group_env"], "Flywheel group"),
    }


def resolve_flywheel_destination(
    destination_name: str,
    config: Mapping | None = None,
    env: Mapping[str, str] | None = None,
    *,
    required: bool | None = None,
) -> str:
    """Resolve a named Flywheel project destination from environment refs."""

    if env is None:
        load_environment_file()
    destinations = (config or load_config())["integrations"]["flywheel"]["destinations"]
    destination = destinations.get(destination_name)
    if not destination:
        raise InvalidConfiguration(f"Unknown Flywheel destination: {destination_name}")

    values = env or os.environ
    must_exist = destination.get("required", False) if required is None else required
    project = values.get(destination["project_env"], "").strip()
    if not project and must_exist:
        raise MissingEnvironmentVariable(
            f"Missing required environment variable for Flywheel destination {destination_name}: "
            f"{destination['project_env']}"
        )
    return project


def validate_environment(config: Mapping | None = None, env: Mapping[str, str] | None = None) -> None:
    """Resolve all required environment references to fail fast at startup."""

    source = config or load_config()
    values = env or os.environ
    for report_name in source["integrations"]["redcap"]["reports"]:
        resolve_redcap_report(report_name, source, values)
    resolve_flywheel_auth(source, values)
    for destination in source["integrations"]["flywheel"]["destinations"]:
        resolve_flywheel_destination(destination, source, values)


def legacy_form_config(config: Mapping | None = None) -> dict:
    """Return enabled form mappings in the shape used by the current CLI ETL."""

    forms = (config or load_config())["forms"]
    return {name: deepcopy(body) for name, body in forms.items() if body.get("enabled")}


def _env_value(env: Mapping[str, str], name: str, label: str) -> str:
    """Resolve one required environment variable without logging its value."""

    value = env.get(name, "").strip()
    if not value:
        raise MissingEnvironmentVariable(f"Missing required environment variable for {label}: {name}")
    return value


def _validate_form_fields(name: str, body: Mapping) -> None:
    """Validate form fields that are consumed directly by ETL code."""

    if type(body["enabled"]) is not bool:
        raise InvalidConfiguration(f"{name}.enabled must be a boolean.")
    for field in ("api_form", "prefix", "slug"):
        _require_nonempty_string(body[field], name, field)
    _require_string_mapping(body["output_rename"], name, "output_rename")
    _require_string_list(body["drop_columns"], name, "drop_columns")
    _require_string_list(body.get("final_output_drop_columns", []), name, "final_output_drop_columns")
    if "submit_flag_col" in body:
        _require_nonempty_string(body["submit_flag_col"], name, "submit_flag_col")
    if "submit_flag_value" in body and not isinstance(body["submit_flag_value"], str):
        raise InvalidConfiguration(f"{name}.submit_flag_value must be a string.")


def _require_nonempty_string(value, label: str, field: str) -> None:
    """Require a field to be a populated string."""

    if not isinstance(value, str) or not value.strip():
        raise InvalidConfiguration(f"{label}.{field} must be a nonempty string.")


def _require_string_mapping(value, label: str, field: str) -> None:
    """Require a dictionary with string keys and values."""

    if not isinstance(value, dict):
        raise InvalidConfiguration(f"{label}.{field} must be a mapping.")
    if any(
        not isinstance(key, str) or not key.strip()
        or not isinstance(item, str) or not item.strip()
        for key, item in value.items()
    ):
        raise InvalidConfiguration(f"{label}.{field} must contain only nonempty string keys and values.")


def _require_string_list(value, label: str, field: str) -> None:
    """Require a list whose entries are all strings."""

    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise InvalidConfiguration(f"{label}.{field} must be a list of nonempty strings.")


def _require_keys(body: Mapping | None, keys: list[str], label: str) -> None:
    """Raise a clear error when a required mapping key is absent."""

    if not isinstance(body, MutableMapping):
        raise InvalidConfiguration(f"{label} must be a mapping.")

    missing = [key for key in keys if key not in body]
    if missing:
        raise InvalidConfiguration(f"{label} is missing required setting(s): {', '.join(missing)}")


def _string_list(value) -> bool:
    """Return True when value is a non-empty list of strings."""

    return isinstance(value, list) and bool(value) and all(isinstance(item, str) and item for item in value)

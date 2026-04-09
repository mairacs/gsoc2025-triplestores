from __future__ import annotations

# Copyright (C) 2025 Maira Papadopoulou
# SPDX-License-Identifier: Apache-2.0
import csv
import json
import logging
import platform
import subprocess
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from triplestore.exceptions import TriplestoreMissingConfigValue
from triplestore.utils_geo import export_geospatial_select_results

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

logger = logging.getLogger(__name__)

SPARQL_QUERY_FORMS = {"SELECT", "ASK", "CONSTRUCT", "DESCRIBE"}

SPARQL_UPDATE_TYPES = {
    "WITH", "INSERT", "DELETE", "LOAD", "CLEAR", "CREATE", "DROP",
    "MOVE", "COPY", "ADD", "MODIFY",
}

SPARQL_DEFAULT_EXPORT_FORMATS = {
    "SELECT": "json",
    "ASK": "json",
    "CONSTRUCT": "ttl",
    "DESCRIBE": "ttl",
}

SPARQL_ALLOWED_EXPORT_FORMATS = {
    "SELECT": {"json", "csv", "geojson", "kml", "kmz", "gml"},
    "ASK": {"json", "txt"},
    "CONSTRUCT": {"ttl"},
    "DESCRIBE": {"ttl"},
}


def detect_host_url(port: int, path: str = "", fallback: str | None = None) -> str:
    """
    Detect the Windows host IP from within WSL and return the base URL to use for HTTP services.
    Falls back to localhost if detection fails.

    Parameters
    ----------
    port : int
        Target TCP port to include in the returned URL.
    path : str, optional
        Optional path suffix to append to the base URL (default: "").
    fallback : str | None, optional
        If provided, this URL is returned when auto-detection fails.

    Returns
    -------
    str
        The detected base URL, or the fallback/localhost URL when detection fails.
    """
    try:
        if "microsoft" in platform.uname().release.lower():
            route = subprocess.check_output(["ip", "route"]).decode()
            for line in route.splitlines():
                if line.startswith("default via"):
                    ip = line.split()[2]
                    return f"http://{ip}:{port}{path}"
    except subprocess.SubprocessError as e:
        msg = f"Auto-detection of host IP failed: {e}"
        logger.warning(msg)

    return fallback or f"http://localhost:{port}{path}"


def detect_graphdb_url() -> str:
    return detect_host_url(7200)


def validate_config(user_config: Mapping[str, Any], *, required_keys: Iterable[str], optional_defaults: Mapping[str, Any] | None,
                    alias_map: Mapping[str, Any] | None, backend_name: str = "backend") -> dict[str, Any]:
    """
    Validate and normalize a backend configuration dictionary.

    This function ensures that:
    1. All required keys are present (after resolving aliases).
    2. Optional keys are filled in with defaults if not provided.
    3. Unknown keys trigger a warning message (but are preserved in the result).

    Parameters
    ----------
    user_config : Mapping[str, Any]
        The configuration dictionary provided by the user.
    required_keys : Iterable[str]
        Keys that must always be present in the final configuration.
    optional_defaults : Mapping[str, Any], optional
        Optional keys with their default values if missing.
    alias_map : Mapping[str, str], optional
        Mapping of alias → canonical key names.
    backend_name : str, default="backend"
        Name of the backend, used in error/warning messages.

    Returns
    -------
    dict[str, Any]
        A normalized configuration dictionary containing:
        - All required keys,
        - All optional keys (with user or default values),
        - All provided aliases converted to canonical keys,
        - Any unknown keys (with a warning).

    Raises
    ------
    TriplestoreMissingConfigValue
        If one or more required keys are missing.
    """

    if optional_defaults is None:
        optional_defaults = {}
    if alias_map is None:
        alias_map = {}

    normalized_config: dict[str, Any] = {}
    for key, value in user_config.items():
        canonical_key = alias_map.get(key, key)
        normalized_config[canonical_key] = value

    missing_keys = [k for k in required_keys if k not in normalized_config]
    if missing_keys:
        msg = (
            f"[{backend_name}] Configuration error: Missing required config keys for: '"
            f"{', '.join(missing_keys)}'"
        )
        raise TriplestoreMissingConfigValue(msg)

    for key, default_val in optional_defaults.items():
        if key not in normalized_config:
            normalized_config[key] = default_val

    allowed_keys = set(required_keys) | set(optional_defaults)
    allowed_with_aliases = allowed_keys | set(alias_map.keys())
    unknown_keys = [k for k in user_config if k not in allowed_with_aliases]

    if unknown_keys:
        msg = (
            f"[{backend_name}] Ignoring unrecognized config keys for: '"
            f"{', '.join(sorted(unknown_keys))}'"
        )
        logger.warning(msg)

    return normalized_config


def get_sparql_query_type(sparql: str) -> str:
    """
    Determine the top-level SPARQL query or update keyword.
    This function extracts the first meaningful keyword of a SPARQL query/update string, ignoring
    leading PREFIX, BASE declarations and comment lines.

    Parameters
    ----------
    sparql : str
        The SPARQL query or update string.

    Returns
    -------
    str
        The uppercase top-level SPARQL keyword (e.g., 'SELECT', 'INSERT', 'ASK').
        Returns an empty string if no valid keyword can be determined.
    """
    lines = [line.strip() for line in sparql.strip().splitlines() if line.strip()]

    for line in lines:
        upper = line.upper()
        if upper.startswith(("PREFIX ", "BASE ")):
            continue
        if upper.startswith("#"):
            continue
        return line.split(None, 1)[0].upper()

    return ""


def resolve_export_format(query_type: str, *, export: bool, output_format: str | None = None, backend_name: str = "backend") -> str | None:
    """
    Determine and validate the export format for a SPARQL query.
    This function resolves the effective export format based on the query type and validates it against the supported formats for that type.

    Parameters
    ----------
    query_type : str
        The uppercase SPARQL query type.
    export : bool
        Whether export has been requested.
    output_format : str | None, optional
        Explicit export format requested by the user. If not provided, a default format is selected based on the query type.
    backend_name : str, default="backend"
        Backend name used in error messages.

    Returns
    -------
    str | None
        The normalized export format (lowercase, without leading dot), or None if export is False.

    Raises
    ------
    ValueError
        If export is requested for an unsupported query type or if the requested format is not allowed for the given query type.
    """
    if not export:
        return None

    if query_type not in SPARQL_DEFAULT_EXPORT_FORMATS:
        msg = f"[{backend_name}] Unsupported export format '{output_format}' for {query_type} query. "
        raise ValueError(msg)

    chosen_format = (output_format or SPARQL_DEFAULT_EXPORT_FORMATS[query_type]).lower().lstrip(".")
    allowed_formats = SPARQL_ALLOWED_EXPORT_FORMATS.get(query_type, set())

    if chosen_format not in allowed_formats:
        msg = (
            f"[{backend_name}] Unsupported export format '{output_format}' for {query_type} query. "
            f"Allowed formats: {sorted(allowed_formats)}"
        )
        raise ValueError(msg)

    return chosen_format


def export_select_results(results: list[dict[str, str]], output_format: str, filename: str | None = None, separator: str = ",", backend_name: str = "backend") -> Path:
    """
    Export SELECT query results to a local file.
    This function serializes the variable bindings returned by a SELECT query and writes them to disk in the specified format.

    Parameters
    ----------
    results : list[dict[str, str]]
        The SELECT query result bindings, where each dictionary represents a result row mapping variable names to their string values.
    output_format : str
        Export format ('json', 'csv', 'geojson', 'kml', 'kmz', or 'gml').
    filename : str, optional
        Output filename with or without extension. If not provided, a default name ('results') is used.
    separator : str, default=","
        Column separator to use when exporting CSV files.
    backend_name : str, default="backend"
        Backend name used in error messages.

    Returns
    -------
    Path
        The path to the generated output file.

    Raises
    ------
    ValueError
        If the requested export format is not supported.
    """
    normalized_format = output_format.lower().lstrip(".")
    output_name = filename or "results"
    output_path = Path(output_name).with_suffix(f".{normalized_format}")

    if normalized_format == "json":
        output_path.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
        return output_path

    if normalized_format == "csv":
        fieldnames: list[str] = []
        for row in results:
            for key in row:
                if key not in fieldnames:
                    fieldnames.append(key)

        with output_path.open("w", newline="", encoding="utf-8") as file_obj:
            writer = csv.DictWriter(file_obj, fieldnames=fieldnames, delimiter=separator)
            writer.writeheader()
            writer.writerows(results)

        return output_path

    if normalized_format in {"geojson", "kml", "kmz", "gml"}:
        return export_geospatial_select_results(results,
            output_format=normalized_format, output_path=output_path, backend_name=backend_name)

    msg = f"[{backend_name}] Unsupported SELECT export format: {output_format}"
    raise ValueError(msg)


def export_ask_result(result: bool, output_format: str, filename: str | None = None, backend_name: str = "backend") -> Path:
    """
    Export an ASK query result to a local file.
    This function serializes the boolean result of an ASK query into the requested format and writes it to disk.

    Parameters
    ----------
    result : bool
        The boolean result of the ASK query.
    output_format : str
        Export format ('json' or 'txt').
    filename : str, optional
        Output filename with or without extension. If not provided, a default name ('results') is used.
    backend_name : str, default="backend"
        Backend name used in error messages.

    Returns
    -------
    Path
        The path to the generated output file.

    Raises
    ------
    ValueError
        If the requested export format is not supported.
    """
    normalized_format = output_format.lower().lstrip(".")
    output_name = filename or "results"
    output_path = Path(output_name).with_suffix(f".{normalized_format}")

    if normalized_format == "json":
        output_path.write_text(json.dumps({"boolean": result}, indent=2, ensure_ascii=False), encoding="utf-8")
        return output_path

    if normalized_format == "txt":
        output_path.write_text(str(result).lower(), encoding="utf-8")
        return output_path

    msg = f"[{backend_name}] Unsupported ASK export format: {output_format}"
    raise ValueError(msg)


def export_rdf_result(rdf_text: str, output_format: str, filename: str | None = None, backend_name: str = "backend") -> Path:
    """
    Export an RDF query result to a local file.
    This function writes the RDF serialization produced by a SPARQL query (e.g., CONSTRUCT or DESCRIBE) to disk using the specified format.

    Parameters
    ----------
    rdf_text : str
        The RDF serialization returned by the query.
    output_format : str
        Export format ('ttl').
    filename : str, optional
        Output filename with or without extension. If not provided, a default name ('results') is used.
    backend_name : str, default="backend"
        Backend name used in error messages.

    Returns
    -------
    Path
        The path to the generated output file.

    Raises
    ------
    ValueError
        If the requested export format is not supported.
    """
    normalized_format = output_format.lower().lstrip(".")
    output_name = filename or "results"
    output_path = Path(output_name).with_suffix(f".{normalized_format}")

    if normalized_format == "ttl":
        output_path.write_text(rdf_text, encoding="utf-8")
        return output_path

    msg = f"[{backend_name}] Unsupported RDF export format: {output_format}"
    raise ValueError(msg)

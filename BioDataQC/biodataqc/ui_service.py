"""Small interface adapters; validation and package rules remain in the backend."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from .history import HistoryError, check_database_destination, database_paths, get_run, save_run
from .packaging import build_package, verify_current_inputs
from .report_io import write_report_json
from .reporting import data_origin_label, render_report, write_report_html
from .results import utc_now
from .validation import validate_dataset


def resolve_path(value: str | Path, project_root: Path) -> Path:
    if not str(value).strip():
        raise ValueError("Enter a nonempty local path.")
    path = Path(value).expanduser()
    return (path if path.is_absolute() else project_root / path).resolve()


def display_time(value: str | None) -> str:
    if not value:
        return "Not recorded"
    try:
        return datetime.fromisoformat(value).astimezone(ZoneInfo("Europe/Paris")).strftime("%d %b %Y · %H:%M:%S %Z")
    except (ValueError, TypeError):
        return str(value)


def _slug(value) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", str(value or "unassigned"))[:40] or "unassigned"


def report_stem(evidence: dict) -> str:
    # The digest also distinguishes any nonstandard IDs that normalize to the same name.
    suffix = hashlib.sha256(evidence["run_id"].encode("utf-8")).hexdigest()[:10]
    return f"{_slug(evidence['project_id'])}_{_slug(evidence['run_id'])}_{suffix}"


def default_package_name(evidence: dict) -> str:
    return f"delivery_{_slug(evidence['project_id'])[:24]}_{_slug(evidence['run_id'])[:8]}_{uuid4().hex[:6]}"


def package_destination(project_root: Path, folder_name: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,95}", folder_name):
        raise ValueError("Use 1–96 letters, digits, underscores or hyphens, starting with a letter or digit.")
    return project_root / "outputs" / "packages" / folder_name


def run_and_store(dataset: Path, database: Path) -> dict:
    check_database_destination(database, dataset)
    report = validate_dataset(dataset)
    saved, storage_error = False, None
    try:
        save_run(report, database)
        saved = True
    except (HistoryError, OSError) as error:
        storage_error = str(error)
    return {"evidence": report.to_dict(), "saved": saved, "storage_error": storage_error}


def check_snapshot(evidence: dict) -> dict:
    if evidence["status"] != "READY":
        return {"state": "NOT_APPLICABLE", "checked_at": utc_now(),
                "message": "A READY run is required before assessing staging eligibility."}
    try:
        verify_current_inputs(evidence)
        return {"state": "MATCHED", "checked_at": utc_now(),
                "message": "Current input inventory and fingerprints match this saved run."}
    except OSError as error:
        return {"state": "UNAVAILABLE", "checked_at": utc_now(),
                "message": f"Current inputs could not be checked: {error}"}
    except ValueError as error:
        return {"state": "REVALIDATE", "checked_at": utc_now(), "message": str(error)}


def delivery_summary(evidence: dict) -> dict:
    status, counts = evidence["status"], evidence["counts"]
    messages = {
        "READY": ("Eligible for verified local staging",
                  "The required validation checks completed without blocking findings.",
                  "Prepare a verified package; its input snapshot will be checked again."),
        "BLOCKED": ("Delivery preparation is blocked",
                    f"This run found {counts['blocking_findings']} blocking finding(s).",
                    "Resolve the reported input problems, then run validation again."),
        "INCOMPLETE": ("The readiness decision is incomplete",
                       "An execution problem or unfinished required check prevents readiness.",
                       "Restore readable inputs or resolve the execution problem, then validate again."),
    }
    title, impact, action = messages[status]
    findings = [c for c in evidence["checks"] if c["outcome"] in {"FAIL", "ERROR"}]
    return {"title": title, "impact": impact, "next_action": action, "findings": findings,
            "named_samples": sorted({c["sample_id"] for c in findings if c.get("sample_id")}),
            "dataset_level_findings": any(not c.get("sample_id") for c in findings)}


def report_downloads(evidence: dict, *, owner="", next_update="") -> dict:
    stem = report_stem(evidence)
    return {"stem": stem,
            "html": render_report(evidence, owner=owner or None, next_update=next_update or None),
            "json": json.dumps(evidence, indent=2, ensure_ascii=False, allow_nan=False) + "\n"}


def save_report_files(evidence: dict, database: Path, project_root: Path, *, owner="", next_update="") -> dict:
    if get_run(database, evidence["run_id"]) != evidence:
        raise HistoryError("The selected evidence does not match its stored run. Reopen it from history.")
    directory = project_root / "outputs" / "reports"
    stem = report_stem(evidence)
    protected = database_paths(database)
    html_path = write_report_html(evidence, directory / f"{stem}.html", owner=owner or None,
                                   next_update=next_update or None, protected_paths=protected)
    json_path = write_report_json(evidence, directory / f"{stem}.json", protected)
    return {"html": str(html_path), "json": str(json_path)}


def create_package(evidence: dict, database: Path, project_root: Path, folder_name: str, *, owner="", next_update="") -> dict:
    if get_run(database, evidence["run_id"]) != evidence:
        raise HistoryError("The selected evidence does not match its stored run. Reopen it from history.")
    destination = package_destination(project_root, folder_name)
    return build_package(database, evidence["run_id"], destination,
                         owner=owner or None, next_update=next_update or None)

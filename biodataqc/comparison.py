"""Compare immutable run evidence; never reread inputs or alter readiness/history.

The unit of comparison is a check_id, not individual biological defects. A check
can still fail for a different reason; both observations are retained below.
"""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import datetime
from pathlib import PurePosixPath
import re


COMPARISON_VERSION = "1.0.0"
ISSUES = {"FAIL", "ERROR"}
LABELS = {
    "RESOLVED": "Resolved", "NEW_FAILURE": "New failure",
    "STILL_FAILING": "Still failing", "UNASSESSED": "Unassessed",
    "NEWLY_ASSESSED_FAILURE": "Newly assessed failure",
    "NOW_PASSES": "Now passes", "ADDED_CHECK": "Added check",
    "STILL_PASSING": "Still passing", "OPTIONAL_NOT_RUN": "Optional check not run",
    "NOT_COMPARABLE": "Not comparable",
}


class ComparisonError(ValueError):
    """Saved evidence cannot be compared safely."""


def _time(value):
    try:
        result = datetime.fromisoformat(value)
        if result.tzinfo is None or result.utcoffset() is None:
            raise ValueError("Missing timezone")
        return result
    except (ValueError, TypeError) as error:
        raise ComparisonError("Saved runs need valid, timezone-aware start and finish times.") from error


def _index(evidence):
    if not isinstance(evidence, dict) or evidence.get("report_schema_version") != "1.1.0":
        raise ComparisonError("Comparison supports saved report schema 1.1.0.")
    for key in ("run_id", "dataset_path", "rules_version"):
        if not isinstance(evidence.get(key), str) or not evidence[key]:
            raise ComparisonError(f"Saved evidence is missing {key}.")
    if evidence.get("status") not in {"READY", "BLOCKED", "INCOMPLETE"}:
        raise ComparisonError("Saved evidence has an unsupported status.")
    for key, kind in (("requirements", dict), ("counts", dict), ("inventory", list),
                      ("input_sha256", dict), ("checks", list), ("expected_check_ids", list)):
        if not isinstance(evidence.get(key), kind):
            raise ComparisonError(f"Saved evidence has invalid {key}.")
    if _time(evidence.get("started_at")) > _time(evidence.get("finished_at")):
        raise ComparisonError("A saved run finishes before it starts.")
    indexed = {}
    for check in evidence["checks"]:
        if (not isinstance(check, dict) or not isinstance(check.get("check_id"), str)
                or not check["check_id"] or check["check_id"] in indexed
                or check.get("outcome") not in {"PASS", "FAIL", "ERROR", "NOT_RUN"}
                or type(check.get("required")) is not bool or not check.get("rule_id")):
            raise ComparisonError("Saved evidence has invalid or duplicate check IDs/outcomes.")
        indexed[check["check_id"]] = check
    for entry in evidence["inventory"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("relative_path"), str):
            raise ComparisonError("Saved evidence has an invalid file inventory.")
    paths = [entry["relative_path"] for entry in evidence["inventory"]]
    if len(paths) != len(set(paths)):
        raise ComparisonError("Saved evidence has duplicate inventory paths.")
    if any(not isinstance(path, str) or not isinstance(digest, str)
           or not re.fullmatch(r"[0-9a-f]{64}", digest) for path, digest in evidence["input_sha256"].items()):
        raise ComparisonError("Saved evidence has invalid SHA-256 fingerprints.")
    return indexed


def _config_changes(before, after, prefix=""):
    changes = []
    for key in sorted(before.keys() | after.keys()):
        name = f"{prefix}.{key}" if prefix else key
        old, new = before.get(key), after.get(key)
        if key in before and key in after and old == new:
            continue
        if isinstance(old, dict) and isinstance(new, dict):
            changes.extend(_config_changes(old, new, name))
        else:
            changes.append({"field": name, "before_present": key in before,
                            "after_present": key in after, "before": old, "after": new})
    return changes


def _classification(old, new, comparable):
    a, b = (old or {}).get("outcome"), (new or {}).get("outcome")
    if b is None:
        return "UNASSESSED", "No corresponding result is recorded in the newer run; resolution is unproven."
    if b == "NOT_RUN":
        if a in ISSUES or new["required"]:
            return "UNASSESSED", "The newer check did not run; no resolution can be inferred."
        if comparable:
            return "OPTIONAL_NOT_RUN", "This optional check was not executed."
    if not comparable:
        return "NOT_COMPARABLE", "Rules, scope or snapshot evidence differ; inspect the observations without inferring resolution."
    if a in ISSUES and b == "PASS":
        return "RESOLVED", "The same check now passes under the same recorded requirements and rules."
    if a in ISSUES and b in ISSUES:
        return "STILL_FAILING", "The same check still fails or errors; the underlying reason may have changed."
    if b in ISSUES:
        if a == "PASS":
            return "NEW_FAILURE", "This check passed before and now fails or errors."
        return "NEWLY_ASSESSED_FAILURE", "No earlier passing result exists; this is newly observed, not proven newly introduced."
    if a == "PASS" and b == "PASS":
        return "STILL_PASSING", "This check passes in both runs."
    if a == "NOT_RUN" and b == "PASS":
        return "NOW_PASSES", "Previously unassessed; the newer check passes. This is not counted as a resolved failure."
    return "ADDED_CHECK", "A passing check appears for the first time in the selected evidence."


def _file_observation(evidence, checks, inventory, path):
    entry = inventory.get(path)
    digest = evidence["input_sha256"].get(path)
    if entry:
        return {"status": entry["status"], "entry_type": entry["entry_type"],
                "expected": entry["expected"], "size_bytes": entry.get("size_bytes"), "sha256": digest}
    if digest:
        return {"status": "PRESENT", "entry_type": "FILE", "expected": True,
                "size_bytes": None, "sha256": digest}
    directory = evidence["requirements"].get("sequencing", {}).get("directory")
    # Only a successful inventory of this exact directory establishes absence.
    inventoried = checks.get("FILE001", {}).get("outcome") == "PASS"
    absent = directory is not None and inventoried and PurePosixPath(path).parent == PurePosixPath(directory)
    return {"status": "ABSENT" if absent else "UNKNOWN", "entry_type": "MISSING" if absent else "UNKNOWN",
            "expected": None, "size_bytes": None, "sha256": None}


def _file_change(old, new):
    absent = {"MISSING", "ABSENT"}
    a, b = old["sha256"], new["sha256"]
    if a and b:
        return "UNCHANGED" if a == b else "CONTENT_CHANGED"
    if old["status"] in absent and new["entry_type"] == "FILE":
        return "ADDED"
    if new["status"] in absent and old["entry_type"] == "FILE":
        return "REMOVED"
    if old["status"] in absent and new["status"] in absent:
        return "STILL_ABSENT"
    if "UNKNOWN" not in (old["entry_type"], new["entry_type"]) and old["entry_type"] != new["entry_type"]:
        return "TYPE_CHANGED"
    if old["size_bytes"] is not None and new["size_bytes"] is not None and old["size_bytes"] != new["size_bytes"]:
        return "SIZE_CHANGED"
    return "UNVERIFIED"


def compare_runs(before: dict, after: dict) -> dict:
    """Compare chronological saved runs from the same declared project.

    All parsed requirements must match for resolution labels (conservative:
    descriptive edits also disable them). Different folders are allowed but
    explicitly disclosed; matching project IDs do not prove common lineage.
    """
    old_checks, new_checks = _index(before), _index(after)
    if before["run_id"] == after["run_id"]:
        raise ComparisonError("Select two different saved runs.")
    if not before.get("project_id") or before["project_id"] != after.get("project_id"):
        raise ComparisonError("Select runs with the same recorded project ID. An unidentified project cannot be compared.")
    if _time(before["finished_at"]) > _time(after["finished_at"]):
        raise ComparisonError("The baseline must finish before the newer run. Swap the selections.")
    reasons, notes = [], []
    if before["rules_version"] != after["rules_version"]:
        reasons.append("Validation rules version changed.")
    if before["rules_version"] not in {"1.0.0", "1.1.0"} or after["rules_version"] not in {"1.0.0", "1.1.0"}:
        reasons.append("A rules version is not supported by this comparison implementation.")
    changes = _config_changes(before["requirements"], after["requirements"])
    if changes:
        reasons.append("Parsed requirements changed; resolution labels require identical requirements.")
    if not before["requirements"] or not after["requirements"]:
        reasons.append("Recorded requirements are unavailable.")
    if _time(before["finished_at"]) > _time(after["started_at"]):
        reasons.append("Run intervals overlap; these are not sequential validation observations.")
    for label, checks in (("Baseline", old_checks), ("Newer", new_checks)):
        if checks.get("CFG001", {}).get("outcome") != "PASS":
            reasons.append(f"{label} configuration did not pass validation.")
        if checks.get("SNAP001", {}).get("outcome") != "PASS":
            reasons.append(f"{label} input snapshot was not confirmed stable.")
    if before["dataset_path"] != after["dataset_path"]:
        notes.append("Different dataset folders: this compares selected snapshots; it does not establish that one corrected the other.")
    notes.append("Historical evidence only. Comparison does not validate current files or authorize packaging.")
    notes.append("Rows count checks, not individual defects. Still failing can include a changed failure reason.")
    rows = []
    for check_id in sorted(set(old_checks) | set(new_checks) | set(before["expected_check_ids"]) | set(after["expected_check_ids"])):
        old, new = old_checks.get(check_id), new_checks.get(check_id)
        identity_changed = bool(old and new and (
            old["rule_id"] != new["rule_id"] or old["required"] != new["required"] or
            any(old.get(key) and new.get(key) and old[key] != new[key] for key in ("file_path", "sample_id"))))
        classification, explanation = _classification(old, new, not reasons and not identity_changed)
        if identity_changed:
            explanation += " This check's target, rule or required flag changed."
        ref = new or old or {}
        rows.append({"check_id": check_id, "rule_id": ref.get("rule_id", check_id.split(":")[0]),
                     "sample_id": ref.get("sample_id"), "file_path": ref.get("file_path"),
                     "change": classification, "label": LABELS[classification], "explanation": explanation,
                     "evidence_changed": old != new, "before": deepcopy(old), "after": deepcopy(new)})
    inventories = [{e["relative_path"]: e for e in run["inventory"]} for run in (before, after)]
    paths = sorted(set(inventories[0]) | set(inventories[1]) | set(before["input_sha256"]) | set(after["input_sha256"]))
    files = []
    for path in paths:
        old = _file_observation(before, old_checks, inventories[0], path)
        new = _file_observation(after, new_checks, inventories[1], path)
        files.append({"relative_path": path, "change": _file_change(old, new), "before": old, "after": new,
                      "inventory_changed": any(old[k] != new[k] for k in ("status", "entry_type", "expected"))})
    keys = ("run_id", "project_id", "dataset_path", "started_at", "finished_at", "rules_version", "status", "counts", "inventory_summary")
    return {"comparison_version": COMPARISON_VERSION, "comparable": not reasons,
            "reasons": reasons, "notes": notes, "before": {k: deepcopy(before.get(k)) for k in keys},
            "after": {k: deepcopy(after.get(k)) for k in keys}, "requirements_changes": changes,
            "checks": rows, "counts": {name: sum(r["change"] == name for r in rows) for name in LABELS},
            "files": files, "file_counts": dict(Counter(f["change"] for f in files))}

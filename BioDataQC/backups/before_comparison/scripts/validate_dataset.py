#!/usr/bin/env python3
"""Validate a BioDataQC dataset and optionally save SQL history and JSON evidence.

Exit codes: 0 READY; 1 BLOCKED; 2 INCOMPLETE or requested history/export failure.
Run from the project root: python scripts/validate_dataset.py data/clean
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from biodataqc.validation import validate_dataset
from biodataqc.history import HistoryError, check_database_destination, database_paths, save_run
from biodataqc.report_io import check_report_destination, write_report_json


def export_json(report, destination: Path, protected_paths=()):
    return write_report_json(report.to_dict(), destination, protected_paths)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path, help="Folder containing requirements.json")
    parser.add_argument("--details", action="store_true", help="Print every check, including dependency skips")
    parser.add_argument("--json", dest="json_path", type=Path, help="Save evidence as JSON outside the dataset")
    parser.add_argument("--db", type=Path, help="Append this run to a SQLite history database outside the dataset")
    args = parser.parse_args()
    protected = database_paths(args.db) if args.db else ()
    try:
        if args.db:
            check_database_destination(args.db, args.dataset)
        if args.json_path:
            check_report_destination({"dataset_path": str(args.dataset)}, args.json_path, protected)
    except (OSError, ValueError) as error:
        print(f"Invalid output path: {error}", file=sys.stderr)
        return 2
    report = validate_dataset(args.dataset)
    print(f"Dataset: {report.dataset_path}")
    print(f"Project: {report.project_id or '(unavailable)'}")
    print(f"Run ID: {report.run_id}")
    print(f"Status: {report.status}")
    for key, value in report.counts().items():
        print(f"{key.replace('_', ' ').capitalize()}: {value}")
    print("\nChecks:" if args.details else "\nFindings:")
    visible = report.checks if args.details else report.issues
    if not visible:
        print("  No blocking findings, warnings or execution errors.")
    for check in visible:
        location = check.file_path or check.sample_id or "dataset"
        if check.record_number is not None:
            location += f", record {check.record_number}"
        severity = f"/{check.severity}" if check.severity else ""
        print(f"  [{check.outcome}{severity}] {check.rule_id} | {location}")
        print(f"    {check.message}")
        if check.next_action:
            print(f"    Action: {check.next_action}")
        if args.details and check.blocked_by:
            print(f"    Depends on: {', '.join(check.blocked_by)}")
    if report.status == "INCOMPLETE" and not args.details:
        for check in report.checks:
            if check.required and check.outcome == "NOT_RUN":
                print(f"  [NOT_RUN] {check.check_id}: {check.message}")
    print("\nReadiness applies to this observed input snapshot and the configured structural checks.")
    persistence_failed = False
    if args.db:
        try:
            inserted = save_run(report, args.db)
            print(f"SQL history {'saved' if inserted else 'already stored'}: {args.db.expanduser().resolve()}")
        except (HistoryError, OSError) as error:
            print(f"History save failed: {error}", file=sys.stderr)
            persistence_failed = True
    if args.json_path:
        try:
            destination = export_json(report, args.json_path, protected)
        except (OSError, ValueError) as error:
            print(f"Report export failed: {error}", file=sys.stderr)
            return 2
        print(f"Validation JSON saved: {destination}")
    if persistence_failed:
        return 2
    return {"READY": 0, "BLOCKED": 1, "INCOMPLETE": 2}[report.status]


if __name__ == "__main__":
    raise SystemExit(main())

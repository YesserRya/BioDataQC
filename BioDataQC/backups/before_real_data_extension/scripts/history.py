#!/usr/bin/env python3
"""Read BioDataQC SQL history without rerunning validation.

From the project root:
    python scripts/history.py list
    python scripts/history.py show latest
    python scripts/history.py show RUN_ID --details

Default database: outputs/biodataqc.sqlite. Exit 0 means history was read;
it does not mean the historical dataset status was READY. Exit 2 means an error.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from biodataqc.history import HistoryError, STATUSES, database_paths, get_latest_run, get_run, list_runs
from biodataqc.report_io import write_report_json


def show_report(evidence, details=False):
    print(f"Run ID: {evidence['run_id']}")
    print(f"Project: {evidence['project_id'] or '(unavailable)'}")
    print(f"Dataset: {evidence['dataset_path']}")
    print(f"Started: {evidence['started_at']}")
    print(f"Finished: {evidence['finished_at']}")
    print(f"Historical status: {evidence['status']}")
    for name, value in evidence["counts"].items():
        print(f"{name.replace('_', ' ').capitalize()}: {value}")
    print(f"Saved metadata rows: {len(evidence['metadata_rows'])}")
    print(f"Saved input fingerprints: {len(evidence['input_sha256'])}")
    visible = evidence["checks"] if details else [c for c in evidence["checks"] if c["outcome"] in {"FAIL", "ERROR"}]
    print("\nChecks:" if details else "\nFindings:")
    if not visible:
        print("  No blocking findings, warnings or execution errors were recorded.")
    for check in visible:
        suffix = f"/{check['severity']}" if check["severity"] else ""
        location = check["file_path"] or check["sample_id"] or "dataset"
        if check["record_number"] is not None:
            location += f", record {check['record_number']}"
        print(f"  [{check['outcome']}{suffix}] {check['rule_id']} | {location}")
        print(f"    {check['message']}")
        if check["next_action"]:
            print(f"    Action: {check['next_action']}")
        if details and check["blocked_by"]:
            print(f"    Depends on: {', '.join(check['blocked_by'])}")
    if not details and evidence["status"] == "INCOMPLETE":
        for check in evidence["checks"]:
            if check["required"] and check["outcome"] == "NOT_RUN":
                print(f"  [NOT_RUN] {check['check_id']}: {check['message']}")
    print("\nThis is saved evidence. Current input files have not been revalidated.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("outputs/biodataqc.sqlite"), help="SQLite history database")
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="List newest stored runs first")
    listing.add_argument("--project", help="Exact project ID")
    listing.add_argument("--dataset", type=Path, help="Filter by dataset path")
    listing.add_argument("--status", choices=STATUSES)
    listing.add_argument("--limit", type=int, default=20)
    showing = commands.add_parser("show", help="Inspect a saved run")
    showing.add_argument("run_id", help="Full run ID, or latest")
    showing.add_argument("--project", help="Limit latest to an exact project ID")
    showing.add_argument("--dataset", type=Path, help="Limit latest to a dataset path")
    showing.add_argument("--details", action="store_true", help="Include passed and skipped checks")
    showing.add_argument("--json", dest="json_path", type=Path, help="Export the saved report as JSON")
    args = parser.parse_args()
    try:
        if args.command == "list":
            runs = list_runs(args.db, project_id=args.project, dataset_path=args.dataset,
                             status=args.status, limit=args.limit)
            print(f"Database: {args.db.expanduser().resolve()}")
            if not runs:
                print("No saved runs match these filters.")
            else:
                print(f"{'Run ID':36}  {'Project':12}  {'Status':10}  {'Blockers':8}  {'Warnings':8}  Finished (UTC)")
                for run in runs:
                    print(f"{run['run_id']:36}  {run['project_id'] or '-':12}  {run['status']:10}  "
                          f"{run['blocking_findings']:<8}  {run['warnings']:<8}  {run['finished_at']}")
                    print(f"  Dataset: {run['dataset_path']}")
            print(f"\nShowing {len(runs)} saved run(s), newest stored first. Statuses are historical.")
        else:
            if args.run_id == "latest":
                evidence = get_latest_run(args.db, project_id=args.project, dataset_path=args.dataset)
            else:
                if args.project is not None or args.dataset is not None:
                    raise HistoryError("--project and --dataset are filters for 'show latest'; use a full run ID on its own.")
                evidence = get_run(args.db, args.run_id)
            show_report(evidence, args.details)
            if args.json_path:
                destination = write_report_json(evidence, args.json_path, database_paths(args.db))
                print(f"Saved evidence exported: {destination}")
    except (HistoryError, OSError, ValueError) as error:
        print(f"History command failed: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

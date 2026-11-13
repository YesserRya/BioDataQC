#!/usr/bin/env python3
"""Create an offline HTML report from SQL history, including blocked runs.

Example: python scripts/report_dataset.py latest --dataset data/broken --output outputs/broken_report.html
"""

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from biodataqc.history import HistoryError, database_paths, get_latest_run, get_run
from biodataqc.reporting import write_report_html


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_id", help="Full run ID, or latest")
    parser.add_argument("--db", type=Path, default=Path("outputs/biodataqc.sqlite"))
    parser.add_argument("--dataset", type=Path, help="Filter latest by dataset path")
    parser.add_argument("--project", help="Filter latest by project ID")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--owner", help="Operator-supplied responsible person or team")
    parser.add_argument("--next-update", help="Operator-supplied next update; no date is inferred")
    args = parser.parse_args()
    try:
        if args.run_id == "latest":
            evidence = get_latest_run(args.db, project_id=args.project, dataset_path=args.dataset)
        else:
            if args.dataset is not None or args.project is not None:
                raise ValueError("--dataset and --project apply to latest, not an explicit run ID.")
            evidence = get_run(args.db, args.run_id)
        destination = write_report_html(evidence, args.output, owner=args.owner, next_update=args.next_update,
                                         protected_paths=database_paths(args.db))
    except (HistoryError, OSError, ValueError) as error:
        print(f"Report generation failed: {error}", file=sys.stderr)
        return 2
    print(f"HTML report saved: {destination}")
    print(f"Run ID: {evidence['run_id']}")
    print(f"Historical status: {evidence['status']}")
    print("Open the HTML file in your browser. Generating a report does not revalidate inputs.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

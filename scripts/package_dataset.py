#!/usr/bin/env python3
"""Build, verify and list local delivery packages linked to SQL history.

Examples:
    python scripts/package_dataset.py build latest --dataset data/clean --output outputs/packages/clean_delivery
    python scripts/package_dataset.py verify outputs/packages/clean_delivery
    python scripts/package_dataset.py list

Exit 0: requested operation completed. Exit 1: packaging/verification refused.
Exit 2: storage, input/output or configuration error. No external transfer occurs.
"""

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from biodataqc.history import HistoryError, get_latest_run, list_packages
from biodataqc.packaging import PackagingError, build_package, verify_package


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("outputs/biodataqc.sqlite"))
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build", help="Prepare a new verified local package")
    build.add_argument("run_id", help="Full run ID, or latest")
    build.add_argument("--dataset", type=Path, help="Filter latest by dataset path")
    build.add_argument("--project", help="Filter latest by project ID")
    build.add_argument("--output", type=Path, required=True, help="New destination directory")
    build.add_argument("--owner")
    build.add_argument("--next-update")
    verify = commands.add_parser("verify", help="Check a recorded package against its manifest and SQL evidence")
    verify.add_argument("package_path", type=Path)
    listing = commands.add_parser("list", help="List completed package references")
    listing.add_argument("--run-id")
    listing.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()
    try:
        if args.command == "build":
            run_id = args.run_id
            if run_id == "latest":
                run_id = get_latest_run(args.db, project_id=args.project, dataset_path=args.dataset)["run_id"]
            elif args.dataset is not None or args.project is not None:
                raise ValueError("--dataset and --project apply to latest, not an explicit run ID.")
            record = build_package(args.db, run_id, args.output, owner=args.owner, next_update=args.next_update)
            print("Package status: COMPLETE")
        elif args.command == "verify":
            record = verify_package(args.db, args.package_path)
            print("Package verification: VERIFIED")
        else:
            packages = list_packages(args.db, run_id=args.run_id, limit=args.limit)
            if not packages:
                print("No completed packages recorded.")
            for package in packages:
                print(f"Package: {package['package_id']} | Run: {package['run_id']}")
                print(f"  Completed: {package['completed_at']} | Files: {package['files_count']}")
                print(f"  Path: {package['package_path']}")
            print("Stored completion records describe creation time; use verify to check a package now.")
            return 0
        print(f"Package ID: {record['package_id']}")
        print(f"Run ID: {record['run_id']}")
        print(f"Directory: {record['package_path']}")
        print(f"Files (including manifest): {record['files_count']}")
        print(f"Manifest SHA-256: {record['manifest_sha256']}")
        print("Local staging only. No files were transferred externally.")
    except PackagingError as error:
        print(f"Package operation refused: {error}", file=sys.stderr)
        return 1
    except (HistoryError, OSError, ValueError) as error:
        print(f"Package operation failed: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

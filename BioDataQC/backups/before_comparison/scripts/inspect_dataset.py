#!/usr/bin/env python3
"""Inspect BioDataQC inputs; this command does not validate FASTQ records.

Examples, from the project root:
    python scripts/inspect_dataset.py data/clean
    python scripts/inspect_dataset.py data/broken --details
    python scripts/inspect_dataset.py data/broken --json outputs/broken_inventory.json

Exit 0: inventory completed, even if missing/unexpected entries were found.
Exit 2: an input could not be loaded or the requested export could not be written.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from biodataqc.inputs import DatasetLoadError, load_dataset


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path, help="Dataset folder containing requirements.json")
    parser.add_argument("--details", action="store_true", help="Also print metadata and the full inventory")
    parser.add_argument("--json", type=Path, dest="json_path", help="Export inventory evidence as JSON")
    args = parser.parse_args()
    try:
        dataset = load_dataset(args.dataset)
    except DatasetLoadError as error:
        print(f"Input loading failed: {error}", file=sys.stderr)
        return 2

    print(f"Dataset: {dataset.root}")
    print(f"Project: {dataset.requirements['project_id']}")
    for key, value in dataset.summary().items():
        print(f"{key.replace('_', ' ').capitalize()}: {value}")
    print("\nExpected sample inventory:")
    print(dataset.sample_inventory().to_string(index=False, na_rep="-"))

    id_column = dataset.requirements["metadata"]["sample_id_column"]
    if id_column not in dataset.metadata.columns:
        print(f"\nCSV has no {id_column!r} column; sample-row association is unavailable.")
    else:
        actual_ids = set(dataset.metadata[id_column])
        extra_ids = sorted(actual_ids - set(dataset.requirements["expected_samples"]))
        if extra_ids:
            print(f"\nMetadata IDs outside the expected list: {extra_ids!r}")

    differences = [entry for entry in dataset.entries if entry.status != "PRESENT"]
    if differences:
        print("\nInventory differences:")
        for entry in differences:
            print(f"  {entry.status}: {entry.relative_path} ({entry.entry_type})")
    else:
        print("\nNo file-inventory differences found.")

    if args.details:
        print("\nMetadata (values preserved as text):")
        print(dataset.metadata.to_string(index=False))
        print("\nFull file inventory:")
        print(dataset.file_inventory().to_string(index=False, na_rep="-"))

    print("\nValidation status: NOT_VALIDATED. FASTQ content checks belong to stage 5.")
    if args.json_path:
        try:
            destination = args.json_path.expanduser().resolve()
            if destination.is_relative_to(dataset.root):
                raise ValueError("Save exports outside the input dataset, for example in outputs/.")
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(
                json.dumps(dataset.to_dict(), indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        except (OSError, ValueError) as error:
            print(f"Inventory export failed: {error}", file=sys.stderr)
            return 2
        print(f"Inventory JSON saved: {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

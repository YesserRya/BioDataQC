#!/usr/bin/env python3
"""Create two saved runs of an isolated copy: missing mate, then restored mate.

Defaults to the already downloaded public yeast dataset. No network access.
Existing destination directories are refused; source data is only read.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import sys
import tempfile


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from biodataqc.comparison import compare_runs
from biodataqc.history import check_database_destination, save_run
from biodataqc.validation import validate_dataset


def create_demo(source: Path, destination: Path, database: Path) -> dict:
    source = source.expanduser().resolve()
    destination = destination.expanduser().absolute()
    if destination.exists() or destination.is_symlink():
        raise ValueError("Demo destination already exists. Choose another --dataset folder; no files were replaced.")
    destination = destination.resolve()
    if destination.is_relative_to(source) or source.is_relative_to(destination):
        raise ValueError("Keep the demo folder separate from its source.")
    check_database_destination(database, source)
    check_database_destination(database, destination)
    original = validate_dataset(source)
    if original.status != "READY":
        raise ValueError("The source must validate READY. Prepare data/public_yeast first, or choose a valid --source.")
    config = json.loads(json.dumps(original.requirements))
    config["project_id"] += "_COMPARISON_DEMO"
    config["client_label"] = "Isolated correction demonstration"
    if isinstance(config.get("provenance"), dict):
        config["provenance"]["deliberate_faults"] = [
            "Correction demonstration: one mate is deliberately removed for the baseline run, then restored for the newer run."
        ]
    missing = next(e["relative_path"] for e in original.inventory if e["expected"] and e["read"] == "R2")
    # Copy only the inventoried delivery inputs, never adjacent reports or SQL.
    # Exclusive creation refuses existing files, including dangling symlinks.
    destination.mkdir(parents=True, exist_ok=False)
    paths = [config["metadata"]["file"], *[e["relative_path"] for e in original.inventory]]
    for relative in paths:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        with (source / relative).open("rb") as reader, target.open("xb") as writer:
            shutil.copyfileobj(reader, writer)
    with (destination / "requirements.json").open("x", encoding="utf-8") as handle:
        json.dump(config, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    mate = destination / missing
    # Restore from the exact copied bytes even if the source changes afterwards.
    with tempfile.TemporaryFile() as preserved:
        with mate.open("rb") as reader:
            shutil.copyfileobj(reader, preserved)
        mate.unlink()
        try:
            before = validate_dataset(destination)
            if before.status != "BLOCKED" or before.counts()["blocking_findings"] != 1:
                raise ValueError("The deliberate fault did not produce the expected single blocker; inspect the demo copy.")
            save_run(before, database)
        finally:
            preserved.seek(0)
            with mate.open("xb") as handle:
                shutil.copyfileobj(preserved, handle)
    after = validate_dataset(destination)
    if after.status != "READY":
        raise ValueError("The restored copy did not validate READY. Any saved baseline is retained; inspect the demo copy.")
    save_run(after, database)
    result = compare_runs(before.to_dict(), after.to_dict())
    return {"dataset": str(destination), "project_id": config["project_id"],
            "baseline_run_id": before.run_id, "baseline_status": before.status,
            "newer_run_id": after.run_id, "newer_status": after.status,
            "restored_file": missing, "comparison_counts": result["counts"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("data/public_yeast"))
    parser.add_argument("--dataset", type=Path, default=Path("data/comparison_demo"))
    parser.add_argument("--db", type=Path, default=Path("outputs/biodataqc.sqlite"))
    args = parser.parse_args()
    try:
        result = create_demo(args.source, args.dataset, args.db)
    except (ValueError, OSError) as error:
        print(f"Comparison demo failed: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, ensure_ascii=False))
    print("Open Streamlit → Custom folder → data/comparison_demo (or your --dataset path) → History → Compare selected runs.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

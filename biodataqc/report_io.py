"""Write report evidence atomically without replacing input or database files."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


def check_report_destination(evidence: dict, destination: str | Path, protected_paths=()) -> Path:
    destination = Path(destination).expanduser().resolve()
    if destination.is_relative_to(Path(evidence["dataset_path"]).expanduser().resolve()):
        raise ValueError("Save validation reports outside the input dataset, for example in outputs/.")
    for protected in protected_paths:
        protected = Path(protected).expanduser().resolve()
        if destination == protected or (destination.exists() and protected.exists() and
                                         destination.samefile(protected)):
            raise ValueError("The report path conflicts with the SQLite database or one of its sidecar files.")
    return destination


def write_report_json(evidence: dict, destination: str | Path, protected_paths=()) -> Path:
    destination = check_report_destination(evidence, destination, protected_paths)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=destination.parent,
                                         prefix=".validation-", suffix=".json", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(evidence, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return destination

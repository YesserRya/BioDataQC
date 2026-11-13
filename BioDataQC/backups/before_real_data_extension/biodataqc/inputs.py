"""Load BioDataQC inputs and inventory files without validating FASTQ content.

Public entry point: load_dataset(dataset_path).
Pandas is used for tables. CSV values are read as literal strings first, so
identifiers, blanks and text such as 'NA' are preserved for later validation.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import stat
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd


class DatasetLoadError(ValueError):
    """A required input could not be interpreted or inventoried safely."""

    def __init__(self, message: str, rule_id: str = "INPUT001", outcome: str = "FAIL"):
        super().__init__(message)
        self.rule_id = rule_id
        self.outcome = outcome


@dataclass(frozen=True)
class InventoryEntry:
    relative_path: str
    sample_id: str | None
    read: str | None
    expected: bool
    status: str
    entry_type: str
    size_bytes: int | None


@dataclass
class DatasetInputs:
    root: Path
    requirements: dict[str, Any]
    metadata: pd.DataFrame
    entries: tuple[InventoryEntry, ...]
    source_hashes: dict[str, str] = field(default_factory=dict)

    def summary(self) -> dict[str, int]:
        return {
            "expected_samples": len(self.requirements["expected_samples"]),
            "metadata_rows": len(self.metadata),
            "expected_files": sum(entry.expected for entry in self.entries),
            "observed_regular_files": sum(entry.entry_type == "FILE" for entry in self.entries),
            "present_expected_files": sum(entry.status == "PRESENT" for entry in self.entries),
            "missing_expected_files": sum(entry.status == "MISSING" for entry in self.entries),
            "unexpected_entries": sum(not entry.expected for entry in self.entries),
            "wrong_type_expected_entries": sum(entry.status == "WRONG_TYPE" for entry in self.entries),
        }

    def file_inventory(self) -> pd.DataFrame:
        frame = pd.DataFrame([asdict(entry) for entry in self.entries])
        frame["size_bytes"] = pd.array(frame["size_bytes"], dtype="Int64")
        return frame

    def sample_inventory(self) -> pd.DataFrame:
        """Map each expected sample to its raw CSV row count and expected files."""
        id_column = self.requirements["metadata"]["sample_id_column"]
        counts = Counter(self.metadata[id_column]) if id_column in self.metadata.columns else None
        lookup = {(entry.sample_id, entry.read): entry.status
                  for entry in self.entries if entry.expected}
        rows = []
        for sample_id in self.requirements["expected_samples"]:
            row = {
                "sample_id": sample_id,
                "metadata_rows": counts[sample_id] if counts is not None else None,
            }
            for read in self.requirements["sequencing"]["reads"]:
                row[read] = lookup[(sample_id, read)]
            rows.append(row)
        frame = pd.DataFrame(rows)
        frame["metadata_rows"] = pd.array(frame["metadata_rows"], dtype="Int64")
        return frame

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready inventory evidence; this is not a validation report."""
        return {
            "dataset_path": str(self.root),
            "project_id": self.requirements["project_id"],
            "stage": "input_loading_and_inventory",
            "validation_status": "NOT_VALIDATED",
            "summary": self.summary(),
            "requirements": self.requirements,
            "metadata_columns": list(self.metadata.columns),
            "metadata_rows": self.metadata.to_dict(orient="records"),
            "inventory": [asdict(entry) for entry in self.entries],
        }


def _unique_json_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _local_path(root: Path, relative: str, label: str) -> Path:
    if not isinstance(relative, str) or not relative or relative in (".", ".."):
        raise DatasetLoadError(f"{label} must be a nonempty relative path.")
    part = Path(relative)
    if part.is_absolute() or ".." in part.parts or "\\" in relative:
        raise DatasetLoadError(f"{label} must stay inside the selected dataset.")
    candidate = root / part
    if candidate.is_symlink() or not candidate.resolve().is_relative_to(root):
        raise DatasetLoadError(f"{label} cannot refer to a symlink or outside path: {relative}")
    return candidate


def _load_requirements(root: Path, source_hashes: dict | None = None) -> dict[str, Any]:
    path = _local_path(root, "requirements.json", "Configuration")
    raw = path.read_bytes()
    config = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_json_keys)
    if source_hashes is not None:
        source_hashes["requirements.json"] = hashlib.sha256(raw).hexdigest()
    if not isinstance(config, dict):
        raise DatasetLoadError("requirements.json must contain a JSON object.")
    if not isinstance(config.get("project_id"), str) or not config["project_id"].strip():
        raise DatasetLoadError("Configuration needs a nonempty project_id.")
    if config.get("specification_version") != "1.0.0":
        raise DatasetLoadError("This loader supports specification_version 1.0.0.")
    samples = config.get("expected_samples")
    if not isinstance(samples, list) or not samples:
        raise DatasetLoadError("expected_samples must be a nonempty list.")
    for sample in samples:
        if (not isinstance(sample, str) or not sample.strip() or sample != sample.strip()
                or sample in (".", "..") or "/" in sample or "\\" in sample):
            raise DatasetLoadError("Expected sample IDs must be nonempty, safe filename components.")
    if len(set(samples)) != len(samples):
        raise DatasetLoadError("expected_samples contains duplicate IDs.")
    metadata = config.get("metadata")
    sequencing = config.get("sequencing")
    if not isinstance(metadata, dict) or not isinstance(sequencing, dict):
        raise DatasetLoadError("Configuration needs metadata and sequencing objects.")
    if metadata.get("encoding") != "utf-8" or metadata.get("delimiter") != ",":
        raise DatasetLoadError("This loader supports comma-delimited UTF-8 metadata.")
    if not isinstance(metadata.get("sample_id_column"), str) or not metadata["sample_id_column"]:
        raise DatasetLoadError("metadata.sample_id_column must be provided.")
    supported = {
        "layout": "paired_end",
        "reads": ["R1", "R2"],
        "files_per_read_per_sample": 1,
        "filename_template": "{sample_id}_{read}.fastq.gz",
        "scan_scope": "immediate_children",
    }
    for key, value in supported.items():
        if sequencing.get(key) != value:
            raise DatasetLoadError(f"Unsupported sequencing.{key}: {sequencing.get(key)!r}")
    _local_path(root, metadata.get("file"), "Metadata file")
    _local_path(root, sequencing.get("directory"), "Sequencing directory")
    return config


def _load_metadata(path: Path, source_hashes: dict | None = None, relative: str = "samples.csv") -> pd.DataFrame:
    raw = path.read_bytes()
    with io.StringIO(raw.decode("utf-8"), newline="") as handle:
        reader = csv.reader(handle, delimiter=",", strict=True)
        try:
            headers = next(reader)
        except StopIteration as error:
            raise DatasetLoadError(f"Empty CSV with no header: {path.name}") from error
        if not headers or any(not column for column in headers):
            raise DatasetLoadError("CSV header names must be nonempty.")
        if len(set(headers)) != len(headers):
            raise DatasetLoadError("Duplicate CSV header names are ambiguous.")
        rows = []
        for row in reader:
            if len(row) != len(headers):
                raise DatasetLoadError(
                    f"CSV record ending at line {reader.line_num} has {len(row)} fields; "
                    f"expected {len(headers)}."
                )
            rows.append(row)
    if source_hashes is not None:
        source_hashes[relative] = hashlib.sha256(raw).hexdigest()
    # No stripping, missing-value inference or numeric conversion.
    return pd.DataFrame(rows, columns=headers, dtype=str)


def _entry_type(entry: Path) -> tuple[str, int | None]:
    info = entry.lstat()  # Inspect a symlink itself rather than following its target.
    if stat.S_ISREG(info.st_mode):
        return "FILE", info.st_size
    if stat.S_ISDIR(info.st_mode):
        return "DIRECTORY", None
    if stat.S_ISLNK(info.st_mode):
        return "SYMLINK", None
    return "OTHER", None


def _inventory(root: Path, config: dict) -> tuple[InventoryEntry, ...]:
    sequencing = config["sequencing"]
    directory = _local_path(root, sequencing["directory"], "Sequencing directory")
    if not directory.is_dir():
        raise DatasetLoadError(f"Sequencing directory is missing or is not a directory: {directory}")
    observed = {entry.name: entry for entry in sorted(directory.iterdir())}
    expected = {}
    for sample in config["expected_samples"]:
        for read in sequencing["reads"]:
            name = sequencing["filename_template"].format(sample_id=sample, read=read)
            if name in expected:
                raise DatasetLoadError(f"Configuration creates a duplicate expected filename: {name}")
            expected[name] = (sample, read)

    entries = []
    for name, (sample, read) in expected.items():
        path = observed.get(name)
        entry_type, size = _entry_type(path) if path is not None else ("MISSING", None)
        status = "MISSING" if path is None else "PRESENT" if entry_type == "FILE" else "WRONG_TYPE"
        entries.append(InventoryEntry(
            (directory / name).relative_to(root).as_posix(), sample, read,
            True, status, entry_type, size,
        ))

    # Recognize the supported naming convention for unexpected samples as well.
    filename_pattern = re.compile(r"(?P<sample>.+)_(?P<read>R1|R2)\.fastq\.gz")
    for name in sorted(set(observed) - set(expected)):
        entry_type, size = _entry_type(observed[name])
        match = filename_pattern.fullmatch(name)
        entries.append(InventoryEntry(
            observed[name].relative_to(root).as_posix(),
            match["sample"] if match else None,
            match["read"] if match else None,
            False, "UNEXPECTED", entry_type, size,
        ))
    return tuple(entries)


def _load_step(rule_id, operation):
    try:
        return operation()
    except DatasetLoadError as error:
        raise DatasetLoadError(str(error), rule_id, error.outcome) from error
    except (FileNotFoundError, NotADirectoryError, IsADirectoryError, UnicodeError,
            csv.Error, ValueError, KeyError, TypeError) as error:
        raise DatasetLoadError(str(error), rule_id, "FAIL") from error
    except OSError as error:
        raise DatasetLoadError(str(error), rule_id, "ERROR") from error


def load_dataset(dataset_path: str | Path) -> DatasetInputs:
    """Read a dataset without changing it or judging its delivery readiness."""
    root = Path(dataset_path).expanduser().resolve()
    if not root.is_dir():
        raise DatasetLoadError(f"Dataset directory does not exist: {root}", "FILE001")
    hashes = {}
    requirements = _load_step("CFG001", lambda: _load_requirements(root, hashes))
    metadata_path = _local_path(root, requirements["metadata"]["file"], "Metadata file")
    metadata = _load_step("CSV001", lambda: _load_metadata(
        metadata_path, hashes, metadata_path.relative_to(root).as_posix()))
    entries = _load_step("FILE001", lambda: _inventory(root, requirements))
    return DatasetInputs(root, requirements, metadata, entries, hashes)

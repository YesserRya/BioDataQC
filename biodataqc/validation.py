"""Specification v1 validation, independent of Streamlit and persistence.

Public entry point: validate_dataset(path) -> ValidationReport.
Inputs are read without correction. Required checks fail closed.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import asdict
from pathlib import Path

from .fastq import FastqScan, scan_fastq
from .inputs import DatasetInputs, DatasetLoadError, _inventory, _local_path, load_dataset
from .results import CheckResult, ValidationReport, utc_now
from .read_headers import PUBLIC_HEADER_PROFILES


BASE_CHECKS = ["CFG001", "CSV001", "FILE001", "META001", "META002", "META003",
               "META004", "SNAP001", "REF001"]


def base_checks(config):
    return BASE_CHECKS + (["SRC001"] if config.get("specification_version") == "1.1.0" else [])


def _pass(rule, message, **kwargs):
    return CheckResult(rule, rule, "PASS", message, **kwargs)


def _skip(check_id, rule, message, dependencies, **kwargs):
    return CheckResult(check_id, rule, "NOT_RUN", message, blocked_by=dependencies, **kwargs)


def _error(check_id, rule, error, **kwargs):
    return CheckResult(check_id, rule, "ERROR", f"{type(error).__name__}: {error}",
                       severity="BLOCKER", next_action="Resolve the execution problem and rerun validation.",
                       **kwargs)


def _configuration(config: dict) -> CheckResult:
    """Reject policies this version cannot execute; never silently ignore them."""
    problems = []

    def exact(group, values):
        actual = config if group is None else config.get(group)
        if not isinstance(actual, dict):
            problems.append(f"{group} must be an object")
            return {}
        for key, value in values.items():
            item = actual.get(key)
            if type(item) is not type(value) or item != value:
                problems.append(f"{group + '.' if group else ''}{key} must be {value!r}")
        return actual

    public_data = config.get("specification_version") == "1.1.0"
    exact(None, {"specification_version": "1.1.0" if public_data else "1.0.0",
                 "synthetic_data_only": not public_data})
    meta = exact("metadata", {
        "encoding": "utf-8", "delimiter": ",", "one_row_per_sample": True,
        "blank_required_values": "block", "blank_or_absent_notes": "warn",
        "duplicate_sample_ids": "block", "missing_or_unexpected_sample_ids": "block",
        "unexpected_columns": "warn", "nonblank_values_with_outer_whitespace": "block",
        "case_sensitive_values": True, "automatic_value_correction": False,
    })
    columns = {}
    for key in ("required_columns", "optional_columns"):
        values = meta.get(key)
        if (not isinstance(values, list) or
                any(not isinstance(v, str) or not v.strip() or v != v.strip() for v in values)):
            problems.append(f"metadata.{key} must be a list of nonblank column names")
            columns[key] = []
        elif len(set(values)) != len(values):
            problems.append(f"metadata.{key} contains duplicates")
            columns[key] = values
        else:
            columns[key] = values
    required, optional = columns["required_columns"], columns["optional_columns"]
    if not required or meta.get("sample_id_column") not in required:
        problems.append("metadata.sample_id_column must be a required column")
    if set(required) & set(optional):
        problems.append("Required and optional column lists must not overlap")
    vocabulary = meta.get("allowed_values")
    if not isinstance(vocabulary, dict):
        problems.append("metadata.allowed_values must be an object")
    else:
        for column, values in vocabulary.items():
            if (column not in required + optional or not isinstance(values, list) or not values or
                    any(not isinstance(v, str) or not v.strip() or v != v.strip() for v in values)):
                problems.append(f"Invalid allowed values for {column!r}")
            elif len(values) != len(set(values)):
                problems.append(f"Duplicate allowed values for {column!r}")
    exact("sequencing", {
        "layout": "paired_end", "reads": ["R1", "R2"], "files_per_read_per_sample": 1,
        "filename_template": "{sample_id}_{read}.fastq.gz", "compression": "gzip",
        "empty_files": "block", "unexpected_files": "block", "unexpected_subdirectories": "block",
        "scan_scope": "immediate_children",
    })
    fastq = exact("fastq", {
        "format": "four_line", "encoding": "ascii", "allow_empty_sequences": False,
        "quality_encoding": "phred33",
        "sequence_quality_lengths_must_match": True, "mate_for_read": {"R1": "1", "R2": "2"},
        "paired_record_counts_must_match": True, "paired_read_ids_must_match_in_order": True,
        "scan_all_records": True,
    })
    if public_data:
        if fastq.get("header_profile") not in PUBLIC_HEADER_PROFILES:
            problems.append("fastq.header_profile must name an explicitly supported public-data profile")
        if fastq.get("separator_line") not in ("+", "plus_or_matching_header"):
            problems.append("Unsupported FASTQ separator policy")
        if "header_template" in fastq or "read_id_pattern" in fastq:
            problems.append("Use header_profile without legacy header_template/read_id_pattern in specification 1.1.0")
    else:
        exact("fastq", {"separator_line": "+", "header_template": "@{read_id}/{mate}",
                         "read_id_pattern": "READ_[0-9]{6}"})
        if fastq.get("header_profile", "synthetic_v1") != "synthetic_v1":
            problems.append("Specification 1.0.0 only supports synthetic_v1 headers")
    alphabet = fastq.get("sequence_alphabet")
    if (not isinstance(alphabet, str) or not alphabet or set(alphabet) - set("ACGTN") or
            len(alphabet) != len(set(alphabet))):
        problems.append("fastq.sequence_alphabet must contain distinct characters from ACGTN")
    low, high = fastq.get("quality_ascii_min"), fastq.get("quality_ascii_max")
    if type(low) is not int or type(high) is not int or not 33 <= low <= high <= 126:
        problems.append("Quality bounds must be integers satisfying 33 <= min <= max <= 126")
    minimum = fastq.get("minimum_records_per_file")
    if type(minimum) is not int or minimum < 1:
        problems.append("fastq.minimum_records_per_file must be a positive integer")
    integrity = exact("integrity", {
        "algorithm": "sha256", "verify_staged_copy_against_validation_snapshot": True,
        "snapshot_inputs": ["requirements.json", meta.get("file"),
                            str(config["sequencing"]["directory"]).rstrip("/") + "/*"],
    })
    if type(integrity.get("require_independent_source_checksums")) is not bool:
        problems.append("integrity.require_independent_source_checksums must be a boolean")
    if public_data:
        references = integrity.get("reference_sha256")
        expected_paths = {f"{config['sequencing']['directory']}/{sample}_{read}.fastq.gz"
                          for sample in config["expected_samples"] for read in ("R1", "R2")}
        if (not isinstance(references, dict) or set(references) != expected_paths or
                any(not isinstance(v, str) or not re.fullmatch(r"[0-9a-f]{64}", v)
                    for v in references.values())):
            problems.append("integrity.reference_sha256 must contain one SHA-256 for every expected FASTQ")
        provenance = config.get("provenance")
        if not isinstance(provenance, dict):
            problems.append("Public-data requirements need a provenance object")
        else:
            for key in ("data_origin", "study_accession", "run_accession", "organism",
                        "source_url", "processing", "checksum_scope"):
                if not isinstance(provenance.get(key), str) or not provenance[key].strip():
                    problems.append(f"provenance.{key} must be a nonblank string")
            if provenance.get("data_origin") != "public_experimental_subset":
                problems.append("This real-data profile supports public_experimental_subset")
            faults = provenance.get("deliberate_faults", [])
            if not isinstance(faults, list) or any(not isinstance(f, str) or not f.strip() for f in faults):
                problems.append("provenance.deliberate_faults must be a list of nonblank descriptions")
    exact("packaging", {
        "mode": "local_staging_only", "report_format": "html", "manifest_format": "csv",
        "manifest_columns": ["relative_path", "sample_id", "read", "size_bytes", "sha256"],
        "exclude_manifest_from_own_inventory": True, "preserve_original_inputs": True,
        "allow_blockers": False, "allow_warnings": True, "allow_incomplete_required_checks": False,
    })
    if problems:
        return CheckResult("CFG001", "CFG001", "FAIL", "; ".join(problems), severity="BLOCKER",
                           next_action="Use supported v1 requirements or implement the requested policies.",
                           details={"problems": problems})
    return _pass("CFG001", "All declared validation policies are supported.")


def _required_metadata(dataset: DatasetInputs) -> CheckResult:
    frame, settings = dataset.metadata, dataset.requirements["metadata"]
    missing = [name for name in settings["required_columns"] if name not in frame.columns]
    blanks = [{"csv_record": index + 2, "column": name}
              for name in settings["required_columns"] if name in frame.columns
              for index, value in enumerate(frame[name]) if not value.strip()]
    if missing or blanks:
        return CheckResult("META001", "META001", "FAIL",
                           f"Missing required columns: {missing}; blank required cells: {len(blanks)}.",
                           severity="BLOCKER", file_path=settings["file"],
                           next_action="Provide the missing required metadata and rerun validation.",
                           details={"missing_columns": missing, "blank_cells": blanks})
    return _pass("META001", "Required columns and values are present.")


def _sample_ids(dataset: DatasetInputs) -> CheckResult:
    column = dataset.requirements["metadata"]["sample_id_column"]
    if column not in dataset.metadata.columns:
        return _skip("META002", "META002", "Sample-ID column is unavailable.", ["META001"])
    counts = Counter(dataset.metadata[column])
    expected = set(dataset.requirements["expected_samples"])
    duplicates = {value: count for value, count in sorted(counts.items()) if count > 1}
    missing, unexpected = sorted(expected - counts.keys()), sorted(counts.keys() - expected)
    if duplicates or missing or unexpected:
        return CheckResult("META002", "META002", "FAIL",
                           f"Duplicate IDs: {len(duplicates)}; missing IDs: {missing}; unexpected IDs: {unexpected}.",
                           severity="BLOCKER", file_path=dataset.requirements["metadata"]["file"],
                           next_action="Reconcile sample IDs with the approved expected-sample list.",
                           details={"duplicates": duplicates, "missing": missing, "unexpected": unexpected})
    return _pass("META002", "Exactly one metadata row exists for every expected sample.")


def _metadata_values(dataset: DatasetInputs) -> CheckResult:
    frame, settings = dataset.metadata, dataset.requirements["metadata"]
    violations = []
    for name in frame.columns:
        allowed = settings["allowed_values"].get(name)
        for index, value in enumerate(frame[name]):
            if not value.strip():
                continue  # Required/optional blank handling has its own result.
            if value != value.strip():
                violations.append({"csv_record": index + 2, "column": name,
                                   "value": value, "reason": "outer_whitespace"})
            if allowed is not None and value not in allowed:
                violations.append({"csv_record": index + 2, "column": name,
                                   "value": value, "reason": "not_in_allowed_values"})
    if violations:
        return CheckResult("META003", "META003", "FAIL",
                           f"Found {len(violations)} vocabulary or whitespace violations.",
                           severity="BLOCKER", file_path=settings["file"],
                           next_action="Review the recorded values; use exact approved spelling and spacing.",
                           details={"violations": violations})
    unavailable = [c for c in settings["allowed_values"]
                   if c in settings["required_columns"] and c not in frame.columns]
    if unavailable:
        return _skip("META003", "META003", f"Required controlled columns unavailable: {unavailable}.", ["META001"])
    return _pass("META003", "Present nonblank values satisfy vocabulary and whitespace rules.")


def _optional_metadata(dataset: DatasetInputs) -> CheckResult:
    frame, settings = dataset.metadata, dataset.requirements["metadata"]
    absent = [c for c in settings["optional_columns"] if c not in frame.columns]
    blanks = [{"csv_record": index + 2, "column": name}
              for name in settings["optional_columns"] if name in frame.columns
              for index, value in enumerate(frame[name]) if not value.strip()]
    extra = sorted(set(frame.columns) - set(settings["required_columns"] + settings["optional_columns"]))
    if absent or blanks or extra:
        return CheckResult("META004", "META004", "FAIL",
                           f"Absent optional columns: {absent}; blank optional cells: {len(blanks)}; extra columns: {extra}.",
                           severity="WARNING", file_path=settings["file"],
                           next_action="Review the optional metadata warning; v1 permits readiness with warnings.",
                           details={"absent_optional_columns": absent, "blank_cells": blanks, "extra_columns": extra})
    return _pass("META004", "Optional metadata is populated and no unexpected columns were found.")


def _snapshot(dataset: DatasetInputs, hashes: dict[str, str], scans: dict[str, FastqScan]) -> CheckResult:
    """Recheck observed inputs; staging must recheck them again before use."""
    if _inventory(dataset.root, dataset.requirements) != dataset.entries:
        raise OSError("File inventory changed during validation; rerun against stable inputs.")
    for relative, expected_hash in list(hashes.items()):
        path = _local_path(dataset.root, relative, "Snapshot input")
        with path.open("rb") as handle:
            actual = hashlib.file_digest(handle, "sha256").hexdigest()
        if actual != expected_hash:
            raise OSError(f"Input changed during validation: {relative}")
    missing_hashes = [f"FASTQ001:{e.relative_path}" for e in dataset.entries
                      if e.expected and e.status == "PRESENT" and
                      (e.relative_path not in scans or scans[e.relative_path].file_sha256 is None)]
    if missing_hashes:
        return _skip("SNAP001", "SNAP001", "A complete fingerprint is unavailable for some observed files.", missing_hashes)
    return _pass("SNAP001", "Input fingerprints and inventory are unchanged at the final check.",
                 details={"fingerprinted_files": len(hashes), "algorithm": "sha256"})


def _source_references(dataset: DatasetInputs, scans: dict[str, FastqScan]) -> CheckResult:
    references = dataset.requirements["integrity"]["reference_sha256"]
    dependencies = [f"FASTQ001:{path}" for path in references
                    if path not in scans or scans[path].check.outcome != "PASS"
                    or scans[path].file_sha256 is None]
    if dependencies:
        return _skip("SRC001", "SRC001", "All expected FASTQ files must be valid and fingerprinted before source comparison.",
                     dependencies)
    mismatches = [path for path, digest in references.items() if scans[path].file_sha256 != digest]
    details = {"reference_source": dataset.requirements["provenance"]["source_url"],
               "checksum_scope": dataset.requirements["provenance"]["checksum_scope"],
               "mismatched_files": mismatches}
    if mismatches:
        return CheckResult("SRC001", "SRC001", "FAIL", "FASTQ content differs from configured source SHA-256 references: " + ", ".join(mismatches),
                           severity="BLOCKER", details=details,
                           next_action="Check the source and dataset specification, restore the intended files, then validate again.")
    return _pass("SRC001", "Compressed FASTQ bytes match the configured source SHA-256 references.", details=details)


def validate_dataset(dataset_path: str | Path) -> ValidationReport:
    report = ValidationReport(str(Path(dataset_path).expanduser().absolute()),
                              expected_check_ids=list(BASE_CHECKS))

    def finish():
        known = {c.check_id for c in report.checks}
        causes = [c.check_id for c in report.checks if c.outcome in {"FAIL", "ERROR"}]
        for check_id in report.expected_check_ids:
            if check_id not in known:
                report.checks.append(_skip(check_id, check_id.split(":")[0],
                                           "Prerequisite unavailable; check was not executed.", causes))
        report.finished_at = utc_now()
        return report

    def execute(rule, operation):
        try:
            result = operation()
        except Exception as error:
            result = _error(rule, rule, error)
        report.checks.append(result)
        return result

    try:
        dataset = load_dataset(dataset_path)
    except DatasetLoadError as error:
        report.checks.append(CheckResult(error.rule_id, error.rule_id, error.outcome, str(error),
                                         severity="BLOCKER", next_action="Restore valid, readable inputs and rerun validation."))
        return finish()
    except Exception as error:
        report.checks.append(_error("INPUT001", "INPUT001", error))
        return finish()

    report.dataset_path = str(dataset.root)
    report.project_id = dataset.requirements["project_id"]
    report.requirements = dataset.requirements
    report.expected_check_ids = list(base_checks(dataset.requirements))
    report.metadata_columns = list(dataset.metadata.columns)
    report.metadata_rows = dataset.metadata.to_dict(orient="records")
    report.inventory_summary = dataset.summary()
    report.inventory = [asdict(e) for e in dataset.entries]
    report.input_sha256 = dict(dataset.source_hashes)
    expected = [e for e in dataset.entries if e.expected]
    report.expected_check_ids += [f"FILE001:{e.relative_path}" for e in dataset.entries]
    report.expected_check_ids += [f"FASTQ001:{e.relative_path}" for e in expected]
    report.expected_check_ids += [f"PAIR001:{s}" for s in dataset.requirements["expected_samples"]]
    if execute("CFG001", lambda: _configuration(dataset.requirements)).outcome != "PASS":
        return finish()
    report.checks += [_pass("CSV001", "CSV was parsed without ambiguous headers or field counts."),
                      _pass("FILE001", "Immediate sequencing-directory entries were inventoried.")]
    for rule, function in [("META001", _required_metadata), ("META002", _sample_ids),
                           ("META003", _metadata_values), ("META004", _optional_metadata)]:
        execute(rule, lambda function=function: function(dataset))

    scans = {}
    for entry in dataset.entries:
        check_id = f"FILE001:{entry.relative_path}"
        common = dict(check_id=check_id, rule_id="FILE001", sample_id=entry.sample_id,
                      file_path=entry.relative_path)
        if entry.status == "PRESENT":
            report.checks.append(CheckResult(**common, outcome="PASS", message="Expected regular file is present."))
        else:
            report.checks.append(CheckResult(
                **common, outcome="FAIL", severity="BLOCKER",
                message=f"{entry.status}: {entry.relative_path} ({entry.entry_type}).",
                next_action="Reconcile the delivery with the expected inventory, then rerun validation.",
                details={"inventory_status": entry.status, "entry_type": entry.entry_type}))
        if not entry.expected:
            continue
        fastq_id = f"FASTQ001:{entry.relative_path}"
        if entry.status != "PRESENT":
            report.checks.append(_skip(fastq_id, "FASTQ001", "Expected regular file is unavailable.",
                                       [check_id], sample_id=entry.sample_id, file_path=entry.relative_path))
            continue
        try:
            scan = scan_fastq(dataset.root, entry, dataset.requirements["fastq"])
        except Exception as error:
            scan = FastqScan(_error(fastq_id, "FASTQ001", error,
                                    sample_id=entry.sample_id, file_path=entry.relative_path))
        scans[entry.relative_path] = scan
        report.checks.append(scan.check)
        if scan.file_sha256 is not None:
            report.input_sha256[entry.relative_path] = scan.file_sha256

    by_sample = {(e.sample_id, e.read): e for e in expected}
    for sample in dataset.requirements["expected_samples"]:
        entries = [by_sample[(sample, read)] for read in ("R1", "R2")]
        pair = [scans.get(e.relative_path) for e in entries]
        dependencies = [f"FASTQ001:{e.relative_path}" for e, scan in zip(entries, pair)
                        if scan is None or scan.check.outcome != "PASS"]
        common = dict(check_id=f"PAIR001:{sample}", rule_id="PAIR001", sample_id=sample)
        if dependencies:
            report.checks.append(CheckResult(**common, outcome="NOT_RUN", blocked_by=dependencies,
                                              message="Both mates must pass structural validation before comparison."))
            continue
        first, second = pair
        details = {"R1_records": first.records, "R2_records": second.records,
                   "R1_read_ids_sha256": first.read_ids_sha256,
                   "R2_read_ids_sha256": second.read_ids_sha256}
        if first.records != second.records or first.read_ids_sha256 != second.read_ids_sha256:
            report.checks.append(CheckResult(
                **common, outcome="FAIL", severity="BLOCKER",
                message="Mate record counts or ordered read IDs differ.", details=details,
                next_action="Obtain the correctly matched R1/R2 pair and rerun validation."))
        else:
            report.checks.append(CheckResult(**common, outcome="PASS", details=details,
                                              message="Mate record counts and ordered read IDs match."))

    if "SRC001" in report.expected_check_ids:
        execute("SRC001", lambda: _source_references(dataset, scans))
    execute("SNAP001", lambda: _snapshot(dataset, report.input_sha256, scans))
    require_source = dataset.requirements["integrity"]["require_independent_source_checksums"]
    report.checks.append(_skip(
        "REF001", "REF001",
        ("Independent source checksums are required, but no trusted reference comparison is implemented."
         if require_source else "Independent source checksums are not required by this specification."),
        [], required=require_source,
        next_action=("Implement and supply independent source checksum references before declaring readiness."
                     if require_source else None)))
    return finish()

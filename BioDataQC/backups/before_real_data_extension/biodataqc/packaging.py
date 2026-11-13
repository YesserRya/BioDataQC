"""Verified local staging from saved READY evidence; no external transfers."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from dataclasses import asdict, fields
from pathlib import Path, PurePosixPath
from uuid import uuid4

from .history import (_connect, _verify_schema, get_package_by_path, get_run)
from .inputs import _inventory
from .reporting import render_report
from .results import CheckResult, RULES_VERSION, ValidationReport, utc_now
from .validation import BASE_CHECKS, _configuration


MANIFEST_COLUMNS = ["relative_path", "sample_id", "read", "size_bytes", "sha256"]
GENERATED = {"report.html", "run_summary.json"}
RESERVED = GENERATED | {"manifest.csv"}


class PackagingError(ValueError):
    """A package cannot be prepared or verified against its recorded evidence."""


def _relative(value: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\0" in value:
        raise PackagingError("Package paths must be nonempty, canonical relative POSIX paths.")
    path = PurePosixPath(value)
    if path.is_absolute() or path.as_posix() != value or any(p in {".", ".."} for p in path.parts) or value == ".":
        raise PackagingError(f"Unsafe or noncanonical relative path: {value!r}")
    return value


def _safe_file(root: Path, relative: str) -> Path:
    path = root
    for component in PurePosixPath(_relative(relative)).parts:
        path = path / component
        if path.is_symlink():
            raise PackagingError(f"Symlinks are not allowed in package input paths: {relative}")
    if not path.is_file() or not stat.S_ISREG(path.stat().st_mode):
        raise PackagingError(f"Expected a regular file: {relative}")
    return path


def _identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _sha256(path: Path) -> str:
    if path.is_symlink():
        raise PackagingError(f"Refusing a symlink: {path.name}")
    with path.open("rb") as handle:
        initial = os.fstat(handle.fileno())
        if not stat.S_ISREG(initial.st_mode):
            raise PackagingError(f"Not a regular file: {path.name}")
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
        if (path.is_symlink() or _identity(initial) != _identity(os.fstat(handle.fileno())) or
                _identity(initial) != _identity(path.stat())):
            raise PackagingError(f"File changed while fingerprinting: {path.name}")
    return digest


def _input_plan(evidence: dict) -> dict[str, tuple[str | None, str | None]]:
    """Recheck readiness and evidence completeness in the shared backend."""
    try:
        if evidence["status"] != "READY":
            raise PackagingError(f"Packaging refused: saved run is {evidence['status']}; a READY run is required.")
        if evidence["rules_version"] != RULES_VERSION or evidence["report_schema_version"] != "1.1.0":
            raise PackagingError("Unsupported evidence version; validate again with the supported engine.")
        config = evidence["requirements"]
        if _configuration(config).outcome != "PASS":
            raise PackagingError("The saved requirements are not supported for packaging.")
        if config.get("project_id") != evidence["project_id"]:
            raise PackagingError("Project identity is inconsistent in the saved evidence.")
        values = {f.name: evidence[f.name] for f in fields(ValidationReport) if f.name in evidence}
        values["checks"] = [CheckResult(**c) for c in evidence["checks"]]
        restored = ValidationReport(**values)
        if restored.status != "READY" or restored.counts() != evidence["counts"]:
            raise PackagingError("Saved readiness or counters disagree with the recorded check outcomes.")
        samples = config["expected_samples"]
        if not isinstance(samples, list) or not samples or len(set(samples)) != len(samples):
            raise PackagingError("Invalid expected sample list in saved evidence.")
        for sample in samples:
            if (not isinstance(sample, str) or not sample or sample != sample.strip() or
                    "/" in sample or "\\" in sample or sample in {".", ".."}):
                raise PackagingError("Unsafe sample ID in saved evidence.")
        metadata = _relative(config["metadata"]["file"])
        directory = _relative(config["sequencing"]["directory"])
        plan = {"requirements.json": (None, None), metadata: (None, None)}
        if metadata == "requirements.json":
            raise PackagingError("Metadata and requirements paths collide.")
        sequence_paths = {}
        for sample in samples:
            for read in ("R1", "R2"):
                relative = _relative(f"{directory}/{sample}_{read}.fastq.gz")
                if relative in plan:
                    raise PackagingError("Configured input paths collide.")
                plan[relative] = sequence_paths[relative] = (sample, read)
        inventory = {e["relative_path"]: e for e in evidence["inventory"]}
        if len(inventory) != len(evidence["inventory"]) or set(inventory) != set(sequence_paths):
            raise PackagingError("Saved inventory does not exactly match the expected sequencing files.")
        for relative, (sample, read) in sequence_paths.items():
            entry = inventory[relative]
            if (entry["expected"] is not True or entry["status"] != "PRESENT" or entry["entry_type"] != "FILE" or
                    entry["sample_id"] != sample or entry["read"] != read or
                    type(entry["size_bytes"]) is not int or entry["size_bytes"] <= 0):
                raise PackagingError(f"Invalid inventory evidence for {relative}.")
        roster = (set(BASE_CHECKS) | {f"FILE001:{p}" for p in sequence_paths} |
                  {f"FASTQ001:{p}" for p in sequence_paths} | {f"PAIR001:{s}" for s in samples})
        if set(evidence["expected_check_ids"]) != roster or {c.check_id for c in restored.checks} != roster:
            raise PackagingError("The required check roster is incomplete or inconsistent.")
        for check in restored.checks:
            required = check.check_id != "REF001" or config["integrity"]["require_independent_source_checksums"]
            if check.required is not required:
                raise PackagingError(f"Required-check flag is inconsistent for {check.check_id}.")
            allowed_warning = check.check_id == "META004" and check.outcome == "FAIL" and check.severity == "WARNING"
            optional_reference = check.check_id == "REF001" and not required and check.outcome == "NOT_RUN"
            if check.outcome != "PASS" and not allowed_warning and not optional_reference:
                raise PackagingError(f"Required check did not pass: {check.check_id}.")
        hashes = evidence["input_sha256"]
        if set(hashes) != set(plan) or any(not isinstance(h, str) or not re.fullmatch("[0-9a-f]{64}", h) for h in hashes.values()):
            raise PackagingError("The saved input fingerprint set is incomplete or invalid.")
        all_paths = list(plan) + list(RESERVED)
        if set(plan) & RESERVED:
            raise PackagingError("An input path collides with a generated package filename.")
        for name in all_paths:
            if any(parent.as_posix() in all_paths for parent in PurePosixPath(name).parents if parent.as_posix() != "."):
                raise PackagingError("Package file and directory paths collide.")
        return plan
    except PackagingError:
        raise
    except (KeyError, TypeError, ValueError) as error:
        raise PackagingError(f"Saved evidence is incomplete or malformed: {error}") from error


def verify_current_inputs(evidence: dict, plan=None) -> Path:
    plan = _input_plan(evidence) if plan is None else plan
    original = Path(evidence["dataset_path"]).expanduser()
    if not original.is_absolute() or original.is_symlink() or original.resolve() != original or not original.is_dir():
        raise PackagingError("The recorded source directory is unavailable or its path changed; revalidate.")
    root = original
    expected_inventory = evidence["inventory"]
    if [asdict(e) for e in _inventory(root, evidence["requirements"])] != expected_inventory:
        raise PackagingError("Input inventory changed after validation; revalidate before packaging.")
    for relative in plan:
        if _sha256(_safe_file(root, relative)) != evidence["input_sha256"][relative]:
            raise PackagingError(f"Input changed after validation: {relative}. Revalidate before packaging.")
    if [asdict(e) for e in _inventory(root, evidence["requirements"])] != expected_inventory:
        raise PackagingError("Input inventory changed while checking the snapshot; revalidate.")
    return root


def _copy_input(source: Path, target: Path):
    target.parent.mkdir(parents=True, exist_ok=True)
    # The target is a fresh private staging tree. Inputs are opened read-only.
    with source.open("rb") as incoming, target.open("xb") as outgoing:
        shutil.copyfileobj(incoming, outgoing, length=1024 * 1024)


def _write_manifest(stage: Path, plan, expected_hashes):
    rows = []
    for relative in sorted(expected_hashes):
        path = _safe_file(stage, relative)
        digest = _sha256(path)
        if digest != expected_hashes[relative]:
            raise PackagingError(f"Staged copy does not match its expected fingerprint: {relative}")
        sample, read = plan.get(relative, (None, None))
        rows.append([relative, sample or "", read or "", path.stat().st_size, digest])
    with (stage / "manifest.csv").open("x", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(MANIFEST_COLUMNS)
        writer.writerows(rows)


def _verify_directory(root: Path, manifest_sha256: str, evidence: dict, plan) -> dict:
    if root.is_symlink() or not root.is_dir() or root.resolve() != root:
        raise PackagingError("Package directory is missing or uses a changed/symlinked path.")
    manifest = _safe_file(root, "manifest.csv")
    if _sha256(manifest) != manifest_sha256:
        raise PackagingError("Manifest fingerprint does not match the recorded package evidence.")
    rows = {}
    with manifest.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle, strict=True)
        if next(reader, None) != MANIFEST_COLUMNS:
            raise PackagingError("Unexpected manifest columns.")
        for row in reader:
            if len(row) != len(MANIFEST_COLUMNS):
                raise PackagingError("Malformed manifest row.")
            relative, sample, read, size, digest = row
            _relative(relative)
            if relative in rows or relative == "manifest.csv":
                raise PackagingError("Duplicate or self-referencing manifest entry.")
            if not size.isdecimal() or not re.fullmatch("[0-9a-f]{64}", digest):
                raise PackagingError("Invalid manifest size or SHA-256 value.")
            rows[relative] = (sample, read, int(size), digest)
    expected_files = set(plan) | GENERATED
    if set(rows) != expected_files:
        raise PackagingError("Manifest file list does not match the expected package contents.")
    observed_files, observed_dirs = set(), set()
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise PackagingError(f"Package contains a symlink: {relative}")
        if path.is_dir():
            observed_dirs.add(relative)
        elif path.is_file() and stat.S_ISREG(path.stat().st_mode):
            observed_files.add(relative)
        else:
            raise PackagingError(f"Package contains a non-regular entry: {relative}")
    expected_dirs = {p.as_posix() for name in expected_files for p in PurePosixPath(name).parents if p.as_posix() != "."}
    if observed_files != expected_files | {"manifest.csv"} or observed_dirs != expected_dirs:
        raise PackagingError("Package has missing or unexpected files/directories.")
    total_bytes = manifest.stat().st_size
    for relative, (sample, read, size, digest) in rows.items():
        path = _safe_file(root, relative)
        if path.stat().st_size != size or _sha256(path) != digest:
            raise PackagingError(f"Package content does not match the manifest: {relative}")
        expected_sample, expected_read = plan.get(relative, (None, None))
        if (sample, read) != (expected_sample or "", expected_read or ""):
            raise PackagingError(f"Manifest sample/read mapping is wrong: {relative}")
        if relative in plan and digest != evidence["input_sha256"][relative]:
            raise PackagingError(f"Packaged input differs from the validated snapshot: {relative}")
        total_bytes += size
    if json.loads((root / "run_summary.json").read_text(encoding="utf-8")) != evidence:
        raise PackagingError("Packaged run summary does not match the saved SQL run.")
    # Check the manifest again after reading all members.
    if _sha256(manifest) != manifest_sha256:
        raise PackagingError("Manifest changed during package verification.")
    return {"files_count": len(rows) + 1, "total_bytes": total_bytes}


def _insert_completed(connection, record):
    connection.execute("""INSERT INTO packages (package_id, run_id, completed_at, package_path,
        manifest_sha256, files_count, total_bytes) VALUES (?, ?, ?, ?, ?, ?, ?)""",
        tuple(record[k] for k in ("package_id", "run_id", "completed_at", "package_path",
                                  "manifest_sha256", "files_count", "total_bytes")))


def build_package(db_path: str | Path, run_id: str, destination: str | Path, *,
                  owner: str | None = None, next_update: str | None = None) -> dict:
    """Create a new verified directory and a completed SQL reference, or fail.

    Source files must be stable during the operation. Filesystem promotion and
    SQLite commit are not one crash-atomic transaction; ordinary failures clean
    up this invocation's output and roll back its SQL writes.
    """
    evidence = get_run(db_path, run_id)
    plan = _input_plan(evidence)
    root = verify_current_inputs(evidence, plan)
    supplied = Path(destination).expanduser().absolute()
    if supplied.is_symlink() or supplied.exists():
        raise PackagingError("Destination already exists. Choose a new package directory; existing content is never overwritten.")
    target = supplied.resolve()
    database = Path(db_path).expanduser().resolve()
    if target.is_relative_to(root) or root.is_relative_to(target) or database.is_relative_to(target):
        raise PackagingError("Package destination must be separate from the source dataset and history database.")
    target.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".biodataqc-staging-", dir=target.parent)).resolve()
    promoted, reserved, committed = False, False, False
    try:
        for relative in sorted(plan):
            _copy_input(_safe_file(root, relative), stage / relative)
        generated = {
            "report.html": render_report(evidence, owner=owner, next_update=next_update),
            "run_summary.json": json.dumps(evidence, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        }
        hashes = dict(evidence["input_sha256"])
        for relative, content in generated.items():
            data = content.encode("utf-8")
            with (stage / relative).open("xb") as handle:
                handle.write(data)
            hashes[relative] = hashlib.sha256(data).hexdigest()
        _write_manifest(stage, plan, hashes)
        manifest_digest = _sha256(stage / "manifest.csv")
        verify_current_inputs(evidence, plan)
        with _connect(db_path, write=True) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                _verify_schema(connection, initialize=True)
                current = connection.execute("SELECT report_json FROM validation_runs WHERE run_id = ?", (run_id,)).fetchone()
                if current is None or json.loads(current["report_json"]) != evidence:
                    raise PackagingError("Saved validation evidence changed during packaging.")
                # Exclusively reserve the final name before replacing our own empty directory.
                target.mkdir()
                reserved = True
                stage.replace(target)
                promoted, reserved = True, False
                verified = _verify_directory(target, manifest_digest, evidence, plan)
                record = {"package_id": str(uuid4()), "run_id": run_id, "completed_at": utc_now(),
                          "package_path": str(target), "manifest_sha256": manifest_digest, **verified}
                _insert_completed(connection, record)
                connection.commit()
                committed = True
            except BaseException:
                connection.rollback()
                raise
        return record
    finally:
        if stage.exists():
            shutil.rmtree(stage)
        if not committed and promoted and target.exists():
            shutil.rmtree(target)
        elif not committed and reserved:
            target.rmdir()


def verify_package(db_path: str | Path, package_path: str | Path) -> dict:
    """Verify a recorded package; source data need not still exist or match."""
    record = get_package_by_path(db_path, package_path)
    evidence = get_run(db_path, record["run_id"])
    plan = _input_plan(evidence)
    result = _verify_directory(Path(record["package_path"]), record["manifest_sha256"], evidence, plan)
    if any(result[key] != record[key] for key in ("files_count", "total_bytes")):
        raise PackagingError("Package totals differ from the completed SQL record.")
    return {**record, "verification_status": "VERIFIED"}

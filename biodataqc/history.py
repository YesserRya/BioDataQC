"""Append completed validation evidence to a local SQLite database.

All inserts for a run use one transaction. Reading history never rereads source
datasets. The API does not update/delete historical runs or infer current readiness.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .results import ValidationReport, utc_now


SCHEMA_VERSION = 2
APPLICATION_ID = int.from_bytes(b"BDQC", "big")
STATUSES = ("READY", "BLOCKED", "INCOMPLETE")


class HistoryError(ValueError):
    """History could not be stored or retrieved safely."""


_SCHEMA = (
    """CREATE TABLE projects (
        project_id TEXT PRIMARY KEY NOT NULL,
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE validation_runs (
        run_id TEXT PRIMARY KEY NOT NULL,
        project_id TEXT REFERENCES projects(project_id),
        dataset_path TEXT NOT NULL,
        started_at TEXT NOT NULL,
        finished_at TEXT NOT NULL,
        stored_at TEXT NOT NULL,
        rules_version TEXT NOT NULL,
        report_schema_version TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('READY', 'BLOCKED', 'INCOMPLETE')),
        blocking_findings INTEGER NOT NULL CHECK (blocking_findings >= 0),
        warnings INTEGER NOT NULL CHECK (warnings >= 0),
        execution_errors INTEGER NOT NULL CHECK (execution_errors >= 0),
        required_checks_not_run INTEGER NOT NULL CHECK (required_checks_not_run >= 0),
        passed_checks INTEGER NOT NULL CHECK (passed_checks >= 0),
        valid_fastq_files INTEGER NOT NULL CHECK (valid_fastq_files >= 0),
        records_scanned INTEGER NOT NULL CHECK (records_scanned >= 0),
        report_json TEXT NOT NULL
    )""",
    """CREATE TABLE checks (
        run_id TEXT NOT NULL REFERENCES validation_runs(run_id),
        check_id TEXT NOT NULL,
        position INTEGER NOT NULL CHECK (position >= 0),
        rule_id TEXT NOT NULL,
        outcome TEXT NOT NULL CHECK (outcome IN ('PASS', 'FAIL', 'ERROR', 'NOT_RUN')),
        required INTEGER NOT NULL CHECK (required IN (0, 1)),
        severity TEXT CHECK (severity IN ('BLOCKER', 'WARNING')),
        sample_id TEXT,
        file_path TEXT,
        record_number INTEGER CHECK (record_number IS NULL OR record_number >= 1),
        message TEXT NOT NULL,
        next_action TEXT,
        blocked_by_json TEXT NOT NULL,
        details_json TEXT NOT NULL,
        PRIMARY KEY (run_id, check_id),
        UNIQUE (run_id, position),
        CHECK (outcome NOT IN ('FAIL', 'ERROR') OR severity IS NOT NULL)
    )""",
    """CREATE TABLE file_snapshots (
        run_id TEXT NOT NULL REFERENCES validation_runs(run_id),
        relative_path TEXT NOT NULL,
        sample_id TEXT,
        read_label TEXT,
        expected INTEGER NOT NULL CHECK (expected IN (0, 1)),
        inventory_status TEXT NOT NULL,
        entry_type TEXT NOT NULL,
        size_bytes INTEGER CHECK (size_bytes IS NULL OR size_bytes >= 0),
        sha256 TEXT CHECK (sha256 IS NULL OR length(sha256) = 64),
        PRIMARY KEY (run_id, relative_path)
    )""",
    """CREATE TABLE metadata_rows (
        run_id TEXT NOT NULL REFERENCES validation_runs(run_id),
        csv_record INTEGER NOT NULL CHECK (csv_record >= 2),
        sample_id TEXT,
        values_json TEXT NOT NULL,
        PRIMARY KEY (run_id, csv_record)
    )""",
    """CREATE VIEW issues AS
        SELECT * FROM checks WHERE outcome IN ('FAIL', 'ERROR')""",
    "CREATE INDEX runs_project_time ON validation_runs(project_id, stored_at DESC)",
    "CREATE INDEX runs_dataset_time ON validation_runs(dataset_path, stored_at DESC)",
    "CREATE INDEX checks_outcome ON checks(run_id, outcome)",
)

_PACKAGE_SCHEMA = (
    """CREATE TABLE packages (
        package_id TEXT PRIMARY KEY NOT NULL,
        run_id TEXT NOT NULL REFERENCES validation_runs(run_id),
        completed_at TEXT NOT NULL,
        package_path TEXT UNIQUE NOT NULL,
        manifest_sha256 TEXT NOT NULL CHECK (length(manifest_sha256) = 64),
        files_count INTEGER NOT NULL CHECK (files_count >= 1),
        total_bytes INTEGER NOT NULL CHECK (total_bytes >= 0)
    )""",
    "CREATE INDEX packages_run_time ON packages(run_id, completed_at DESC)",
    """CREATE TRIGGER packages_require_ready BEFORE INSERT ON packages
        WHEN COALESCE((SELECT status FROM validation_runs WHERE run_id = NEW.run_id), '') != 'READY'
        BEGIN SELECT RAISE(ABORT, 'Only READY runs can have completed packages'); END""",
)


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def database_paths(db_path: str | Path) -> set[Path]:
    """Paths reserved for SQLite, including its possible sidecar files."""
    path = Path(db_path).expanduser().resolve()
    return {path, *(Path(str(path) + suffix) for suffix in ("-journal", "-wal", "-shm"))}


def check_database_destination(db_path: str | Path, dataset_path: str | Path) -> Path:
    path = Path(db_path).expanduser().resolve()
    root = Path(dataset_path).expanduser().resolve()
    if path.is_relative_to(root):
        raise HistoryError("Store the history database outside the input dataset, for example in outputs/.")
    return path


def _verify_schema(connection, *, initialize=False):
    application = connection.execute("PRAGMA application_id").fetchone()[0]
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    objects = {row[0]: row[1] for row in connection.execute(
        "SELECT name, type FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")}
    if application == 0 and version == 0 and not objects and initialize:
        for statement in _SCHEMA + _PACKAGE_SCHEMA:
            connection.execute(statement)
        connection.execute(f"PRAGMA application_id = {APPLICATION_ID}")
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        return
    if application != APPLICATION_ID:
        raise HistoryError("This file is not an initialized BioDataQC history database; choose a new database path.")
    if version not in (1, SCHEMA_VERSION):
        raise HistoryError(f"Unsupported history schema version {version}; this code supports {SCHEMA_VERSION}.")
    required = {"projects": "table", "validation_runs": "table", "checks": "table",
                "file_snapshots": "table", "metadata_rows": "table", "issues": "view"}
    if any(objects.get(name) != kind for name, kind in required.items()):
        raise HistoryError("The BioDataQC database schema is incomplete; restore a valid database.")
    if version == 1 and initialize:
        # The caller already owns a transaction: schema and data writes roll back together.
        for statement in _PACKAGE_SCHEMA:
            connection.execute(statement)
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    elif version == 2 and (objects.get("packages") != "table" or
                           objects.get("packages_require_ready") != "trigger"):
        raise HistoryError("The BioDataQC package schema is incomplete.")


@contextmanager
def _connect(db_path: str | Path, *, write=False):
    path = Path(db_path).expanduser().resolve()
    connection = None
    try:
        if write:
            path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(path, timeout=5.0, isolation_level=None)
        else:
            if not path.is_file():
                raise HistoryError(f"History database not found: {path}. Save a run with --db first.")
            connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True,
                                         timeout=5.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            raise HistoryError("SQLite foreign-key enforcement could not be enabled.")
        yield connection
    except (sqlite3.Error, OSError) as error:
        raise HistoryError(f"SQLite history operation failed: {error}") from error
    finally:
        if connection is not None:
            connection.close()


def _file_rows(evidence):
    inventory = {entry["relative_path"]: entry for entry in evidence["inventory"]}
    for relative in sorted(set(inventory) | set(evidence["input_sha256"])):
        entry = inventory.get(relative, {})
        # CSV and requirements have fingerprints but no inventory size field.
        # Never reread them here to fill that field with later source information.
        yield (evidence["run_id"], relative, entry.get("sample_id"), entry.get("read"),
               int(entry.get("expected", True)), entry.get("status", "PRESENT"),
               entry.get("entry_type", "FILE"), entry.get("size_bytes"),
               evidence["input_sha256"].get(relative))


def save_run(report: ValidationReport, db_path: str | Path) -> bool:
    """Store completed evidence. Return True if inserted, False for an exact retry.

    A reused run ID with different evidence is an error. A fresh validation gets
    a fresh ID, so repeated validation of identical data still adds history.
    """
    if not isinstance(report, ValidationReport) or not report.finished_at:
        raise HistoryError("Only a finished ValidationReport can be stored.")
    path = check_database_destination(db_path, report.dataset_path)
    try:
        serialized = _json(report.to_dict())
        evidence = json.loads(serialized)  # Freeze the object before starting SQL writes.
    except (TypeError, ValueError) as error:
        raise HistoryError(f"Validation evidence is not JSON serializable: {error}") from error
    if not evidence["run_id"] or evidence["status"] not in STATUSES:
        raise HistoryError("Validation evidence needs a run ID and a supported final status.")
    with _connect(path, write=True) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            _verify_schema(connection, initialize=True)
            previous = connection.execute("SELECT report_json FROM validation_runs WHERE run_id = ?",
                                          (evidence["run_id"],)).fetchone()
            if previous is not None:
                if previous["report_json"] != serialized:
                    raise HistoryError("Run ID already exists with different evidence. Revalidate to create a new run; the stored run was retained.")
                connection.commit()
                return False
            stored_at = utc_now()
            if evidence["project_id"] is not None:
                connection.execute("INSERT INTO projects(project_id, created_at) VALUES (?, ?) ON CONFLICT(project_id) DO NOTHING",
                                   (evidence["project_id"], stored_at))
            counts = evidence["counts"]
            connection.execute("""INSERT INTO validation_runs (
                run_id, project_id, dataset_path, started_at, finished_at, stored_at,
                rules_version, report_schema_version, status, blocking_findings, warnings,
                execution_errors, required_checks_not_run, passed_checks, valid_fastq_files,
                records_scanned, report_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", (
                evidence["run_id"], evidence["project_id"], evidence["dataset_path"],
                evidence["started_at"], evidence["finished_at"], stored_at, evidence["rules_version"],
                evidence["report_schema_version"], evidence["status"], counts["blocking_findings"],
                counts["warnings"], counts["execution_errors"], counts["required_checks_not_run"],
                counts["passed_checks"], counts["valid_fastq_files"], counts["records_scanned"], serialized))
            connection.executemany("""INSERT INTO checks (
                run_id, check_id, position, rule_id, outcome, required, severity, sample_id,
                file_path, record_number, message, next_action, blocked_by_json, details_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", [
                (evidence["run_id"], check["check_id"], index, check["rule_id"], check["outcome"],
                 int(check["required"]), check["severity"], check["sample_id"], check["file_path"],
                 check["record_number"], check["message"], check["next_action"],
                 _json(check["blocked_by"]), _json(check["details"]))
                for index, check in enumerate(evidence["checks"])])
            connection.executemany("""INSERT INTO file_snapshots (
                run_id, relative_path, sample_id, read_label, expected, inventory_status,
                entry_type, size_bytes, sha256
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""", _file_rows(evidence))
            id_column = evidence["requirements"].get("metadata", {}).get("sample_id_column")
            connection.executemany("""INSERT INTO metadata_rows (run_id, csv_record, sample_id, values_json)
                VALUES (?, ?, ?, ?)""", [
                (evidence["run_id"], index + 2, row.get(id_column), _json(row))
                for index, row in enumerate(evidence["metadata_rows"])])
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
    return True


def list_runs(db_path: str | Path, *, project_id: str | None = None,
              dataset_path: str | Path | None = None, status: str | None = None,
              limit: int = 20) -> list[dict]:
    """Return newest stored runs first; filters are bound SQL parameters."""
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise HistoryError("History limit must be an integer between 1 and 1000.")
    if status is not None and status not in STATUSES:
        raise HistoryError(f"Unknown history status: {status}")
    clauses, values = [], []
    for column, value in (("project_id", project_id), ("dataset_path", None if dataset_path is None
                           else str(Path(dataset_path).expanduser().resolve())), ("status", status)):
        if value is not None:
            clauses.append(f"{column} = ?")  # Column names are internal constants.
            values.append(value)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with _connect(db_path) as connection:
        _verify_schema(connection)
        rows = connection.execute("""SELECT run_id, project_id, dataset_path, started_at,
            finished_at, stored_at, rules_version, report_schema_version, status,
            blocking_findings, warnings, execution_errors, required_checks_not_run,
            passed_checks, valid_fastq_files, records_scanned FROM validation_runs""" +
            where + " ORDER BY stored_at DESC, rowid DESC LIMIT ?", (*values, limit)).fetchall()
        return [dict(row) for row in rows]


def get_run(db_path: str | Path, run_id: str) -> dict:
    """Return the saved report exactly, without consulting current input files."""
    with _connect(db_path) as connection:
        _verify_schema(connection)
        row = connection.execute("SELECT report_json FROM validation_runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise HistoryError(f"No saved validation run has ID {run_id!r}.")
        try:
            return json.loads(row["report_json"])
        except (TypeError, ValueError) as error:
            raise HistoryError("The saved report JSON is damaged.") from error


def get_latest_run(db_path: str | Path, *, project_id: str | None = None,
                   dataset_path: str | Path | None = None) -> dict:
    runs = list_runs(db_path, project_id=project_id, dataset_path=dataset_path, limit=1)
    if not runs:
        raise HistoryError("No saved runs match these history filters.")
    return get_run(db_path, runs[0]["run_id"])


def list_packages(db_path: str | Path, *, run_id: str | None = None, limit: int = 20) -> list[dict]:
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise HistoryError("Package limit must be an integer between 1 and 1000.")
    with _connect(db_path) as connection:
        _verify_schema(connection)
        if connection.execute("PRAGMA user_version").fetchone()[0] == 1:
            return []
        where, values = (" WHERE run_id = ?", [run_id]) if run_id is not None else ("", [])
        rows = connection.execute("SELECT * FROM packages" + where +
                                  " ORDER BY completed_at DESC, rowid DESC LIMIT ?", (*values, limit)).fetchall()
        return [dict(row) for row in rows]


def get_package_by_path(db_path: str | Path, package_path: str | Path) -> dict:
    with _connect(db_path) as connection:
        _verify_schema(connection)
        if connection.execute("PRAGMA user_version").fetchone()[0] == 1:
            raise HistoryError("No completed packages have been recorded yet.")
        row = connection.execute("SELECT * FROM packages WHERE package_path = ?",
                                 (str(Path(package_path).expanduser().absolute()),)).fetchone()
        if row is None:
            raise HistoryError("No completed package is recorded at this path.")
        return dict(row)

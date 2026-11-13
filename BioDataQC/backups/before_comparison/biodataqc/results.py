"""Structured evidence shared by the CLI, SQL history and future interface."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4


RULES_VERSION = "1.1.0"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class CheckResult:
    check_id: str
    rule_id: str
    outcome: str
    message: str
    required: bool = True
    severity: str | None = None
    sample_id: str | None = None
    file_path: str | None = None
    record_number: int | None = None
    next_action: str | None = None
    blocked_by: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if self.outcome not in {"PASS", "FAIL", "ERROR", "NOT_RUN"}:
            raise ValueError(f"Unknown check outcome: {self.outcome}")
        if self.outcome in {"FAIL", "ERROR"} and self.severity not in {"BLOCKER", "WARNING"}:
            raise ValueError("A failed or errored check must have an explicit severity.")


@dataclass
class ValidationReport:
    dataset_path: str
    run_id: str = field(default_factory=lambda: str(uuid4()))
    started_at: str = field(default_factory=utc_now)
    finished_at: str | None = None
    project_id: str | None = None
    requirements: dict = field(default_factory=dict)
    metadata_columns: list[str] = field(default_factory=list)
    metadata_rows: list[dict[str, str]] = field(default_factory=list)
    inventory_summary: dict = field(default_factory=dict)
    inventory: list[dict] = field(default_factory=list)
    checks: list[CheckResult] = field(default_factory=list)
    expected_check_ids: list[str] = field(default_factory=list)
    input_sha256: dict[str, str] = field(default_factory=dict)

    @property
    def status(self) -> str:
        if any(c.outcome == "FAIL" and c.severity == "BLOCKER" for c in self.checks):
            return "BLOCKED"
        known = [c.check_id for c in self.checks]
        if (not self.finished_at or not self.expected_check_ids or
                len(known) != len(set(known)) or not set(self.expected_check_ids).issubset(known)):
            return "INCOMPLETE"
        if any(c.outcome == "ERROR" or (c.required and c.outcome == "NOT_RUN") for c in self.checks):
            return "INCOMPLETE"
        return "READY"

    @property
    def issues(self) -> list[CheckResult]:
        return [c for c in self.checks if c.outcome in {"FAIL", "ERROR"}]

    def counts(self) -> dict[str, int]:
        return {
            "blocking_findings": sum(c.outcome == "FAIL" and c.severity == "BLOCKER" for c in self.checks),
            "warnings": sum(c.outcome == "FAIL" and c.severity == "WARNING" for c in self.checks),
            "execution_errors": sum(c.outcome == "ERROR" for c in self.checks),
            "required_checks_not_run": sum(c.required and c.outcome == "NOT_RUN" for c in self.checks),
            "passed_checks": sum(c.outcome == "PASS" for c in self.checks),
            "valid_fastq_files": sum(c.rule_id == "FASTQ001" and c.outcome == "PASS" for c in self.checks),
            "records_scanned": sum(c.details.get("records_scanned", 0) for c in self.checks if c.rule_id == "FASTQ001"),
        }

    def to_dict(self) -> dict:
        return {
            "report_schema_version": "1.1.0", "rules_version": RULES_VERSION,
            "run_id": self.run_id, "started_at": self.started_at, "finished_at": self.finished_at,
            "dataset_path": self.dataset_path, "project_id": self.project_id,
            "status": self.status, "counts": self.counts(),
            "inventory_summary": self.inventory_summary,
            "requirements": self.requirements, "inventory": self.inventory,
            "metadata_columns": self.metadata_columns, "metadata_rows": self.metadata_rows,
            "input_sha256": self.input_sha256,
            "expected_check_ids": self.expected_check_ids,
            "checks": [asdict(c) for c in self.checks],
        }

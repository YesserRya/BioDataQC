"""Behavioral tests using small, isolated synthetic deliveries.

No test modifies data/clean or data/broken. Run with unittest discovery.
"""

import csv
import gzip
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from biodataqc.fastq import scan_fastq
from biodataqc.results import CheckResult, ValidationReport, utc_now
from biodataqc.validation import validate_dataset


PROJECT_ROOT = Path(__file__).resolve().parent.parent


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "dataset"
        self.seq = self.root / "sequencing"
        self.seq.mkdir(parents=True)
        self.config = json.loads((PROJECT_ROOT / "tests/fixtures/validation_requirements.json").read_text())
        self.config["expected_samples"] = ["S001", "S002"]
        self.write_config()
        self.headers = ["sample_id", "condition", "tissue", "notes"]
        self.rows = [["S001", "Control", "Liver", "one"], ["S002", "Treated", "Liver", "two"]]
        self.write_csv()
        for sample in self.config["expected_samples"]:
            for mate in (1, 2):
                self.write_fastq(f"{sample}_R{mate}.fastq.gz", self.records(mate))

    def write_config(self):
        (self.root / "requirements.json").write_text(json.dumps(self.config), encoding="utf-8")

    def write_csv(self):
        with (self.root / "samples.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(self.headers)
            writer.writerows(self.rows)

    @staticmethod
    def records(mate=1, ids=(1, 2, 3), sequence="ACGTACGTACGT"):
        return "".join(f"@READ_{number:06d}/{mate}\n{sequence}\n+\n{'I' * len(sequence)}\n" for number in ids)

    def write_fastq(self, name, text):
        content = text.encode("ascii") if isinstance(text, str) else text
        (self.seq / name).write_bytes(gzip.compress(content, mtime=0))

    @staticmethod
    def check(report, check_id):
        return next(c for c in report.checks if c.check_id == check_id)

    def assert_file_fails(self, report, name="S001_R1.fastq.gz"):
        self.assertEqual(report.status, "BLOCKED")
        result = self.check(report, f"FASTQ001:sequencing/{name}")
        self.assertEqual(result.outcome, "FAIL")
        return result

    def test_clean_delivery_is_ready_and_json_serializable(self):
        report = validate_dataset(self.root)
        self.assertEqual(report.status, "READY")
        self.assertEqual(report.counts()["blocking_findings"], 0)
        self.assertEqual(report.counts()["records_scanned"], 12)
        self.assertEqual(report.counts()["valid_fastq_files"], 4)
        self.assertEqual(report.counts()["required_checks_not_run"], 0)
        self.assertEqual(json.loads(json.dumps(report.to_dict()))["status"], "READY")

    def test_two_root_faults_do_not_become_extra_pair_blockers(self):
        (self.seq / "S001_R2.fastq.gz").unlink()
        self.write_fastq("S002_R1.fastq.gz", "@READ_000001/1\nACGTACGTACGT\n+\nIIIIIIII\n")
        report = validate_dataset(self.root)
        self.assertEqual(report.status, "BLOCKED")
        self.assertEqual(report.counts()["blocking_findings"], 2)
        self.assertEqual(report.counts()["required_checks_not_run"], 3)
        self.assertEqual(self.check(report, "PAIR001:S001").outcome, "NOT_RUN")
        self.assertEqual(self.check(report, "PAIR001:S002").outcome, "NOT_RUN")
        self.assertEqual(self.check(report, "PAIR001:S002").blocked_by, ["FASTQ001:sequencing/S002_R1.fastq.gz"])

    def test_missing_required_column_blocks(self):
        self.headers.remove("tissue")
        for row in self.rows:
            row.pop(2)
        self.write_csv()
        report = validate_dataset(self.root)
        self.assertEqual(report.status, "BLOCKED")
        self.assertEqual(self.check(report, "META001").details["missing_columns"], ["tissue"])

    def test_missing_id_column_skips_identity_check(self):
        self.headers.pop(0)
        for row in self.rows:
            row.pop(0)
        self.write_csv()
        report = validate_dataset(self.root)
        self.assertEqual(report.status, "BLOCKED")
        self.assertEqual(self.check(report, "META002").outcome, "NOT_RUN")

    def test_blank_required_value_blocks(self):
        self.rows[0][2] = "  "
        self.write_csv()
        report = validate_dataset(self.root)
        self.assertEqual(report.status, "BLOCKED")
        self.assertEqual(self.check(report, "META001").details["blank_cells"], [{"csv_record": 2, "column": "tissue"}])

    def test_duplicate_and_missing_ids_are_reported(self):
        self.rows[1][0] = "S001"
        self.write_csv()
        result = self.check(validate_dataset(self.root), "META002")
        self.assertEqual(result.outcome, "FAIL")
        self.assertEqual(result.details["duplicates"], {"S001": 2})
        self.assertEqual(result.details["missing"], ["S002"])

    def test_unexpected_id_is_not_accepted_by_row_count(self):
        self.rows[1][0] = "S999"
        self.write_csv()
        result = self.check(validate_dataset(self.root), "META002")
        self.assertEqual(result.details["unexpected"], ["S999"])
        self.assertEqual(result.details["missing"], ["S002"])

    def test_header_only_csv_blocks_missing_samples(self):
        self.rows = []
        self.write_csv()
        self.assertEqual(validate_dataset(self.root).status, "BLOCKED")

    def test_case_sensitive_vocabulary(self):
        self.rows[0][1] = "control"
        self.write_csv()
        report = validate_dataset(self.root)
        self.assertEqual(report.status, "BLOCKED")
        self.assertEqual(self.check(report, "META003").details["violations"][0]["value"], "control")

    def test_nonblank_outer_whitespace_is_preserved_and_blocked(self):
        self.rows[0][2] = " Liver "
        self.write_csv()
        report = validate_dataset(self.root)
        self.assertEqual(report.status, "BLOCKED")
        self.assertEqual(self.check(report, "META003").details["violations"][0]["value"], " Liver ")
        self.assertIn(" Liver ", (self.root / "samples.csv").read_text())

    def test_literal_na_is_not_treated_as_missing(self):
        self.rows[0][2] = "NA"
        self.write_csv()
        self.assertEqual(validate_dataset(self.root).status, "READY")

    def test_blank_optional_notes_warn_without_blocking(self):
        self.rows[0][3] = ""
        self.write_csv()
        report = validate_dataset(self.root)
        self.assertEqual(report.status, "READY")
        self.assertEqual(report.counts()["warnings"], 1)

    def test_missing_optional_column_warns_without_blocking(self):
        self.headers.pop()
        for row in self.rows:
            row.pop()
        self.write_csv()
        report = validate_dataset(self.root)
        self.assertEqual(report.status, "READY")
        self.assertEqual(self.check(report, "META004").severity, "WARNING")

    def test_extra_column_warns_without_blocking(self):
        self.headers.append("batch")
        for row in self.rows:
            row.append("B01")
        self.write_csv()
        report = validate_dataset(self.root)
        self.assertEqual(report.status, "READY")
        self.assertEqual(self.check(report, "META004").details["extra_columns"], ["batch"])

    def test_config_can_promote_notes_to_required(self):
        self.rows[0][3] = ""
        self.write_csv()
        self.config["metadata"]["optional_columns"] = []
        self.config["metadata"]["required_columns"].append("notes")
        self.write_config()
        report = validate_dataset(self.root)
        self.assertEqual(report.status, "BLOCKED")
        self.assertEqual(self.check(report, "META001").outcome, "FAIL")

    def test_duplicate_csv_header_is_a_blocking_parse_failure(self):
        self.headers[3] = "sample_id"
        self.write_csv()
        report = validate_dataset(self.root)
        self.assertEqual(report.status, "BLOCKED")
        self.assertEqual(self.check(report, "CSV001").outcome, "FAIL")

    def test_wrong_csv_field_count_is_a_blocking_parse_failure(self):
        self.rows[0].pop()
        self.write_csv()
        self.assertEqual(self.check(validate_dataset(self.root), "CSV001").outcome, "FAIL")

    def test_unsupported_policy_cannot_silently_pass(self):
        self.config["fastq"]["scan_all_records"] = False
        self.write_config()
        report = validate_dataset(self.root)
        self.assertEqual(report.status, "BLOCKED")
        self.assertEqual(self.check(report, "CFG001").outcome, "FAIL")
        self.assertEqual(report.counts()["records_scanned"], 0)

    def test_boolean_cannot_replace_numeric_requirement(self):
        self.config["fastq"]["minimum_records_per_file"] = True
        self.write_config()
        self.assertEqual(self.check(validate_dataset(self.root), "CFG001").outcome, "FAIL")

    def test_contradictory_column_lists_are_rejected(self):
        self.config["metadata"]["required_columns"].append("notes")
        self.write_config()
        self.assertEqual(self.check(validate_dataset(self.root), "CFG001").outcome, "FAIL")

    def test_minimum_record_count_is_configurable(self):
        self.config["fastq"]["minimum_records_per_file"] = 4
        self.write_config()
        report = validate_dataset(self.root)
        self.assertEqual(report.counts()["blocking_findings"], 4)
        self.assertEqual(report.status, "BLOCKED")

    def test_missing_dataset_blocks(self):
        self.assertEqual(validate_dataset(self.root / "missing").status, "BLOCKED")

    def test_unexpected_file_blocks(self):
        (self.seq / "extra.txt").write_text("unexpected")
        report = validate_dataset(self.root)
        self.assertEqual(report.status, "BLOCKED")
        self.assertEqual(report.counts()["blocking_findings"], 1)

    def test_same_file_count_does_not_hide_wrong_sample_filename(self):
        (self.seq / "S001_R2.fastq.gz").rename(self.seq / "S999_R2.fastq.gz")
        report = validate_dataset(self.root)
        self.assertEqual(report.counts()["blocking_findings"], 2)
        self.assertEqual(report.status, "BLOCKED")

    def test_unexpected_subdirectory_blocks(self):
        (self.seq / "hidden_batch").mkdir()
        self.assertEqual(validate_dataset(self.root).status, "BLOCKED")

    def test_symlink_cannot_replace_expected_file(self):
        target = self.seq / "S001_R1.fastq.gz"
        target.unlink()
        target.symlink_to(self.seq / "S002_R1.fastq.gz")
        report = validate_dataset(self.root)
        self.assertEqual(report.status, "BLOCKED")
        self.assertEqual(self.check(report, "FASTQ001:sequencing/S001_R1.fastq.gz").outcome, "NOT_RUN")

    def test_zero_byte_file_blocks(self):
        (self.seq / "S001_R1.fastq.gz").write_bytes(b"")
        self.assert_file_fails(validate_dataset(self.root))

    def test_empty_gzip_file_blocks(self):
        self.write_fastq("S001_R1.fastq.gz", "")
        self.assert_file_fails(validate_dataset(self.root))

    def test_plain_text_with_gzip_suffix_blocks(self):
        (self.seq / "S001_R1.fastq.gz").write_text(self.records())
        self.assert_file_fails(validate_dataset(self.root))

    def test_truncated_gzip_blocks(self):
        path = self.seq / "S001_R1.fastq.gz"
        path.write_bytes(path.read_bytes()[:-8])
        result = self.assert_file_fails(validate_dataset(self.root))
        self.assertIsNotNone(result.details["stream_error"])

    def test_bad_gzip_crc_blocks(self):
        path = self.seq / "S001_R1.fastq.gz"
        content = bytearray(path.read_bytes())
        content[-8] ^= 1
        path.write_bytes(content)
        result = self.assert_file_fails(validate_dataset(self.root))
        self.assertIsNotNone(result.details["stream_error"])

    def test_non_ascii_record_blocks(self):
        self.write_fastq("S001_R1.fastq.gz", self.records().encode().replace(b"ACGT", b"\xffCGT", 1))
        self.assert_file_fails(validate_dataset(self.root))

    def test_invalid_header_blocks(self):
        self.write_fastq("S001_R1.fastq.gz", self.records().replace("@READ_000001/1", "READ_000001/1"))
        self.assert_file_fails(validate_dataset(self.root))

    def test_wrong_header_mate_blocks(self):
        self.write_fastq("S001_R1.fastq.gz", self.records(2))
        self.assert_file_fails(validate_dataset(self.root))

    def test_invalid_separator_blocks(self):
        self.write_fastq("S001_R1.fastq.gz", self.records().replace("\n+\n", "\n+identifier\n", 1))
        self.assert_file_fails(validate_dataset(self.root))

    def test_invalid_sequence_base_blocks(self):
        self.write_fastq("S001_R1.fastq.gz", self.records(sequence="ACXT"))
        self.assert_file_fails(validate_dataset(self.root))

    def test_empty_sequence_blocks(self):
        self.write_fastq("S001_R1.fastq.gz", "@READ_000001/1\n\n+\n\n")
        self.assert_file_fails(validate_dataset(self.root))

    def test_quality_outside_phred33_range_blocks(self):
        self.write_fastq("S001_R1.fastq.gz", self.records().replace("IIII", " III", 1))
        self.assert_file_fails(validate_dataset(self.root))

    def test_sequence_quality_length_mismatch_blocks(self):
        self.write_fastq("S001_R1.fastq.gz", self.records().replace("IIII", "II", 1))
        result = self.assert_file_fails(validate_dataset(self.root))
        self.assertEqual(result.record_number, 1)
        self.assertIn("does not match", result.message)

    def test_incomplete_four_line_record_blocks(self):
        self.write_fastq("S001_R1.fastq.gz", "@READ_000001/1\nACGT\n+\n")
        self.assert_file_fails(validate_dataset(self.root))

    def test_invalid_later_record_is_not_missed(self):
        content = self.records(ids=tuple(range(1, 201)))
        content = content.replace("@READ_000200/1\nACGTACGTACGT\n+\nIIIIIIIIIIII", "@READ_000200/1\nACGT\n+\nII")
        self.write_fastq("S001_R1.fastq.gz", content)
        result = self.assert_file_fails(validate_dataset(self.root))
        self.assertEqual(result.record_number, 200)
        self.assertEqual(result.details["records_scanned"], 200)
        self.assertTrue(result.details["scan_complete"])

    def test_scanning_continues_after_first_structural_error(self):
        self.write_fastq("S001_R1.fastq.gz", self.records().replace("IIII", "II", 1))
        result = self.assert_file_fails(validate_dataset(self.root))
        self.assertEqual(result.details["records_scanned"], 3)
        self.assertTrue(result.details["scan_complete"])

    def test_crlf_fastq_is_accepted(self):
        self.write_fastq("S001_R1.fastq.gz", self.records().replace("\n", "\r\n"))
        self.assertEqual(validate_dataset(self.root).status, "READY")

    def test_concatenated_gzip_members_are_scanned(self):
        path = self.seq / "S001_R1.fastq.gz"
        path.write_bytes(gzip.compress(self.records(ids=(1,)).encode(), mtime=0) +
                         gzip.compress(self.records(ids=(2, 3)).encode(), mtime=0))
        report = validate_dataset(self.root)
        self.assertEqual(report.status, "READY")
        self.assertEqual(report.input_sha256["sequencing/S001_R1.fastq.gz"], hashlib.sha256(path.read_bytes()).hexdigest())

    def test_mate_count_mismatch_blocks_pair_only(self):
        self.write_fastq("S001_R2.fastq.gz", self.records(2, ids=(1, 2)))
        report = validate_dataset(self.root)
        self.assertEqual(report.status, "BLOCKED")
        self.assertEqual(report.counts()["valid_fastq_files"], 4)
        self.assertEqual(report.counts()["blocking_findings"], 1)
        self.assertEqual(self.check(report, "PAIR001:S001").outcome, "FAIL")

    def test_same_count_but_reordered_read_ids_blocks(self):
        self.write_fastq("S001_R2.fastq.gz", self.records(2, ids=(2, 1, 3)))
        report = validate_dataset(self.root)
        self.assertEqual(report.status, "BLOCKED")
        self.assertEqual(self.check(report, "PAIR001:S001").outcome, "FAIL")

    def test_mates_need_not_have_equal_sequence_lengths(self):
        self.write_fastq("S001_R2.fastq.gz", self.records(2, sequence="ACGT"))
        self.assertEqual(validate_dataset(self.root).status, "READY")

    def test_scanner_crash_causes_incomplete_not_ready(self):
        with patch("biodataqc.validation.scan_fastq", side_effect=RuntimeError("simulated scanner error")):
            report = validate_dataset(self.root)
        self.assertEqual(report.status, "INCOMPLETE")
        self.assertEqual(report.counts()["execution_errors"], 4)
        self.assertEqual(report.counts()["blocking_findings"], 0)

    def test_metadata_checker_crash_causes_incomplete(self):
        with patch("biodataqc.validation._metadata_values", side_effect=RuntimeError("simulated check error")):
            report = validate_dataset(self.root)
        self.assertEqual(report.status, "INCOMPLETE")
        self.assertEqual(self.check(report, "META003").outcome, "ERROR")

    def test_loader_permission_error_is_incomplete(self):
        with patch("biodataqc.inputs._load_metadata", side_effect=PermissionError("no read permission")):
            report = validate_dataset(self.root)
        self.assertEqual(report.status, "INCOMPLETE")
        self.assertEqual(self.check(report, "CSV001").outcome, "ERROR")

    def test_known_blocker_takes_priority_over_execution_error(self):
        (self.seq / "S001_R2.fastq.gz").unlink()
        with patch("biodataqc.validation._metadata_values", side_effect=RuntimeError("simulated check error")):
            report = validate_dataset(self.root)
        self.assertEqual(report.status, "BLOCKED")
        self.assertEqual(report.counts()["execution_errors"], 1)

    def test_required_reference_checksums_cannot_silently_pass(self):
        self.config["integrity"]["require_independent_source_checksums"] = True
        self.write_config()
        report = validate_dataset(self.root)
        self.assertEqual(report.status, "INCOMPLETE")
        self.assertTrue(self.check(report, "REF001").required)
        self.assertEqual(self.check(report, "REF001").outcome, "NOT_RUN")

    def test_metadata_changed_after_loading_is_detected(self):
        mutated = False

        def edit_then_scan(*args):
            nonlocal mutated
            if not mutated:
                path = self.root / "samples.csv"
                path.write_text(path.read_text().replace("Liver", "Brain"))
                mutated = True
            return scan_fastq(*args)

        with patch("biodataqc.validation.scan_fastq", side_effect=edit_then_scan):
            report = validate_dataset(self.root)
        self.assertEqual(report.status, "INCOMPLETE")
        self.assertEqual(self.check(report, "SNAP001").outcome, "ERROR")

    def test_fastq_changed_after_scan_is_detected(self):
        first_path = self.seq / "S001_R1.fastq.gz"

        def scan_then_edit(*args):
            result = scan_fastq(*args)
            if args[1].relative_path.endswith("S002_R2.fastq.gz"):
                self.write_fastq(first_path.name, self.records(sequence="TTTT"))
            return result

        with patch("biodataqc.validation.scan_fastq", side_effect=scan_then_edit):
            report = validate_dataset(self.root)
        self.assertEqual(report.status, "INCOMPLETE")
        self.assertEqual(self.check(report, "SNAP001").outcome, "ERROR")

    def test_new_file_during_run_is_detected(self):
        def scan_then_add(*args):
            result = scan_fastq(*args)
            (self.seq / "new_file.txt").write_text("arrived during scan")
            return result

        with patch("biodataqc.validation.scan_fastq", side_effect=scan_then_add):
            report = validate_dataset(self.root)
        self.assertEqual(report.status, "INCOMPLETE")
        self.assertEqual(self.check(report, "SNAP001").outcome, "ERROR")

    def test_report_snapshot_hashes_match_unchanged_originals(self):
        before = {p.relative_to(self.root).as_posix(): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        report = validate_dataset(self.root)
        after = {p.relative_to(self.root).as_posix(): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(report.input_sha256, {name: hashlib.sha256(content).hexdigest() for name, content in before.items()})

    def test_missing_registered_check_cannot_produce_ready(self):
        report = ValidationReport(str(self.root), finished_at=utc_now(), expected_check_ids=["A", "B"])
        report.checks = [CheckResult("A", "A", "PASS", "completed")]
        self.assertEqual(report.status, "INCOMPLETE")

    def test_duplicate_check_ids_cannot_produce_ready(self):
        report = ValidationReport(str(self.root), finished_at=utc_now(), expected_check_ids=["A"])
        report.checks = [CheckResult("A", "A", "PASS", "completed"), CheckResult("A", "A", "PASS", "duplicate")]
        self.assertEqual(report.status, "INCOMPLETE")

    def test_unfinished_or_empty_report_cannot_produce_ready(self):
        report = ValidationReport(str(self.root))
        self.assertEqual(report.status, "INCOMPLETE")
        report.finished_at = utc_now()
        self.assertEqual(report.status, "INCOMPLETE")

    def test_cli_exports_json_and_returns_status_exit_codes(self):
        destination = Path(self.temporary.name) / "outputs/report.json"
        command = [sys.executable, str(PROJECT_ROOT / "scripts/validate_dataset.py"), str(self.root), "--json", str(destination)]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(destination.read_text())["status"], "READY")
        (self.seq / "S001_R2.fastq.gz").unlink()
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(json.loads(destination.read_text())["status"], "BLOCKED")
        self.config["integrity"]["require_independent_source_checksums"] = True
        self.write_config()
        self.write_fastq("S001_R2.fastq.gz", self.records(2))
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(json.loads(destination.read_text())["status"], "INCOMPLETE")

    def test_cli_refuses_report_inside_input_directory(self):
        destination = self.root / "report.json"
        result = subprocess.run([sys.executable, str(PROJECT_ROOT / "scripts/validate_dataset.py"),
                                 str(self.root), "--json", str(destination)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()

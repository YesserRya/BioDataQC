"""Meaningful state transitions and read-only comparison of historical evidence."""

from copy import deepcopy
import csv
import gzip
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from biodataqc.comparison import ComparisonError, compare_runs
from biodataqc.history import get_run, list_runs, save_run
from biodataqc.validation import validate_dataset


PROJECT_ROOT = Path(__file__).resolve().parent.parent


class ComparisonFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "data/delivery"
        self.seq = self.root / "sequencing"
        self.seq.mkdir(parents=True)
        self.db = self.base / "outputs/biodataqc.sqlite"
        self.config = json.loads((PROJECT_ROOT / "tests/fixtures/validation_requirements.json").read_text())
        self.config["expected_samples"] = ["S001", "S002"]
        self.write_config()
        with (self.root / "samples.csv").open("w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["sample_id", "condition", "tissue", "notes"])
            writer.writerows([["S001", "Control", "Liver", "one"], ["S002", "Treated", "Liver", "two"]])
        for sample in self.config["expected_samples"]:
            for mate in (1, 2):
                self.write_fastq(sample, mate)

    def write_config(self):
        (self.root / "requirements.json").write_text(json.dumps(self.config))

    def write_fastq(self, sample="S001", mate=2, quality="IIII"):
        path = self.seq / f"{sample}_R{mate}.fastq.gz"
        path.write_bytes(gzip.compress(f"@READ_000001/{mate}\nACGT\n+\n{quality}\n".encode(), mtime=0))

    def run_evidence(self):
        report = validate_dataset(self.root)
        save_run(report, self.db)
        return get_run(self.db, report.run_id)

    @staticmethod
    def row(result, check_id):
        return next(row for row in result["checks"] if row["check_id"] == check_id)

    @staticmethod
    def file(result, path):
        return next(row for row in result["files"] if row["relative_path"] == path)


class ComparisonTests(ComparisonFixture):
    def test_two_repaired_blockers_are_resolved_and_skips_now_pass(self):
        (self.seq / "S001_R2.fastq.gz").unlink()
        self.write_fastq("S002", 1, "II")
        before = self.run_evidence()
        self.write_fastq("S001", 2)
        self.write_fastq("S002", 1)
        after = self.run_evidence()
        result = compare_runs(before, after)
        self.assertTrue(result["comparable"])
        self.assertEqual((result["before"]["status"], result["after"]["status"]), ("BLOCKED", "READY"))
        self.assertEqual(result["counts"]["RESOLVED"], 2)
        self.assertEqual(result["counts"]["NOW_PASSES"], 3)
        self.assertEqual(result["counts"]["UNASSESSED"], 0)
        self.assertEqual(self.file(result, "sequencing/S001_R2.fastq.gz")["change"], "ADDED")
        self.assertEqual(self.file(result, "sequencing/S002_R1.fastq.gz")["change"], "CONTENT_CHANGED")

    def test_passing_check_that_fails_is_a_new_failure(self):
        before = self.run_evidence()
        self.write_fastq(quality="II")
        after = self.run_evidence()
        result = compare_runs(before, after)
        self.assertEqual(result["counts"]["NEW_FAILURE"], 1)
        self.assertEqual(self.row(result, "PAIR001:S001")["change"], "UNASSESSED")

    def test_previously_unrun_failure_is_newly_assessed_not_new(self):
        (self.seq / "S001_R2.fastq.gz").unlink()
        before = self.run_evidence()
        self.write_fastq(quality="II")
        result = compare_runs(before, self.run_evidence())
        self.assertEqual(self.row(result, "FASTQ001:sequencing/S001_R2.fastq.gz")["change"], "NEWLY_ASSESSED_FAILURE")
        self.assertEqual(result["counts"]["NEW_FAILURE"], 0)
        self.assertEqual(result["counts"]["RESOLVED"], 1)

    def test_failed_fastq_becoming_missing_is_unassessed(self):
        self.write_fastq(quality="II")
        before = self.run_evidence()
        (self.seq / "S001_R2.fastq.gz").unlink()
        result = compare_runs(before, self.run_evidence())
        self.assertEqual(self.row(result, "FASTQ001:sequencing/S001_R2.fastq.gz")["change"], "UNASSESSED")
        self.assertEqual(result["counts"]["RESOLVED"], 0)
        self.assertEqual(self.file(result, "sequencing/S001_R2.fastq.gz")["change"], "REMOVED")

    def test_different_failure_reasons_remain_visible(self):
        self.write_fastq(quality="II")
        before = self.run_evidence()
        self.write_fastq(quality="I")
        result = compare_runs(before, self.run_evidence())
        row = self.row(result, "FASTQ001:sequencing/S001_R2.fastq.gz")
        self.assertEqual(row["change"], "STILL_FAILING")
        self.assertNotEqual(row["before"]["message"], row["after"]["message"])
        self.assertTrue(row["evidence_changed"])

    def test_warning_resolution_is_counted_without_changing_ready_status(self):
        path = self.root / "samples.csv"
        original = path.read_text()
        path.write_text(original.replace(",one", ","))
        before = self.run_evidence()
        path.write_text(original)
        after = self.run_evidence()
        result = compare_runs(before, after)
        self.assertEqual((before["status"], after["status"]), ("READY", "READY"))
        self.assertEqual(self.row(result, "META004")["change"], "RESOLVED")
        self.assertEqual(self.row(result, "META004")["before"]["severity"], "WARNING")

    def test_execution_error_resolution_is_supported(self):
        with patch("biodataqc.validation._optional_metadata", side_effect=OSError("temporary error")):
            before = self.run_evidence()
        after = self.run_evidence()
        result = compare_runs(before, after)
        self.assertEqual((before["status"], after["status"]), ("INCOMPLETE", "READY"))
        self.assertEqual(self.row(result, "META004")["change"], "RESOLVED")
        self.assertEqual(self.row(result, "META004")["before"]["outcome"], "ERROR")

    def test_missing_check_result_is_never_a_resolution(self):
        self.write_fastq(quality="II")
        before = self.run_evidence()
        self.write_fastq()
        after = self.run_evidence()
        check_id = "FASTQ001:sequencing/S001_R2.fastq.gz"
        after["checks"] = [c for c in after["checks"] if c["check_id"] != check_id]
        result = compare_runs(before, after)
        self.assertEqual(self.row(result, check_id)["change"], "UNASSESSED")
        self.assertEqual(result["counts"]["RESOLVED"], 0)

    def test_loosened_validation_policy_does_not_claim_resolution(self):
        self.write_fastq(quality="!!!!")
        self.config["fastq"]["quality_ascii_min"] = 40
        self.write_config()
        before = self.run_evidence()
        self.config["fastq"]["quality_ascii_min"] = 33
        self.write_config()
        result = compare_runs(before, self.run_evidence())
        self.assertFalse(result["comparable"])
        self.assertEqual(result["counts"]["RESOLVED"], 0)
        self.assertIn("fastq.quality_ascii_min", [r["field"] for r in result["requirements_changes"]])

    def test_removed_expected_sample_is_not_a_fixed_delivery(self):
        (self.seq / "S001_R2.fastq.gz").unlink()
        before = self.run_evidence()
        self.config["expected_samples"] = ["S002"]
        self.write_config()
        result = compare_runs(before, self.run_evidence())
        self.assertFalse(result["comparable"])
        self.assertEqual(result["counts"]["RESOLVED"], 0)
        self.assertIn("expected_samples", [r["field"] for r in result["requirements_changes"]])

    def test_requirements_whitespace_is_a_byte_change_but_same_policy(self):
        before = self.run_evidence()
        path = self.root / "requirements.json"
        path.write_text(path.read_text() + "\n")
        result = compare_runs(before, self.run_evidence())
        self.assertTrue(result["comparable"])
        self.assertEqual(result["requirements_changes"], [])
        self.assertEqual(self.file(result, "requirements.json")["change"], "CONTENT_CHANGED")

    def test_version_change_prevents_resolution_and_old_matching_versions_work(self):
        (self.seq / "S001_R2.fastq.gz").unlink()
        before = self.run_evidence()
        self.write_fastq()
        after = self.run_evidence()
        before["rules_version"] = "1.0.0"
        self.assertEqual(compare_runs(before, after)["counts"]["RESOLVED"], 0)
        after["rules_version"] = "1.0.0"
        self.assertEqual(compare_runs(before, after)["counts"]["RESOLVED"], 1)

    def test_unconfirmed_snapshot_or_configuration_prevents_resolution(self):
        (self.seq / "S001_R2.fastq.gz").unlink()
        before = self.run_evidence()
        self.write_fastq()
        after = self.run_evidence()
        for rule in ("CFG001", "SNAP001"):
            with self.subTest(rule=rule):
                altered = deepcopy(before)
                next(c for c in altered["checks"] if c["check_id"] == rule)["outcome"] = "NOT_RUN"
                result = compare_runs(altered, after)
                self.assertFalse(result["comparable"])
                self.assertEqual(result["counts"]["RESOLVED"], 0)

    def test_new_required_flag_prevents_resolution_for_that_check(self):
        self.write_fastq(quality="II")
        before = self.run_evidence()
        self.write_fastq()
        after = self.run_evidence()
        check_id = "FASTQ001:sequencing/S001_R2.fastq.gz"
        next(c for c in after["checks"] if c["check_id"] == check_id)["required"] = False
        self.assertEqual(self.row(compare_runs(before, after), check_id)["change"], "NOT_COMPARABLE")

    def test_no_hash_is_not_reported_as_unchanged(self):
        before, after = self.run_evidence(), self.run_evidence()
        path = "sequencing/S001_R2.fastq.gz"
        del before["input_sha256"][path]
        row = self.file(compare_runs(before, after), path)
        self.assertEqual(row["before"]["size_bytes"], row["after"]["size_bytes"])
        self.assertEqual(row["change"], "UNVERIFIED")

    def test_partial_inventory_does_not_claim_file_removal(self):
        before, after = self.run_evidence(), self.run_evidence()
        after["inventory"], after["input_sha256"] = [], {}
        next(c for c in after["checks"] if c["check_id"] == "FILE001")["outcome"] = "NOT_RUN"
        row = self.file(compare_runs(before, after), "sequencing/S001_R2.fastq.gz")
        self.assertEqual(row["after"]["status"], "UNKNOWN")
        self.assertEqual(row["change"], "UNVERIFIED")

    def test_same_run_different_projects_and_reverse_order_are_rejected(self):
        before, after = self.run_evidence(), self.run_evidence()
        for old, new in ((before, before), (after, before), (before, dict(after, project_id="OTHER"))):
            with self.subTest(old=old["run_id"], new=new["project_id"]):
                with self.assertRaises(ComparisonError):
                    compare_runs(old, new)

    def test_overlapping_run_intervals_do_not_infer_resolutions(self):
        before, after = self.run_evidence(), self.run_evidence()
        after["started_at"] = before["started_at"]
        result = compare_runs(before, after)
        self.assertFalse(result["comparable"])
        self.assertTrue(any("overlap" in r for r in result["reasons"]))

    def test_timezone_offsets_order_instants_correctly(self):
        before, after = self.run_evidence(), self.run_evidence()
        before["started_at"], before["finished_at"] = "2026-11-12T11:00:00+01:00", "2026-11-12T11:01:00+01:00"
        after["started_at"], after["finished_at"] = "2026-11-12T10:02:00+00:00", "2026-11-12T10:03:00+00:00"
        self.assertTrue(compare_runs(before, after)["comparable"])

    def test_invalid_and_duplicate_evidence_is_rejected(self):
        before, after = self.run_evidence(), self.run_evidence()
        alterations = [dict(after, report_schema_version="9.0"), dict(after, started_at="yesterday"),
                       dict(after, checks=after["checks"] + [after["checks"][0]]),
                       dict(after, input_sha256={"samples.csv": "invalid"})]
        for altered in alterations:
            with self.subTest(altered=altered["report_schema_version"]):
                with self.assertRaises(ComparisonError):
                    compare_runs(before, altered)

    def test_same_project_different_folders_are_explicit(self):
        before = self.run_evidence()
        other = self.base / "other"
        shutil.copytree(self.root, other)
        after = validate_dataset(other).to_dict()
        result = compare_runs(before, after)
        self.assertTrue(result["comparable"])
        self.assertTrue(any("Different dataset folders" in note for note in result["notes"]))

    def test_comparison_works_after_deleting_sources_and_leaves_sql_unchanged(self):
        before, after = self.run_evidence(), self.run_evidence()
        original = deepcopy((before, after))
        database_bytes = self.db.read_bytes()
        shutil.rmtree(self.root)
        result = compare_runs(get_run(self.db, before["run_id"]), get_run(self.db, after["run_id"]))
        self.assertTrue(result["comparable"])
        self.assertEqual(result["file_counts"], {"UNCHANGED": 6})
        self.assertEqual((before, after), original)
        self.assertEqual(self.db.read_bytes(), database_bytes)
        self.assertEqual(len(list_runs(self.db)), 2)
        self.assertEqual(json.loads(json.dumps(result)), result)


if __name__ == "__main__":
    unittest.main()

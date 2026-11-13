"""SQL persistence, snapshot fidelity, failure handling and command integration."""

import csv
import gzip
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from biodataqc.history import (APPLICATION_ID, HistoryError, _connect, database_paths,
                               get_latest_run, get_run, list_runs, save_run)
from biodataqc.report_io import write_report_json
from biodataqc.results import ValidationReport
from biodataqc.validation import validate_dataset


PROJECT_ROOT = Path(__file__).resolve().parent.parent


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / "dataset"
        self.seq = self.root / "sequencing"
        self.seq.mkdir(parents=True)
        self.db = self.base / "outputs/biodataqc.sqlite"
        self.config = json.loads((PROJECT_ROOT / "tests/fixtures/validation_requirements.json").read_text())
        self.config["expected_samples"] = ["S001", "S002"]
        self.write_config()
        self.rows = [["S001", "Control", "Liver", "one"], ["S002", "Treated", "Liver", "two"]]
        self.write_metadata()
        for sample in self.config["expected_samples"]:
            for mate in (1, 2):
                content = "".join(f"@READ_{i:06d}/{mate}\nACGT\n+\nIIII\n" for i in (1, 2, 3))
                (self.seq / f"{sample}_R{mate}.fastq.gz").write_bytes(gzip.compress(content.encode(), mtime=0))

    def write_config(self):
        (self.root / "requirements.json").write_text(json.dumps(self.config), encoding="utf-8")

    def write_metadata(self):
        with (self.root / "samples.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["sample_id", "condition", "tissue", "notes"])
            writer.writerows(self.rows)

    def query(self, query, parameters=()):
        connection = sqlite3.connect(self.db)
        try:
            return connection.execute(query, parameters).fetchall()
        finally:
            connection.close()

    def cli(self, script, *arguments):
        return subprocess.run([sys.executable, str(PROJECT_ROOT / "scripts" / script),
                               *map(str, arguments)], cwd=self.base, capture_output=True, text=True)

    def test_save_round_trip_retains_complete_evidence(self):
        report = validate_dataset(self.root)
        self.assertTrue(save_run(report, self.db))
        self.assertEqual(get_run(self.db, report.run_id), report.to_dict())
        self.assertEqual(get_latest_run(self.db), report.to_dict())
        self.assertEqual(list_runs(self.db)[0]["status"], "READY")

    def test_normalized_tables_contain_checks_snapshots_and_metadata(self):
        report = validate_dataset(self.root)
        save_run(report, self.db)
        self.assertEqual(self.query("SELECT COUNT(*) FROM projects")[0][0], 1)
        self.assertEqual(self.query("SELECT COUNT(*) FROM checks")[0][0], len(report.checks))
        self.assertEqual(self.query("SELECT COUNT(*) FROM file_snapshots")[0][0], 6)
        self.assertEqual(self.query("SELECT COUNT(*) FROM metadata_rows")[0][0], 2)
        self.assertEqual(self.query("SELECT COUNT(*) FROM issues")[0][0], 0)
        self.assertEqual(dict(self.query("SELECT relative_path, sha256 FROM file_snapshots")), report.input_sha256)

    def test_identical_retry_is_idempotent(self):
        report = validate_dataset(self.root)
        self.assertTrue(save_run(report, self.db))
        self.assertFalse(save_run(report, self.db))
        self.assertEqual(len(list_runs(self.db)), 1)

    def test_revalidation_creates_a_new_run_for_unchanged_data(self):
        first = validate_dataset(self.root)
        second = validate_dataset(self.root)
        self.assertNotEqual(first.run_id, second.run_id)
        save_run(first, self.db)
        save_run(second, self.db)
        self.assertEqual([r["run_id"] for r in list_runs(self.db)], [second.run_id, first.run_id])
        self.assertEqual(self.query("SELECT COUNT(*) FROM projects")[0][0], 1)

    def test_corrected_data_keeps_the_previous_failed_run(self):
        path = self.seq / "S001_R2.fastq.gz"
        original = path.read_bytes()
        path.unlink()
        failed = validate_dataset(self.root)
        save_run(failed, self.db)
        path.write_bytes(original)
        corrected = validate_dataset(self.root)
        save_run(corrected, self.db)
        self.assertEqual([r["status"] for r in list_runs(self.db)], ["READY", "BLOCKED"])
        self.assertEqual(get_run(self.db, failed.run_id), failed.to_dict())
        missing = self.query("SELECT inventory_status, sha256 FROM file_snapshots WHERE run_id = ? AND relative_path = ?",
                             (failed.run_id, "sequencing/S001_R2.fastq.gz"))
        self.assertEqual(missing, [("MISSING", None)])

    def test_same_run_id_cannot_overwrite_different_evidence(self):
        report = validate_dataset(self.root)
        save_run(report, self.db)
        original = get_run(self.db, report.run_id)
        report.checks[0].message = "changed after storage"
        with self.assertRaisesRegex(HistoryError, "different evidence"):
            save_run(report, self.db)
        self.assertEqual(get_run(self.db, report.run_id), original)

    def test_old_snapshot_remains_readable_after_sources_are_deleted(self):
        report = validate_dataset(self.root)
        expected = report.to_dict()
        save_run(report, self.db)
        shutil.rmtree(self.root)
        self.assertEqual(get_run(self.db, report.run_id), expected)

    def test_saving_does_not_reread_changed_metadata(self):
        report = validate_dataset(self.root)
        self.rows[0][2] = "Brain"
        self.write_metadata()
        save_run(report, self.db)
        self.assertEqual(get_run(self.db, report.run_id)["metadata_rows"][0]["tissue"], "Liver")
        metadata = self.query("SELECT values_json FROM metadata_rows WHERE csv_record = 2")[0][0]
        self.assertEqual(json.loads(metadata)["tissue"], "Liver")

    def test_literal_values_duplicate_ids_and_blank_cells_survive_storage(self):
        self.rows = [["001", "Control", "NA", ""], ["001", "Treated", " Liver ", "échantillon"]]
        self.write_metadata()
        report = validate_dataset(self.root)
        self.assertEqual(report.status, "BLOCKED")
        save_run(report, self.db)
        stored_rows = [json.loads(row[0]) for row in self.query("SELECT values_json FROM metadata_rows ORDER BY csv_record")]
        self.assertEqual(stored_rows, report.metadata_rows)
        self.assertEqual(stored_rows[0], {"sample_id": "001", "condition": "Control", "tissue": "NA", "notes": ""})
        self.assertEqual([r[0] for r in self.query("SELECT sample_id FROM metadata_rows ORDER BY csv_record")], ["001", "001"])

    def test_incomplete_run_and_required_skips_are_stored(self):
        self.config["integrity"]["require_independent_source_checksums"] = True
        self.write_config()
        report = validate_dataset(self.root)
        self.assertEqual(report.status, "INCOMPLETE")
        save_run(report, self.db)
        self.assertEqual(list_runs(self.db)[0]["required_checks_not_run"], 1)
        self.assertEqual(self.query("SELECT outcome, required FROM checks WHERE check_id = 'REF001'"), [("NOT_RUN", 1)])

    def test_execution_error_is_stored_in_issues(self):
        with patch("biodataqc.validation._metadata_values", side_effect=RuntimeError("test execution failure")):
            report = validate_dataset(self.root)
        save_run(report, self.db)
        self.assertEqual(list_runs(self.db)[0]["status"], "INCOMPLETE")
        self.assertEqual(self.query("SELECT outcome FROM issues"), [("ERROR",)])

    def test_warnings_do_not_change_saved_ready_status(self):
        self.rows[0][3] = ""
        self.write_metadata()
        report = validate_dataset(self.root)
        save_run(report, self.db)
        self.assertEqual(list_runs(self.db)[0]["status"], "READY")
        self.assertEqual(self.query("SELECT severity FROM issues"), [("WARNING",)])

    def test_load_failure_without_project_is_retained(self):
        report = validate_dataset(self.root / "missing")
        save_run(report, self.db)
        self.assertIsNone(get_run(self.db, report.run_id)["project_id"])
        self.assertEqual(list_runs(self.db)[0]["status"], "BLOCKED")
        self.assertEqual(self.query("SELECT COUNT(*) FROM projects")[0][0], 0)

    def test_unfinished_report_is_rejected_before_database_creation(self):
        with self.assertRaisesRegex(HistoryError, "finished"):
            save_run(ValidationReport(str(self.root)), self.db)
        self.assertFalse(self.db.exists())

    def test_failed_child_insert_rolls_back_run_and_new_project(self):
        good = validate_dataset(self.root)
        save_run(good, self.db)
        other = validate_dataset(self.root)
        other.project_id = "PROJECT_ROLLBACK"
        other.checks.append(other.checks[0])  # Unique check ID violation after run insertion.
        with self.assertRaises(HistoryError):
            save_run(other, self.db)
        self.assertEqual([r["run_id"] for r in list_runs(self.db)], [good.run_id])
        self.assertEqual(self.query("SELECT project_id FROM projects"), [(good.project_id,)])
        self.assertEqual(self.query("SELECT COUNT(*) FROM checks WHERE run_id = ?", (other.run_id,))[0][0], 0)

    def test_foreign_keys_are_enforced_on_application_connections(self):
        save_run(validate_dataset(self.root), self.db)
        with self.assertRaises(HistoryError):
            with _connect(self.db, write=True) as connection:
                connection.execute("INSERT INTO metadata_rows VALUES (?, ?, ?, ?)", ("no-such-run", 2, "S001", "{}"))
        self.assertEqual(self.query("SELECT COUNT(*) FROM metadata_rows")[0][0], 2)

    def test_database_inside_input_is_rejected(self):
        destination = self.root / "history.sqlite"
        with self.assertRaisesRegex(HistoryError, "outside"):
            save_run(validate_dataset(self.root), destination)
        self.assertFalse(destination.exists())

    def test_reading_missing_history_does_not_create_an_empty_database(self):
        with self.assertRaisesRegex(HistoryError, "not found"):
            list_runs(self.db)
        self.assertFalse(self.db.exists())
        self.assertFalse(self.db.parent.exists())

    def test_unrelated_database_is_not_repurposed(self):
        self.db.parent.mkdir()
        connection = sqlite3.connect(self.db)
        connection.execute("CREATE TABLE unrelated (value TEXT)")
        connection.commit()
        connection.close()
        before = self.db.read_bytes()
        with self.assertRaisesRegex(HistoryError, "not an initialized"):
            save_run(validate_dataset(self.root), self.db)
        self.assertEqual(self.db.read_bytes(), before)

    def test_newer_schema_version_is_rejected_without_modification(self):
        report = validate_dataset(self.root)
        save_run(report, self.db)
        connection = sqlite3.connect(self.db)
        connection.execute("PRAGMA user_version = 999")
        connection.close()
        before = self.db.read_bytes()
        with self.assertRaisesRegex(HistoryError, "Unsupported history schema"):
            save_run(validate_dataset(self.root), self.db)
        self.assertEqual(self.db.read_bytes(), before)

    def test_non_database_file_is_not_overwritten(self):
        self.db.parent.mkdir()
        self.db.write_text("Keep this original file.")
        with self.assertRaises(HistoryError):
            save_run(validate_dataset(self.root), self.db)
        self.assertEqual(self.db.read_text(), "Keep this original file.")

    def test_filters_and_queries_bind_user_values(self):
        report = validate_dataset(self.root)
        report.project_id = "O'Brien'); DROP TABLE validation_runs; --"
        save_run(report, self.db)
        self.assertEqual(len(list_runs(self.db, project_id=report.project_id)), 1)
        self.assertEqual(list_runs(self.db, project_id="' OR 1=1 --"), [])
        self.assertEqual(len(list_runs(self.db, dataset_path=self.root, status="READY")), 1)
        self.assertEqual(list_runs(self.db, status="BLOCKED"), [])
        self.assertEqual(self.query("SELECT COUNT(*) FROM validation_runs")[0][0], 1)

    def test_latest_and_list_filters_do_not_mix_datasets(self):
        first = validate_dataset(self.root)
        save_run(first, self.db)
        second_path = self.base / "second_dataset"
        shutil.copytree(self.root, second_path)
        second = validate_dataset(second_path)
        save_run(second, self.db)
        self.assertEqual(get_latest_run(self.db, dataset_path=self.root)["run_id"], first.run_id)
        self.assertEqual(get_latest_run(self.db)["run_id"], second.run_id)
        self.assertEqual(len(list_runs(self.db, limit=1)), 1)

    def test_invalid_limit_and_status_fail_clearly(self):
        with self.assertRaises(HistoryError):
            list_runs(self.db, limit=0)
        with self.assertRaises(HistoryError):
            list_runs(self.db, limit=True)
        with self.assertRaises(HistoryError):
            list_runs(self.db, status="RUNNING")

    def test_missing_run_and_no_matching_latest_raise_clear_errors(self):
        save_run(validate_dataset(self.root), self.db)
        with self.assertRaisesRegex(HistoryError, "No saved validation run"):
            get_run(self.db, "not-a-run")
        with self.assertRaisesRegex(HistoryError, "No saved runs match"):
            get_latest_run(self.db, project_id="OTHER")

    def test_report_export_cannot_replace_database_or_sidecars(self):
        report = validate_dataset(self.root)
        save_run(report, self.db)
        for path in database_paths(self.db):
            with self.subTest(path=path):
                with self.assertRaisesRegex(ValueError, "conflicts"):
                    write_report_json(report.to_dict(), path, database_paths(self.db))
        self.assertEqual(get_run(self.db, report.run_id), report.to_dict())

    def test_cli_validation_saves_same_run_to_sql_and_json(self):
        destination = self.base / "outputs/run.json"
        result = self.cli("validate_dataset.py", self.root, "--db", self.db, "--json", destination)
        self.assertEqual(result.returncode, 0, result.stderr)
        exported = json.loads(destination.read_text())
        self.assertEqual(get_run(self.db, exported["run_id"]), exported)
        self.assertIn("SQL history saved:", result.stdout)

    def test_cli_saves_blocked_run_despite_exit_one(self):
        (self.seq / "S001_R2.fastq.gz").unlink()
        result = self.cli("validate_dataset.py", self.root, "--db", self.db)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(list_runs(self.db)[0]["status"], "BLOCKED")

    def test_cli_saves_incomplete_run_despite_exit_two(self):
        self.config["integrity"]["require_independent_source_checksums"] = True
        self.write_config()
        result = self.cli("validate_dataset.py", self.root, "--db", self.db)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(list_runs(self.db)[0]["status"], "INCOMPLETE")
        self.assertIn("SQL history saved:", result.stdout)

    def test_cli_history_failure_is_explicit_and_json_evidence_is_preserved(self):
        self.db.parent.mkdir()
        self.db.write_text("not a database")
        destination = self.base / "outputs/run.json"
        result = self.cli("validate_dataset.py", self.root, "--db", self.db, "--json", destination)
        self.assertEqual(result.returncode, 2)
        self.assertIn("History save failed:", result.stderr)
        self.assertEqual(json.loads(destination.read_text())["status"], "READY")

    def test_cli_rejects_conflicting_output_paths_before_writing(self):
        result = self.cli("validate_dataset.py", self.root, "--db", self.db, "--json", self.db)
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.db.exists())

    def test_history_cli_lists_and_exports_saved_evidence(self):
        report = validate_dataset(self.root)
        save_run(report, self.db)
        listing = self.cli("history.py", "--db", self.db, "list")
        self.assertEqual(listing.returncode, 0, listing.stderr)
        self.assertIn(report.run_id, listing.stdout)
        destination = self.base / "outputs/recovered.json"
        result = self.cli("history.py", "--db", self.db, "show", "latest", "--details", "--json", destination)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Historical status: READY", result.stdout)
        self.assertEqual(json.loads(destination.read_text()), report.to_dict())

    def test_history_cli_refuses_export_over_database(self):
        report = validate_dataset(self.root)
        save_run(report, self.db)
        result = self.cli("history.py", "--db", self.db, "show", "latest", "--json", self.db)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(get_run(self.db, report.run_id), report.to_dict())


if __name__ == "__main__":
    unittest.main()

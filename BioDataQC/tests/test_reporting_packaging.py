"""Acceptance tests for reports, snapshot gates, verified copies and SQL migration."""

import copy
import csv
import gzip
import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from biodataqc.history import HistoryError, get_run, list_packages, list_runs, save_run
from biodataqc.packaging import (PackagingError, _copy_input, _input_plan,
                                 build_package, verify_package)
from biodataqc.reporting import render_report, write_report_html
from biodataqc.validation import validate_dataset


PROJECT_ROOT = Path(__file__).resolve().parent.parent


class DeliveryFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / "data"
        self.seq = self.root / "sequencing"
        self.seq.mkdir(parents=True)
        self.db = self.base / "outputs/history.sqlite"
        self.destination = self.base / "outputs/packages/delivery"
        self.config = json.loads((PROJECT_ROOT / "tests/fixtures/validation_requirements.json").read_text())
        self.config["expected_samples"] = ["S001", "S002"]
        self.write_config()
        self.metadata = "sample_id,condition,tissue,notes\nS001,Control,Liver,one\nS002,Treated,Liver,two\n"
        (self.root / "samples.csv").write_text(self.metadata)
        for sample in self.config["expected_samples"]:
            for mate in (1, 2):
                text = "".join(f"@READ_{i:06d}/{mate}\nACGT\n+\nIIII\n" for i in (1, 2, 3))
                (self.seq / f"{sample}_R{mate}.fastq.gz").write_bytes(gzip.compress(text.encode(), mtime=0))
        self.report = validate_dataset(self.root)
        save_run(self.report, self.db)

    def write_config(self):
        (self.root / "requirements.json").write_text(json.dumps(self.config))

    def build(self):
        return build_package(self.db, self.report.run_id, self.destination)

    def sql(self, statement, values=()):
        connection = sqlite3.connect(self.db)
        try:
            rows = connection.execute(statement, values).fetchall()
            connection.commit()
            return rows
        finally:
            connection.close()

    def cli(self, name, *args):
        return subprocess.run([sys.executable, str(PROJECT_ROOT / "scripts" / name), *map(str, args)],
                               cwd=self.base, capture_output=True, text=True)

    def assert_no_package(self):
        self.assertFalse(self.destination.exists())
        self.assertEqual(list_packages(self.db), [])
        self.assertEqual(list(self.destination.parent.glob(".biodataqc-staging-*")), [])


class ReportingTests(DeliveryFixture):
    def test_report_contains_saved_identity_summary_and_technical_evidence(self):
        html = render_report(self.report.to_dict())
        for expected in (self.report.run_id, "Delivery summary", "Technical evidence", "READY", "S001", "Input fingerprints"):
            self.assertIn(expected, html)
        self.assertIn("Not assigned", html)
        self.assertIn("To be confirmed", html)

    def test_blocked_report_explains_actual_findings(self):
        (self.seq / "S001_R2.fastq.gz").unlink()
        (self.seq / "S002_R1.fastq.gz").write_bytes(gzip.compress(b"@READ_000001/1\nACGT\n+\nII\n"))
        evidence = validate_dataset(self.root).to_dict()
        html = render_report(evidence)
        self.assertIn("Delivery preparation is blocked", html)
        self.assertIn("2 blocking finding(s)", html)
        self.assertIn("Sequence length 4 does not match quality length 2", html)
        self.assertIn("S001_R2.fastq.gz", html)

    def test_incomplete_report_is_not_presented_as_ready(self):
        self.config["integrity"]["require_independent_source_checksums"] = True
        self.write_config()
        html = render_report(validate_dataset(self.root).to_dict())
        self.assertIn("A readiness decision is incomplete", html)
        self.assertNotIn("Eligible for verified local staging", html)

    def test_load_failure_report_is_renderable(self):
        html = render_report(validate_dataset(self.root / "missing").to_dict())
        self.assertIn("Project unavailable", html)
        self.assertIn("BLOCKED", html)

    def test_operator_fields_and_source_values_are_html_escaped(self):
        evidence = self.report.to_dict()
        evidence["requirements"]["client_label"] = '<script>alert("bad")</script>'
        evidence["metadata_rows"][0]["notes"] = '<img src=x onerror="alert(1)">'
        html = render_report(evidence, owner="<b>Owner & team</b>", next_update="Tomorrow < 12:00")
        self.assertNotIn("<script", html.lower())
        self.assertNotIn("<img", html.lower())
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("&lt;b&gt;Owner &amp; team&lt;/b&gt;", html)
        self.assertIn("Tomorrow &lt; 12:00", html)

    def test_report_is_self_contained(self):
        html = render_report(self.report.to_dict())
        self.assertNotIn('src="http', html)
        self.assertNotIn('href="http', html)
        self.assertNotIn("<script", html)
        self.assertIn("Content-Security-Policy", html)

    def test_report_can_be_created_after_inputs_are_deleted(self):
        shutil.rmtree(self.root)
        path = write_report_html(get_run(self.db, self.report.run_id), self.base / "report.html")
        self.assertIn(self.report.run_id, path.read_text())

    def test_report_output_inside_input_is_refused(self):
        with self.assertRaises(ValueError):
            write_report_html(self.report.to_dict(), self.root / "report.html")
        self.assertFalse((self.root / "report.html").exists())

    def test_report_cli_uses_saved_run_and_explicit_owner(self):
        output = self.base / "reports/clean.html"
        result = self.cli("report_dataset.py", "latest", "--dataset", self.root, "--db", self.db,
                          "--output", output, "--owner", "Demo data team")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Demo data team", output.read_text())
        self.assertIn(self.report.run_id, output.read_text())

    def test_report_cli_cannot_overwrite_sql_database(self):
        result = self.cli("report_dataset.py", "latest", "--db", self.db, "--output", self.db)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(get_run(self.db, self.report.run_id), self.report.to_dict())


class PackagingTests(DeliveryFixture):
    def test_ready_package_contains_exact_files_and_preserves_sources(self):
        before = {p.relative_to(self.root).as_posix(): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        record = self.build()
        after = {p.relative_to(self.root).as_posix(): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        package_files = {p.relative_to(self.destination).as_posix() for p in self.destination.rglob("*") if p.is_file()}
        self.assertEqual(package_files, set(before) | {"report.html", "run_summary.json", "manifest.csv"})
        self.assertEqual(record["files_count"], 9)
        self.assertEqual(json.loads((self.destination / "run_summary.json").read_text()), self.report.to_dict())
        for relative, content in before.items():
            self.assertEqual((self.destination / relative).read_bytes(), content)
        self.assertEqual(verify_package(self.db, self.destination)["verification_status"], "VERIFIED")

    def test_manifest_covers_every_file_except_itself(self):
        record = self.build()
        with (self.destination / "manifest.csv").open(newline="") as handle:
            reader = csv.DictReader(handle)
            self.assertEqual(reader.fieldnames, ["relative_path", "sample_id", "read", "size_bytes", "sha256"])
            rows = list(reader)
        self.assertEqual(len(rows), 8)
        self.assertNotIn("manifest.csv", [r["relative_path"] for r in rows])
        for row in rows:
            content = (self.destination / row["relative_path"]).read_bytes()
            self.assertEqual(int(row["size_bytes"]), len(content))
            self.assertEqual(row["sha256"], hashlib.sha256(content).hexdigest())
            if not row["relative_path"].startswith("sequencing/"):
                self.assertEqual((row["sample_id"], row["read"]), ("", ""))
        self.assertEqual(record["manifest_sha256"], hashlib.sha256((self.destination / "manifest.csv").read_bytes()).hexdigest())

    def test_completed_package_is_linked_to_the_saved_run(self):
        record = self.build()
        self.assertEqual(list_packages(self.db), [record])
        self.assertEqual(list_packages(self.db, run_id=self.report.run_id), [record])
        self.assertEqual(len(list_runs(self.db)), 1)

    def test_blocked_run_is_refused_before_creating_output(self):
        (self.seq / "S001_R2.fastq.gz").unlink()
        blocked = validate_dataset(self.root)
        save_run(blocked, self.db)
        with self.assertRaisesRegex(PackagingError, "BLOCKED"):
            build_package(self.db, blocked.run_id, self.destination)
        self.assert_no_package()

    def test_incomplete_run_is_refused(self):
        self.config["integrity"]["require_independent_source_checksums"] = True
        self.write_config()
        report = validate_dataset(self.root)
        save_run(report, self.db)
        with self.assertRaisesRegex(PackagingError, "INCOMPLETE"):
            build_package(self.db, report.run_id, self.destination)
        self.assert_no_package()

    def test_warning_only_run_is_allowed(self):
        (self.root / "samples.csv").write_text(self.metadata.replace("Liver,one", "Liver,"))
        report = validate_dataset(self.root)
        self.assertEqual(report.status, "READY")
        self.assertEqual(report.counts()["warnings"], 1)
        save_run(report, self.db)
        record = build_package(self.db, report.run_id, self.destination)
        self.assertEqual(record["run_id"], report.run_id)
        self.assertIn("WARNING", (self.destination / "report.html").read_text())

    def test_metadata_change_invalidates_old_ready_run(self):
        (self.root / "samples.csv").write_text(self.metadata.replace("Liver", "Brain"))
        with self.assertRaisesRegex(PackagingError, "Input changed"):
            self.build()
        self.assert_no_package()

    def test_requirements_byte_change_invalidates_old_ready_run(self):
        path = self.root / "requirements.json"
        path.write_text(path.read_text() + "\n")
        with self.assertRaisesRegex(PackagingError, "Input changed"):
            self.build()
        self.assert_no_package()

    def test_same_size_fastq_change_is_detected(self):
        path = self.seq / "S001_R1.fastq.gz"
        content = bytearray(path.read_bytes())
        content[4] ^= 1  # Gzip mtime byte; size and decompressed records stay unchanged.
        path.write_bytes(content)
        with self.assertRaisesRegex(PackagingError, "Input changed"):
            self.build()
        self.assert_no_package()

    def test_missing_file_since_validation_is_detected(self):
        (self.seq / "S001_R1.fastq.gz").unlink()
        with self.assertRaisesRegex(PackagingError, "inventory changed"):
            self.build()
        self.assert_no_package()

    def test_unexpected_sequencing_file_since_validation_is_detected(self):
        (self.seq / "extra.txt").write_text("new file")
        with self.assertRaisesRegex(PackagingError, "inventory changed"):
            self.build()
        self.assert_no_package()

    def test_unexpected_subdirectory_since_validation_is_detected(self):
        (self.seq / "extra").mkdir()
        with self.assertRaisesRegex(PackagingError, "inventory changed"):
            self.build()

    def test_unrelated_root_file_is_not_included_in_package(self):
        (self.root / "personal_notes.txt").write_text("outside the input contract")
        self.build()
        self.assertFalse((self.destination / "personal_notes.txt").exists())

    def test_symlink_replacing_input_is_refused(self):
        path = self.seq / "S001_R1.fastq.gz"
        path.unlink()
        path.symlink_to(self.seq / "S002_R1.fastq.gz")
        with self.assertRaises(PackagingError):
            self.build()

    def test_missing_check_cannot_be_hidden_behind_ready_label(self):
        evidence = self.report.to_dict()
        evidence["checks"] = evidence["checks"][1:]
        self.sql("UPDATE validation_runs SET report_json = ? WHERE run_id = ?", (json.dumps(evidence), self.report.run_id))
        with self.assertRaises(PackagingError):
            self.build()
        self.assert_no_package()

    def test_required_failure_cannot_be_downgraded_to_warning_for_packaging(self):
        report = copy.deepcopy(self.report)
        check = next(c for c in report.checks if c.rule_id == "FASTQ001")
        check.outcome, check.severity = "FAIL", "WARNING"
        self.assertEqual(report.status, "READY")
        with self.assertRaisesRegex(PackagingError, "Required check did not pass"):
            _input_plan(report.to_dict())

    def test_missing_hash_cannot_support_packaging(self):
        evidence = self.report.to_dict()
        evidence["input_sha256"].pop("samples.csv")
        with self.assertRaisesRegex(PackagingError, "fingerprint set"):
            _input_plan(evidence)

    def test_unsafe_path_in_saved_requirements_is_refused(self):
        evidence = self.report.to_dict()
        evidence["requirements"]["metadata"]["file"] = "../outside.csv"
        evidence["requirements"]["integrity"]["snapshot_inputs"][1] = "../outside.csv"
        with self.assertRaises(PackagingError):
            _input_plan(evidence)

    def test_reserved_output_name_cannot_replace_input(self):
        (self.root / "samples.csv").rename(self.root / "report.html")
        self.config["metadata"]["file"] = "report.html"
        self.config["integrity"]["snapshot_inputs"][1] = "report.html"
        self.write_config()
        report = validate_dataset(self.root)
        self.assertEqual(report.status, "READY")
        save_run(report, self.db)
        with self.assertRaisesRegex(PackagingError, "collides"):
            build_package(self.db, report.run_id, self.destination)

    def test_destination_inside_source_is_refused(self):
        destination = self.root / "delivery"
        with self.assertRaisesRegex(PackagingError, "separate"):
            build_package(self.db, self.report.run_id, destination)
        self.assertFalse(destination.exists())

    def test_existing_empty_destination_is_not_overwritten(self):
        self.destination.mkdir(parents=True)
        with self.assertRaisesRegex(PackagingError, "already exists"):
            self.build()
        self.assertTrue(self.destination.is_dir())
        self.assertEqual(list(self.destination.iterdir()), [])

    def test_existing_completed_package_is_not_overwritten(self):
        self.build()
        before = (self.destination / "manifest.csv").read_bytes()
        with self.assertRaisesRegex(PackagingError, "already exists"):
            self.build()
        self.assertEqual((self.destination / "manifest.csv").read_bytes(), before)
        self.assertEqual(len(list_packages(self.db)), 1)

    def test_changed_input_during_copy_stops_completion(self):
        changed = False

        def copy_then_change(source, target):
            nonlocal changed
            _copy_input(source, target)
            if not changed:
                path = self.root / "requirements.json"
                path.write_text(path.read_text() + "\n")
                changed = True

        with patch("biodataqc.packaging._copy_input", side_effect=copy_then_change):
            with self.assertRaises(PackagingError):
                self.build()
        self.assert_no_package()

    def test_corrupted_staged_copy_stops_completion(self):
        def corrupt_copy(source, target):
            _copy_input(source, target)
            if source.name == "samples.csv":
                target.write_bytes(target.read_bytes() + b"corrupted")

        with patch("biodataqc.packaging._copy_input", side_effect=corrupt_copy):
            with self.assertRaisesRegex(PackagingError, "Staged copy"):
                self.build()
        self.assert_no_package()

    def test_copy_io_failure_cleans_up_staging(self):
        with patch("biodataqc.packaging._copy_input", side_effect=OSError("simulated disk failure")):
            with self.assertRaises(OSError):
                self.build()
        self.assert_no_package()

    def test_final_verification_failure_removes_uncommitted_output(self):
        with patch("biodataqc.packaging._verify_directory", side_effect=PackagingError("simulated corruption")):
            with self.assertRaises(PackagingError):
                self.build()
        self.assert_no_package()

    def test_sql_failure_after_promotion_rolls_back_and_removes_output(self):
        with patch("biodataqc.packaging._insert_completed", side_effect=sqlite3.OperationalError("simulated SQL failure")):
            with self.assertRaises(HistoryError):
                self.build()
        self.assert_no_package()
        self.assertEqual(get_run(self.db, self.report.run_id), self.report.to_dict())

    def test_verification_detects_changed_packaged_file(self):
        self.build()
        (self.destination / "samples.csv").write_text("changed")
        with self.assertRaisesRegex(PackagingError, "does not match"):
            verify_package(self.db, self.destination)

    def test_verification_detects_changed_manifest(self):
        self.build()
        path = self.destination / "manifest.csv"
        path.write_bytes(path.read_bytes() + b"\n")
        with self.assertRaisesRegex(PackagingError, "Manifest fingerprint"):
            verify_package(self.db, self.destination)

    def test_verification_detects_missing_package_file(self):
        self.build()
        (self.destination / "report.html").unlink()
        with self.assertRaisesRegex(PackagingError, "missing or unexpected"):
            verify_package(self.db, self.destination)

    def test_verification_detects_extra_package_file(self):
        self.build()
        (self.destination / "extra.txt").write_text("extra")
        with self.assertRaisesRegex(PackagingError, "missing or unexpected"):
            verify_package(self.db, self.destination)

    def test_verification_detects_extra_empty_package_directory(self):
        self.build()
        (self.destination / "extra").mkdir()
        with self.assertRaisesRegex(PackagingError, "missing or unexpected"):
            verify_package(self.db, self.destination)

    def test_verification_refuses_symlinked_package_file(self):
        self.build()
        path = self.destination / "samples.csv"
        path.unlink()
        path.symlink_to(self.root / "samples.csv")
        with self.assertRaisesRegex(PackagingError, "symlink"):
            verify_package(self.db, self.destination)

    def test_package_verification_does_not_depend_on_current_sources(self):
        self.build()
        shutil.rmtree(self.root)
        self.assertEqual(verify_package(self.db, self.destination)["verification_status"], "VERIFIED")

    def test_version_one_database_is_readable_and_migrated_without_losing_history(self):
        self.sql("DROP TRIGGER packages_require_ready")
        self.sql("DROP TABLE packages")
        self.sql("PRAGMA user_version = 1")
        before = self.db.read_bytes()
        self.assertEqual(list_packages(self.db), [])
        self.assertEqual(get_run(self.db, self.report.run_id), self.report.to_dict())
        self.assertEqual(self.db.read_bytes(), before)
        record = self.build()
        self.assertEqual(self.sql("PRAGMA user_version"), [(2,)])
        self.assertEqual(list_packages(self.db), [record])
        self.assertEqual(get_run(self.db, self.report.run_id), self.report.to_dict())

    def test_sql_trigger_disallows_completed_package_for_blocked_run(self):
        (self.seq / "S001_R2.fastq.gz").unlink()
        report = validate_dataset(self.root)
        save_run(report, self.db)
        with self.assertRaises(sqlite3.IntegrityError):
            self.sql("INSERT INTO packages VALUES (?, ?, ?, ?, ?, ?, ?)",
                     ("fake", report.run_id, "now", str(self.destination), "a" * 64, 1, 1))
        self.assertEqual(list_packages(self.db), [])

    def test_package_cli_build_verify_and_list(self):
        result = self.cli("package_dataset.py", "--db", self.db, "build", "latest", "--dataset", self.root,
                          "--output", self.destination)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Package status: COMPLETE", result.stdout)
        verify = self.cli("package_dataset.py", "--db", self.db, "verify", self.destination)
        self.assertEqual(verify.returncode, 0, verify.stderr)
        self.assertIn("VERIFIED", verify.stdout)
        listing = self.cli("package_dataset.py", "--db", self.db, "list")
        self.assertEqual(listing.returncode, 0, listing.stderr)
        self.assertIn(self.report.run_id, listing.stdout)

    def test_package_cli_returns_one_for_blocked_run(self):
        (self.seq / "S001_R2.fastq.gz").unlink()
        report = validate_dataset(self.root)
        save_run(report, self.db)
        result = self.cli("package_dataset.py", "--db", self.db, "build", "latest", "--output", self.destination)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("BLOCKED", result.stderr)
        self.assert_no_package()


if __name__ == "__main__":
    unittest.main()

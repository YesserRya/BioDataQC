"""The optional demo preserves source bytes and records a real correction."""

import hashlib
import subprocess
import sys

from biodataqc.comparison import compare_runs
from biodataqc.history import get_run, list_runs
from test_comparison import ComparisonFixture, PROJECT_ROOT


class ComparisonDemoTests(ComparisonFixture):
    def command(self, destination):
        return subprocess.run([sys.executable, str(PROJECT_ROOT / "scripts/demo_comparison.py"),
                               "--source", str(self.root), "--dataset", str(destination), "--db", str(self.db)],
                              capture_output=True, text=True)

    def source_hashes(self):
        return {p.relative_to(self.root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in self.root.rglob("*") if p.is_file()}

    def test_demo_records_a_real_failure_then_repair_and_preserves_source(self):
        hashes = self.source_hashes()
        destination = self.base / "data/comparison_demo"
        completed = self.command(destination)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        rows = list_runs(self.db)
        self.assertEqual([r["status"] for r in rows], ["READY", "BLOCKED"])
        before, after = get_run(self.db, rows[1]["run_id"]), get_run(self.db, rows[0]["run_id"])
        self.assertEqual(before["dataset_path"], after["dataset_path"])
        self.assertEqual(before["requirements"], after["requirements"])
        self.assertEqual(compare_runs(before, after)["counts"]["RESOLVED"], 1)
        self.assertEqual(self.source_hashes(), hashes)
        self.assertTrue(before["project_id"].endswith("_COMPARISON_DEMO"))

    def test_demo_refuses_existing_destination_without_touching_history_or_files(self):
        destination = self.base / "data/comparison_demo"
        destination.mkdir()
        marker = destination / "keep.txt"
        marker.write_text("keep")
        result = self.command(destination)
        self.assertEqual(result.returncode, 2)
        self.assertIn("already exists", result.stderr)
        self.assertFalse(self.db.exists())
        self.assertEqual(marker.read_text(), "keep")

    def test_demo_refuses_nonready_source(self):
        (self.seq / "S001_R2.fastq.gz").unlink()
        destination = self.base / "data/comparison_demo"
        result = self.command(destination)
        self.assertEqual(result.returncode, 2)
        self.assertIn("must validate READY", result.stderr)
        self.assertFalse(destination.exists())
        self.assertFalse(self.db.exists())

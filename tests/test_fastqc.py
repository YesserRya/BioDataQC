"""Tool execution, parsed output, persistence and snapshot binding."""

import json
import os
from pathlib import Path
import shutil
import sys
import unittest
from unittest.mock import patch
import zipfile

from biodataqc import fastqc
from biodataqc.history import get_run, list_runs
from test_comparison import ComparisonFixture, PROJECT_ROOT


def make_stub(directory):
    path = directory / "fake fastqc"
    source = (PROJECT_ROOT / "tests/fixtures/fastqc_stub.py").read_text()
    path.write_text(f"#!{sys.executable}\n" + source)
    path.chmod(0o755)
    return path


class FastQCFixture(ComparisonFixture):
    def setUp(self):
        super().setUp()
        self.exe = make_stub(self.base)
        self.evidence = self.run_evidence()
        self.output = self.base / "outputs/fastqc"

    def analyze(self, **kwargs):
        return fastqc.run_fastqc(self.db, self.evidence["run_id"], self.output, executable=str(self.exe), **kwargs)

    def rewrite_zip(self, directory, transform):
        metadata = fastqc.load_analysis(directory, self.evidence)
        item = metadata["files"][0]
        archive = directory / item["zip"]
        with zipfile.ZipFile(archive) as source:
            contents = {n: source.read(n) for n in source.namelist()}
        transform(contents)
        with zipfile.ZipFile(archive, "w") as target:
            for name, content in contents.items():
                target.writestr(name, content)
        return archive, item


class FastQCTests(FastQCFixture):
    def test_execution_keeps_quality_flags_separate_from_readiness(self):
        database = self.db.read_bytes()
        path = self.analyze()
        result = fastqc.load_analysis(path, self.evidence)
        self.assertEqual(result["execution_status"], "COMPLETED")
        self.assertEqual(len(result["files"]), 4)
        self.assertEqual(result["files"][0]["result"]["module_counts"], {"PASS": 2, "WARN": 1, "FAIL": 1})
        self.assertEqual(get_run(self.db, self.evidence["run_id"])["status"], "READY")
        self.assertEqual(self.db.read_bytes(), database)
        self.assertFalse((path / "inputs").exists())
        self.assertEqual(len(list_runs(self.db)), 1)

    def test_actual_command_uses_verified_copies_and_handles_spaces(self):
        path = self.analyze()
        result = fastqc.load_analysis(path, self.evidence)
        self.assertEqual(result["command"][0], str(self.exe))
        self.assertIn("--noextract", result["command"])
        inputs = [s for s in result["command"] if s.endswith(".fastq.gz")]
        self.assertEqual(len(inputs), 4)
        self.assertTrue(all(".pending-" in p for p in inputs))
        self.assertTrue(all(str(self.root) not in p for p in inputs))

    def test_same_validation_can_have_multiple_analyses(self):
        first, second = self.analyze(), self.analyze()
        self.assertNotEqual(first, second)
        rows, errors = fastqc.list_analyses(self.output, self.evidence)
        self.assertEqual(len(rows), 2)
        self.assertEqual(errors, [])
        self.assertEqual(len(list_runs(self.db)), 1)

    def test_stale_source_is_rejected_before_launch(self):
        (self.root / "samples.csv").write_text("changed")
        with patch("biodataqc.fastqc.check_installation") as check:
            with self.assertRaisesRegex(ValueError, "changed"):
                self.analyze()
            check.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_source_change_during_tool_run_is_rejected(self):
        execute = fastqc._execute
        def changing(command, log, **kwargs):
            result = execute(command, log, **kwargs)
            if "--version" not in command:
                path = self.root / "samples.csv"
                path.write_text(path.read_text() + "\n")
            return result
        with patch("biodataqc.fastqc._execute", side_effect=changing):
            with self.assertRaisesRegex(ValueError, "changed"):
                self.analyze()
        self.assertEqual(fastqc.list_analyses(self.output, self.evidence), ([], []))

    def test_failed_timeout_incomplete_and_wrong_input_reports_never_publish(self):
        for mode in ("fail", "timeout", "missing_report", "wrong_count", "mutate_input"):
            with self.subTest(mode=mode), patch.dict(os.environ, {"BIODATAQC_TEST_FASTQC_MODE": mode}):
                with self.assertRaises(ValueError):
                    self.analyze(timeout=1)
                self.assertEqual(fastqc.list_analyses(self.output, self.evidence), ([], []))
                self.assertEqual(list(self.output.rglob(".pending-*")), [])

    def test_missing_and_wrong_version_tools_are_explicit(self):
        with self.assertRaisesRegex(fastqc.FastQCError, "not found"):
            fastqc.check_installation(str(self.base / "missing"))
        with patch.dict(os.environ, {"BIODATAQC_TEST_FASTQC_MODE": "wrong_version"}):
            with self.assertRaisesRegex(fastqc.FastQCError, "found 0.13.0"):
                fastqc.check_installation(self.exe)

    def test_installation_changes_during_execution_are_rejected(self):
        check = fastqc.check_installation
        calls = 0
        def changed(executable):
            nonlocal calls
            result = check(executable)
            calls += 1
            if calls == 2:
                result["launcher_sha256"] = "0" * 64
            return result
        with patch("biodataqc.fastqc.check_installation", side_effect=changed):
            with self.assertRaisesRegex(fastqc.FastQCError, "installation changed"):
                self.analyze()
        self.assertEqual(fastqc.list_analyses(self.output, self.evidence), ([], []))

    def test_original_input_bytes_are_preserved(self):
        before = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.analyze()
        self.assertEqual(before, {p: p.read_bytes() for p in before})

    def test_blocked_unsaved_and_input_output_collisions_are_rejected(self):
        (self.seq / "S001_R2.fastq.gz").unlink()
        blocked = self.run_evidence()
        with self.assertRaises(ValueError):
            fastqc.run_fastqc(self.db, blocked["run_id"], self.output, executable=self.exe)
        with self.assertRaises(ValueError):
            fastqc.run_fastqc(self.db, "unknown", self.output, executable=self.exe)
        self.write_fastq()
        with self.assertRaises(ValueError):
            fastqc.run_fastqc(self.db, self.evidence["run_id"], self.root / "qc", executable=self.exe)
        with self.assertRaises(ValueError):
            fastqc.run_fastqc(self.db, self.evidence["run_id"], self.db, executable=self.exe)

    def test_load_survives_source_deletion_and_missing_executable(self):
        path = self.analyze()
        shutil.rmtree(self.root)
        self.exe.unlink()
        self.assertEqual(fastqc.load_analysis(path, self.evidence)["execution_status"], "COMPLETED")

    def test_reports_cannot_be_attached_to_a_different_validation(self):
        path = self.analyze()
        newer = self.run_evidence()
        with self.assertRaisesRegex(ValueError, "does not match"):
            fastqc.load_analysis(path, newer)
        self.assertEqual(fastqc.list_analyses(self.output, newer), ([], []))

    def test_report_tampering_is_detected_for_display_and_download(self):
        path = self.analyze()
        result = fastqc.load_analysis(path, self.evidence)
        relative = result["files"][0]["html"]
        self.assertIn(b"Test double", fastqc.read_artifact(path, result, relative))
        (path / relative).write_text("changed")
        with self.assertRaisesRegex(ValueError, "changed"):
            fastqc.load_analysis(path, self.evidence)
        with self.assertRaisesRegex(ValueError, "changed"):
            fastqc.read_artifact(path, result, relative)
        with self.assertRaises(ValueError):
            fastqc.read_artifact(path, result, "../database.sqlite")

    def test_tampered_cached_summary_is_checked_against_original_report(self):
        path = self.analyze()
        record = path / "analysis.json"
        result = json.loads(record.read_text())
        result["files"][0]["result"]["records"] = 9999
        record.write_text(json.dumps(result))
        with self.assertRaisesRegex(ValueError, "summary differs"):
            fastqc.load_analysis(path, self.evidence)

    def test_changed_sample_mapping_is_rejected(self):
        path = self.analyze()
        record = path / "analysis.json"
        result = json.loads(record.read_text())
        result["files"][0]["sample_id"] = "OTHER_SAMPLE"
        record.write_text(json.dumps(result))
        with self.assertRaisesRegex(ValueError, "input mapping"):
            fastqc.load_analysis(path, self.evidence)

    def test_corrupt_metadata_is_reported_in_history(self):
        path = self.analyze()
        (path / "analysis.json").write_text("broken")
        rows, errors = fastqc.list_analyses(self.output, self.evidence)
        self.assertEqual(rows, [])
        self.assertEqual(len(errors), 1)

    def test_parameters_are_bounded(self):
        for parameters in ({"threads": 0}, {"threads": 5}, {"threads": True}, {"timeout": 0}):
            with self.subTest(parameters=parameters), self.assertRaises(ValueError):
                self.analyze(**parameters)

    def test_position_groups_and_raw_metrics_are_preserved(self):
        result = fastqc.load_analysis(self.analyze(), self.evidence)["files"][0]["result"]
        self.assertEqual(result["plots"]["quality"][1]["Base group"], "2-4")
        self.assertEqual(result["plots"]["quality"][1]["Position"], 3)
        self.assertEqual(result["plots"]["adapters"][1]["Test adapter"], 15)
        self.assertEqual(result["gc_percent"], 50)

    def test_truncated_report_is_rejected(self):
        path = self.analyze()
        def truncate(contents):
            key = next(k for k in contents if k.endswith("fastqc_data.txt"))
            contents[key] = contents[key].rsplit(b">>END_MODULE", 1)[0]
        archive, item = self.rewrite_zip(path, truncate)
        with self.assertRaisesRegex(ValueError, "truncated"):
            fastqc.parse_report(archive, input_name=item["input_name"], expected_records=1)

    def test_summary_and_data_disagreement_is_rejected(self):
        path = self.analyze()
        def change(contents):
            key = next(k for k in contents if k.endswith("summary.txt"))
            contents[key] = contents[key].replace(b"FAIL", b"PASS")
        archive, item = self.rewrite_zip(path, change)
        with self.assertRaisesRegex(ValueError, "disagree"):
            fastqc.parse_report(archive, input_name=item["input_name"], expected_records=1)

    def test_wrong_report_filename_and_count_are_rejected(self):
        result = fastqc.load_analysis(self.analyze(), self.evidence)
        directory = fastqc.list_analyses(self.output, self.evidence)[0][0]["directory"]
        item = result["files"][0]
        for name, count in (("wrong.fastq.gz", 1), (item["input_name"], 999)):
            with self.subTest(name=name), self.assertRaises(ValueError):
                fastqc.parse_report(Path(directory) / item["zip"], input_name=name, expected_records=count)

    def test_zip_traversal_duplicate_and_oversized_members_are_rejected(self):
        for mode in ("traversal", "duplicate", "oversized"):
            with self.subTest(mode=mode):
                path = self.analyze()
                result = fastqc.load_analysis(path, self.evidence)
                item = result["files"][0]
                archive = path / item["zip"]
                with zipfile.ZipFile(archive, "a") as handle:
                    if mode == "traversal":
                        handle.writestr("../escape", "bad")
                    elif mode == "duplicate":
                        with self.assertWarns(UserWarning):
                            handle.writestr(handle.namelist()[0], "duplicate")
                    else:
                        pass
                context = patch("biodataqc.fastqc.MAX_TEXT_BYTES", 10) if mode == "oversized" else patch.dict(os.environ, {})
                with context, self.assertRaises(ValueError):
                    fastqc.parse_report(archive, input_name=item["input_name"], expected_records=1)


if __name__ == "__main__":
    unittest.main()

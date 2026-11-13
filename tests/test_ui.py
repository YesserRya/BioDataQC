"""Exercise the real Streamlit interface with AppTest, without a browser/server."""

import csv
import gzip
import json
import logging
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from biodataqc import ui_service as service
from biodataqc.history import get_run, list_packages, list_runs, save_run
from biodataqc.validation import validate_dataset


PROJECT_ROOT = Path(__file__).resolve().parent.parent
logging.getLogger("streamlit.runtime.scriptrunner_utils.script_run_context").setLevel(logging.ERROR)


class UIFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.clean = self.root / "data/clean"
        self.broken = self.root / "data/broken"
        sequence = self.clean / "sequencing"
        sequence.mkdir(parents=True)
        config = json.loads((PROJECT_ROOT / "tests/fixtures/validation_requirements.json").read_text())
        config["expected_samples"] = ["S001", "S002"]
        (self.clean / "requirements.json").write_text(json.dumps(config))
        with (self.clean / "samples.csv").open("w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["sample_id", "condition", "tissue", "notes"])
            writer.writerows([["S001", "Control", "Liver", "one"], ["S002", "Treated", "Liver", "two"]])
        for sample in config["expected_samples"]:
            for mate in (1, 2):
                text = "".join(f"@READ_{i:06d}/{mate}\nACGT\n+\nIIII\n" for i in (1, 2, 3))
                (sequence / f"{sample}_R{mate}.fastq.gz").write_bytes(gzip.compress(text.encode(), mtime=0))
        shutil.copytree(self.clean, self.broken)
        (self.broken / "sequencing/S001_R2.fastq.gz").unlink()
        (self.broken / "sequencing/S002_R1.fastq.gz").write_bytes(gzip.compress(b"@READ_000001/1\nACGT\n+\nII\n"))
        self.db = self.root / "outputs/biodataqc.sqlite"

    def start(self):
        source = f"from pathlib import Path\nfrom biodataqc.ui import main\nmain(Path({str(self.root)!r}))\n"
        self.app = AppTest.from_string(source, default_timeout=30).run()
        self.assert_no_errors()
        return self.app

    def assert_no_errors(self):
        self.assertEqual([e.message for e in self.app.exception], [])

    def validate(self):
        self.app.button(key="bqc_run").click().run()
        self.assert_no_errors()
        return self.app.session_state["bqc_evidence"]


class InterfaceTests(UIFixture):
    def test_startup_does_not_create_database_or_claim_ready(self):
        app = self.start()
        self.assertFalse(self.db.exists())
        self.assertNotIn("bqc_evidence", app.session_state)
        self.assertEqual(len(app.metric), 0)
        self.assertEqual([t.label for t in app.tabs], ["Delivery summary", "Technical results", "Sequencing quality", "Reports & packages", "History"])

    def test_inventory_preview_is_not_validation(self):
        app = self.start()
        app.button(key="bqc_load").click().run()
        self.assert_no_errors()
        self.assertFalse(self.db.exists())
        self.assertNotIn("bqc_evidence", app.session_state)
        self.assertEqual(app.session_state["bqc_inventory"]["validation_status"], "NOT_VALIDATED")
        self.assertIn("NOT_VALIDATED", " ".join(info.value for info in app.info))

    def test_clean_validation_saves_exact_evidence_and_enables_packaging(self):
        app = self.start()
        evidence = self.validate()
        self.assertEqual(evidence["status"], "READY")
        self.assertTrue(app.session_state["bqc_saved"])
        self.assertEqual(get_run(self.db, evidence["run_id"]), evidence)
        self.assertFalse(app.button(key="bqc_build_package").disabled)
        self.assertIn(("Files present", "4 / 4"), [(m.label, m.value) for m in app.metric])

    def test_broken_validation_has_two_root_blockers_and_disabled_packaging(self):
        app = self.start()
        app.radio(key="bqc_source").set_value("Broken demo").run()
        evidence = self.validate()
        self.assertEqual(evidence["status"], "BLOCKED")
        self.assertEqual(evidence["counts"]["blocking_findings"], 2)
        self.assertEqual(evidence["counts"]["required_checks_not_run"], 3)
        self.assertTrue(app.button(key="bqc_build_package").disabled)
        self.assertFalse(app.button(key="bqc_save_reports").disabled)

    def test_changing_dataset_clears_previous_ready_result(self):
        app = self.start()
        self.validate()
        app.radio(key="bqc_source").set_value("Broken demo").run()
        self.assert_no_errors()
        self.assertNotIn("bqc_evidence", app.session_state)
        self.assertEqual(len(app.metric), 0)
        self.assertEqual(len(list_runs(self.db)), 1)

    def test_changing_database_clears_active_evidence(self):
        app = self.start()
        self.validate()
        other = self.root / "outputs/other.sqlite"
        app.text_input(key="bqc_database").set_value(str(other)).run()
        self.assert_no_errors()
        self.assertNotIn("bqc_evidence", app.session_state)
        self.assertFalse(other.exists())
        self.assertEqual(len(list_runs(self.db)), 1)

    def test_reruns_and_owner_edits_do_not_create_more_sql_runs(self):
        app = self.start()
        first = self.validate()
        app.run()
        app.text_input(key="bqc_owner").set_value("Yesser Rizi").run()
        app.text_input(key="bqc_next_update").set_value("To be confirmed").run()
        app.multiselect(key="bqc_outcomes").set_value(["PASS"]).run()
        self.assert_no_errors()
        self.assertEqual([r["run_id"] for r in list_runs(self.db)], [first["run_id"]])

    def test_each_explicit_validation_adds_a_new_run(self):
        self.start()
        first = self.validate()
        second = self.validate()
        self.assertNotEqual(first["run_id"], second["run_id"])
        self.assertEqual(len(list_runs(self.db)), 2)

    def test_ui_saves_reports_with_operator_fields(self):
        app = self.start()
        evidence = self.validate()
        app.text_input(key="bqc_owner").set_value("Yesser Rizi").run()
        app.text_input(key="bqc_next_update").set_value("After review").run()
        app.button(key="bqc_save_reports").click().run()
        self.assert_no_errors()
        notice = app.session_state["bqc_report_notice"]
        html = Path(notice["paths"]["html"]).read_text()
        self.assertIn("Yesser Rizi", html)
        self.assertIn("After review", html)
        self.assertIn(evidence["run_id"], html)
        self.assertEqual(json.loads(Path(notice["paths"]["json"]).read_text()), evidence)
        self.assertEqual(len(list_runs(self.db)), 1)

    def test_build_and_verify_package_from_interface(self):
        app = self.start()
        evidence = self.validate()
        app.button(key="bqc_build_package").click().run()
        self.assert_no_errors()
        record = app.session_state["bqc_package_notice"]
        self.assertEqual(record["run_id"], evidence["run_id"])
        self.assertEqual(record["files_count"], 9)
        self.assertTrue(Path(record["package_path"]).is_dir())
        self.assertTrue(app.button(key="bqc_build_package").disabled)
        app.button(key="bqc_verify_package").click().run()
        self.assert_no_errors()
        self.assertTrue(app.session_state["bqc_package_verification"]["ok"])
        self.assertEqual(len(list_packages(self.db)), 1)
        self.assertEqual(len(list_runs(self.db)), 1)

    def test_refresh_detects_changed_inputs_and_disables_package_button(self):
        app = self.start()
        evidence = self.validate()
        path = self.clean / "samples.csv"
        path.write_text(path.read_text().replace("Liver", "Brain"))
        app.button(key="bqc_check_snapshot").click().run()
        self.assert_no_errors()
        self.assertEqual(app.session_state["bqc_freshness"]["state"], "REVALIDATE")
        self.assertTrue(app.button(key="bqc_build_package").disabled)
        self.assertEqual(get_run(self.db, evidence["run_id"])["status"], "READY")
        visible_text = " ".join(item.value for item in app.markdown)
        self.assertIn("resolve input changes or access issues", visible_text)
        self.assertNotIn("Next action: Prepare a verified package", visible_text)

    def test_backend_refuses_changed_inputs_even_without_refresh(self):
        app = self.start()
        self.validate()
        path = self.clean / "requirements.json"
        path.write_text(path.read_text() + "\n")
        app.button(key="bqc_build_package").click().run()
        self.assert_no_errors()
        self.assertEqual(list_packages(self.db), [])
        self.assertEqual(app.session_state["bqc_freshness"]["state"], "REVALIDATE")
        self.assertTrue(app.button(key="bqc_build_package").disabled)
        self.assertIn("Input changed", " ".join(e.value for e in app.error))

    def test_new_validation_clears_staleness_and_keeps_old_run(self):
        app = self.start()
        old = self.validate()
        path = self.clean / "samples.csv"
        path.write_text(path.read_text().replace("Liver", "Brain"))
        app.button(key="bqc_check_snapshot").click().run()
        new = self.validate()
        self.assertEqual(new["status"], "READY")
        self.assertEqual(app.session_state["bqc_freshness"]["state"], "MATCHED")
        self.assertFalse(app.button(key="bqc_build_package").disabled)
        self.assertNotEqual(old["run_id"], new["run_id"])
        self.assertEqual(len(list_runs(self.db)), 2)

    def test_history_opens_run_and_switches_dataset_context(self):
        app = self.start()
        clean = self.validate()
        app.radio(key="bqc_source").set_value("Broken demo").run()
        broken = self.validate()
        app.checkbox(key="bqc_all_history").set_value(True).run()
        app.selectbox(key="bqc_history_selection").set_value(clean["run_id"]).run()
        app.button(key="bqc_open_history").click().run()
        self.assert_no_errors()
        self.assertEqual(app.radio(key="bqc_source").value, "Clean demo")
        self.assertEqual(app.session_state["bqc_evidence"]["run_id"], clean["run_id"])
        self.assertEqual(app.session_state["bqc_origin"], "history")
        self.assertEqual(app.session_state["bqc_freshness"]["state"], "UNCHECKED")
        self.assertEqual(len(list_runs(self.db)), 2)
        self.assertEqual(get_run(self.db, broken["run_id"])["status"], "BLOCKED")
        self.assertEqual([w.value for w in app.warning], [])

    def test_history_can_reopen_evidence_after_source_deletion(self):
        app = self.start()
        evidence = self.validate()
        shutil.rmtree(self.clean)
        app.button(key="bqc_open_history").click().run()
        self.assert_no_errors()
        self.assertEqual(app.session_state["bqc_evidence"]["run_id"], evidence["run_id"])
        self.assertFalse(app.button(key="bqc_save_reports").disabled)
        app.button(key="bqc_check_snapshot").click().run()
        self.assertEqual(app.session_state["bqc_freshness"]["state"], "REVALIDATE")

    def test_storage_failure_is_explicit_and_packaging_stays_disabled(self):
        self.db.parent.mkdir(parents=True)
        self.db.write_text("unrelated file")
        app = self.start()
        evidence = self.validate()
        self.assertEqual(evidence["status"], "READY")
        self.assertFalse(app.session_state["bqc_saved"])
        self.assertTrue(app.button(key="bqc_build_package").disabled)
        self.assertTrue(app.button(key="bqc_save_reports").disabled)
        self.assertIn("SQL history save failed", " ".join(e.value for e in app.error))
        self.assertIn("not stored", " ".join(e.value for e in app.success))
        self.assertEqual(self.db.read_text(), "unrelated file")

    def test_unexpected_validation_exception_clears_previous_ready_result(self):
        app = self.start()
        self.validate()
        with patch("biodataqc.ui_service.validate_dataset", side_effect=RuntimeError("simulated failure")):
            app.button(key="bqc_run").click().run()
        self.assert_no_errors()
        self.assertNotIn("bqc_evidence", app.session_state)
        self.assertIn("Validation could not complete", " ".join(e.value for e in app.error))
        self.assertEqual(len(list_runs(self.db)), 1)

    def test_snapshot_exception_does_not_leave_current_match_claim(self):
        app = self.start()
        self.validate()
        with patch("biodataqc.ui_service.verify_current_inputs", side_effect=RuntimeError("simulated snapshot failure")):
            app.button(key="bqc_check_snapshot").click().run()
        self.assert_no_errors()
        self.assertEqual(app.session_state["bqc_freshness"]["state"], "UNAVAILABLE")
        self.assertTrue(app.button(key="bqc_build_package").disabled)

    def test_custom_folder_and_failed_inventory_can_still_be_validated(self):
        app = self.start()
        app.radio(key="bqc_source").set_value("Custom folder").run()
        app.text_input(key="bqc_custom_path").set_value("data/missing").run()
        app.button(key="bqc_load").click().run()
        self.assert_no_errors()
        self.assertIn("Inventory loading failed", " ".join(e.value for e in app.error))
        evidence = self.validate()
        self.assertEqual(evidence["status"], "BLOCKED")
        self.assertTrue(app.session_state["bqc_saved"])
        self.assertTrue(app.button(key="bqc_build_package").disabled)

    def test_database_inside_dataset_is_refused_without_creating_it(self):
        app = self.start()
        path = self.clean / "history.sqlite"
        app.text_input(key="bqc_database").set_value(str(path)).run()
        app.button(key="bqc_run").click().run()
        self.assert_no_errors()
        self.assertFalse(path.exists())
        self.assertNotIn("bqc_evidence", app.session_state)

    def test_invalid_package_name_is_refused(self):
        app = self.start()
        self.validate()
        app.text_input(key="bqc_package_name").set_value("../escape").run()
        self.assert_no_errors()
        self.assertTrue(app.button(key="bqc_build_package").disabled)
        self.assertEqual(list_packages(self.db), [])

    def test_package_verification_shows_file_corruption(self):
        app = self.start()
        self.validate()
        app.button(key="bqc_build_package").click().run()
        record = app.session_state["bqc_package_notice"]
        (Path(record["package_path"]) / "samples.csv").write_text("corrupted")
        app.button(key="bqc_verify_package").click().run()
        self.assert_no_errors()
        self.assertFalse(app.session_state["bqc_package_verification"]["ok"])
        self.assertIn("does not match", " ".join(e.value for e in app.error))


class InterfaceServiceTests(UIFixture):
    def test_relative_paths_resolve_from_project_root(self):
        self.assertEqual(service.resolve_path("data/clean", self.root), self.clean)
        with self.assertRaises(ValueError):
            service.resolve_path("", self.root)

    def test_user_facing_timestamps_use_paris_timezone(self):
        self.assertIn("13:00:00 CET", service.display_time("2026-11-11T12:00:00+00:00"))
        self.assertIn("14:00:00 CEST", service.display_time("2026-07-11T12:00:00+00:00"))

    def test_download_payload_matches_same_saved_run_and_escapes_owner(self):
        report = validate_dataset(self.clean)
        save_run(report, self.db)
        downloads = service.report_downloads(report.to_dict(), owner="<b>Owner</b>")
        self.assertEqual(json.loads(downloads["json"]), report.to_dict())
        self.assertIn("&lt;b&gt;Owner&lt;/b&gt;", downloads["html"])
        self.assertIn(report.run_id, downloads["html"])

    def test_changed_in_memory_report_cannot_be_packaged(self):
        report = validate_dataset(self.clean)
        save_run(report, self.db)
        evidence = report.to_dict()
        evidence["requirements"]["client_label"] = "Changed in memory"
        with self.assertRaisesRegex(ValueError, "does not match"):
            service.create_package(evidence, self.db, self.root, "delivery")
        self.assertEqual(list_packages(self.db), [])

    def test_saved_reports_retain_full_json_evidence(self):
        report = validate_dataset(self.clean)
        save_run(report, self.db)
        paths = service.save_report_files(report.to_dict(), self.db, self.root)
        self.assertEqual(json.loads(Path(paths["json"]).read_text()), report.to_dict())
        self.assertIn("Not assigned", Path(paths["html"]).read_text())


if __name__ == "__main__":
    unittest.main()

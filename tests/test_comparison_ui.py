"""Comparison controls run against the real Streamlit interface."""

import json
import shutil
from unittest.mock import patch

from biodataqc.history import get_run, list_runs, save_run
from biodataqc.validation import validate_dataset
from test_ui import UIFixture


class ComparisonInterfaceTests(UIFixture):
    def stored_pair(self):
        path = self.clean / "sequencing/S001_R2.fastq.gz"
        original = path.read_bytes()
        path.unlink()
        before = validate_dataset(self.clean)
        save_run(before, self.db)
        path.write_bytes(original)
        after = validate_dataset(self.clean)
        save_run(after, self.db)
        return before.to_dict(), after.to_dict()

    def compare(self):
        self.app.button(key="bqc_compare_runs").click().run()
        self.assert_no_errors()
        return self.app.session_state["bqc_comparison"]

    def test_real_correction_flow_displays_resolved_without_creating_runs(self):
        before, after = self.stored_pair()
        original = self.db.read_bytes()
        app = self.start()
        self.assertEqual(app.selectbox(key="bqc_compare_newer").value, after["run_id"])
        self.assertEqual(app.selectbox(key="bqc_compare_baseline").value, before["run_id"])
        result = self.compare()
        self.assertEqual(result["counts"]["RESOLVED"], 1)
        self.assertIn(("Resolved checks", "1"), [(m.label, m.value) for m in app.metric])
        self.assertIn("Saved readiness: BLOCKED → READY", [s.value for s in app.subheader])
        self.assertEqual(len(app.get("download_button")), 1)
        app.checkbox(key="bqc_compare_all_checks").set_value(True).run()
        app.checkbox(key="bqc_compare_all_files").set_value(True).run()
        self.assert_no_errors()
        self.assertEqual(len(list_runs(self.db)), 2)
        self.assertEqual(self.db.read_bytes(), original)
        self.assertNotIn("bqc_evidence", app.session_state)

    def test_single_run_prompts_for_another_validation(self):
        save_run(validate_dataset(self.clean), self.db)
        app = self.start()
        self.assertNotIn("bqc_compare_runs", [b.key for b in app.button])
        self.assertIn("Save at least two runs", " ".join(m.value for m in app.info))

    def test_changing_selected_runs_hides_previous_comparison(self):
        before, after = self.stored_pair()
        third = validate_dataset(self.clean)
        save_run(third, self.db)
        app = self.start()
        self.compare()
        app.selectbox(key="bqc_compare_baseline").set_value(before["run_id"]).run()
        self.assert_no_errors()
        self.assertNotIn("bqc_comparison", app.session_state)
        result = self.compare()
        self.assertEqual(result["before"]["run_id"], before["run_id"])
        app.selectbox(key="bqc_compare_newer").set_value(before["run_id"]).run()
        self.assert_no_errors()
        self.assertNotIn("bqc_comparison", app.session_state)

    def test_changing_dataset_or_database_clears_comparison(self):
        self.stored_pair()
        app = self.start()
        self.compare()
        app.radio(key="bqc_source").set_value("Broken demo").run()
        self.assert_no_errors()
        self.assertNotIn("bqc_comparison", app.session_state)
        app.radio(key="bqc_source").set_value("Clean demo").run()
        self.compare()
        app.text_input(key="bqc_database").set_value(str(self.root / "outputs/other.sqlite")).run()
        self.assert_no_errors()
        self.assertNotIn("bqc_comparison", app.session_state)

    def test_requirements_change_shows_limit_and_never_resolves(self):
        before, _ = self.stored_pair()
        config_path = self.clean / "requirements.json"
        config = json.loads(config_path.read_text())
        config["client_label"] = "Updated label"
        config_path.write_text(json.dumps(config))
        save_run(validate_dataset(self.clean), self.db)
        app = self.start()
        app.selectbox(key="bqc_compare_baseline").set_value(before["run_id"]).run()
        result = self.compare()
        self.assertFalse(result["comparable"])
        self.assertEqual(result["counts"]["RESOLVED"], 0)
        self.assertIn("Comparison is limited", " ".join(w.value for w in app.warning))

    def test_other_folders_require_explicit_inclusion(self):
        old = validate_dataset(self.broken)
        save_run(old, self.db)
        new = validate_dataset(self.clean)
        save_run(new, self.db)
        app = self.start()
        app.checkbox(key="bqc_all_history").set_value(True).run()
        self.assertNotIn("bqc_compare_runs", [b.key for b in app.button])
        app.checkbox(key="bqc_compare_other_folders").set_value(True).run()
        self.assert_no_errors()
        result = self.compare()
        self.assertEqual(result["counts"]["RESOLVED"], 2)
        self.assertTrue(any("Different dataset folders" in note for note in result["notes"]))

    def test_other_project_is_not_offered_as_a_baseline(self):
        before, after = self.stored_pair()
        config_path = self.broken / "requirements.json"
        config = json.loads(config_path.read_text())
        config["project_id"] = "ANOTHER_PROJECT"
        config_path.write_text(json.dumps(config))
        different = validate_dataset(self.broken)
        save_run(different, self.db)
        app = self.start()
        app.checkbox(key="bqc_all_history").set_value(True).run()
        app.checkbox(key="bqc_compare_other_folders").set_value(True).run()
        app.selectbox(key="bqc_compare_newer").set_value(after["run_id"]).run()
        self.assert_no_errors()
        self.assertEqual(app.selectbox(key="bqc_compare_baseline").value, before["run_id"])
        self.assertEqual(len(app.selectbox(key="bqc_compare_baseline").options), 1)

    def test_historical_comparison_survives_source_deletion(self):
        self.stored_pair()
        shutil.rmtree(self.clean)
        self.start()
        self.assertTrue(self.compare()["comparable"])

    def test_comparison_failure_does_not_retain_previous_result(self):
        self.stored_pair()
        app = self.start()
        self.compare()
        with patch("biodataqc.ui.get_run", side_effect=ValueError("Damaged saved evidence")):
            app.button(key="bqc_compare_runs").click().run()
        self.assert_no_errors()
        self.assertNotIn("bqc_comparison", app.session_state)
        self.assertIn("Comparison could not be completed", " ".join(e.value for e in app.error))

    def test_comparison_keeps_the_open_delivery_and_packaging_context(self):
        before, after = self.stored_pair()
        app = self.start()
        app.button(key="bqc_open_history").click().run()
        self.assert_no_errors()
        self.assertEqual(app.session_state["bqc_evidence"]["run_id"], after["run_id"])
        self.compare()
        self.assertEqual(app.session_state["bqc_evidence"]["run_id"], after["run_id"])
        self.assertFalse(app.button(key="bqc_build_package").disabled)
        self.assertEqual(get_run(self.db, before["run_id"])["status"], "BLOCKED")

"""FastQC integration exercised through Streamlit AppTest."""

import os
from pathlib import Path
from unittest.mock import patch

from biodataqc import fastqc
from biodataqc.history import get_run, list_runs
from test_fastqc import make_stub
from test_ui import UIFixture


class FastQCInterfaceTests(UIFixture):
    def setUp(self):
        super().setUp()
        self.exe = make_stub(self.root)

    def ready(self):
        app = self.start()
        evidence = self.validate()
        app.text_input(key="bqc_fastqc_executable").set_value(str(self.exe)).run()
        self.assert_no_errors()
        return app, evidence

    def run_fastqc(self):
        self.app.button(key="bqc_fastqc_run").click().run(timeout=30)
        self.assert_no_errors()

    def test_startup_and_reruns_do_not_execute_external_tools(self):
        with patch("biodataqc.fastqc._execute") as execute:
            app, _ = self.ready()
            app.run()
            execute.assert_not_called()
        self.assertIn("Sequencing quality", [tab.label for tab in app.tabs])
        self.assertEqual(len(list_runs(self.db)), 1)

    def test_installation_check_does_not_analyze_or_create_history(self):
        app = self.start()
        app.text_input(key="bqc_fastqc_executable").set_value(str(self.exe)).run()
        app.button(key="bqc_fastqc_check").click().run()
        self.assert_no_errors()
        self.assertIn("FastQC 0.12.1 is available", " ".join(s.value for s in app.success))
        self.assertFalse(self.db.exists())
        self.assertFalse((self.root / "outputs/fastqc").exists())

    def test_success_shows_flags_charts_and_downloads_but_retains_ready(self):
        app, evidence = self.ready()
        self.run_fastqc()
        self.assertIn(("FASTQ files analyzed", "4"), [(m.label, m.value) for m in app.metric])
        self.assertIn(("FastQC WARN modules", "4"), [(m.label, m.value) for m in app.metric])
        self.assertIn(("FastQC FAIL modules", "4"), [(m.label, m.value) for m in app.metric])
        self.assertEqual(get_run(self.db, evidence["run_id"])["status"], "READY")
        self.assertFalse(app.button(key="bqc_build_package").disabled)
        self.assertEqual(len(app.get("vega_lite_chart")), 3)
        downloads = [button.label for button in app.get("download_button")]
        self.assertIn("Download FastQC HTML", downloads)
        self.assertIn("Download FastQC ZIP", downloads)
        self.assertIn("Download FastQC analysis JSON", downloads)
        app.selectbox(key="bqc_fastqc_file").set_value("sequencing/S002_R2.fastq.gz").run()
        self.assert_no_errors()
        self.assertEqual(len(fastqc.list_analyses(self.root / "outputs/fastqc", evidence)[0]), 1)
        self.assertEqual(len(list_runs(self.db)), 1)

    def test_blocked_run_disables_fastqc(self):
        app = self.start()
        app.radio(key="bqc_source").set_value("Broken demo").run()
        self.validate()
        self.assertTrue(app.button(key="bqc_fastqc_run").disabled)
        self.assertFalse((self.root / "outputs/fastqc").exists())

    def test_missing_executable_reports_error_without_creating_analysis(self):
        app, evidence = self.ready()
        app.text_input(key="bqc_fastqc_executable").set_value(str(self.root / "missing")).run()
        self.run_fastqc()
        self.assertIn("FastQC was not found", " ".join(e.value for e in app.error))
        self.assertEqual(fastqc.list_analyses(self.root / "outputs/fastqc", evidence)[0], [])
        self.assertEqual(len(list_runs(self.db)), 1)

    def test_changed_source_is_refused_and_requests_revalidation(self):
        app, evidence = self.ready()
        path = self.clean / "samples.csv"
        path.write_text(path.read_text().replace("Liver", "Brain"))
        self.run_fastqc()
        self.assertIn("Input changed", " ".join(e.value for e in app.error))
        self.assertEqual(app.session_state["bqc_freshness"]["state"], "REVALIDATE")
        self.assertTrue(app.button(key="bqc_fastqc_run").disabled)
        self.assertTrue(app.button(key="bqc_build_package").disabled)

    def test_failed_new_attempt_is_not_presented_as_another_completed_analysis(self):
        app, evidence = self.ready()
        self.run_fastqc()
        previous = app.selectbox(key="bqc_fastqc_selection").value
        with patch.dict(os.environ, {"BIODATAQC_TEST_FASTQC_MODE": "fail"}):
            self.run_fastqc()
        self.assertIn("Last FastQC attempt failed", " ".join(e.value for e in app.error))
        self.assertIn("earlier saved results", " ".join(e.value for e in app.caption))
        self.assertEqual(app.selectbox(key="bqc_fastqc_selection").value, previous)
        self.assertEqual(len(fastqc.list_analyses(self.root / "outputs/fastqc", evidence)[0]), 1)

    def test_new_validation_does_not_reuse_previous_run_analysis(self):
        app, first = self.ready()
        self.run_fastqc()
        second = self.validate()
        self.assertNotEqual(first["run_id"], second["run_id"])
        self.assertNotIn("bqc_fastqc_selection", [s.key for s in app.selectbox])
        self.assertNotIn("bqc_fastqc_notice", app.session_state)
        self.assertIn("No completed FastQC analysis", " ".join(i.value for i in app.info))
        app.selectbox(key="bqc_history_selection").set_value(first["run_id"]).run()
        app.button(key="bqc_open_history").click().run()
        self.assert_no_errors()
        self.assertIn(("FASTQ files analyzed", "4"), [(m.label, m.value) for m in app.metric])

    def test_changing_dataset_clears_fastqc_context(self):
        app, _ = self.ready()
        self.run_fastqc()
        app.radio(key="bqc_source").set_value("Broken demo").run()
        self.assert_no_errors()
        self.assertNotIn("bqc_fastqc_selection", app.session_state)
        self.assertNotIn("bqc_fastqc_notice", app.session_state)

    def test_existing_analyses_are_loaded_after_restart_without_running_tool(self):
        _, evidence = self.ready()
        self.run_fastqc()
        with patch("biodataqc.fastqc._execute") as execute:
            app = self.start()
            app.button(key="bqc_open_history").click().run()
            self.assert_no_errors()
            execute.assert_not_called()
        self.assertIn(("FASTQ files analyzed", "4"), [(m.label, m.value) for m in app.metric])
        self.assertEqual(len(list_runs(self.db)), 1)

    def test_corrupted_report_cannot_be_displayed_or_downloaded(self):
        app, evidence = self.ready()
        self.run_fastqc()
        directory = Path(fastqc.list_analyses(self.root / "outputs/fastqc", evidence)[0][0]["directory"])
        result = fastqc.load_analysis(directory, evidence)
        (directory / result["files"][0]["html"]).write_text("corrupted")
        app.run()
        self.assert_no_errors()
        self.assertIn("Saved FastQC reports could not be verified", " ".join(e.value for e in app.error))
        self.assertNotIn("Download FastQC HTML", [b.label for b in app.get("download_button")])

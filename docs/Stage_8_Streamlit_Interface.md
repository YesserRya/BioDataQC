# BioDataQC — Stage 8: Streamlit interface

This update connects your existing input loader, validator, SQLite history,
reporting and verified packaging to a local Streamlit application. The same
backend functions serve the interface and the command-line tools.

## 1. Install the update

Prerequisites: stages 4–7 are installed and working in
`~/Precursion/BioDataQC`; the `biodataqc` Conda environment uses Python 3.12.
Download `BioDataQC_Stage8_Streamlit_Interface.zip` into Downloads.

If Streamlit is already running, stop it with **Ctrl+C** in its terminal. Then:

```bash
conda activate biodataqc
cd ~/Precursion/BioDataQC
cp -n app.py app.before_stage8.py
python -m zipfile -e ~/Downloads/BioDataQC_Stage8_Streamlit_Interface.zip .
python -m pip install -r requirements-ui.txt
```

The backup command retains your original starter app. The ZIP replaces `app.py`
and adds the files below. Your datasets, SQL database and completed packages are
retained. This is an incremental update; it needs the previous stages.

| File | Responsibility |
| --- | --- |
| `app.py` | Streamlit entry point at your project root |
| `biodataqc/ui.py` | Four views, controls and session state |
| `biodataqc/ui_service.py` | Adapters to the tested backend functions |
| `.streamlit/config.toml` | Light theme, localhost binding and telemetry setting |
| `requirements-ui.txt` | Tested Streamlit version |
| `tests/test_ui.py` | 27 interface and adapter behavior tests |
| `docs/Stage_8_Streamlit_Interface.md` | This installation and demonstration guide |

Streamlit is pinned to version 1.67.0, the version used for this update's tests.
Install it inside your active Conda environment using `python -m pip`, as above.

## 2. Run all tests

```bash
python -m unittest discover -s tests -p 'test_*.py' -v
```

Expected result with stages 4–8 installed:

```text
Ran 184 tests in ...s

OK
```

The suite includes the 157 existing tests and 27 new tests. The new tests use
Streamlit AppTest to operate the actual controls: validation, context switching,
reports, package creation and verification, saved history, storage failures and
source changes. They use temporary datasets and databases. An occasional
`missing ScriptRunContext` warning from AppTest's bare test mode is not a failed
test; look for the final `OK`.

## 3. Start the application

Run this from the project root:

```bash
python -m streamlit run app.py
```

Open **http://localhost:8501** in your browser if it does not open automatically.
Keep this terminal running while using the app. Stop the app with **Ctrl+C**.

The sidebar selects **Clean demo**, **Broken demo**, or a **Custom folder**.
Custom folder paths may be absolute or relative to the folder containing
`app.py`. The default history database is `outputs/biodataqc.sqlite`, so your
earlier CLI runs are available. Change it in **Workspace settings** if you used
a different database path. Keep the database outside the input dataset.

The theme and server settings are in `.streamlit/config.toml`; the app binds to
`127.0.0.1` for this local demonstration. Displayed timestamps use Europe/Paris.
The original timestamps remain unchanged in SQL and exported JSON.

## 4. Understand the four views

| View | What you can do |
| --- | --- |
| Delivery summary | Read the selected run's decision, key counts, practical impact and next actions; compare current inputs with a saved READY snapshot |
| Technical results | Filter check outcomes and inspect dependencies, file locations, record numbers, inventory, literal metadata and SHA-256 fingerprints |
| Reports & packages | Set report owner/next update, save or download reports, build an eligible local package and verify completed packages |
| History | Browse saved runs and reopen their exact evidence; optionally include every dataset in the selected database |

On first launch there is no active validation result. A saved run is displayed
only after you explicitly validate or open one from History.

## 5. Demonstrate the clean dataset

1. Select **Clean demo** in the sidebar.
2. Click **Load inventory**. Expect 6 samples and 12/12 expected files. The
   preview is marked **NOT_VALIDATED** and creates no SQL run.
3. Click **Run validation**. Inventory loading is optional; validation loads and
   checks the complete dataset itself.
4. In **Delivery summary**, expect **Saved run: READY**, 0 blockers, 0 warnings,
   12 structurally valid FASTQ files and 12,000 records scanned.
5. In **Technical results**, add **PASS** to the outcome filter to show passed
   checks. The optional independent-reference check can remain NOT_RUN under
   this specification; it does not prevent READY.

The result records one new run in SQLite. Each explicit click on **Run
validation** intentionally creates a new run ID. Editing report fields,
filtering outcomes and Streamlit reruns do not create additional validation
runs. Changing the selected dataset or database clears the active result.

## 6. Create the reports and package

In **Reports & packages**:

1. Optionally enter **Yesser Rizi** as the responsible person and a next update
   such as **After delivery review**. Blank fields render as **Not assigned**
   and **To be confirmed**. These fields are supplied by you; they do not modify
   the recorded readiness decision.
2. Click **Save report files** to write an HTML report and the full validation
   JSON under `outputs/reports/`. The interface displays their exact paths.
   Saving again for the same run updates those exports with the current report
   fields while preserving the SQL evidence.
3. Use **Download HTML report** or **Download validation JSON** to download
   through your browser. Open the HTML report in your browser; it works offline.
4. Leave the generated package folder name or supply a new name using letters,
   digits, underscores and hyphens.
5. Click **Build verified package**. The backend compares current inputs with
   the saved run, stages copies, verifies them, and records completion in SQL.
6. Expect **17 files, including the manifest** for the unchanged clean fixture.
   The destination is inside `outputs/packages/`, and its exact path is shown.
7. Click **Verify selected package**. Expect **Package verification passed**
   with a check timestamp.

The package contains 12 FASTQ files, `samples.csv`, `requirements.json`,
`report.html`, `run_summary.json` and `manifest.csv`. The manifest covers the
other 16 files; SQL stores the manifest's digest.

Existing package folders are preserved. After a successful build, the build
button stays disabled for that destination; choose a different folder name if
you intentionally want another package. The package is a local directory.

## 7. Demonstrate the broken dataset

1. Select **Broken demo**. The clean run disappears from the active views.
2. Click **Run validation**.
3. Expect **Saved run: BLOCKED**, 2 blocking findings, 3 required checks not run,
   10 valid FASTQ files and 11,000 scanned records.
4. Inspect the exact findings in **Technical results**:
   - `FILE001`: missing `sequencing/S003_R2.fastq.gz`.
   - `FASTQ001`: sequence length 12 versus quality length 8 in
     `sequencing/S004_R1.fastq.gz`, record 1.
5. The skipped FASTQ/pair checks appear separately with their dependencies.
6. In **Reports & packages**, save or download a report explaining the problem.
   **Build verified package** is disabled for this BLOCKED run.

The backend enforces the packaging gate independently of the button state.
INCOMPLETE runs and unsaved results are also ineligible for packaging.

## 8. Reopen history and understand snapshot freshness

1. Open **History**.
2. Enable **Show all datasets** to include both clean and broken runs from the
   selected database, including earlier command-line runs.
3. Select a saved run and click **Open selected run**.
4. The sidebar switches to that run's dataset. All views show that exact run,
   with its UUID and completion time. Opening it creates no new validation run.

Historical evidence remains available even if the original source directory is
unavailable. A saved READY decision describes the input snapshot at validation
time. On reopening history, the interface says that current files have not yet
been compared in this session.

For a READY run, **Check current inputs** compares the current inventory and
hashes with the selected run. Changed or unreadable inputs produce a
revalidation message and disable package creation. The old SQL decision remains
historical evidence. Run validation again after fixing or changing inputs.

This comparison is optional before packaging because every build performs the
same backend gate itself. A source modification between validation and a build
is rejected even if you did not click the comparison button. Displayed snapshot
and package verification results always refer to their displayed check times;
the application does not continuously monitor files.

If SQL saving fails, the interface identifies the result as **not stored**. Its
JSON is available for recovery, while report saving, HTML download and package
creation remain disabled. Resolve the database problem and validate again.

## 9. What was verified for this update

- All 184 automated tests passed with Python 3.12 and Streamlit 1.67.0.
- A separate AppTest workflow used the original six-sample demo fixtures:
  clean READY with 12,000 records, broken BLOCKED with the expected 2 findings,
  both report exports, one 17-file verified package, and history reopening in a
  new session.
- That workflow retained exactly two validation runs and one completed package;
  report generation, widget changes and history browsing added no extra runs.
- All pre-existing backend modules and CLI scripts are unchanged by this update.

AppTest verifies application behavior without a browser. Browser appearance and
the browser download interaction have not been visually checked in the
development environment; review those once on your laptop after launching.

The application remains a local synthetic-data prototype. READY covers the
configured structural checks and the observed input snapshot. It does not
establish biological quality or independent pre-receipt provenance. Packaging
uses stable local files and retains the stage-7 filesystem/SQL crash-consistency
limitations documented in that guide.

Stage 8 completes the planned core implementation. The next pass is the final
integration review and rehearsal of the clean → broken → history interview demo.

## References

- [Streamlit AppTest](https://docs.streamlit.io/develop/api-reference/app-testing/st.testing.v1.apptest)
- [Streamlit session state](https://docs.streamlit.io/develop/api-reference/caching-and-state/st.session_state)
- [Streamlit download button](https://docs.streamlit.io/develop/api-reference/widgets/st.download_button)

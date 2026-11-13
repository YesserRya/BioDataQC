# BioDataQC — compare validation runs

This update adds **History → Compare validation runs**. It compares two saved
observations, shows check and file changes, explains comparison limits and
downloads the result as JSON. It uses your existing SQLite history. There is
no database migration and no new Python dependency.

## 1. Install

Prerequisite: your working stage-8 interface and real-data extension are
installed in `~/Precursion/BioDataQC`, in the `biodataqc` Conda environment.
Download `BioDataQC_Run_Comparison.zip` to Downloads. Stop Streamlit with
**Ctrl+C** in its terminal, then run:

```bash
conda activate biodataqc
cd ~/Precursion/BioDataQC
mkdir -p backups/before_comparison
cp -rn biodataqc scripts tests docs backups/before_comparison/
python -m zipfile -e ~/Downloads/BioDataQC_Run_Comparison.zip .
```

The ZIP updates `biodataqc/ui.py` and adds the comparison module, demo command,
tests and this guide. It contains no datasets, database, credentials or existing
delivery outputs. The backup command retains an earlier backup at the same path.
Your root-level `app.py` continues to be the entry point.

## 2. Test

```bash
python -m unittest discover -s tests -p 'test_*.py' -v
```

Expected with all previous updates: **256 tests, OK** (221 existing tests and
35 new tests). The tests use temporary small datasets and databases and do not
download anything. Streamlit AppTest may emit a `missing ScriptRunContext`
warning in bare mode; check the final unittest result.

## 3. Create a correction demonstration using your real yeast subset

Your existing `data/public_yeast` must still validate READY. Run:

```bash
python scripts/demo_comparison.py
```

The command:

1. Checks the source, then creates a separate `data/comparison_demo` delivery.
2. Gives the copy its own project ID ending in `_COMPARISON_DEMO` and a clear
   demonstration label. The source data remains unchanged.
3. Temporarily removes the copy's R2 file and saves a **BLOCKED** run to
   `outputs/biodataqc.sqlite`.
4. Restores the exact copied R2 bytes and saves a **READY** run of the same folder.

The requirements are identical between these two runs. The real-data copy's
provenance records the deliberate correction demonstration. The command uses
the already downloaded data and needs no network connection.

Expected comparison:

| Result | Expected |
| --- | --- |
| Baseline → newer status | BLOCKED → READY |
| Resolved checks | 1: the missing R2 file check |
| New failures | 0 |
| Still failing | 0 |
| Unassessed required checks | 0 |
| Previously unassessed checks now passing | 3: R2 structure, pair consistency, source references |
| File changes | R2 added; other recorded input bytes unchanged |

The source precheck is not saved to SQL. The command adds exactly two saved
runs on success. It refuses an existing destination folder. For another demo,
use a new name rather than deleting existing work:

```bash
python scripts/demo_comparison.py --dataset data/comparison_demo_2
```

You may alternatively use the clean synthetic dataset:

```bash
python scripts/demo_comparison.py --source data/clean --dataset data/comparison_synthetic
```

The synthetic demo has two previously unassessed checks now passing, because
its specification does not include the public-data source-reference check.
Custom sources must use the same supported BioDataQC input structure. Paths
passed to this command are relative to the current terminal directory.

If preparation fails, inspect the reported error. An incomplete demo folder
or a saved baseline can remain; existing historical runs are never deleted.
Ordinary baseline-validation or SQL-save errors restore the removed mate before
returning an error. This is a local demonstration, not a crash-recovery service.

## 4. Open the comparison

```bash
python -m streamlit run app.py
```

At `http://localhost:8501`:

1. In the sidebar, select **Custom folder**.
2. Enter **`data/comparison_demo`** (or the alternative folder you chose).
3. Open the **History** tab. The demo's two saved runs are already there;
   you do not need to validate again.
4. Scroll to **Compare validation runs** below **Open selected run**.
5. Select the **READY** observation as **Newer run**, and the earlier
   **BLOCKED** observation as **Baseline run**. They should be selected by default.
6. Click **Compare selected runs**.

The result appears in the same tab. Expand **Inspect check messages and evidence**
to read both original observations, including failure messages, record numbers,
severity and dependencies. **Full file fingerprints** shows the saved hashes.
**Download comparison JSON** exports both run identities and the derived comparison.

The other tabs continue to describe the run opened with **Open selected run**;
comparison does not switch that active run or change packaging eligibility.
Comparing, filtering and downloading never add a validation run to history.

## 5. Interpret the labels

| Label | Meaning |
| --- | --- |
| Resolved | A previous FAIL or ERROR now has a corresponding PASS under comparable rules and requirements. |
| New failure | A check previously passed and now fails or errors. |
| Still failing | The same check still fails or errors. Its underlying reason can have changed; inspect both observations. |
| Unassessed | A required check did not run, an earlier failure was not rechecked, or the newer result is missing. It is not considered resolved. |
| Newly assessed failure | A failure is now observed, but the earlier check was missing or not run. It is not proven newly introduced. |
| Now passes | A previously unrun check now passes. It is separate from resolved failures. |
| Added check | A new passing check has no earlier observation. |
| Still passing | The check passed in both runs. Hidden initially to keep the view focused. |
| Optional check not run | An optional check remains unexecuted. It is hidden initially. |
| Not comparable | A change of rules, requirements, target or supporting evidence prevents a safe resolution inference. |

Counts describe **checks**, not individual defects. A metadata check can cover
many cells and FASTQ001 can cover a whole file. Resolving a WARNING also counts
as a resolved check; severity remains visible. Dependency checks becoming PASS
are displayed separately from the original blocking finding.

## 6. Comparability and historical evidence

- Both runs need the same nonempty recorded project ID and must be different
  runs. Select the baseline by validation time, not SQL insertion time.
- By default, baseline choices use the same folder. To compare copies, enable
  **Show all datasets**, then **Include other folders in this project**. The UI
  explicitly identifies different folders; a matching project ID does not
  establish that one delivery is a correction of the other.
- Resolution labels require identical parsed requirements and matching supported
  rules versions, passing configuration checks, stable recorded snapshots, and
  sequential run intervals. A changed required flag or target also prevents
  resolution for that check.
- This implementation conservatively treats all parsed requirements changes,
  including client labels or provenance, as a comparability limit. The changed
  fields are displayed. Whitespace-only JSON edits preserve the parsed
  requirements but still appear as file-byte changes.
- Changing the expected samples or loosening validation bounds therefore cannot
  make old failures silently appear fixed.
- A missing result is not a PASS. In particular, an unexpected-file check that
  disappears after removal remains unassessed in this check-ID-based comparison;
  its disappearance alone is not counted as a resolved check. The file table
  and the newer run's inventory checks provide the additional evidence.
- Comparison uses saved JSON only. It still works after source deletion and
  does not rescan current files. Saved readiness belongs to each original run;
  the existing packaging checks still determine whether current files can be staged.
- Both legacy rules 1.0.0 and real-data rules 1.1.0 are supported with saved report
  schema 1.1.0. Mixed rule versions are shown with comparison limits.
- The history view displays at most the latest 200 matching saved runs, as before.

The existing **Public yeast** and **Public yeast · missing mate** presets have
different project IDs by design. Use the new demo command for a same-project,
same-folder correction sequence rather than comparing those two presets.

## 7. File evidence

Matching SHA-256 values mean matching recorded bytes. A changed compressed FASTQ
hash can reflect changed gzip bytes without changed biological reads. No
biological interpretation is inferred.

File rows distinguish content changes, additions, removals, type/size changes,
continued absence and unverified content. Equal sizes without both hashes do
not establish unchanged content. Missing inventory after an input-loading
failure does not establish file removal. Expectedness and inventory changes
are retained even when file bytes are unchanged.

## 8. Interview walkthrough

“I received a delivery with a missing mate file. That blocked preparation and
left dependent checks unassessed. After the mate was restored, I validated the
same delivery again. The comparison confirms that the original file check now
passes and the dependent checks have completed. Both observations remain in
SQL, with their recorded fingerprints. I can now open the READY run and build
a verified package against its exact input snapshot.”

## 9. Scope of this update

Added: `biodataqc/comparison.py`, `scripts/demo_comparison.py`, three comparison
test files and this guide. Updated: `biodataqc/ui.py`.

The validation engine, report schema, SQLite schema and packaging rules are
unchanged. The comparison JSON is a separate derived artifact; it is not
automatically added to an existing delivery package.

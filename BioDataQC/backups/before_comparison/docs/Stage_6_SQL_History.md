# BioDataQC — Stage 6: SQL validation history

This update adds persistent SQLite history to the stage-5 validator. Each stored
run has its own ID, timestamp, decision, requirements, metadata values, file
inventory, fingerprints and check results. Rerunning validation creates a new
run; previous evidence remains available.

## 1. Install the update

Prerequisite: stage 5 is installed and working in your `biodataqc` Conda
environment. Download `BioDataQC_Stage6_SQL_History.zip` to Downloads, then run:

```bash
conda activate biodataqc
cd ~/Precursion/BioDataQC
python -m zipfile -e ~/Downloads/BioDataQC_Stage6_SQL_History.zip .
```

SQLite uses Python's built-in `sqlite3` module. No server, database account or
additional Python package is needed for this stage.

The update adds `biodataqc/history.py`, `biodataqc/report_io.py`,
`scripts/history.py`, `tests/test_history.py`, this guide and an SQL query example
file. It updates the result model, validator and validation command to capture
sample metadata and support SQL persistence. The rest of stage 5 remains a
prerequisite. Existing commands without `--db` continue to validate and export
JSON without writing SQL history.

## 2. Run the tests

```bash
python -m unittest discover -s tests -p 'test_*.py' -v
```

Expected with stages 4–6 installed:

```text
Ran 109 tests in ...s

OK
```

That is the previous 76 tests plus 33 history tests. The new checks cover full
report round trips, preserved literal metadata, corrected datasets retaining
their earlier failures, exact retry behavior, transactional rollback, foreign
keys, filters, invalid databases, output-path conflicts and command integration.
Tests use temporary files and do not populate your actual history database.

## 3. Save the clean validation run

```bash
python scripts/validate_dataset.py data/clean --db outputs/biodataqc.sqlite --json outputs/clean_validation.json
```

Expect `Status: READY`, zero blockers and a new `Run ID`. You should also see:

```text
SQL history saved: /home/yesser/Precursion/BioDataQC/outputs/biodataqc.sqlite
```

The first successful save automatically creates the database and schema.
The database and JSON export contain the same run ID and evidence.

## 4. Save the broken validation run

Run this as a separate command:

```bash
python scripts/validate_dataset.py data/broken --db outputs/biodataqc.sqlite --json outputs/broken_validation.json
```

Expect `Status: BLOCKED`, two blockers, three required checks not run, a different
run ID and `SQL history saved`. The missing S003 R2 file and malformed S004 R1
record remain the two root findings. The blocked run is saved even though the
command intentionally returns exit code 1.

| Exit code | Meaning for the validation command |
| --- | --- |
| 0 | READY; any requested history save and JSON export succeeded |
| 1 | BLOCKED; any requested history save and JSON export succeeded |
| 2 | INCOMPLETE, or a requested output operation failed; read the printed explanation |

INCOMPLETE runs are also saved when persistence succeeds. If SQL saving fails,
the command reports that failure and still attempts a separately requested JSON
export. A successfully saved database run is retained if JSON export subsequently
fails. These two destinations do not form one cross-file transaction.

## 5. List the saved runs

```bash
python scripts/history.py list
```

The default database is `outputs/biodataqc.sqlite`, relative to the current
working directory. The list shows newest stored first. Starting from an empty
database and running each validation once, you should see:

| Dataset | Historical status | Blockers | Warnings |
| --- | --- | --- | --- |
| broken | BLOCKED | 2 | 0 |
| clean | READY | 0 | 0 |

Actual run IDs and timestamps are generated at execution time. Additional runs
will appear if you repeat the validation commands.

Useful filters:

```bash
python scripts/history.py list --project DEMO_001
python scripts/history.py list --status BLOCKED
python scripts/history.py list --dataset data/clean --limit 5
```

For a different database path, put `--db` before the subcommand:

```bash
python scripts/history.py --db outputs/another_history.sqlite list
```

## 6. Inspect or recover a run

```bash
python scripts/history.py show latest
python scripts/history.py show latest --details
python scripts/history.py show latest --dataset data/clean
python scripts/history.py show latest --json outputs/latest_saved_run.json
```

To retrieve a particular run, replace `YOUR_RUN_ID` with its full ID from the list:

```bash
python scripts/history.py show YOUR_RUN_ID --details
```

`show latest` uses the newest saved run across all projects and datasets unless
you add `--project` or `--dataset`. These filters apply to `latest`, not to a full
run ID. A history command returns 0 when reading succeeds, even if the recorded
validation status was BLOCKED. Reading an unknown path fails without creating an
empty database.

History is read from saved evidence. It can still be inspected after source files
have moved, changed or been deleted. A historical READY result does not establish
that today's files are ready; package creation in the next stage must compare
current inputs against the saved fingerprints.

## 7. See the SQL structure

| SQL object | What it stores |
| --- | --- |
| `projects` | Project IDs and their first storage time |
| `validation_runs` | One row per run, timestamps, decision, counters, versions and full report JSON |
| `checks` | Every PASS, FAIL, ERROR and NOT_RUN, with location, dependencies and suggested action |
| `file_snapshots` | Inventory entries and input fingerprints, including missing expected files |
| `metadata_rows` | Literal sample metadata per CSV record, including duplicate or invalid IDs |
| `issues` | SQL view selecting FAIL and ERROR rows from `checks` |

All child tables reference `validation_runs.run_id`. Sample ID is not a unique key
in `metadata_rows`, because duplicated IDs must be preserved as evidence rather
than dropped. CSV record numbering starts at 2 after the header and is not a
physical line number when a quoted cell contains newlines. Column order is kept
in the complete report's `metadata_columns` field.

The complete report preserves the applied requirements, literal metadata values
and original input hashes. FASTQ contents are not copied into SQLite. Missing or
unscanned files can have a null fingerprint. Configuration and CSV fingerprints
do not have inventory byte sizes, so those sizes are null rather than reread from
possibly changed files.

Python 3.12 includes a SQLite command interface. For example:

```bash
python -m sqlite3 outputs/biodataqc.sqlite "SELECT status, COUNT(*) AS runs FROM validation_runs GROUP BY status;"
```

More read-only query examples are in `examples/history_queries.sql`.

## 8. Reuse the history API

```python
from biodataqc.validation import validate_dataset
from biodataqc.history import save_run, list_runs, get_run

report = validate_dataset("data/clean")
inserted = save_run(report, "outputs/biodataqc.sqlite")

runs = list_runs("outputs/biodataqc.sqlite", project_id="DEMO_001")
saved_evidence = get_run("outputs/biodataqc.sqlite", report.run_id)
```

Each new validation generates a new run ID. Saving the same unchanged report
object twice is an exact retry: the second save returns `False` without a duplicate
row. Reusing an existing ID with different evidence raises `HistoryError` and
retains the original. A database transaction commits the run and all its child
records together; a failure rolls back those inserts.

SQL values are bound parameters. Foreign-key enforcement is enabled on every
application connection. The database carries an application ID and schema
version; unrelated or incompatible database files are refused. The API provides
no run-update or run-delete operation. This is application-level history retention,
not a tamper-proof audit system against someone editing the SQLite file directly.

The report format is now `1.1.0` because it includes metadata snapshots. Validation
rule version remains `1.0.0`. The SQLite schema starts at version 1. Completed
package references will be added with the packaging stage.

## Acceptance check

Stage 6 is working when the tests pass, clean and broken runs are both stored with
different IDs, `history.py list` shows both, and `show latest --details` retrieves
the saved checks. Repeating clean validation should add another READY run while
retaining the blocked run.

For the interview: "Every decision is tied to a specific run, requirement set and
input snapshot. When a delivery is corrected, the new result is added to history
and the earlier evidence remains traceable."

## References

- [Python 3.12 sqlite3 documentation](https://docs.python.org/3.12/library/sqlite3.html)
- [SQLite foreign-key documentation](https://www.sqlite.org/foreignkeys.html)

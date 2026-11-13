# BioDataQC — Stage 7: reporting and verified local packaging

This stage produces an offline HTML report for any saved validation run and a
verified delivery directory for an eligible READY run. Every completed package
is linked to its validation run in SQLite, including the manifest's SHA-256.

## 1. Install the update

Prerequisite: stage 6 is installed, and you have clean and broken validation runs
saved in `outputs/biodataqc.sqlite`.

Download `BioDataQC_Stage7_Reporting_Packaging.zip` into Downloads, then run:

```bash
conda activate biodataqc
cd ~/Precursion/BioDataQC
python -m zipfile -e ~/Downloads/BioDataQC_Stage7_Reporting_Packaging.zip .
```

No new Python packages are needed. The update adds reporting and packaging
modules, two commands, tests, query examples and this guide. It updates the SQL
history module and the shared report-output path checks. Your existing runs are
retained. Schema version 1 is upgraded to version 2 automatically on the next
successful history/package write; reading old history does not modify it.

| Main file | Responsibility |
| --- | --- |
| `biodataqc/reporting.py` | Self-contained, escaped HTML from saved evidence |
| `biodataqc/packaging.py` | Readiness gate, snapshot comparison, staged copy and verification |
| `biodataqc/history.py` | Existing history plus migration and completed package references |
| `scripts/report_dataset.py` | Generate a report from an identified saved run |
| `scripts/package_dataset.py` | Build, verify and list packages |
| `tests/test_reporting_packaging.py` | 48 new behavior tests |

## 2. Run the tests

```bash
python -m unittest discover -s tests -p 'test_*.py' -v
```

Expected result with stages 4–7 installed:

```text
Ran 157 tests in ...s

OK
```

The new tests cover HTML escaping, report content, blocked/incomplete runs,
warning-only acceptance, changed requirements/metadata/FASTQ bytes, missing and
unexpected files, copied-file corruption, output preservation, SQL failures,
cleanup, manifest verification and preservation of existing history during schema
migration. They use temporary data and do not change your demo datasets.

## 3. Generate the clean and broken reports

```bash
python scripts/report_dataset.py latest --dataset data/clean --output outputs/clean_report.html
python scripts/report_dataset.py latest --dataset data/broken --output outputs/broken_report.html
```

Open them in your Ubuntu browser:

```bash
xdg-open outputs/clean_report.html
xdg-open outputs/broken_report.html
```

The clean report should show READY. The broken report should show BLOCKED, two
findings and the exact locations already identified by validation. Expand the
technical sections to inspect all check outcomes, inventory, metadata,
fingerprints and applied requirements.

Both views use the same saved run:

- The delivery summary explains practical impact and the next step.
- Technical evidence preserves the rule IDs, locations, messages, skipped-check
  dependencies and input snapshot.

The responsible person/team defaults to `Not assigned`; the next update defaults
to `To be confirmed`. Supply them explicitly when appropriate, for example:

```bash
python scripts/report_dataset.py latest --dataset data/broken --output outputs/broken_report.html --owner "Demo data team" --next-update "To be confirmed"
```

These are operator-supplied reporting fields; the application does not infer an
owner or promise a delivery date. A report can be generated for a blocked or
incomplete run. Successful report generation returns exit code 0 regardless of
the recorded dataset status. It does not revalidate current files.

`examples/reports/clean_report.html` and `broken_report.html` are example outputs
from the development fixtures. Your commands above create reports using your own
run IDs and timestamps. Browser rendering could not be visually inspected in the
development environment; report-generation and HTML-escaping tests passed.

## 4. Build the clean delivery package

```bash
python scripts/package_dataset.py build latest --dataset data/clean --output outputs/packages/clean_delivery
```

Expected output includes:

```text
Package status: COMPLETE
Files (including manifest): 17
```

The command also prints the package ID, run ID, absolute directory and manifest
SHA-256. Choose a new destination name if you have already created this directory;
the command never overwrites an existing package or even an existing empty target
directory. The parent folder is created automatically when needed.

For the unchanged six-sample fixture, the package contains:

| Content | Count | Purpose |
| --- | --- | --- |
| `sequencing/*.fastq.gz` | 12 | The exact validated R1/R2 input files |
| `samples.csv` | 1 | The validated sample sheet |
| `requirements.json` | 1 | The exact requirements file used for the run |
| `report.html` | 1 | Human-readable report |
| `run_summary.json` | 1 | Full saved validation evidence |
| `manifest.csv` | 1 | Inventory and checksums for the other 16 files |

The manifest columns are `relative_path`, `sample_id`, `read`, `size_bytes` and
`sha256`. Non-sequencing entries have blank sample/read fields. The manifest
excludes itself to avoid a circular checksum; its own digest is stored in SQL.
Only declared input files and generated report files are copied. Unrelated files
elsewhere in the source directory are not included.

This is a local directory, not an uploaded delivery or an external transfer.
The original input files are opened for reading and retained unchanged.

## 5. Verify the resulting package

```bash
python scripts/package_dataset.py verify outputs/packages/clean_delivery
```

Expected:

```text
Package verification: VERIFIED
```

Verification checks the manifest against its SQL digest, checks every listed
file's size and SHA-256, rejects missing/extra files or directories and symlinks,
compares packaged input fingerprints with the saved validation snapshot, and
checks that `run_summary.json` matches the saved run. It also checks file-count
and total-byte evidence recorded at completion.

It verifies the recorded package at its recorded path. Current source files need
not still exist for this operation. A moved package requires explicit relocation
support, which is outside this version. Changing a completed package is detected
by verification; its historical SQL completion record is retained rather than
rewritten.

## 6. Confirm that the broken delivery is refused

```bash
python scripts/package_dataset.py build latest --dataset data/broken --output outputs/packages/broken_delivery
```

Expected: a refusal explaining that the saved run is BLOCKED, exit code 1, and
no newly created `broken_delivery` directory or completed package record.

Reporting a problem is allowed; preparing a deliverable from that failed run is
not. INCOMPLETE runs are also refused. A READY run with permitted optional
metadata warnings can be packaged, with those warnings retained in its report.

## 7. Inspect package history

```bash
python scripts/package_dataset.py list
```

This lists completed package IDs, their linked run IDs, dates, file counts and
paths. The existing run-history command remains available:

```bash
python scripts/history.py list
```

Package list entries record completion at creation time. Use `verify` when you
need to check the files now. An SQL query joining the two histories is:

```bash
python -m sqlite3 outputs/biodataqc.sqlite "SELECT p.package_id, r.project_id, r.status, p.files_count FROM packages p JOIN validation_runs r ON r.run_id = p.run_id;"
```

More read-only examples are in `examples/package_queries.sql`.

## How the packaging gate works

1. Load the explicitly selected saved run. Confirm supported evidence/rule
   versions, complete expected checks, permitted outcomes, exact inventory and
   complete SHA-256 fingerprints. A READY label alone is insufficient.
2. Compare current source inventory and bytes with that run. A modification to
   any included input, including requirements formatting, requires revalidation.
3. Copy only the declared files into a fresh private staging directory outside
   the source. Add the HTML report and run summary.
4. Compare staged fingerprints with their expected values and write the manifest.
   Recheck the source snapshot after copying.
5. Reserve a new destination, promote the staging directory and verify its full
   contents against the manifest and saved run.
6. Record the completed package and manifest digest in SQLite. The package table
   references the run, and an SQL trigger rejects references to non-READY runs.

The same Python function performs these gates for the CLI and the future
Streamlit interface. The decision does not depend on a disabled UI button.

Work with stable local directories. This prototype does not lock source files
against concurrent writers. Ordinary exceptions roll back package SQL writes
and remove this invocation's incomplete output. Filesystem promotion and SQL
commit are not one crash-atomic transaction: a process or machine crash can leave
an unregistered directory. Only a successful completion message and SQL record
identify a completed package; no existing destination is overwritten on retry.

Checksums establish equality with this local validation snapshot and later
copy consistency. They do not establish pre-receipt provenance or biological
quality without further independent evidence.

## Reuse from Python

```python
from biodataqc.history import get_latest_run
from biodataqc.reporting import write_report_html
from biodataqc.packaging import build_package, verify_package

database = "outputs/biodataqc.sqlite"
evidence = get_latest_run(database, dataset_path="data/clean")
write_report_html(evidence, "outputs/clean_report.html")

package = build_package(database, evidence["run_id"], "outputs/packages/new_delivery")
verified = verify_package(database, package["package_path"])
```

Custom database paths are accepted by both commands. For package operations,
place the global `--db` argument before `build`, `verify` or `list`:

```bash
python scripts/package_dataset.py --db outputs/another.sqlite list
```

Use a full run ID instead of `latest` to make a selection explicit. `--dataset`
and `--project` narrow `latest`; they are not accepted alongside a full run ID.
Package commands return 0 on success, 1 for a readiness/integrity refusal and 2
for a storage, input/output or configuration error.

Stage 7 is complete locally when the tests pass, both reports open, clean staging
and verification succeed, broken staging is refused, and the completed clean
package appears in SQL history. Stage 8 will connect these same functions to the
Streamlit interface.

## Technical references

- [Python HTML escaping](https://docs.python.org/3.12/library/html.html)
- [Python file copying](https://docs.python.org/3.12/library/shutil.html)
- [Python SQLite transactions](https://docs.python.org/3.12/library/sqlite3.html)

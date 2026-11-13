# BioDataQC — Stage 5: build and test the validation engine

This stage turns the stage-4 inventory into a reproducible validation decision.
It uses the existing dataset-specific `requirements.json` and performs a complete
structural scan of each available expected FASTQ file. The engine is independent
of Streamlit, so the CLI, database and interface can all reuse it.

## 1. Install the stage files

Download `BioDataQC_Stage5_Validation.zip` into `~/Downloads`, then run:

```bash
conda activate biodataqc
cd ~/Precursion/BioDataQC
python -m zipfile -e ~/Downloads/BioDataQC_Stage5_Validation.zip .
```

The archive contains paths relative to the project root. It adds the engine,
command and tests, and updates `biodataqc/inputs.py` to capture fingerprints of
the exact configuration and metadata bytes it reads. Existing stage-4 APIs remain
available. `tests/test_inputs.py` is included unchanged for regression checks.
No additional packages are needed beyond your existing Python 3.12 and pandas.
There are no changes to `app.py`, the demo generator, your datasets or your
dataset requirements. Streamlit integration comes in the interface stage.

| File | Responsibility |
| --- | --- |
| `biodataqc/inputs.py` | Load literal CSV values, inventory files and fingerprint loaded sources |
| `biodataqc/results.py` | Structured check evidence and overall readiness decision |
| `biodataqc/fastq.py` | Stream gzip content, validate records and fingerprint ordered read IDs |
| `biodataqc/validation.py` | Execute configuration, metadata, file, pairing and snapshot rules |
| `scripts/validate_dataset.py` | Terminal command and JSON report export |
| `tests/test_validation.py` | Isolated validation and CLI behavior tests |
| `tests/test_inputs.py` | Existing loader regression tests |
| `tests/fixtures/validation_requirements.json` | Independent test configuration, not your active requirements |

## 2. Run the test suite

From the project root:

```bash
python -m unittest discover -s tests -p 'test_*.py' -v
```

Expected result for this archive:

```text
Ran 76 tests in ...s

OK
```

The tests generate small temporary datasets. They do not modify `data/clean` or
`data/broken`. Coverage includes metadata rules, missing and unexpected files,
gzip CRC/truncation failures, malformed records, late-file faults, pairing order,
configured thresholds, execution errors, changes during validation, JSON output
and CLI exit codes. The original 15 loader tests remain part of the suite.

## 3. Validate the clean synthetic delivery

```bash
python scripts/validate_dataset.py data/clean --json outputs/clean_validation.json
```

Expected with the unchanged stage-3 fixtures:

```text
Status: READY
Blocking findings: 0
Warnings: 0
Execution errors: 0
Required checks not run: 0
Valid fastq files: 12
Records scanned: 12000
```

## 4. Validate the broken delivery

```bash
python scripts/validate_dataset.py data/broken --json outputs/broken_validation.json
```

Expected:

```text
Status: BLOCKED
Blocking findings: 2
Warnings: 0
Execution errors: 0
Required checks not run: 3
Valid fastq files: 10
Records scanned: 11000
```

The root findings are:

1. `FILE001`: missing `sequencing/S003_R2.fastq.gz`.
2. `FASTQ001`: record 1 of `sequencing/S004_R1.fastq.gz` has sequence length 12
   and quality length 8.

Three required checks cannot proceed because of these known faults: the content
scan of the missing file, the S003 pair comparison and the S004 pair comparison.
They are `NOT_RUN`, linked to their failed prerequisites, rather than extra
blocking findings. The malformed S004 file is still scanned to the end when its
gzip stream permits it, so all 11,000 available records are examined.

To see individual passes and skipped prerequisites:

```bash
python scripts/validate_dataset.py data/broken --details
```

The broken validation intentionally returns exit code **1**. That means the
command completed and found a blocked delivery. Exit code 0 means READY; exit
code 2 means INCOMPLETE or a report-export failure. Run clean and broken as
separate commands rather than depending on a successful broken exit code.

## 5. Understand the rules and decisions

| Rule | What it checks |
| --- | --- |
| `CFG001` | Supported v1 policies and valid configuration values |
| `CSV001` | UTF-8 CSV, unique headers and consistent field counts |
| `FILE001` | Exact expected file inventory, regular file types and unexpected entries |
| `META001` | Required metadata columns and nonblank required values |
| `META002` | Exactly one metadata row for every expected sample ID |
| `META003` | Exact allowed values and nonblank values with outer whitespace |
| `META004` | Optional metadata and extra-column warnings |
| `FASTQ001` | Gzip integrity and every available four-line FASTQ record |
| `PAIR001` | Equal mate record counts and matching read IDs in the same order |
| `SNAP001` | Input fingerprints and inventory unchanged at the final recheck |
| `REF001` | Optional independent source-checksum comparison; not implemented in v1 |

The FASTQ dialect is the narrow synthetic format in our specification:
`@READ_000001/1` or `/2`, uppercase ACGTN, a literal `+` separator, nonempty
sequences and quality strings, and equal sequence/quality lengths within each
record. Quality characters must fall within the configured Phred+33 ASCII bounds.
R1 and R2 sequences may have different lengths; their read IDs and counts must
match. Ordered ID streams are compared using SHA-256 fingerprints, avoiding
storage of all IDs in memory.

The first structural problem per file is reported with its record number, while
the number of malformed records and scan-completion evidence are also retained.
Gzip damage can prevent completion; such a file is still a known blocking failure.

| Overall status | Meaning |
| --- | --- |
| `READY` | Every required check completed; no blocking findings. Warnings are allowed by v1. |
| `BLOCKED` | At least one known blocking failure, even if dependent checks could not run. |
| `INCOMPLETE` | No known blocking failure, but an execution error or unfinished required check prevents a decision. |

`FAIL` describes a rule violation. `ERROR` describes an execution problem.
`NOT_RUN` records an unexecuted check and its dependencies. These distinctions
make the decision explainable, rather than treating every problem as the same
kind of failure.

Metadata is never silently trimmed, corrected or converted. Optional blank notes
produce a warning. Promoting notes to `required_columns` and removing them from
`optional_columns` makes blank notes blocking. Unsupported policy changes are
rejected instead of being silently ignored.

## 6. Reuse the engine

From Python running in the project environment:

```python
from biodataqc.validation import validate_dataset

report = validate_dataset("data/clean")
print(report.status)
print(report.counts())
evidence = report.to_dict()
```

The JSON evidence includes a unique run ID, UTC timestamps, rules version,
requirements snapshot, inventory, file SHA-256 fingerprints and all check
outcomes with locations, dependencies and suggested actions. Stage 6 can store
this evidence in SQLite without reimplementing validation.

## Scope of readiness

READY refers to the observed input snapshot and the declared structural rules.
It does not assert biological quality, sample identity or provenance. A locally
computed checksum cannot prove that a file was correct before receipt without
an independent trusted reference. With the current configuration, `REF001` is
explicitly optional and not run. Requiring it makes the run INCOMPLETE until a
trusted-reference comparison is implemented.

The engine reads inputs without correction and detects ordinary changes during
its run, but it does not lock files or guarantee an atomic view against concurrent
writers. Work with stable input directories. Later staging must compare the
copied files against this validation snapshot again before preparing a delivery.
This stage produces evidence; it does not package or transfer any files.

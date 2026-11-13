# BioDataQC — Architecture and data flow

## High-level flow

```text
Delivery folder (requirements.json + samples.csv + FASTQ .fastq.gz files)
        |
        v
Inventory / input loading (inputs.py)
        |
        v
Config-driven structural checks (validation.py + fastq.py)
        |  READY / BLOCKED / INCOMPLETE + detailed findings
        +----> JSON / HTML evidence (report_io.py, reporting.py)
        +----> SQLite run history (history.py)
        |          +----> run-to-run comparison (comparison.py)
        +----> verified package creation (packaging.py)
        |
        +----> OPTIONAL external FastQC 0.12.1 (fastqc.py)
                       +----> module flags, plots, HTML/ZIP reports
```

The UI (`app.py`, `biodataqc/ui.py`, `ui_service.py`, `fastqc_ui.py`) coordinates these operations. State-changing operations are triggered by explicit user actions. The core validation routines also have command-line entry points in `scripts/`.

## Modules

| Component | Responsibility |
|---|---|
| `inputs.py` | Load the configured delivery, sample metadata and inventory. |
| `fastq.py`, `read_headers.py` | Parse FASTQ content and read identifiers. |
| `validation.py`, `results.py` | Execute structural rules, track check dependencies and produce status plus findings. |
| `history.py` | Save and retrieve validation runs and packaging records using SQLite. |
| `comparison.py` | Compare previous and newer runs, including resolved or newly introduced findings. |
| `reporting.py`, `report_io.py` | Generate user-readable and machine-readable evidence. |
| `packaging.py` | Prepare copied deliverables and verify the files against saved validation evidence. |
| `fastqc.py`, `fastqc_ui.py` | Invoke/version-check external FastQC, read its result artifacts and display module-level QC outcomes. |
| `public_data.py` | Prepare the pinned public yeast example, verifying recorded source hashes. |
| `ui.py`, `ui_service.py`, `appearance.py` | Streamlit workflow, user-facing messages and visual presentation. |

## Status semantics

- **READY:** required structural checks succeeded for the recorded input snapshot.
- **BLOCKED:** one or more blocking findings prevent a valid delivery.
- **INCOMPLETE:** sufficient checks could not be completed reliably.

A historical READY is not an unconditional authorization to package files that have since changed. The packaging workflow compares current inputs with the saved snapshot. FastQC is supplemental sequencing-quality evidence: its WARN/FAIL flags **do not silently override** the structural READY/BLOCKED decision.

## Data and persistence

Each delivery has a configuration `requirements.json`, metadata `samples.csv`, and expected sequencing inputs. The SQLite database under `outputs/` records run history separately from source data. Generated HTML/JSON reports, FastQC results, and verifiable packages also go under `outputs/`. Temporary artifacts and caches are deliberately excluded from version control.

## Quality and limits

Run `python -m unittest discover -s tests -p 'test_*.py' -v` from the project root. This is a prototype and **not** a substitute for lab-specific QC policy, statistical quality assessment, biological interpretation, or clinical validation. Do not use protected sequencing data without appropriate authorization.

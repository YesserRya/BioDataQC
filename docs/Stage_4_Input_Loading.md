# Stage 4 - Input loading and inventory

Project: `~/Precursion/BioDataQC`  
Environment: `conda activate biodataqc`

This stage reads configuration and sample metadata, then compares the expected sequencing filenames with entries directly inside the configured sequencing directory. It does not decompress FASTQ files, evaluate biological data, or decide whether delivery is ready.

## Install the files

Extract `BioDataQC_Stage4_Input_Loading.zip` into the project root. It adds:

| File | Purpose |
| --- | --- |
| `biodataqc/inputs.py` | Reusable loader, typed file inventory and pandas tables |
| `scripts/inspect_dataset.py` | Command-line inspection and optional JSON export |
| `tests/test_inputs.py` | Isolated behavior checks, using temporary datasets |
| `docs/Stage_4_Input_Loading.md` | This guide |

Your existing `app.py`, generator, configuration and demo datasets are not in the archive. Pandas is the only external dependency used here; it was installed in stage 2.

## Run it

```bash
conda activate biodataqc
cd ~/Precursion/BioDataQC
python scripts/inspect_dataset.py data/clean
python scripts/inspect_dataset.py data/broken
```

| Counter | Clean | Broken |
| --- | --- | --- |
| Expected samples | 6 | 6 |
| Metadata rows | 6 | 6 |
| Expected files | 12 | 12 |
| Observed regular files | 12 | 11 |
| Present expected files | 12 | 11 |
| Missing expected files | 0 | 1 |
| Unexpected entries | 0 | 0 |
| Expected entries with the wrong filesystem type | 0 | 0 |

The broken fixture should report `MISSING: sequencing/S003_R2.fastq.gz`. The malformed `S004_R1.fastq.gz` should still be `PRESENT`: stage 4 is checking its presence and size, not its internal records. Its format problem is for stage 5.

Both runs explicitly report `NOT_VALIDATED`. A successful inventory command exits with code 0 even when it lists missing or unexpected files. Exit code 2 means an input could not be loaded or an export could not be written.

## Inspect or export details

```bash
python scripts/inspect_dataset.py data/broken --details
python scripts/inspect_dataset.py data/broken --json outputs/broken_inventory.json
```

The JSON contains the configuration, literal metadata values and file inventory. It is an inventory export, not a quality-control report. Exports must be placed outside the input dataset so inspection does not alter its inventory.

## Run the checks

```bash
python -m unittest discover -s tests -p 'test_inputs.py' -v
```

The tests use temporary folders. They cover missing mates, a wrong sample with the same total file count, raw CSV values, duplicate CSV/JSON keys, malformed row lengths, absent inputs, wrong filesystem entry types and path containment.

## Design decisions to understand

1. The dataset's own `requirements.json` is authoritative for the inventory. We do not silently fall back to the template in `config/`.
2. Expected filenames are built from the declared sample IDs and read types. The loader compares names, not only counts.
3. CSV values remain strings. Leading zeros, the text `NA`, whitespace and empty cells are preserved for later rules. Invalid row structures and duplicate column names fail explicitly.
4. Every expected file gets an inventory entry, including missing ones. Unexpected files and folders get additional entries. Symlinks are identified without reading their targets.
5. A sample-to-file table shows how many metadata rows match each expected sample. Duplicate rows are preserved and remain visible for stage 5.
6. A missing sample-ID column leaves association unavailable; it does not invent zero matches. Required-column rules will handle this in stage 5.
7. Inventory results remain separate from validation outcomes. `PRESENT` means a regular file exists. It does not mean its gzip stream or FASTQ records are valid.

## Reuse from Python

```python
from biodataqc.inputs import load_dataset

dataset = load_dataset("data/broken")
print(dataset.summary())
print(dataset.metadata)
print(dataset.file_inventory())
print(dataset.sample_inventory())
```

Stage 5 will consume this loaded representation and return structured rule results. The current Streamlit starter page does not change in stage 4.

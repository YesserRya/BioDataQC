# BioDataQC — real-data extension

This extension adds an explicitly defined public-data profile alongside the
existing synthetic profile. It includes a reproducible download/preparation
command, source fingerprint comparisons, source details in the interface and
reports, and two new dataset choices. The original demos continue to work.

## 1. Install the update

Prerequisite: stages 4–8 are installed in `~/Precursion/BioDataQC` and working
inside your `biodataqc` Conda environment. Stop the running Streamlit process
with **Ctrl+C**. Save `BioDataQC_Real_Data_Extension.zip` in Downloads.

```bash
conda activate biodataqc
cd ~/Precursion/BioDataQC
mkdir -p backups/before_real_data_extension
cp -rn app.py biodataqc scripts tests docs backups/before_real_data_extension/
python -m zipfile -e ~/Downloads/BioDataQC_Real_Data_Extension.zip .
```

The backup command keeps the existing code and does not replace an earlier
backup at that path. The update replaces selected Python modules and adds the
public-data importer, a pinned source catalog, tests and this guide. It does not
replace your datasets or database. No additional Python packages are needed
beyond the stage-8 environment.

## 2. Run the tests

```bash
python -m unittest discover -s tests -p 'test_*.py' -v
```

Expected result:

```text
Ran 221 tests in ...s

OK
```

These are the 184 existing tests plus 37 new tests. Unit tests use small
synthetic sequences with realistic header formats; they do not download data.
The separate integration check described below uses the actual downloaded
experimental subset. Occasional AppTest `missing ScriptRunContext` warnings are
harmless when the final test result is `OK`.

## 3. Prepare the real dataset

```bash
python scripts/prepare_public_demo.py
```

The command downloads two files totaling approximately **4.5 MB**, verifies
their sizes, SHA-256 values and Git blob identities, then creates:

| Location | Contents |
| --- | --- |
| `outputs/downloads/public_yeast/` | Verified original downloaded files, retained for reuse |
| `data/public_yeast/` | Complete dataset, requirements and sample metadata |
| `data/public_yeast_missing_mate/` | A separate test copy with its R2 file deliberately removed |

It checks the complete prepared dataset before reporting success. Expected
output includes:

```text
Public-data preparation: COMPLETE
"records_scanned": 100000
"run_accession": "SRR6357070"
```

Preparation performs a validation precheck but does not create a SQL history
run. Use the interface's **Run validation** button to create a saved run.

The downloaded FASTQ bytes, including read identifiers, are unchanged in the
complete dataset. Only the local filenames are mapped to our delivery naming
convention:

| Original filename | Local dataset filename |
| --- | --- |
| `SRR6357070_1.fastq.gz` | `sequencing/SRR6357070_R1.fastq.gz` |
| `SRR6357070_2.fastq.gz` | `sequencing/SRR6357070_R2.fastq.gz` |

`SRR6357070` is the sequencing run accession and the delivery sample ID used in
this small demonstration. The metadata also records GEO sample `GSM2879618`.

## 4. What the data represents

The source is the nf-core/rnaseq public test fixture derived from a published
*Saccharomyces cerevisiae* (yeast) RNA-seq experiment, study **GSE110004**.
The selected run is **SRR6357070**, wild-type biological replicate 1.

The nf-core maintainers filtered the experimental reads using a yeast
chromosome-I Kraken2 reference and subsampled each mate to 50,000 records using
seqtk with seed 100. This is a **processed experimental subset**, not the full
original sequencing run or an unbiased representation of that entire run.
BioDataQC performs no additional sequence editing or subsampling.

The extension pins repository commit:

```text
9dd6d014f560e182047992834a8d267214fbccd1
```

Both files have 50,000 FASTQ records, giving **50,000 read pairs and 100,000
individual FASTQ records**. The read IDs match between the files in the same
order. Their actual header format includes an SRA spot identifier and an
explicit mate token, for example:

```text
@SRR6357070.25017281 25017281/1 kraken:taxid|4932
@SRR6357070.25017281 25017281/2 kraken:taxid|4932
```

The paired key is `SRR6357070.25017281`; `1` and `2` identify the mates. The
parser checks that the repeated spot number agrees with the first token. It
does not replace real identifiers with synthetic `READ_...` identifiers.

Study, sample, source URLs, processing description, commit, reference hashes,
preparation time and filename mapping are recorded in `requirements.json`.
This file is fingerprinted, stored with the run evidence, and included in the
package. The metadata uses organism and accession information from the source;
it does not invent tissue or treatment information to fit the synthetic demo.

## 5. Launch and validate through Streamlit

```bash
python -m streamlit run app.py
```

Open **http://localhost:8501**. The sidebar now offers:

- **Clean demo**
- **Broken demo**
- **Public yeast**
- **Public yeast · missing mate**
- **Custom folder**

Select **Public yeast**, optionally click **Load inventory**, then click
**Run validation**. Expect:

| Measure | Complete public dataset |
| --- | --- |
| Status | READY |
| Expected samples | 1 |
| Expected files present | 2/2 |
| Structurally valid FASTQ files | 2 |
| Records scanned | 100,000 |
| Blocking findings | 0 |
| Warnings | 0 |
| Execution errors | 0 |
| Required checks not run | 0 |
| Passed checks | 14 |

The profile is labeled **Public experimental subset (declared)**. Expand
**Recorded data source** to inspect the provenance recorded in the dataset
specification. The label distinguishes a source declaration from independent
authentication of biological origin.

In **Technical results**, include **PASS** in the outcome filter and inspect:

- `FASTQ001`: valid four-line records, explicit mate identity, sequence/quality
  length agreement and configured character checks.
- `PAIR001`: equal mate counts and matching ordered read identifiers.
- `SRC001`: compressed FASTQ bytes match the configured source SHA-256 references.
- `SNAP001`: input fingerprints and inventory remained stable during validation.

`REF001` remains optional and NOT_RUN. It concerns independent original-source
checksum verification, which this prototype does not implement. `SRC001` pins
the selected nf-core subset, using SHA-256 references shipped with this update;
the importer additionally checks the Git blob identities retrieved from that
repository. Neither check establishes biological quality or proves that the
original full sequencing run was correct before it reached the archive.

## 6. Build and verify a real-data package

In **Reports & packages**:

1. Optionally enter a responsible person/team and next update.
2. Click **Save report files**, or download the HTML/JSON using the browser.
3. Keep the generated fresh package folder name.
4. Click **Build verified package**.
5. Click **Verify selected package**.

Expected: a completed **seven-file package** and a successful verification.

| Package file | Count |
| --- | --- |
| R1 and R2 FASTQ files | 2 |
| `samples.csv` | 1 |
| `requirements.json`, including source details | 1 |
| `report.html` | 1 |
| `run_summary.json` | 1 |
| `manifest.csv` | 1 |

The manifest covers the other six files; SQL stores the manifest digest.
Packaging repeats the readiness and snapshot gates, verifies the copies, and
links the completed package to the exact saved run. If input bytes or
requirements change after validation, packaging requires revalidation.

## 7. Demonstrate the deliberate failure case

Select **Public yeast · missing mate** and click **Run validation**.

Expected:

| Measure | Deliberately modified copy |
| --- | --- |
| Status | BLOCKED |
| Expected files present | 1/2 |
| Blocking findings | 1 |
| Required checks not run | 3 |
| Valid FASTQ files | 1 |
| Records scanned | 50,000 |
| Passed checks | 10 |

The root finding is `FILE001`: missing
`sequencing/SRR6357070_R2.fastq.gz`. Its FASTQ check, the pair comparison and the
source-reference comparison are NOT_RUN because their prerequisites are missing.
The interface and report explicitly identify the deliberate test modification.
The complete dataset and cached downloads remain intact.

You can export a report for this run, but packaging is refused. Use **History →
Show all datasets** to compare the two runs and reopen either one without
creating another validation run.

## 8. Optional command-line checks

These commands use the same engine and can save additional runs to SQL:

```bash
python scripts/validate_dataset.py data/public_yeast --db outputs/biodataqc.sqlite --details
python scripts/validate_dataset.py data/public_yeast_missing_mate --db outputs/biodataqc.sqlite --details
```

The complete dataset returns exit code 0. The deliberately incomplete delivery
returns exit code 1 because its missing file is a known blocker.

```bash
python scripts/report_dataset.py latest --dataset data/public_yeast --output outputs/public_yeast_report.html
python scripts/package_dataset.py build latest --dataset data/public_yeast --output outputs/packages/public_yeast_delivery
python scripts/package_dataset.py verify outputs/packages/public_yeast_delivery
```

Choose a different destination if that package folder already exists.

## Compatibility and implementation

| Component | Change |
| --- | --- |
| Requirements profile 1.0.0 | Preserves the existing strict synthetic rules |
| Requirements profile 1.1.0 | Public experimental subset, explicit header profile, source metadata and required SHA-256 references |
| Validation rules version | New runs record 1.1.0 |
| Report JSON schema | Remains 1.1.0 |
| SQLite schema | Remains version 2; no migration or history reset |
| Old rules-1.0.0 runs/packages | Remain readable and usable under their original synthetic specification |

The public profile supports three explicit header parsers:

| `fastq.header_profile` | Example |
| --- | --- |
| `sra_spot_mate` | `@SRR6357070.123 123/1 optional annotation` |
| `slash_mate` | `@instrument:read/1 optional annotation` |
| `illumina_casava18` | `@instrument:run:flowcell:lane:tile:x:y 1:N:0:ATCG` |

All require an explicit mate identifier, and matching pairs use the exact parsed
read key in order. There is no format guessing or stripping of a numeric SRA
spot suffix as if it were a mate. SRA exports with no explicit mate token are
outside these profiles and are rejected. The selected preset uses
`sra_spot_mate`.

The public profile permits a bare `+` separator or an exact repetition of the
header following `+`. It still requires gzip compression, four-line ASCII FASTQ,
the configured uppercase A/C/G/T/N alphabet, configured Phred+33 character
bounds, one R1 and one R2 file per delivery sample, and full-file scanning.
Other FASTQ dialects, wrapped records, interleaved reads, multi-lane aggregation,
BAM/CRAM and biological QC are outside this extension.

The same engine handles CLI and UI validation. The backend packaging gate checks
the new source-check roster and compares saved FASTQ fingerprints with the
configured references; a READY label or enabled button alone is insufficient.

## Failure and retry behavior

- Existing dataset directories are never overwritten. After successful
  preparation, use the app to validate again; you do not need to prepare again.
- Network interruption leaves no partial dataset. Verified cached files are
  retained, so rerunning preparation can reuse them.
- A cached file with the wrong size/hash is preserved and rejected. Use a fresh
  cache directory to retry while retaining the suspect file for inspection.
- To prepare another independent copy, supply new paths; select those datasets
  through **Custom folder** in the app:

```bash
python scripts/prepare_public_demo.py --output data/public_yeast_2 --broken-output data/public_yeast_missing_mate_2 --cache outputs/downloads/public_yeast_retry
```

The importer downloads from public HTTPS URLs and needs internet access only
when the verified source files are absent from its cache. Validation, reporting,
packaging and history browsing are local operations. Work with stable local
directories. Ordinary import failures roll back newly created dataset outputs;
filesystem promotion is not a crash-atomic transaction across both directories.
Stage-7 packaging limitations remain unchanged.

## Verification performed for this release

- All **221 automated tests passed**, including unchanged synthetic workflows
  and new header, checksum, importer, history and interface tests.
- The preparation command downloaded and verified the two pinned public files.
- Streamlit AppTest operated the real application entry point on all **100,000
  experimental FASTQ records**, exported reports, built and verified a
  **seven-file package**, and reopened history without duplicate validation runs.
- The missing-mate copy produced the expected **one blocker and three required
  skipped checks**; its package button was disabled.
- Original source bytes remained unchanged.
- A package actually created in stage 7 verified successfully with this release,
  and its old SQL database was unchanged by the read/verification operations.

AppTest exercises controls and session behavior without a browser. Browser
appearance and the download interaction still need your local visual check.

## Sources

- [Pinned nf-core source and sampling procedure](https://github.com/nf-core/test-datasets/blob/9dd6d014f560e182047992834a8d267214fbccd1/README.md)
- [Pinned source file directory](https://github.com/nf-core/test-datasets/tree/9dd6d014f560e182047992834a8d267214fbccd1/testdata/GSE110004)
- [Study GSE110004](https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc=GSE110004)
- [GEO sample GSM2879618](https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc=GSM2879618)
- [NCBI SRA format guide](https://www.ncbi.nlm.nih.gov/sra/docs/submitformats/)
- [Illumina FASTQ upload requirements](https://help.basespace.illumina.com/manage-data/import-data/fastq-upload-reqs)

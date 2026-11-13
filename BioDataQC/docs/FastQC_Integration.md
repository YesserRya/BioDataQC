# BioDataQC — FastQC integration

This update adds a **Sequencing quality** tab. It runs the external FastQC
program, shows its per-file module flags, displays quality/GC/adapter plots,
and provides the original HTML and ZIP reports plus a linked analysis JSON.

FastQC findings are additional evidence for review. They do not rewrite the
saved structural READY/BLOCKED decision or automatically change packaging
eligibility. This update does not trim reads, remove duplicates, align reads
or make a biological acceptance decision.

## 1. Install the update

Prerequisite: the Streamlit interface, real-data extension and run-comparison
update are already installed and working in `~/Precursion/BioDataQC`.
Download `BioDataQC_FastQC_Integration.zip` to Downloads, then stop Streamlit
with **Ctrl+C** in its terminal.

```bash
conda activate biodataqc
cd ~/Precursion/BioDataQC
mkdir -p backups/before_fastqc
cp -rn biodataqc tests docs backups/before_fastqc/
python -m zipfile -e ~/Downloads/BioDataQC_FastQC_Integration.zip .
```

The update changes `biodataqc/ui.py` and its tab-layout test; it adds the FastQC
backend, its interface module, tests and this guide. It contains no datasets,
database, completed analyses or external FastQC binaries. No additional Python
packages are needed beyond your current Streamlit environment.

## 2. Install the external FastQC tool with Anaconda

In the same active `biodataqc` environment:

```bash
conda install --override-channels --strict-channel-priority -c conda-forge -c bioconda fastqc=0.12.1
fastqc --version
```

Expected version output:

```text
FastQC v0.12.1
```

Review and accept Conda's proposed installation when prompted. Bioconda's
FastQC package brings its required runtime dependencies. The channel arguments
apply to this command and do not edit your global Conda channel configuration.
Version **0.12.1 is deliberately pinned to the version tested here**; this is
not a claim that it is the newest FastQC release. The adapter rejects a different
version rather than silently interpreting an untested report format.

If FastQC is already installed elsewhere, the interface accepts its full
executable path in **Sequencing quality → FastQC setup**. Its Java runtime must
also be available in the environment that launches Streamlit. Installing it in
the same `biodataqc` environment is the simplest path for this project.

## 3. Run the tests

```bash
python -m unittest discover -s tests -p 'test_*.py' -v
```

Expected with all earlier updates: **289 tests, OK**. The 33 new automated tests
use an explicitly labeled test double in temporary directories to exercise
execution, parsing, error handling, artifact integrity and the interface.
They do not download data or require FastQC/Java. They also confirm that quality
WARN/FAIL flags do not turn a saved READY validation into BLOCKED.

Separately, the integration was exercised with the actual upstream FastQC 0.12.1
binary and the real 100,000-record yeast subset, including the Streamlit
execution, plots, report verification and download controls. That runtime check
used Linux, Python 3.12, Streamlit 1.67.0 and OpenJDK 21.

## 4. Run FastQC through the interface

```bash
python -m streamlit run app.py
```

At `http://localhost:8501`:

1. Select **Public yeast** in the sidebar.
2. Click **Run validation** and wait for a saved **READY** result. Alternatively,
   open an existing READY yeast run from **History** if its inputs still match.
3. Open the new **Sequencing quality** tab.
4. Expand **FastQC setup** and click **Check FastQC installation**. It should
   report that FastQC 0.12.1 is available.
5. Click **Run FastQC**.

The integration analyzes all expected FASTQ files. The interface uses two
simultaneous files and a ten-minute execution timeout. Clicking the button
creates a new FastQC analysis, not a new validation run. Changing filters,
switching tabs or opening an old analysis does not execute FastQC again.

A BLOCKED or INCOMPLETE run cannot start this integration. Correct the
structural problem and save a READY validation first. Current inputs are
checked against the saved snapshot before and after execution.

## 5. What appears in the tab

| Area | Contents |
| --- | --- |
| Saved FastQC analysis | Completed analyses linked to the selected validation run, with timestamps and tool version. |
| Summary cards | Number of FASTQ files analyzed and total WARN/FAIL module flags. |
| File overview | Original file path, sample, mate, record count, GC percentage, read length, inferred encoding and module counts. |
| FASTQ file to inspect | Select R1, R2 or another sample's file. |
| Module table | The exact PASS/WARN/FAIL results emitted by FastQC. |
| Quality along the read | Mean, median and lower-quartile quality across base positions. |
| GC distribution | FastQC's distribution of GC content across sequences. |
| Adapter content | Adapter traces from the installed FastQC configuration. |
| Downloads | Original HTML report, original FastQC ZIP and complete BioDataQC analysis JSON. |
| Analysis settings and log | Tool path/version, available configuration fingerprints, command, input hashes and the end of the execution log. |

FastQC can group adjacent positions. Inline plots use the midpoint of each
reported group; the original range remains in the analysis JSON. The original
HTML report contains FastQC's full plots, including the per-base box plot.
Non-finite numerical values are represented as missing plot values rather than
fabricated zeros.

Quality scores use FastQC's inferred encoding, which is shown in the file
overview. BioDataQC's configured input profile declares Phred+33; inspect an
unexpected encoding inference before interpreting its numerical quality scores.
FastQC documents that encoding inference can be ambiguous for some datasets.

## 6. Expected result for the pinned yeast example

The following were observed with the pinned public-data files and upstream
FastQC 0.12.1 defaults:

| File | Records | PASS modules | WARN modules | FAIL modules |
| --- | ---: | ---: | ---: | ---: |
| SRR6357070_R1.fastq.gz | 50,000 | 6 | 2 | 2 |
| SRR6357070_R2.fastq.gz | 50,000 | 7 | 1 | 2 |
| Total | 100,000 | 13 | 3 | 4 |

The saved structural status remains **READY**. FastQC execution is **COMPLETED**,
and the WARN/FAIL flags are displayed for review. Execution completion means
the tool ran and its expected reports were verified; it does not mean every
quality module passed.

These counts are an observed reference, not hard-coded acceptance criteria.
Different tool configuration or input data can produce different flags.
The files are a processed, chromosome-I-enriched experimental RNA-seq subset,
not a full unbiased sequencing run. FastQC's documentation explains that
RNA-seq libraries can show duplication and sequence-composition biases. A flag
alone does not identify its cause, prove contamination or justify automatically
deleting or modifying reads. Review the experiment and original report with the
bioinformatics team.

## 7. Linkage and artifact storage

Completed analyses are stored under:

```text
outputs/fastqc/<validation-run-key>/<analysis-id>/
```

Each directory contains `analysis.json`, `fastqc.log`, and a `reports/` directory
with one HTML and one FastQC ZIP per input file. The original FastQC ZIP includes
its `fastqc_data.txt`, `summary.txt` and other native report assets.

The analysis JSON records:

- The exact validation run ID, project and dataset path.
- A SHA-256 of the canonical saved validation JSON and its input fingerprints.
- The original sample/mate/file mapping for each FastQC report.
- Tool version, launcher hash, available installed configuration hashes, options,
  execution times, module results and parsed plot data.
- SHA-256 values for the retained reports and execution log.

The runner first verifies the original snapshot. It creates temporary FASTQ
copies, verifies their hashes, analyzes those copies, verifies their hashes
again, and rechecks the original snapshot before publishing the analysis.
Temporary inputs are removed on ordinary completion or failure.

The copies use names such as `input_0001.fastq.gz` to keep command arguments and
report names predictable. The HTML's Filename field therefore uses that alias;
the interface and analysis JSON show its exact original-file mapping. Downloaded
HTML/ZIP filenames are labeled with the original sample filename.

Analysis directories are separate artifacts linked to existing SQL evidence;
this update does **not** add a FastQC table or migrate the SQLite schema. Existing
validation JSON, comparison results and verified delivery packages retain their
original meaning. FastQC reports are not automatically added to delivery packages.

Reopening a saved validation run reloads its analyses without rerunning FastQC.
The original input files and FastQC executable need not remain available to
view a completed analysis. Retained artifact hashes and parsed summaries are
checked before display/download. These checks detect local inconsistencies;
they are not a signed audit trail against an adversary who rewrites every record.

## 8. Troubleshooting

| Message or situation | Action |
| --- | --- |
| FastQC not found | Activate `biodataqc`, install the pinned package and restart Streamlit from that environment. Check `which fastqc` and `fastqc --version`. |
| Different FastQC version | Install `fastqc=0.12.1`, or deliberately extend and test the parser for the other version. |
| Java cannot start | Check the Conda installation and `java -version` in the same terminal used to launch Streamlit. |
| Input changed after validation | Run validation again, then run FastQC against that new saved run. |
| Run FastQC disabled | Open or create a saved READY run, and resolve any detected input changes. |
| Tool timeout or nonzero exit | Read the displayed error. No completed analysis is published for the failed attempt. Earlier completed analyses remain explicitly labeled historical. |
| Missing or mismatched report | The tool did not produce verified reports for every expected input; the attempt is rejected. |
| Saved report changed | Restore the original artifact or create a new FastQC analysis; altered reports are not displayed as verified evidence. |
| New validation has no FastQC result | Analyses belong to exact validation run IDs. Reopen the earlier run or execute FastQC for the new run. |

The Python API supports `threads` from 1 to 4 and an explicit execution `timeout`
up to 86,400 seconds for scripted work. The UI deliberately uses fixed demo
defaults. FASTQ copies temporarily require disk space approximately equal to
the compressed input size, in addition to the reports. This prototype assumes
stable local directories and is not a background job scheduler. A hard process
crash can leave an ignored `.pending-...` work directory; ordinary failures clean
up their temporary work.

## 9. Interview explanation

“I separate delivery integrity from sequencing-quality interpretation. The
validation engine confirms that files, metadata and paired reads satisfy the
delivery specification. FastQC adds quality measurements and flags. Each analysis
is tied to the exact saved input snapshot, so I can explain which files were
analyzed and show the original reports. Quality flags go to the bioinformatics
team for context-specific review.”

## Sources

1. FastQC overview: https://www.bioinformatics.babraham.ac.uk/projects/fastqc/
2. Upstream version used: https://github.com/s-andrews/FastQC/releases/tag/v0.12.1
3. FastQC 0.12.1 command-line source/options: https://github.com/s-andrews/FastQC/blob/v0.12.1/fastqc
4. Bioconda FastQC recipe: https://bioconda.github.io/recipes/fastqc/README.html
5. Bioconda channels: https://bioconda.github.io/
6. Available package versions: https://anaconda.org/bioconda/fastqc
7. Quality/encoding interpretation: https://www.bioinformatics.babraham.ac.uk/projects/fastqc/Help/3%20Analysis%20Modules/2%20Per%20Base%20Sequence%20Quality.html
8. Duplicate sequences/RNA-seq: https://www.bioinformatics.babraham.ac.uk/projects/fastqc/Help/3%20Analysis%20Modules/8%20Duplicate%20Sequences.html
9. Per-base sequence content/RNA-seq: https://www.bioinformatics.babraham.ac.uk/projects/fastqc/Help/3%20Analysis%20Modules/4%20Per%20Base%20Sequence%20Content.html

FastQC remains a separately installed upstream program; this update distributes
the BioDataQC adapter and its tests, not FastQC binaries or upstream source code.

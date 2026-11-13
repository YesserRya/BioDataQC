"""Test double ONLY: exercise process/report boundaries without installing Java.

This file is never used by the application. Tests copy it to a temporary executable.
Real FastQC is exercised separately against the public yeast subset.
"""
import gzip
import os
from pathlib import Path
import sys
import time
import zipfile

mode = os.environ.get("BIODATAQC_TEST_FASTQC_MODE", "normal")
if "--version" in sys.argv:
    print("FastQC v" + ("0.13.0" if mode == "wrong_version" else "0.12.1"))
    raise SystemExit(0)
if mode == "fail":
    print("simulated execution failure")
    raise SystemExit(7)
if mode == "timeout":
    time.sleep(10)
outdir = Path(sys.argv[sys.argv.index("--outdir") + 1])
inputs = [Path(arg) for arg in sys.argv[1:] if arg.endswith(".fastq.gz")]
for index, path in enumerate(inputs):
    if mode == "missing_report" and index == len(inputs) - 1:
        continue
    with gzip.open(path, "rt") as handle:
        records = sum(1 for _ in handle) // 4
    if mode == "wrong_count":
        records += 1
    prefix = path.name.removesuffix(".fastq.gz") + "_fastqc"
    data = ("##FastQC\t0.12.1\n"
            ">>Basic Statistics\tpass\n#Measure\tValue\n"
            f"Filename\t{path.name}\nTotal Sequences\t{records}\nSequence length\t4\n%GC\t50\n"
            "Encoding\tSanger / Illumina 1.9\n>>END_MODULE\n"
            ">>Per base sequence quality\twarn\n"
            "#Base\tMean\tMedian\tLower Quartile\tUpper Quartile\t10th Percentile\t90th Percentile\n"
            "1\t30\t31\t25\t35\t20\t38\n2-4\t29\t30\t24\t34\t19\t37\n>>END_MODULE\n"
            ">>Per sequence GC content\tpass\n#GC Content\tCount\n0\t0\n50\t3\n100\t0\n>>END_MODULE\n"
            ">>Adapter Content\tfail\n#Position\tTest adapter\n1\t0\n2-4\t15\n>>END_MODULE\n")
    summary = "".join(f"{state}\t{module}\t{path.name}\n" for state, module in (
        ("PASS", "Basic Statistics"), ("WARN", "Per base sequence quality"),
        ("PASS", "Per sequence GC content"), ("FAIL", "Adapter Content")))
    (outdir / f"{prefix}.html").write_text("<!doctype html><title>Test double</title><p>Not a real FastQC analysis.</p>")
    with zipfile.ZipFile(outdir / f"{prefix}.zip", "w") as archive:
        archive.writestr(f"{prefix}/fastqc_data.txt", data)
        archive.writestr(f"{prefix}/summary.txt", summary)
    if mode == "mutate_input":
        path.write_bytes(b"changed during execution")
print("TEST DOUBLE: completed simulated report generation")

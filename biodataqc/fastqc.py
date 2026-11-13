"""Optional FastQC 0.12.1 analyses bound to immutable validation evidence.

Quality module flags never change structural readiness. Completed analyses are
separate local artifacts; SQL runs and verified delivery packages are untouched.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
import subprocess
import tempfile
from uuid import uuid4
import zipfile

from .history import database_paths, get_run
from .packaging import _copy_input, _input_plan, _safe_file, _sha256, verify_current_inputs
from .report_io import check_report_destination
from .results import utc_now


SUPPORTED_VERSION = "0.12.1"
SCHEMA_VERSION = "1.0.0"
MAX_TEXT_BYTES = 16 * 1024 * 1024
STATES = {"PASS", "WARN", "FAIL"}


class FastQCError(ValueError):
    """Tool execution, report parsing or snapshot binding failed."""


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def evidence_digest(evidence):
    return hashlib.sha256(_json(evidence).encode()).hexdigest()


def _run_root(output_root, run_id):
    key = hashlib.sha256(run_id.encode()).hexdigest()
    return Path(output_root).expanduser().resolve() / key


def _execute(command, log_path, *, timeout):
    """No shell; bounded wait; terminate the whole job on timeout/interruption."""
    with Path(log_path).open("wb") as log:
        try:
            process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT,
                                       start_new_session=(os.name == "posix"))
        except OSError as error:
            raise FastQCError(f"Could not start FastQC: {error}") from error
        try:
            return_code = process.wait(timeout=timeout)
        except BaseException as error:
            try:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
            except ProcessLookupError:
                pass
            process.wait()
            if isinstance(error, subprocess.TimeoutExpired):
                raise FastQCError(f"FastQC exceeded the {timeout}-second timeout; no completed analysis was published.") from error
            raise
    with Path(log_path).open("rb") as log:
        log.seek(max(0, Path(log_path).stat().st_size - 6000))
        tail = log.read().decode("utf-8", errors="replace")
    if return_code != 0:
        raise FastQCError(f"FastQC exited with code {return_code}.\n{tail}")
    return tail


def check_installation(executable="fastqc"):
    value = str(executable).strip()
    if not value:
        raise FastQCError("Enter the FastQC executable name or full path.")
    found = shutil.which(str(Path(value).expanduser()))
    if found is None:
        raise FastQCError("FastQC was not found. Install fastqc=0.12.1 in the active Conda environment, then restart Streamlit.")
    path = Path(found).resolve()
    with tempfile.TemporaryDirectory(prefix="biodataqc-fastqc-version-") as temp:
        output = _execute([str(path), "--version"], Path(temp) / "version.log", timeout=15)
    match = re.search(r"^FastQC v(\d+\.\d+\.\d+)\s*$", output, re.MULTILINE)
    if match is None:
        raise FastQCError("The executable did not return a recognized FastQC version. Check the installation and Java runtime.")
    version = match.group(1)
    if version != SUPPORTED_VERSION:
        raise FastQCError(f"This integration is tested with FastQC {SUPPORTED_VERSION}; found {version}. Install the pinned version.")
    configs = {}
    for name in ("limits.txt", "adapter_list.txt", "contaminant_list.txt"):
        config = path.parent / "Configuration" / name
        if config.is_file():
            configs[name] = _sha256(config)
    return {"executable": str(path), "version": version, "launcher_sha256": _sha256(path),
            "configuration_sha256": configs, "settings": "Installed FastQC defaults; no custom limits, adapters or contaminants supplied."}


def _number(value):
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise FastQCError("FastQC contains malformed numeric plot data.") from error
    return number if math.isfinite(number) else None


def _position(value):
    if not re.fullmatch(r"\d+(?:-\d+)?", value):
        raise FastQCError("FastQC contains an invalid base-position group.")
    values = [int(v) for v in value.split("-")]
    if min(values) < 1 or values[-1] < values[0]:
        raise FastQCError("FastQC contains an invalid base-position range.")
    return sum(values) / len(values)


def _plots(modules):
    plots = {"quality": [], "gc": [], "adapters": []}
    quality = modules.get("Per base sequence quality")
    if quality:
        for values in quality["rows"]:
            row = dict(zip(quality["columns"], values))
            plots["quality"].append({"Base group": row["Base"], "Position": _position(row["Base"]),
                                     "Mean": _number(row["Mean"]), "Median": _number(row["Median"]),
                                     "Lower quartile": _number(row["Lower Quartile"]),
                                     "Upper quartile": _number(row["Upper Quartile"])})
    gc = modules.get("Per sequence GC content")
    if gc:
        for values in gc["rows"]:
            row = dict(zip(gc["columns"], values))
            plots["gc"].append({"GC (%)": _number(row["GC Content"]), "Count": _number(row["Count"])})
    adapters = modules.get("Adapter Content")
    if adapters:
        for values in adapters["rows"]:
            row = dict(zip(adapters["columns"], values))
            plots["adapters"].append({"Base group": row["Position"], "Position": _position(row["Position"]),
                                      **{k: _number(v) for k, v in row.items() if k != "Position"}})
    return plots


def parse_report(archive, *, input_name, expected_records):
    """Read only bounded text members; never extract third-party ZIP contents."""
    prefix = input_name.removesuffix(".fastq.gz") + "_fastqc"
    try:
        with zipfile.ZipFile(archive) as handle:
            names = handle.namelist()
            if len(names) != len(set(names)):
                raise FastQCError("FastQC ZIP contains duplicate members.")
            for name in names:
                path = PurePosixPath(name)
                if path.is_absolute() or ".." in path.parts or "\\" in name:
                    raise FastQCError("FastQC ZIP contains an unsafe member path.")
            texts = {}
            for name in ("fastqc_data.txt", "summary.txt"):
                member = f"{prefix}/{name}"
                info = handle.getinfo(member)
                if info.file_size > MAX_TEXT_BYTES:
                    raise FastQCError("FastQC text output exceeds the supported 16 MiB per-member limit.")
                texts[name] = handle.read(member).decode("utf-8")
        lines = texts["fastqc_data.txt"].splitlines()
        if not lines or lines.pop(0) != "##FastQC\t" + SUPPORTED_VERSION:
            raise FastQCError("FastQC report version does not match the supported tool version.")
        modules, active = {}, None
        for line in lines:
            if not line:
                continue
            if line == ">>END_MODULE":
                if active is None:
                    raise FastQCError("Unexpected FastQC module terminator.")
                active = None
            elif line.startswith(">>"):
                fields = line[2:].split("\t")
                if active is not None or len(fields) != 2 or fields[0] in modules or fields[1].upper() not in STATES:
                    raise FastQCError("Invalid or duplicate FastQC module.")
                active = {"status": fields[1].upper(), "columns": [], "comments": [], "rows": []}
                modules[fields[0]] = active
            elif active is None:
                raise FastQCError("FastQC text appears outside a module.")
            elif line.startswith("#"):
                active["comments"].append(line[1:])
                active["columns"] = line[1:].split("\t")
            else:
                values = line.split("\t")
                if not active["columns"] or len(values) != len(active["columns"]):
                    raise FastQCError("FastQC table has an unexpected number of columns.")
                active["rows"].append(values)
        if active is not None or "Basic Statistics" not in modules:
            raise FastQCError("FastQC output is truncated or lacks Basic Statistics.")
        summary = {}
        for line in texts["summary.txt"].splitlines():
            status, module, filename = line.split("\t")
            if status not in STATES or module in summary or filename != input_name:
                raise FastQCError("FastQC summary has invalid states, duplicated modules or the wrong filename.")
            summary[module] = status
        if summary != {name: module["status"] for name, module in modules.items()}:
            raise FastQCError("FastQC summary and module data disagree.")
        basic = dict(modules["Basic Statistics"]["rows"])
        if basic.get("Filename") != input_name or not basic.get("Total Sequences", "").isdecimal():
            raise FastQCError("FastQC Basic Statistics do not identify the expected input.")
        records = int(basic["Total Sequences"])
        if records != expected_records:
            raise FastQCError(f"FastQC counted {records} records; the saved validation counted {expected_records}.")
        gc = _number(basic["%GC"])
        if gc is None or not 0 <= gc <= 100:
            raise FastQCError("FastQC reported an invalid GC percentage.")
        counts = {state: sum(value == state for value in summary.values()) for state in ("PASS", "WARN", "FAIL")}
        return {"version": SUPPORTED_VERSION, "basic": basic, "records": records, "gc_percent": gc,
                "module_counts": counts, "modules": modules, "plots": _plots(modules)}
    except FastQCError:
        raise
    except (OSError, ValueError, KeyError, UnicodeError, zipfile.BadZipFile, RuntimeError) as error:
        raise FastQCError(f"FastQC report cannot be interpreted: {error}") from error


def run_fastqc(database, run_id, output_root, *, executable="fastqc", threads=2, timeout=600):
    if type(threads) is not int or not 1 <= threads <= 4:
        raise FastQCError("Choose 1–4 simultaneous FastQC files.")
    if type(timeout) is not int or not 1 <= timeout <= 86400:
        raise FastQCError("Timeout must be an integer from 1 to 86400 seconds.")
    evidence = get_run(database, run_id)
    if evidence.get("status") != "READY":
        raise FastQCError("FastQC integration requires a saved READY validation run.")
    plan = _input_plan(evidence)
    output_root = check_report_destination(evidence, output_root, database_paths(database))
    source = verify_current_inputs(evidence, plan)
    tool = check_installation(executable)
    started = utc_now()
    parent = _run_root(output_root, run_id)
    parent.mkdir(parents=True, exist_ok=True)
    analysis_id = uuid4().hex
    destination = parent / analysis_id
    records = {c["file_path"]: c["details"]["records_scanned"] for c in evidence["checks"] if c["rule_id"] == "FASTQ001"}
    with tempfile.TemporaryDirectory(prefix=".pending-", dir=parent) as temporary:
        stage = Path(temporary)
        inputs, reports = stage / "inputs", stage / "reports"
        inputs.mkdir()
        reports.mkdir()
        files = []
        for index, entry in enumerate(evidence["inventory"], start=1):
            relative = entry["relative_path"]
            alias = f"input_{index:04d}.fastq.gz"
            target = inputs / alias
            _copy_input(_safe_file(source, relative), target)
            if _sha256(target) != evidence["input_sha256"][relative]:
                raise FastQCError(f"Input changed while copying {relative}; validate again.")
            files.append({"relative_path": relative, "sample_id": entry["sample_id"], "read": entry["read"],
                          "input_name": alias, "input_sha256": evidence["input_sha256"][relative]})
        command = [tool["executable"], "--noextract", "--format", "fastq", "--threads", str(threads),
                   "--memory", "512", "--outdir", str(reports), *[str(inputs / f["input_name"]) for f in files]]
        _execute(command, stage / "fastqc.log", timeout=timeout)
        for item in files:
            alias = item["input_name"]
            stem = alias.removesuffix(".fastq.gz") + "_fastqc"
            item["html"] = f"reports/{stem}.html"
            item["zip"] = f"reports/{stem}.zip"
            if _sha256(_safe_file(inputs, alias)) != item["input_sha256"]:
                raise FastQCError("An input copy changed during FastQC; no analysis was published.")
            _safe_file(stage, item["html"])
            item["result"] = parse_report(_safe_file(stage, item["zip"]), input_name=alias,
                                          expected_records=records[item["relative_path"]])
        verify_current_inputs(evidence, plan)
        if check_installation(tool["executable"]) != tool:
            raise FastQCError("FastQC installation changed during analysis; rerun with a stable installation.")
        shutil.rmtree(inputs)
        artifacts = {"fastqc.log", *[f[key] for f in files for key in ("html", "zip")]}
        observed = {p.relative_to(stage).as_posix() for p in stage.rglob("*") if not p.is_dir()}
        if observed != artifacts or any(p.is_symlink() for p in stage.rglob("*")):
            raise FastQCError("FastQC did not produce the exact expected report set.")
        result = {"schema_version": SCHEMA_VERSION, "analysis_id": analysis_id, "execution_status": "COMPLETED",
                  "validation_run_id": run_id, "validation_sha256": evidence_digest(evidence),
                  "project_id": evidence["project_id"], "dataset_path": evidence["dataset_path"],
                  "saved_structural_status": evidence["status"], "input_sha256": evidence["input_sha256"],
                  "started_at": started, "finished_at": utc_now(), "tool": tool,
                  "command": command, "threads": threads, "timeout_seconds": timeout,
                  "files": files, "artifact_sha256": {p: _sha256(_safe_file(stage, p)) for p in sorted(artifacts)}}
        (stage / "analysis.json").write_text(_json(result) + "\n", encoding="utf-8")
        stage.rename(destination)
    return destination


def _read_metadata(directory, evidence):
    path = _safe_file(directory, "analysis.json")
    if path.stat().st_size > 64 * 1024 * 1024:
        raise FastQCError("FastQC analysis metadata exceeds the supported size.")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if (data["schema_version"] != SCHEMA_VERSION or data["execution_status"] != "COMPLETED"
                or data["analysis_id"] != directory.name or not re.fullmatch(r"[0-9a-f]{32}", directory.name)
                or data["validation_run_id"] != evidence["run_id"]
                or data["validation_sha256"] != evidence_digest(evidence)
                or data["input_sha256"] != evidence["input_sha256"]
                or data["tool"]["version"] != SUPPORTED_VERSION
                or data["project_id"] != evidence["project_id"]
                or data["dataset_path"] != evidence["dataset_path"]
                or data["saved_structural_status"] != evidence["status"]):
            raise FastQCError("FastQC analysis does not match the selected saved validation evidence.")
        return data
    except (KeyError, TypeError, ValueError) as error:
        raise FastQCError(f"Invalid FastQC analysis metadata: {error}") from error


def list_analyses(output_root, evidence):
    parent = _run_root(output_root, evidence["run_id"])
    rows, errors = [], []
    if not parent.exists():
        return rows, errors
    if parent.is_symlink():
        raise FastQCError("FastQC history directory uses a symlink.")
    for directory in parent.iterdir():
        if directory.name.startswith(".pending-"):
            continue
        if directory.is_symlink() or not directory.is_dir():
            errors.append(f"Unexpected FastQC history entry: {directory.name}")
            continue
        try:
            data = _read_metadata(directory, evidence)
            rows.append({"analysis_id": data["analysis_id"], "finished_at": data["finished_at"],
                         "directory": str(directory), "version": data["tool"]["version"]})
        except (ValueError, OSError, KeyError, TypeError) as error:
            errors.append(f"{directory.name}: {error}")
    return sorted(rows, key=lambda r: (r["finished_at"], r["analysis_id"]), reverse=True), errors


def load_analysis(directory, evidence):
    directory = Path(directory).expanduser()
    if directory.is_symlink() or not directory.is_dir() or directory.resolve() != directory:
        raise FastQCError("FastQC analysis directory is unavailable or uses a changed path.")
    data = _read_metadata(directory, evidence)
    expected = {e["relative_path"] for e in evidence["inventory"]}
    files = data["files"]
    if {f["relative_path"] for f in files} != expected or len(files) != len(expected):
        raise FastQCError("FastQC reports do not cover exactly the selected FASTQ inventory.")
    artifacts = {"fastqc.log", *[f[key] for f in files for key in ("html", "zip")]}
    if set(data["artifact_sha256"]) != artifacts:
        raise FastQCError("FastQC artifact fingerprint list is inconsistent.")
    observed = {p.relative_to(directory).as_posix() for p in directory.rglob("*") if not p.is_dir()}
    if observed != artifacts | {"analysis.json"} or any(p.is_symlink() for p in directory.rglob("*")):
        raise FastQCError("FastQC analysis has missing or unexpected files.")
    for name, digest in data["artifact_sha256"].items():
        if _sha256(_safe_file(directory, name)) != digest:
            raise FastQCError(f"FastQC report changed since analysis: {name}.")
    records = {c["file_path"]: c["details"]["records_scanned"] for c in evidence["checks"] if c["rule_id"] == "FASTQ001"}
    for index, (item, entry) in enumerate(zip(files, evidence["inventory"]), start=1):
        alias = f"input_{index:04d}.fastq.gz"
        prefix = alias.removesuffix(".fastq.gz") + "_fastqc"
        if (item["input_sha256"] != evidence["input_sha256"][entry["relative_path"]]
                or item["relative_path"] != entry["relative_path"]
                or item["sample_id"] != entry["sample_id"] or item["read"] != entry["read"]
                or item["input_name"] != alias or item["html"] != f"reports/{prefix}.html"
                or item["zip"] != f"reports/{prefix}.zip"):
            raise FastQCError("FastQC input mapping differs from the saved snapshot.")
        parsed = parse_report(_safe_file(directory, item["zip"]), input_name=item["input_name"],
                              expected_records=records[item["relative_path"]])
        if parsed != item["result"]:
            raise FastQCError("Saved FastQC summary differs from the original tool report.")
    return data


def read_artifact(directory, analysis, relative):
    if relative not in analysis["artifact_sha256"]:
        raise FastQCError("This artifact is not part of the selected analysis.")
    path = _safe_file(Path(directory), relative)
    content = path.read_bytes()
    if hashlib.sha256(content).hexdigest() != analysis["artifact_sha256"][relative]:
        raise FastQCError("FastQC artifact changed before download; reload the analysis.")
    return content

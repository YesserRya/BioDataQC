"""Prepare the pinned, experimentally derived nf-core yeast fixture locally."""

from __future__ import annotations

import copy
import csv
import hashlib
import json
import os
import re
import shutil
import tempfile
import urllib.request
from pathlib import Path

from .results import utc_now
from .validation import _configuration, validate_dataset


CATALOG = Path(__file__).parent / "data/public_yeast_requirements.json"


class PreparationError(ValueError):
    pass


def _catalog():
    return json.loads(CATALOG.read_text(encoding="utf-8"))


def _check_catalog(config):
    if _configuration(config).outcome != "PASS" or config["expected_samples"] != ["SRR6357070"]:
        raise PreparationError("The bundled public yeast requirements are invalid.")
    provenance = config["provenance"]
    commit = provenance.get("repository_commit", "")
    files = provenance.get("files")
    if not re.fullmatch(r"[0-9a-f]{40}", commit) or not isinstance(files, list) or len(files) != 2:
        raise PreparationError("The source catalog needs one pinned commit and exactly two mate files.")
    for mate, specification in enumerate(files, start=1):
        name = f"SRR6357070_{mate}.fastq.gz"
        local = f"sequencing/SRR6357070_R{mate}.fastq.gz"
        url = f"https://raw.githubusercontent.com/nf-core/test-datasets/{commit}/testdata/GSE110004/{name}"
        if (specification.get("original_name") != name or specification.get("local_path") != local or
                specification.get("url") != url or specification.get("sha256") != config["integrity"]["reference_sha256"][local] or
                not isinstance(specification.get("git_blob_sha1"), str) or
                not re.fullmatch(r"[0-9a-f]{40}", specification["git_blob_sha1"]) or
                type(specification.get("size_bytes")) is not int or not 0 < specification["size_bytes"] <= 10_000_000):
            raise PreparationError("Invalid public yeast file mapping, size, URL or reference checksum.")


def _verify_download(path, specification):
    if path.is_symlink() or not path.is_file():
        raise PreparationError(f"Source must be a regular, non-symlink file: {path}")
    size = path.stat().st_size
    if size != specification["size_bytes"]:
        raise PreparationError(f"Source size mismatch: {path.name}. Preserve this file and use a new cache directory to retry.")
    sha256 = hashlib.sha256()
    git_blob = hashlib.sha1(f"blob {size}\0".encode("ascii"))
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            sha256.update(block)
            git_blob.update(block)
    if sha256.hexdigest() != specification["sha256"] or git_blob.hexdigest() != specification["git_blob_sha1"]:
        raise PreparationError(f"Source checksum mismatch: {path.name}. Preserve this file and use a new cache directory to retry.")


def _download(specification, cache):
    target = cache / specification["original_name"]
    if target.exists() or target.is_symlink():
        _verify_download(target, specification)
        return target
    url = specification["url"]
    if not url.startswith("https://raw.githubusercontent.com/nf-core/test-datasets/"):
        raise PreparationError("The bundled fixture catalog must use the pinned nf-core HTTPS source.")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("wb", prefix=".download-", suffix=".part", dir=cache, delete=False) as handle:
            temporary = Path(handle.name)
            request = urllib.request.Request(url, headers={"User-Agent": "BioDataQC-public-demo/1.1"})
            with urllib.request.urlopen(request, timeout=30) as response:
                size = 0
                while block := response.read(1024 * 1024):
                    size += len(block)
                    if size > specification["size_bytes"]:
                        raise PreparationError("Download exceeded the pinned source size.")
                    handle.write(block)
        _verify_download(temporary, specification)
        # Publish without replacing a concurrently created cache entry.
        try:
            os.link(temporary, target)
        except FileExistsError:
            _verify_download(target, specification)
        return target
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _check_locations(output, broken_output, cache):
    paths = [Path(p).expanduser().absolute() for p in (output, broken_output, cache)]
    if any(p.is_symlink() for p in paths):
        raise PreparationError("Dataset and cache directories cannot be symlinks.")
    paths = [p.resolve() for p in paths]
    if any(a.is_relative_to(b) or b.is_relative_to(a) for i, a in enumerate(paths) for b in paths[i + 1:]):
        raise PreparationError("Clean, missing-mate and download-cache directories must be separate, non-overlapping locations.")
    for target in paths[:2]:
        if target.exists():
            raise PreparationError(f"Destination already exists: {target}. Existing datasets are never overwritten.")
    return paths


def prepare_public_demo(output, broken_output, cache, *, progress=print):
    """Download/cache exact source bytes, prepare both datasets, and retain originals.

    Ordinary failures remove only newly created dataset directories. Verified cache
    downloads are retained for retry. No validation run is saved to SQL here.
    """
    output, broken_output, cache = _check_locations(output, broken_output, cache)
    config = _catalog()
    _check_catalog(config)
    cache.mkdir(parents=True, exist_ok=True)
    sources = []
    for specification in config["provenance"]["files"]:
        progress("Checking/downloading " + specification["original_name"])
        sources.append((_download(specification, cache), specification))
    stages, created = [], []
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix=".public-data-", dir=output.parent))
        stages.append(stage)
        for source, specification in sources:
            target = stage / specification["local_path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            _verify_download(target, specification)
        config["provenance"]["prepared_at"] = utc_now()
        (stage / "requirements.json").write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
        with (stage / "samples.csv").open("w", encoding="utf-8", newline="") as handle:
            columns = config["metadata"]["required_columns"] + config["metadata"]["optional_columns"]
            writer = csv.DictWriter(handle, fieldnames=columns, lineterminator="\n")
            writer.writeheader()
            writer.writerow({"sample_id": "SRR6357070", "organism": "Saccharomyces cerevisiae",
                             "run_accession": "SRR6357070", "geo_sample": "GSM2879618", "condition": "Wild-type",
                             "notes": f"Public experimental subset from nf-core; {config['provenance']['expected_records_per_mate']:,} pairs; original compressed bytes retained."})
        progress("Validating the prepared public-data copy…")
        checked = validate_dataset(stage)
        if checked.status != "READY":
            raise PreparationError("Prepared dataset did not pass: " + "; ".join(c.message for c in checked.issues))
        broken_output.parent.mkdir(parents=True, exist_ok=True)
        broken = Path(tempfile.mkdtemp(prefix=".public-fault-", dir=broken_output.parent))
        stages.append(broken)
        shutil.copytree(stage, broken, dirs_exist_ok=True)
        missing = "sequencing/SRR6357070_R2.fastq.gz"
        (broken / missing).unlink()
        fault_config = copy.deepcopy(config)
        fault_config["project_id"] += "_FAULT"
        fault_config["provenance"]["deliberate_faults"] = [f"Removed {missing} from this separate test copy."]
        (broken / "requirements.json").write_text(json.dumps(fault_config, indent=2) + "\n", encoding="utf-8")
        for temporary, target in [(stage, output), (broken, broken_output)]:
            target.mkdir()  # Exclusive reservation; never overwrite an existing destination.
            created.append(target)
            os.replace(temporary, target)
        return {"dataset": str(output), "missing_mate_dataset": str(broken_output),
                "cache": str(cache), "records_scanned": checked.counts()["records_scanned"],
                "run_accession": config["provenance"]["run_accession"]}
    except BaseException:
        for target in reversed(created):
            shutil.rmtree(target)
        raise
    finally:
        for temporary in stages:
            if temporary.exists():
                shutil.rmtree(temporary)
